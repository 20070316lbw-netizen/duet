"""FastAPI Web 服务:

路由:
  GET  /                 → 主页(static/index.html)
  GET  /api/providers    → 当前 cfg 里的 provider 列表(redacted)
  POST /api/start        → 接收 provider/model/key,启动一个会话,返回 session_id
  GET  /api/sessions     → 列出历史 session(供 resume)
  WS   /api/ws/{sid}     → 双向事件流

设计:
- 一个 web 进程同时只跑 0..N 个 session;每个 session 有独立的 WebApp + 后台 task。
- API key 仅放进当前进程的内存 cfg,不落盘。
- 工作目录默认 = duet 启动时所在的目录(和 CLI 一致)。前端可改 cwd(可选)。
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from duet import config as cfgmod
from duet.web.app_adapter import WebApp


@dataclass
class _Session:
    id: str
    app: WebApp
    task: asyncio.Task
    cfg: cfgmod.Config
    workspace: str
    websocket: Any | None = None  # 当前连接的 ws(单连接;断开重连会替换)
    pump_task: asyncio.Task | None = None
    backlog: list[dict] = field(default_factory=list)  # ws 未连上前缓存


_sessions: dict[str, _Session] = {}


def _make_app():  # noqa: C901
    try:
        from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError:
        sys.stderr.write(
            "\n[duet] Web UI 需要 fastapi 和 uvicorn:\n"
            "       pip install 'fastapi>=0.110' 'uvicorn[standard]>=0.27'\n"
            "       (或 pip install duet[web])\n"
        )
        sys.exit(2)

    app = FastAPI(title="duet")

    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index() -> Any:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/providers")
    async def providers() -> Any:
        try:
            cfg = cfgmod.load()
        except SystemExit as e:
            return JSONResponse(
                {"error": "no_config", "detail": str(e)}, status_code=500,
            )
        return {
            "providers": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "base_url": p.base_url,
                    "has_key": bool(p.api_key),
                    "redacted": p.redact(),
                }
                for p in cfg.providers.values()
            ],
            "default_planner_provider": cfg.planner.provider,
            "default_planner_model": cfg.planner.model,
            "default_supervisor_provider": cfg.supervisor.provider,
            "default_supervisor_model": cfg.supervisor.model,
            "supervisor_enabled_default": cfg.supervisor.enabled,
        }

    @app.get("/api/sessions")
    async def list_sessions() -> Any:
        from duet.sessions import list_sessions as _ls
        home = Path(os.path.expanduser("~/.duet"))
        return {
            "sessions": [
                {"id": s.id, "msg_count": s.msg_count, "last_ts": s.last_ts}
                for s in _ls(home)[:50]
            ]
        }

    @app.post("/api/start")
    async def start(body: dict) -> Any:
        try:
            cfg = cfgmod.load()
        except SystemExit as e:
            raise HTTPException(500, detail=str(e))

        prov_name = body.get("provider") or cfg.planner.provider
        model = body.get("model") or cfg.planner.model
        key = (body.get("api_key") or "").strip()
        sup_enabled = bool(body.get("supervisor_enabled",
                                    cfg.supervisor.enabled))
        sup_model = body.get("supervisor_model") or cfg.supervisor.model
        resume_id = body.get("resume_id")

        if prov_name not in cfg.providers:
            raise HTTPException(400, detail=f"unknown provider: {prov_name}")
        prov = cfg.providers[prov_name]
        if key:
            prov.api_key = key  # 仅内存
        if not prov.api_key:
            # 还允许 env
            from duet.launcher import _env_var_for
            env_key = os.environ.get(_env_var_for(prov_name, cfg)) or ""
            if env_key:
                prov.api_key = env_key
        if not prov.api_key:
            raise HTTPException(400, detail=f"{prov_name} 还没有 API key")

        cfg.planner.provider = prov_name
        cfg.planner.model = model
        cfg.supervisor.enabled = sup_enabled
        if sup_enabled and sup_model:
            cfg.supervisor.model = sup_model
        # supervisor 默认共用同一 provider(若无独立 key)
        sup_prov = cfg.providers.get(cfg.supervisor.provider)
        if not (sup_prov and sup_prov.api_key):
            cfg.supervisor.provider = prov_name

        sid = resume_id or uuid.uuid4().hex[:8]
        web_app = WebApp()
        # 在这个 task 里跑 _run。注意 _run 在 run_async() 里 await stop event。
        from duet.cli import _run
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            _run(sid, cfg, resume=bool(resume_id), app_factory=lambda: web_app),
            name=f"duet-session-{sid}",
        )

        def _on_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            import traceback
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            sys.stderr.write(f"\n[duet] session {sid} task crashed:\n{tb}\n")
            sys.stderr.flush()
            # 把消息塞进 web_app,这样下次有 ws 连上能看到
            try:
                web_app.lobby_say("system", f"[启动失败] {type(exc).__name__}: {exc}")
            except Exception:
                pass

        task.add_done_callback(_on_done)
        _sessions[sid] = _Session(
            id=sid, app=web_app, task=task, cfg=cfg,
            workspace=str(Path.cwd()),
        )
        return {"session_id": sid}

    @app.websocket("/api/ws/{sid}")
    async def ws_endpoint(websocket: WebSocket, sid: str) -> None:
        sess = _sessions.get(sid)
        if not sess:
            await websocket.close(code=4404, reason="no such session")
            return
        
        await websocket.accept()

        # 替换连接(允许断线重连)
        if sess.pump_task and not sess.pump_task.done():
            sess.pump_task.cancel()
        sess.websocket = websocket

        # 把 backlog 发掉
        for ev in sess.backlog:
            await websocket.send_json(ev)
        sess.backlog.clear()

        async def pump() -> None:
            try:
                while True:
                    ev = await sess.app.next_event()
                    if sess.websocket is websocket:
                        await websocket.send_json(ev)
                    else:
                        # 已被新连接替换
                        sess.backlog.append(ev)
                        return
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

        sess.pump_task = asyncio.create_task(pump(), name=f"duet-pump-{sid}")

        try:
            while True:
                data = await websocket.receive_json()
                await sess.app.handle_client(data)
                if data.get("t") == "submit" and data.get("text", "").strip() == "/exit":
                    break
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            if sess.pump_task and not sess.pump_task.done():
                sess.pump_task.cancel()
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    return app


def run_web(*, host: str = "0.0.0.0", port: int = 8080,
            resume_id: str | None = None, no_scan: bool = False) -> None:
    """duet --web 的入口。"""
    if not no_scan:
        hits = cfgmod.scan_workspace_for_secrets(Path.cwd())
        if hits:
            sys.stderr.write("\n\033[31m[duet] 拒绝启动 Web UI:工作目录中发现明文密钥:\033[0m\n")
            for p, prefix in hits:
                sys.stderr.write(f"   {p}  → {prefix}\n")
            sys.exit(2)

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write(
            "\n[duet] Web UI 需要 uvicorn:\n"
            "       pip install 'uvicorn[standard]>=0.27' 'fastapi>=0.110'\n"
        )
        sys.exit(2)

    app = _make_app()
    msg = f"""
┌─────────────────────────────────────────────────────────────
│  duet Web UI  →  http://{host}:{port}
│  • 在浏览器打开上面的地址
│  • 选 provider、粘 API key,即可开始对话
│  • API key 仅保存在当前进程内存,关闭即失效
{f'│  • 已预设 resume id = {resume_id}' if resume_id else '│'}
└─────────────────────────────────────────────────────────────
"""
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()

    # 把 resume_id 通过环境变量传给前端
    if resume_id:
        os.environ["DUET_RESUME_ID"] = resume_id

    uvicorn.run(app, host=host, port=port, log_level="info", ws="wsproto")
