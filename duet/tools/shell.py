"""Shell tool — sync, timeout-bounded, denylist-checked, approval-gated."""
from __future__ import annotations

import asyncio

from duet.tools.base import ToolContext, ToolOutcome, ToolSpec

MAX_TOOL_RETURN = 64 * 1024


def _truncate(s: str) -> str:
    if len(s) <= MAX_TOOL_RETURN:
        return s
    return s[: MAX_TOOL_RETURN // 2] + "\n[... truncated ...]\n" + s[-MAX_TOOL_RETURN // 4 :]


async def _run_shell(args: dict, ctx: ToolContext) -> ToolOutcome:
    cmd = args.get("cmd", "")
    cwd = args.get("cwd", "")
    timeout = int(args.get("timeout", 30))
    if not cmd:
        return ToolOutcome(content="[cmd is required]", is_error=True)
    for bad in ctx.shell_denylist:
        if bad in cmd:
            return ToolOutcome(
                content=f"[shell denylist: refused (matched '{bad}')]",
                is_error=True,
            )

    artifact = {"cmd": cmd, "cwd": cwd or str(ctx.workspace_root), "timeout": timeout}
    auto = ctx.trust is not None and ctx.trust.auto_approve_shell(cmd)
    if auto:
        ctx.notify("trust_auto", {"kind": "shell", "cmd": cmd})
        ok = True
    else:
        ok = await ctx.approve(f"run_shell `{cmd}`", artifact)
    if not ok:
        return ToolOutcome(content=f"[user rejected run_shell]", is_error=True)

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd or str(ctx.workspace_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return ToolOutcome(content=f"[shell timeout after {timeout}s]", is_error=True)
    text = out.decode("utf-8", "ignore")
    rc = proc.returncode
    ctx.notify("shell_done", {"cmd": cmd, "rc": rc, "output": text})
    label = f"$ {cmd}\n[exit {rc}]\n{text}"
    return ToolOutcome(content=_truncate(label), is_error=(rc != 0))


RUN_SHELL = ToolSpec(
    name="run_shell",
    description="Run a short shell command (default timeout 30s). User must approve.",
    input_schema={
        "type": "object",
        "properties": {
            "cmd": {"type": "string"},
            "cwd": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
        },
        "required": ["cmd"],
    },
    handler=_run_shell,
    requires_approval=True,
)
