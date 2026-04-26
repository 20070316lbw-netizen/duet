"""Sessions:列出 / 恢复会话,把 transcript 回放成 agent.history。

深度重放:user 文本、assistant 的 text+tool_use、tool_result 全部还原。
依赖 transcript 里的 payload kind:
  - user        → {"text": "..."}
  - planner/sup → {"text": "...", "blocks": [...]}    # blocks 是 anthropic content blocks
  - tool        → {"tool_use_id": "...", "content": "...", "is_error": bool, "for_agent": "planner"}
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SessionInfo:
    id: str
    path: Path
    msg_count: int
    last_ts: float


def list_sessions(home: Path) -> list[SessionInfo]:
    root = home / "sessions"
    if not root.exists():
        return []
    out: list[SessionInfo] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        jl = d / "branches" / "main" / "messages.jsonl"
        if not jl.exists():
            continue
        n = 0
        last = 0.0
        with jl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    last = max(last, json.loads(line).get("ts", 0.0))
                except Exception:
                    pass
        out.append(SessionInfo(id=d.name, path=d, msg_count=n, last_ts=last))
    return sorted(out, key=lambda s: -s.last_ts)


def replay_into(transcript_messages: list, agents_history: dict[str, list]) -> int:
    """把 transcript 重放进 agent histories。

    遍历 transcript:
      - user:按 route 把 user 文本 append 到目标 agent
      - planner/supervisor:用 payload['blocks'] 重建 assistant content blocks
        (没有 blocks 字段就退化为纯文本 assistant)
      - tool:把 tool_result block 拼回 *该工具属于哪个 agent* 的 history
    """
    # tool result 要紧接在 assistant tool_use 之后;但 transcript 不一定严格
    # 交错。简化策略:每个 agent 维护一个 buffer,按出现顺序一个个 append。
    n = 0
    for msg in transcript_messages:
        author = msg.author
        if author == "user":
            for tgt in (msg.route or []):
                hist = agents_history.get(tgt)
                if hist is None:
                    continue
                txt = msg.payload.get("text", "")
                if not txt:
                    continue
                # supervisor view 已经在 transcript 里被 prefix 过(_supervisor_view),
                # 直接 append 就好;如果是 supervisor 的 user 行,我们没法精确还原
                # 当时是不是被加了 [来自 Planner] 前缀,所以保守地按原文塞进去。
                hist.append({"role": "user", "content": txt})
                n += 1

        elif author in ("planner", "supervisor"):
            hist = agents_history.get(author)
            if hist is None:
                continue
            blocks = msg.payload.get("blocks")
            text = msg.payload.get("text") or ""
            if blocks:
                hist.append({"role": "assistant", "content": blocks})
            elif text and text != "(streamed inline)":
                hist.append({"role": "assistant", "content": text})
            else:
                continue
            n += 1

        elif author == "tool":
            for_agent = msg.payload.get("for_agent")
            hist = agents_history.get(for_agent) if for_agent else None
            if hist is None:
                continue
            tool_use_id = msg.payload.get("tool_use_id")
            content = msg.payload.get("content", "")
            is_error = bool(msg.payload.get("is_error", False))
            if not tool_use_id:
                continue
            # 单独一行 user 块,内含 tool_result
            block = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": str(content),
                "is_error": is_error,
            }
            # 如果上一条 history 已经是 tool_result-only user message,合并进去
            if hist and hist[-1].get("role") == "user" and \
                    isinstance(hist[-1].get("content"), list) and \
                    all(isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in hist[-1]["content"]):
                hist[-1]["content"].append(block)
            else:
                hist.append({"role": "user", "content": [block]})
            n += 1
        # author == "system" 不重放
    return n
