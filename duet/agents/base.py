"""Agent base + Planner / Supervisor.

每个 Agent 拥有自己的 history,Lobby Router 决定塞什么进哪个 history。
本类负责跑 tool-use 循环 + 把每一步写进 transcript(若提供)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from duet.errors import humanize
from duet.providers import (
    BaseProvider,
    Done,
    TextDelta,
    ToolUse,
    Usage,
)
from duet.tools.base import ToolContext, ToolOutcome, ToolSpec, anthropic_schema


PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


@dataclass
class AgentRunStats:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    rounds: int = 0


@dataclass
class Agent:
    name: str
    model: str
    provider: BaseProvider
    tools: list[ToolSpec]
    system_prompt: str
    history: list[dict] = field(default_factory=list)
    stats: AgentRunStats = field(default_factory=AgentRunStats)
    max_rounds: int = 8
    # 注入项:
    transcript: object | None = None        # Transcript;None 表示不持久化 blocks
    base_url: str = ""                       # 错误格式化时用

    # ── 历史操作 ─────────────────────────────────────────────────────────
    def append_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def append_assistant_text(self, text: str) -> None:
        if not text:
            return
        self.history.append({"role": "assistant", "content": text})

    # ── 主循环 ───────────────────────────────────────────────────────────
    async def run(
        self,
        *,
        ctx: ToolContext,
        on_text: Callable[[str], None],
        on_tool_use: Callable[[ToolUse], None] = lambda _t: None,
        on_tool_result: Callable[[str, ToolOutcome], None] = lambda _i, _r: None,
    ) -> AgentRunStats:
        tool_schemas = [anthropic_schema(t) for t in self.tools]
        tool_by_name = {t.name: t for t in self.tools}

        for _ in range(self.max_rounds):
            self.stats.rounds += 1
            collected_text: list[str] = []
            tool_uses: list[ToolUse] = []
            stop = "end_turn"

            try:
                async for ev in self.provider.stream_events(
                    model=self.model,
                    system=self.system_prompt,
                    messages=self.history,
                    tools=tool_schemas,
                ):
                    if isinstance(ev, TextDelta):
                        collected_text.append(ev.text)
                        on_text(ev.text)
                    elif isinstance(ev, ToolUse):
                        tool_uses.append(ev)
                        on_tool_use(ev)
                    elif isinstance(ev, Usage):
                        self.stats.input_tokens += ev.input_tokens
                        self.stats.output_tokens += ev.output_tokens
                    elif isinstance(ev, Done):
                        stop = ev.stop_reason
            except Exception as e:  # noqa: BLE001
                friendly = humanize(
                    e,
                    hint_provider=self.provider.name,
                    hint_model=self.model,
                    hint_base_url=self.base_url,
                )
                # 抛回去让 cli 层显示;但保留可读 message
                raise RuntimeError(friendly) from e

            # 拼出 assistant turn(Anthropic content blocks)
            content_blocks: list[dict] = []
            text_joined = "".join(collected_text)
            if text_joined:
                content_blocks.append({"type": "text", "text": text_joined})
            for tu in tool_uses:
                content_blocks.append(
                    {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}
                )
            if content_blocks:
                self.history.append({"role": "assistant", "content": content_blocks})
                # 持久化(完整 blocks,供 --resume 深度重放)
                if self.transcript is not None:
                    self.transcript.append(
                        author=self.name,                       # type: ignore[arg-type]
                        payload={"text": text_joined, "blocks": content_blocks},
                    )

            if not tool_uses or stop != "tool_use":
                return self.stats

            # 跑工具,拼 tool_result 块
            results: list[dict] = []
            for tu in tool_uses:
                self.stats.tool_calls += 1
                spec = tool_by_name.get(tu.name)
                if spec is None:
                    outcome = ToolOutcome(
                        content=f"[unknown tool: {tu.name}]", is_error=True
                    )
                else:
                    try:
                        outcome = await spec.handler(tu.input, ctx)
                    except PermissionError as e:
                        outcome = ToolOutcome(content=f"[sandbox: {e}]", is_error=True)
                    except Exception as e:  # noqa: BLE001
                        outcome = ToolOutcome(content=f"[tool error: {e!r}]", is_error=True)
                on_tool_result(tu.id, outcome)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": outcome.content,
                        "is_error": outcome.is_error,
                    }
                )
                if self.transcript is not None:
                    self.transcript.append(
                        author="tool",                          # type: ignore[arg-type]
                        payload={
                            "tool_use_id": tu.id,
                            "content": outcome.content,
                            "is_error": outcome.is_error,
                            "for_agent": self.name,
                            "tool_name": tu.name,
                        },
                    )
            self.history.append({"role": "user", "content": results})

        return self.stats


def load_prompt(name: str) -> str:
    p = PROMPTS_DIR / f"{name}.md"
    return p.read_text(encoding="utf-8") if p.exists() else f"You are {name}."
