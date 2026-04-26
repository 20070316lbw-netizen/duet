"""Filesystem tools: read_file / list_dir / grep / write_file / edit_file."""
from __future__ import annotations

import asyncio
import difflib
import os
from pathlib import Path

from duet.safety.sandbox import resolve_safe
from duet.tools.base import ToolContext, ToolOutcome, ToolSpec

MAX_READ_BYTES = 256 * 1024
MAX_TOOL_RETURN = 64 * 1024


def _pick(args: dict, *keys: str, default: str = "") -> str:
    """Return the first non-empty value among args[keys], else default.

    Models occasionally use synonyms (file/filename/contents/text) instead of
    the exact schema field. We accept the common aliases instead of crashing.
    """
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v != "":
            return v
    return default


def _truncate(s: str, limit: int = MAX_TOOL_RETURN) -> str:
    if len(s) <= limit:
        return s
    head = s[: limit // 2]
    tail = s[-limit // 4 :]
    return f"{head}\n\n[... truncated {len(s) - limit} chars ...]\n\n{tail}"


# -----------------------------------------------------------------------------

async def _read_file(args: dict, ctx: ToolContext) -> ToolOutcome:
    path = _pick(args, "path", "file", "filename")
    if not path:
        return ToolOutcome(content="[需要 path 参数]", is_error=True)
    p = resolve_safe(
        path,
        workspace_root=ctx.workspace_root,
        write_paths=ctx.write_paths,
        deny_paths=ctx.deny_paths,
        for_write=False,
    )
    if not p.exists():
        return ToolOutcome(content=f"[no such file: {path}]", is_error=True)
    if not p.is_file():
        return ToolOutcome(content=f"[not a file: {path}]", is_error=True)
    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        return ToolOutcome(
            content=f"[file too large: {size} bytes; cap is {MAX_READ_BYTES}. "
                    f"use grep or read a slice instead.]",
            is_error=True,
        )
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolOutcome(content=f"[binary or non-utf8: {path}]", is_error=True)
    return ToolOutcome(content=_truncate(text))


READ_FILE = ToolSpec(
    name="read_file",
    description="Read a UTF-8 text file inside the workspace. Returns up to 256KB.",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    handler=_read_file,
)

# -----------------------------------------------------------------------------

async def _list_dir(args: dict, ctx: ToolContext) -> ToolOutcome:
    path = _pick(args, "path", "dir", "directory", default=".")
    depth = int(args.get("depth", 1))
    p = resolve_safe(
        path,
        workspace_root=ctx.workspace_root,
        write_paths=ctx.write_paths,
        deny_paths=ctx.deny_paths,
        for_write=False,
    )
    if not p.exists() or not p.is_dir():
        return ToolOutcome(content=f"[not a directory: {path}]", is_error=True)
    lines: list[str] = []
    base = len(p.parts)
    for dirpath, dirnames, filenames in os.walk(p):
        d = Path(dirpath)
        cur_depth = len(d.parts) - base
        if cur_depth > depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(x for x in dirnames if not x.startswith(".") or x in {".duet"})
        prefix = "  " * cur_depth
        lines.append(f"{prefix}{d.name}/" if cur_depth else f"{d.name}/")
        for fn in sorted(filenames):
            lines.append(f"{prefix}  {fn}")
        if len(lines) > 2000:
            lines.append("... (truncated)")
            break
    return ToolOutcome(content=_truncate("\n".join(lines)))


LIST_DIR = ToolSpec(
    name="list_dir",
    description="List a directory, up to a given depth (default 1).",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 6},
        },
        "required": ["path"],
    },
    handler=_list_dir,
)

# -----------------------------------------------------------------------------

