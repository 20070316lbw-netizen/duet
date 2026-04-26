"""Watch subsystem: long-running shell processes, tail buffers, agent-readable cursors.

Each watcher runs `cmd` in a subprocess (own session, so we can SIGTERM the group).
stdout+stderr are merged into a ring buffer. Each agent gets its own read cursor
via watch_read(label, since=<ts|line_no>) so two agents can tail independently.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from duet.tools.base import ToolContext, ToolOutcome, ToolSpec

MAX_LINES = 5000
MAX_TAIL = 200


@dataclass
class Watcher:
    label: str
    cmd: str
    cwd: str
    proc: asyncio.subprocess.Process
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    started: float = field(default_factory=time.time)
    cursors: dict[str, int] = field(default_factory=dict)  # agent → next line idx
    closed: bool = False
    rc: int | None = None
    listeners: list[Callable[[str, str], None]] = field(default_factory=list)


class WatchManager:
    """Owns all live watchers + EventBus subscribers."""

    def __init__(self):
        self.watchers: dict[str, Watcher] = {}
        # EventBus: subscribers receive (event_type, payload) callbacks.
        # Used by Lobby/TUI to render tail output and to inject system events into agents.
        self.subscribers: list[Callable[[str, dict], None]] = []

    def subscribe(self, fn: Callable[[str, dict], None]) -> None:
        self.subscribers.append(fn)

    def emit(self, event: str, payload: dict) -> None:
        for fn in list(self.subscribers):
            try:
                fn(event, payload)
            except Exception:  # noqa: BLE001
                pass

    async def start(self, label: str, cmd: str, cwd: str) -> Watcher:
        if label in self.watchers and not self.watchers[label].closed:
            raise ValueError(f"watcher '{label}' already running")
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=os.setsid,  # own process group → clean kill
        )
        w = Watcher(label=label, cmd=cmd, cwd=cwd, proc=proc)
        self.watchers[label] = w
        asyncio.create_task(self._pump(w))
        self.emit("watch_started", {"label": label, "cmd": cmd})
        return w

    async def _pump(self, w: Watcher) -> None:
        assert w.proc.stdout is not None
        while True:
            line = await w.proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "ignore").rstrip("\n")
            w.lines.append(text)
            self.emit("watch_line", {"label": w.label, "line": text})
        w.rc = await w.proc.wait()
        w.closed = True
        self.emit("watch_exit", {"label": w.label, "rc": w.rc})

    def read(self, label: str, agent: str, tail: int = MAX_TAIL) -> tuple[str, int]:
        w = self.watchers.get(label)
        if w is None:
            return f"[no such watcher: {label}]", 0
        cur = w.cursors.get(agent, 0)
        all_lines = list(w.lines)
        # Approximate: ring buffer doesn't preserve absolute indices when full,
        # so we just hand over everything since the cursor in current buffer.
        new = all_lines[cur:][-tail:]
        w.cursors[agent] = len(all_lines)
        status = "running" if not w.closed else f"exited rc={w.rc}"
        return "\n".join(new) or f"[no new output] ({status})", len(new)

    async def stop(self, label: str) -> str:
        w = self.watchers.get(label)
        if w is None:
            return f"[no such watcher: {label}]"
        if w.closed:
            return f"[already exited rc={w.rc}]"
        try:
            os.killpg(os.getpgid(w.proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(w.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(w.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        return f"stopped {label}"

    async def shutdown_all(self) -> None:
        for label in list(self.watchers):
            await self.stop(label)


# ── tool wrappers ──────────────────────────────────────────────────────────

def make_watch_tools(mgr: WatchManager, agent_name: str) -> list[ToolSpec]:
    """Returns three ToolSpecs bound to this manager + agent identity."""

    async def _start(args: dict, ctx: ToolContext) -> ToolOutcome:
        label = args.get("label", "")
        cmd = args.get("cmd", "")
        if not label or not cmd:
            return ToolOutcome(content="[label and cmd required]", is_error=True)
        for bad in ctx.shell_denylist:
            if bad in cmd:
                return ToolOutcome(content=f"[denylist match: {bad}]", is_error=True)
        ok = await ctx.approve(f"watch_start [{label}] `{cmd}`",
                               {"cmd": cmd, "label": label})
        if not ok:
            return ToolOutcome(content="[user rejected watch_start]", is_error=True)
        try:
            await mgr.start(label, cmd, str(ctx.workspace_root))
        except Exception as e:  # noqa: BLE001
            return ToolOutcome(content=f"[watch_start failed: {e!r}]", is_error=True)
        return ToolOutcome(content=f"started watcher '{label}'")

    async def _read(args: dict, ctx: ToolContext) -> ToolOutcome:
        label = args.get("label", "")
        tail = int(args.get("tail", MAX_TAIL))
        out, n = mgr.read(label, agent_name, tail=tail)
        return ToolOutcome(content=f"[{n} new line(s)]\n{out}")

    async def _stop(args: dict, ctx: ToolContext) -> ToolOutcome:
        label = args.get("label", "")
        msg = await mgr.stop(label)
        return ToolOutcome(content=msg)

    return [
        ToolSpec(
            name="watch_start",
            description="Start a long-running shell process. Output is buffered; tail with watch_read.",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "unique handle"},
                    "cmd":   {"type": "string"},
                },
                "required": ["label", "cmd"],
            },
            handler=_start,
            requires_approval=True,
        ),
        ToolSpec(
            name="watch_read",
            description="Read new lines from a watcher since this agent's last cursor (default tail=200).",
            input_schema={
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "tail":  {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["label"],
            },
            handler=_read,
        ),
        ToolSpec(
            name="watch_stop",
            description="Terminate a watcher (SIGTERM, then SIGKILL after 5s).",
            input_schema={
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
            handler=_stop,
        ),
    ]
