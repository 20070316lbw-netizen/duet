"""Tool definitions: schema + handler. Each tool also has an OpenAI/Anthropic JSON schema."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict          # JSON-schema, Anthropic-style
    handler: Callable[[dict, "ToolContext"], Awaitable["ToolOutcome"]]
    requires_approval: bool = False   # write_file/edit_file/run_shell → True


@dataclass
class ToolOutcome:
    """Returned by tool handlers."""
    content: str
    is_error: bool = False
    # Optional structured payload for the Diff Pane (file paths, patches…).
    artifact: dict | None = None


@dataclass
class ToolContext:
    """Passed to handlers — provides workspace root, safety hooks, approval callback."""
    workspace_root: Any            # pathlib.Path
    write_paths: list[str]
    deny_paths: list[str]
    shell_denylist: list[str]
    approve: Callable[[str, dict], Awaitable[bool]]   # async (label, artifact) -> bool
    notify: Callable[[str, dict], None]                # sync (event, payload) -> None
    trust: Any = None              # duet.trust.TrustPolicy | None


def anthropic_schema(spec: ToolSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    }


def openai_schema(spec: ToolSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": spec.input_schema,
    }
