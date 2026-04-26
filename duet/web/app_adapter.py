"""WebApp:把 cli._run 调的那一堆 app.xxx 方法,翻译成 WebSocket 事件。

目标是和 Textual DuetApp 的"鸭子接口"完全一致,这样 cli._run 不需要改动:
  - lobby_say(who, text)
  - lobby_stream_begin(who) / lobby_stream(text) / lobby_stream_end()
  - ask_approval(label, artifact) -> bool          (await)
  - diff_show(path, diff)                          (写文件 diff)
  - diff_shell(label, output)                      (shell / watcher 输出)
  - set_slash_registry(reg)                         (向前端推送命令列表)
  - call_after_refresh(fn)                          (启动时打招呼)
  - run_async()                                     (主循环 → 我们用 stop_event)
  - exit()                                          (/exit)
  - _on_submit / _on_interrupt                      (cli 直接赋值)

事件协议(JSON over WebSocket,server → client):
  {"t":"say","who":"system|user|planner|supervisor|tool","text":"..."}
  {"t":"stream_begin","who":"planner|supervisor"}
  {"t":"stream","text":"..."}
  {"t":"stream_end"}
  {"t":"diff","kind":"file|shell","label":"path","text":"..."}
  {"t":"approval","id":"abc123","label":"...","artifact":{...}}
  {"t":"slash","commands":[{"name":"...","summary":"...","usage":"..."}]}
  {"t":"exit"}

client → server:
  {"t":"submit","text":"..."}
  {"t":"interrupt"}
  {"t":"approval_reply","id":"abc123","ok":true}
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable


class WebApp:
    def __init__(self) -> None:
        # 对外的两个 callback,cli 会赋值
        self._on_submit: Callable[[str], Awaitable[None]] | None = None
        self._on_interrupt: Callable[[], None] | None = None

        # 内部:出口队列 + 待审批表 + 退出信号 + 启动钩子
        self._out: asyncio.Queue[dict] = asyncio.Queue()
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}
        self._stop = asyncio.Event()
        self._after_refresh: list[Callable[[], None]] = []
        self._slash_payload: list[dict] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── 出口:server 端会用一个 task 不停 pump 这个队列到 WebSocket ──
    async def next_event(self) -> dict:
        return await self._out.get()

    def _emit(self, ev: dict) -> None:
        # 即便没人在监听也要塞进队列(连接还没建立时)。
        try:
            self._out.put_nowait(ev)
        except asyncio.QueueFull:  # 不会发生,默认无上限
            pass

    # ── 入口:server 收到 client 消息时调用 ──
    async def handle_client(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "submit":
            text = (msg.get("text") or "").strip()
            if text and self._on_submit:
                # 不阻塞当前 ws read loop —— 用 task
                asyncio.create_task(self._safe_submit(text))
        elif t == "interrupt":
            if self._on_interrupt:
                try:
                    self._on_interrupt()
                except Exception as e:  # noqa: BLE001
                    self.lobby_say("system", f"中断处理失败:{e!r}")
        elif t == "approval_reply":
            aid = msg.get("id", "")
            ok = bool(msg.get("ok"))
            fut = self._pending_approvals.pop(aid, None)
            if fut and not fut.done():
                fut.set_result(ok)
        elif t == "ready":
            # 前端连上后通知:跑 on_ready 钩子
            for fn in list(self._after_refresh):
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    self.lobby_say("system", f"on_ready 出错:{e!r}")
            self._after_refresh.clear()
            # 把 slash 命令补发一遍(可能在 ready 之前注册的)
            if self._slash_payload:
                self._emit({"t": "slash", "commands": self._slash_payload})

    async def _safe_submit(self, text: str) -> None:
        try:
            await self._on_submit(text)  # type: ignore[misc]
        except Exception as e:  # noqa: BLE001
            self.lobby_say("system", f"提交处理失败:{e!r}")

    # ── 鸭子接口:和 Textual DuetApp 同名方法 ──────────────────────────
    def lobby_say(self, who: str, text: str) -> None:
        self._emit({"t": "say", "who": who, "text": text})

    def lobby_stream_begin(self, who: str) -> None:
        self._emit({"t": "stream_begin", "who": who})

    def lobby_stream(self, text: str) -> None:
        if not text:
            return
        self._emit({"t": "stream", "text": text})

    def lobby_stream_end(self) -> None:
        self._emit({"t": "stream_end"})

    def diff_show(self, path: str, text: str) -> None:
        self._emit({"t": "diff", "kind": "file", "label": path, "text": text})

    def diff_shell(self, label: str, text: str) -> None:
        self._emit({"t": "diff", "kind": "shell", "label": label, "text": text})

    def set_slash_registry(self, reg: Any) -> None:
        try:
            self._slash_payload = [
                {"name": c.name, "summary": c.summary, "usage": c.usage}
                for c in reg.all()
            ]
        except Exception:
            self._slash_payload = []
        self._emit({"t": "slash", "commands": self._slash_payload})

    def call_after_refresh(self, fn: Callable[[], None]) -> None:
        # 等到前端发了 ready 再跑
        self._after_refresh.append(fn)

    def exit(self) -> None:
        self._emit({"t": "exit"})
        self._stop.set()

    async def run_async(self) -> None:
        # 一直挂着,直到 /exit 触发 _stop。
        self._loop = asyncio.get_running_loop()
        await self._stop.wait()

    async def ask_approval(self, label: str, artifact: dict) -> bool:
        aid = uuid.uuid4().hex[:10]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending_approvals[aid] = fut
        # 把 artifact 里不可序列化的字段(如 Path)转 str
        clean = _jsonable(artifact)
        self._emit({"t": "approval", "id": aid, "label": label, "artifact": clean})
        try:
            return await fut
        finally:
            self._pending_approvals.pop(aid, None)


def _jsonable(obj: Any) -> Any:
    """递归把 Path / 集合 / 自定义对象转成 JSON 可序列化结构。"""
    from pathlib import PurePath
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, set):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)
