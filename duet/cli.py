"""Entry point: wire config + transcript + agents + memory + trust + tui."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

from duet import config as cfgmod
from duet.agents.base import Agent, load_prompt
from duet.clipboard import copy_to_clipboard, copy_to_file
from duet.cost import CostTracker, TokenGuard
from duet.lobby import Transcript, parse, filter_by_enabled, session_dir
from duet.memory import MemoryStore
from duet.providers.anthropic import AnthropicProvider
from duet.providers.openai_compat import OpenAICompatProvider
from duet.sessions import list_sessions, replay_into
from duet.slash import SlashCommand, SlashRegistry
from duet.tools import WatchManager, tools_for
from duet.tools.base import ToolContext
from duet.trust import TrustPolicy
from duet.tui import DuetApp


DUET_HOME = Path(os.path.expanduser("~/.duet"))


def _make_provider(name: str, cfg: cfgmod.Config):
    pcfg = cfg.providers.get(name)
    if pcfg is None:
        raise SystemExit(f"[duet] provider '{name}' 未声明")
    if not pcfg.api_key:
        raise SystemExit(f"[duet] provider '{name}' 没有 api_key")
    kind = pcfg.kind or name
    if kind == "anthropic":
        return AnthropicProvider(api_key=pcfg.api_key, base_url=pcfg.base_url)
    if kind == "openai_compat":
        return OpenAICompatProvider(api_key=pcfg.api_key, base_url=pcfg.base_url)
    raise SystemExit(f"[duet] provider '{name}': unknown kind '{kind}'")


def _build_system_prompt(base: str, mem: MemoryStore, workspace: str) -> str:
    block = mem.render_for_prompt(workspace)
    return (block + "\n" + base) if block else base


def _supervisor_view(planner_msg: str, label: str = "Planner") -> str:
    return f"[来自 {label}]\n{planner_msg}"


async def _run(session_id: str, cfg: cfgmod.Config, *, resume: bool,
               app_factory=None) -> None:
    workspace = Path.cwd()
    sess_dir = session_dir(DUET_HOME, session_id)
    transcript = Transcript(sess_dir, branch_id="main")
    mem = MemoryStore(DUET_HOME / "memory.db")
    trust = TrustPolicy()

    watch_mgr = WatchManager()
    cost = CostTracker()
    guards = {"planner": TokenGuard(), "supervisor": TokenGuard()}
    last_outputs: dict[str, str] = {"planner": "", "supervisor": ""}

    base_planner_prompt = load_prompt("planner")
    base_supervisor_prompt = load_prompt("supervisor")

    p_cfg = cfg.providers.get(cfg.planner.provider)
    s_cfg = cfg.providers.get(cfg.supervisor.provider)

    planner = Agent(
        name="planner",
        model=cfg.planner.model,
        provider=_make_provider(cfg.planner.provider, cfg),
        tools=tools_for("planner", watch_mgr=watch_mgr),
        system_prompt=_build_system_prompt(base_planner_prompt, mem, str(workspace)),
        transcript=transcript,
        base_url=p_cfg.base_url if p_cfg else "",
    )
    supervisor = Agent(
        name="supervisor",
        model=cfg.supervisor.model,
        provider=_make_provider(cfg.supervisor.provider, cfg)
            if cfg.supervisor.enabled else planner.provider,
        tools=tools_for("supervisor", watch_mgr=watch_mgr),
        system_prompt=_build_system_prompt(base_supervisor_prompt, mem, str(workspace)),
        transcript=transcript,
        base_url=s_cfg.base_url if s_cfg else "",
    )
    agents = {"planner": planner, "supervisor": supervisor}
    enabled = {"planner": True, "supervisor": cfg.supervisor.enabled}

    if resume:
        replay_into(transcript.messages,
                    {n: a.history for n, a in agents.items()})

    app = (app_factory() if app_factory is not None
           else DuetApp(on_submit=None, on_interrupt=lambda: None))

    reg = SlashRegistry()

    async def approve(label: str, artifact: dict) -> bool:
        return await app.ask_approval(label, artifact)

    def notify(event: str, payload: dict) -> None:
        if event == "file_written":
            app.diff_show(payload.get("path", "?"), payload.get("diff", ""))
        elif event == "shell_done":
            app.diff_shell(f"shell rc={payload.get('rc')}", payload.get("output", ""))
        elif event == "trust_auto":
            kind = payload.get("kind", "?")
            tgt = payload.get("path") or payload.get("cmd") or "?"
            app.lobby_say("system", f"⚙ trust 自动通过 [{kind}]: {tgt}")

    def on_bus(event: str, payload: dict) -> None:
        if event == "watch_started":
            app.lobby_say("system", f"▶ watch '{payload['label']}': {payload['cmd']}")
        elif event == "watch_line":
            app.diff_shell(f"[{payload['label']}]", payload["line"])
        elif event == "watch_exit":
            app.lobby_say("system", f"■ watch '{payload['label']}' 退出 rc={payload['rc']}")

    watch_mgr.subscribe(on_bus)

    tool_ctx = ToolContext(
        workspace_root=workspace,
        write_paths=cfg.write_paths,
        deny_paths=cfg.deny_paths,
        shell_denylist=cfg.shell_denylist,
        approve=approve, notify=notify,
        trust=trust,
    )

    async def run_one_agent(tname: str, body: str, *, source_label: str | None = None) -> str:
        agent = agents[tname]
        injected = _supervisor_view(body, source_label) \
            if (tname == "supervisor" and source_label) else body
        agent.append_user(injected)
        app.lobby_stream_begin(tname)
        collected: list[str] = []
        try:
            stats = await agent.run(
                ctx=tool_ctx,
                on_text=lambda t: (collected.append(t), app.lobby_stream(t))[-1],
                on_tool_use=lambda tu: (
                    app.lobby_stream_end(),
                    app.lobby_say("tool", f"→ {tu.name}({_short(tu.input)})"),
                ),
                on_tool_result=lambda _id, r: app.lobby_say(
                    "tool", ("失败 " if r.is_error else "完成 ") + _short(r.content[:200]),
                ),
            )
            app.lobby_stream_end()
            cost.add(tname, agent.model, stats.input_tokens, stats.output_tokens)
            if guards[tname].observe(stats.output_tokens):
                app.lobby_say("system",
                    f"⚠ TokenGuard:{tname} 已进入 Poor Mode(阈值 {guards[tname].threshold})")
        except Exception as e:  # noqa: BLE001
            app.lobby_say("system", f"[{tname}] 出错:\n{e}")

        text = "".join(collected).strip()
        last_outputs[tname] = text
        return text

    async def handle_submit(text: str) -> None:
        if text.startswith("/"):
            await _handle_slash(text, app, enabled, agents, transcript,
                                cost=cost, guards=guards, watch_mgr=watch_mgr,
                                mem=mem, trust=trust, last_outputs=last_outputs,
                                workspace=str(workspace), reg=reg)
            return

        route = parse(text, default=cfg.default_route)
        route = filter_by_enabled(route, enabled)
        transcript.append(author="user", payload={"text": text}, route=route.targets)
        app.lobby_say("user", text)

        if not route.targets:
            app.lobby_say("system", "没有可响应的 agent(supervisor 已关闭,默认 @planner)。")
            return

        last_planner_text = ""
        for tname in route.targets:
            if tname == "supervisor" and last_planner_text:
                await run_one_agent("supervisor", last_planner_text, source_label="Planner")
            else:
                last_planner_text = await run_one_agent(tname, route.body)

    def handle_interrupt() -> None:
        app.lobby_say("system", "(已请求中断,将在下一个工具调用边界停下)")

    app._on_submit = handle_submit
    app._on_interrupt = handle_interrupt

    # ── 注册斜杠命令 ──────────────────────────────────────────────────────
    reg.register(SlashCommand("/help", "显示所有命令"))
    reg.register(SlashCommand("/status", "查看 Planner/Supervisor 状态"))
    reg.register(SlashCommand("/cost", "本会话累计 token 与费用"))
    reg.register(SlashCommand("/poor", "重置 TokenGuard"))
    reg.register(SlashCommand("/agent", "开关 agent", "/agent <名字> on|off"))
    reg.register(SlashCommand("/watch", "watcher 列表", "/watch list"))
    reg.register(SlashCommand("/clear", "清空内存历史(磁盘保留)"))
    reg.register(SlashCommand("/memory", "查看/添加记忆", "/memory [add <kind> <key>=<val>]"))
    reg.register(SlashCommand("/forget", "删除记忆", "/forget <id> | /forget all"))
    reg.register(SlashCommand("/sessions", "列出历史会话"))
    reg.register(SlashCommand("/copy", "复制最近一次输出", "/copy [planner|supervisor]"))
    reg.register(SlashCommand("/trust", "信任策略", "/trust <write|shell> add <pattern> | list | clear"))
    reg.register(SlashCommand("/exit", "退出"))
    app.set_slash_registry(reg)

    def on_ready() -> None:
        msg = (f"duet 就绪。工作目录={workspace}  会话={session_id}  "
               f"planner={cfg.planner.model}  supervisor=" +
               ("开" if cfg.supervisor.enabled else "关(默认静默)"))
        app.lobby_say("system", msg)
        app.lobby_say("system",
            "复制提示:终端用 Shift(macOS:Option)+ 鼠标拖拽即可选区复制;"
            "也可用 /copy 把最近一次输出送进系统剪贴板。")
        if resume:
            n = sum(len(a.history) for a in agents.values())
            app.lobby_say("system", f"已恢复会话:重放了 {n} 条消息到 agent 历史")

    app.call_after_refresh(on_ready)
    await app.run_async()


def _short(x) -> str:
    s = str(x)
    return s if len(s) < 120 else s[:117] + "..."


async def _handle_slash(text, app, enabled, agents, transcript, *,
                        cost, guards, watch_mgr, mem, trust, last_outputs,
                        workspace, reg):
    parts = text.strip().split()
    cmd = parts[0]
    if cmd == "/help":
        for c in reg.all():
            line = f"  {c.name:<12} {c.summary}"
            if c.usage:
                line += f"   ({c.usage})"
            app.lobby_say("system", line)
    elif cmd == "/exit":
        if watch_mgr is not None:
            await watch_mgr.shutdown_all()
        app.exit()
    elif cmd == "/status":
        app.lobby_say("system", f"planner:{agents['planner'].model}  "
                                f"supervisor:{'开' if enabled['supervisor'] else '关'}")
    elif cmd == "/cost":
        app.lobby_say("system", cost.summary())
    elif cmd == "/poor":
        for g in guards.values():
            if g.tripped:
                g.reset()
        app.lobby_say("system", "TokenGuard 已重置")
    elif cmd == "/agent" and len(parts) >= 3:
        name, state = parts[1], parts[2].lower()
        if name not in enabled:
            app.lobby_say("system", f"未知 agent:{name}")
            return
        enabled[name] = state in ("on", "true", "1", "yes", "开")
        app.lobby_say("system", f"{name} = {'开' if enabled[name] else '关'}")
    elif cmd == "/watch" and len(parts) >= 2 and parts[1] == "list":
        if not watch_mgr.watchers:
            app.lobby_say("system", "(当前无运行中的 watcher)")
        else:
            for lbl, w in watch_mgr.watchers.items():
                state = "运行中" if not w.closed else f"已退出 rc={w.rc}"
                app.lobby_say("system", f"  [{lbl}] {state}  cmd={w.cmd}")
    elif cmd == "/clear":
        for a in agents.values():
            a.history.clear()
        app.lobby_say("system", "已清空 agent 内存历史(磁盘 transcript 保留)")
    elif cmd == "/memory":
        if len(parts) >= 2 and parts[1] == "add":
            if len(parts) < 4:
                app.lobby_say("system", "用法:/memory add <kind> <key>=<value>")
                return
            kind = parts[2]
            kv = " ".join(parts[3:])
            if "=" not in kv:
                app.lobby_say("system", "需要 key=value 形式")
                return
            key, value = kv.split("=", 1)
            scope = "*" if kind == "user" else workspace
            mid = mem.add(kind, scope, key.strip(), value.strip())
            app.lobby_say("system", f"已记住 #{mid} [{kind}] {key.strip()}={value.strip()}")
        else:
            mems = mem.list_all()
            if not mems:
                app.lobby_say("system", "(暂无记忆)")
            else:
                for m in mems:
                    app.lobby_say("system",
                                  f"  #{m.id}  [{m.kind}] {m.key}={m.value}  scope={m.scope}")
    elif cmd == "/forget" and len(parts) >= 2:
        if parts[1] == "all":
            n = mem.delete_project(workspace)
            app.lobby_say("system", f"已删除 {n} 条 project 记忆")
        else:
            try:
                mid = int(parts[1])
            except ValueError:
                app.lobby_say("system", "用法:/forget <id> 或 /forget all")
                return
            ok = mem.delete(mid)
            app.lobby_say("system", f"#{mid} {'已删除' if ok else '不存在'}")
    elif cmd == "/sessions":
        sess = list_sessions(Path(os.path.expanduser("~/.duet")))
        if not sess:
            app.lobby_say("system", "(无历史会话)")
        else:
            for s in sess[:20]:
                app.lobby_say("system",
                              f"  {s.id}  {s.msg_count} 条消息  "
                              f"(用 duet --resume {s.id} 恢复)")
    elif cmd == "/copy":
        target = parts[1] if len(parts) >= 2 else "planner"
        text = last_outputs.get(target, "")
        if not text:
            app.lobby_say("system", f"{target} 还没有输出过内容")
            return
        ok, detail = copy_to_clipboard(text)
        if ok:
            app.lobby_say("system", f"✓ {detail}({len(text)} 字符)")
        else:
            fp = Path(os.path.expanduser("~/.duet")) / f"copy_{int(time.time())}.txt"
            copy_to_file(text, fp)
            app.lobby_say("system", f"剪贴板不可用 → 已落盘到 {fp}")
    elif cmd == "/trust":
        sub = parts[1] if len(parts) >= 2 else "list"
        if sub == "list":
            for line in trust.summary():
                app.lobby_say("system", "  " + line)
        elif sub == "clear":
            trust.clear()
            app.lobby_say("system", "已清空所有 trust 规则")
        elif sub in ("write", "shell") and len(parts) >= 4 and parts[2] == "add":
            pattern = " ".join(parts[3:])
            app.lobby_say("system", trust.add(sub, pattern))
        elif sub in ("write", "shell") and len(parts) >= 4 and parts[2] == "remove":
            pattern = " ".join(parts[3:])
            app.lobby_say("system", trust.remove(sub, pattern))
        else:
            app.lobby_say("system",
                "用法:/trust list | clear | write add <glob> | shell add <cmd> | "
                "write/shell remove <pattern>")
    else:
        app.lobby_say("system", f"未知命令:{cmd}(输入 /help 查看)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="duet")
    ap.add_argument("--resume", help="要恢复的会话 id", default=None)
    ap.add_argument("--no-scan", action="store_true", help="跳过密钥扫描")
    ap.add_argument("--web", action="store_true", help="启动 Web UI(本地浏览器访问)")
    ap.add_argument("--web-port", type=int, default=7878, help="Web UI 端口")
    args = ap.parse_args()

    if args.web:
        from duet.web.server import run_web
        run_web(host="127.0.0.1", port=args.web_port,
                resume_id=args.resume, no_scan=args.no_scan)
        return

    cfg = cfgmod.load()
    from duet.launcher import select_provider
    try:
        select_provider(cfg)
    except KeyboardInterrupt:
        sys.exit(0)

    if not args.no_scan:
        hits = cfgmod.scan_workspace_for_secrets(Path.cwd())
        if hits:
            sys.stderr.write("\n\033[31m[duet] 拒绝启动:工作目录中发现明文密钥:\033[0m\n")
            for p, prefix in hits:
                sys.stderr.write(f"   {p}  → {prefix}\n")
            sys.stderr.write("\n请将密钥移到 ~/.duet/config.toml 或环境变量后重试。\n")
            sys.exit(2)

    session_id = args.resume or uuid.uuid4().hex[:8]
    asyncio.run(_run(session_id, cfg, resume=bool(args.resume)))


if __name__ == "__main__":
    main()
