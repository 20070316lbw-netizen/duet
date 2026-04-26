"""Provider abstraction. Only httpx — no SDKs (avoids proxy / version traps)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Literal


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUse:
    """Model wants to invoke a tool."""
    id: str
    name: str
    input: dict


@dataclass
class ToolResult:
    """We send this back to the model after running a tool."""
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class Done:
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "error"]


Event = TextDelta | ToolUse | ToolResult | Usage | Done


class BaseProvider:
    """All providers expose stream_events() yielding the unified Event stream."""

    name: str = "base"

    async def stream_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[Event]:
        raise NotImplementedError
        yield  # pragma: no cover (make it a generator for typing)