async def _grep(args: dict, ctx: ToolContext) -> ToolOutcome:
    pattern = _pick(args, "pattern", "query", "regex")
    path = _pick(args, "path", "dir", "directory", default=".")
    if not pattern:
        return ToolOutcome(content="[需要 pattern 参数]", is_error=True)
    p = resolve_safe(
        path,
        workspace_root=ctx.workspace_root,
        write_paths=ctx.write_paths,
        deny_paths=ctx.deny_paths,
        for_write=False,
    )
    cmd = ["grep", "-RIn", "--color=never", pattern, str(p)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return ToolOutcome(content="[grep timed out]", is_error=True)
    text = out.decode("utf-8", "ignore") or err.decode("utf-8", "ignore") or "[no matches]"
    return ToolOutcome(content=_truncate(text))


GREP = ToolSpec(
    name="grep",
    description="Recursive grep. Pattern is plain regex (POSIX).",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["pattern"],
    },
    handler=_grep,
)

# -----------------------------------------------------------------------------

def _make_diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    ) or "(no diff)"


async def _write_file(args: dict, ctx: ToolContext) -> ToolOutcome:
    path = _pick(args, "path", "file", "filename")
    content = _pick(args, "content", "contents", "text", "data", "body")
    if not path:
        return ToolOutcome(content="[需要 path 参数(文件路径不能为空)]", is_error=True)
    p = resolve_safe(
        path,
        workspace_root=ctx.workspace_root,
        write_paths=ctx.write_paths,
        deny_paths=ctx.deny_paths,
        for_write=True,
    )
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    diff = _make_diff(old, content, path)
    artifact = {"path": path, "diff": diff, "old": old, "new": content}
    auto = ctx.trust is not None and ctx.trust.auto_approve_write(path)
    if auto:
        ctx.notify("trust_auto", {"kind": "write", "path": path})
        ok = True
    else:
        ok = await ctx.approve(f"write_file {path}", artifact)
    if not ok:
        return ToolOutcome(content=f"[user rejected write_file {path}]", is_error=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    ctx.notify("file_written", artifact)
    return ToolOutcome(content=f"wrote {path} ({len(content)} chars)", artifact=artifact)


WRITE_FILE = ToolSpec(
    name="write_file",
    description="Overwrite or create a UTF-8 text file. User must approve via diff.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    handler=_write_file,
    requires_approval=True,
)

# -----------------------------------------------------------------------------

async def _edit_file(args: dict, ctx: ToolContext) -> ToolOutcome:
    path = _pick(args, "path", "file", "filename")
    old_str = _pick(args, "old", "old_str", "old_string", "find", "search")
    new_str = _pick(args, "new", "new_str", "new_string", "replace", "replacement")
    if not path:
        return ToolOutcome(content="[需要 path 参数]", is_error=True)
    if not old_str:
        return ToolOutcome(content="[需要 old 参数,且必须在文件中唯一出现]", is_error=True)
    p = resolve_safe(
        path,
        workspace_root=ctx.workspace_root,
        write_paths=ctx.write_paths,
        deny_paths=ctx.deny_paths,
        for_write=True,
    )
    if not p.exists():
        return ToolOutcome(content=f"[no such file: {path}]", is_error=True)
    text = p.read_text(encoding="utf-8")
    n = text.count(old_str)
    if n == 0:
        return ToolOutcome(content=f"[old not found in {path}]", is_error=True)
    if n > 1:
        return ToolOutcome(
            content=f"[old appears {n} times in {path}; supply more context to make it unique]",
            is_error=True,
        )
    new_text = text.replace(old_str, new_str, 1)
    diff = _make_diff(text, new_text, path)
    artifact = {"path": path, "diff": diff, "old": text, "new": new_text}
    auto = ctx.trust is not None and ctx.trust.auto_approve_write(path)
    if auto:
        ctx.notify("trust_auto", {"kind": "write", "path": path})
        ok = True
    else:
        ok = await ctx.approve(f"edit_file {path}", artifact)
    if not ok:
        return ToolOutcome(content=f"[user rejected edit_file {path}]", is_error=True)
    p.write_text(new_text, encoding="utf-8")
    ctx.notify("file_written", artifact)
    return ToolOutcome(content=f"edited {path}", artifact=artifact)


EDIT_FILE = ToolSpec(
    name="edit_file",
    description=(
        "Replace exactly one occurrence of `old` with `new` inside a file. "
        "`old` must appear exactly once — include surrounding context to make it unique."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
    },
    handler=_edit_file,
    requires_approval=True,
)
