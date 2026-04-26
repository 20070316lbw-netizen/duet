"""Tool registry + ToolPolicy gate (per-agent allow-lists)."""
from __future__ import annotations

from duet.tools.base import ToolSpec
from duet.tools.fs import EDIT_FILE, GREP, LIST_DIR, READ_FILE, WRITE_FILE
from duet.tools.shell import RUN_SHELL
from duet.tools.watch import WatchManager, make_watch_tools

ALL_STATIC_TOOLS: list[ToolSpec] = [
    READ_FILE, LIST_DIR, GREP, WRITE_FILE, EDIT_FILE, RUN_SHELL,
]

DEFAULT_POLICY: dict[str, list[str]] = {
    # MVP: planner has the full toolbelt; supervisor (if ever woken) is read-only.
    "planner":    [t.name for t in ALL_STATIC_TOOLS]
                  + ["watch_start", "watch_read", "watch_stop"],
    "supervisor": ["read_file", "list_dir", "grep", "watch_read"],
}


def tools_for(
    agent: str,
    *,
    watch_mgr: WatchManager | None = None,
    policy: dict[str, list[str]] | None = None,
) -> list[ToolSpec]:
    pol = policy or DEFAULT_POLICY
    allowed = set(pol.get(agent, []))
    out = [t for t in ALL_STATIC_TOOLS if t.name in allowed]
    if watch_mgr is not None:
        out.extend(t for t in make_watch_tools(watch_mgr, agent) if t.name in allowed)
    return out


__all__ = [
    "ALL_STATIC_TOOLS",
    "DEFAULT_POLICY",
    "tools_for",
    "ToolSpec",
    "WatchManager",
]
