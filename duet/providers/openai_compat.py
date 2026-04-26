"""OpenAI-compatible chat-completions streaming. SDK-free, httpx + SSE."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from duet.providers.base import (
    BaseProvider,
    Done,
    Event,
    TextDelta,
    ToolUse,
    Usage,
)


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Translate Anthropic-style content blocks → OpenAI chat messages.

    Internal history uses Anthropic's shape:
      assistant: [{type:"text",...}, {type:"tool_use", id, name, input}]
      user:      [{type:"tool_result", tool_use_id, content, is_error}, ...]

    OpenAI/DeepSeek expect:
      assistant: {content:"...", tool_calls:[{id,type:"function",function:{name,arguments}}]}
      tool:      {role:"tool", tool_call_id, content}

    DeepSeek strictly rejects unknown block types, so we MUST translate.
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")

        # Plain string content — pass through.
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        # Anthropic-style block list.
        if role == "assistant" and isinstance(content, list):
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for blk in content:
                t = blk.get("type")
                if t == "text":
                    text_parts.append(blk.get("text", ""))
                elif t == "tool_use":
                    tool_calls.append({
                        "id": blk.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": blk.get("name", ""),
                            "arguments": json.dumps(blk.get("input") or {}),
                        },
                    })
            msg: dict = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        if role == "user" and isinstance(content, list):
            # Tool results split into separate role:"tool" messages.
            # Plain text blocks (if any) collapse into a regular user message.
            text_parts: list[str] = []
            for blk in content:
                t = blk.get("type")
                if t == "tool_result":
                    body = blk.get("content", "")
                    if isinstance(body, list):
                        body = "".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in body
                        )
                    out.append({
                        "role": "tool",
                        "tool_call_id": blk.get("tool_use_id", ""),
                        "content": str(body),
                    })
                elif t == "text":
                    text_parts.append(blk.get("text", ""))
            if text_parts:
                out.append({"role": "user", "content": "".join(text_parts)})
            continue

        # Fallback: pass through whatever shape it is.
        out.append(m)
    return out


class OpenAICompatProvider(BaseProvider):
    name = "openai_compat"

    def __init__(self, api_key: str, base_url: str):
        if not api_key:
            raise ValueError("openai_compat api_key is empty")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def stream_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[Event]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }
        oai_messages = [{"role": "system", "content": system},
                        *_to_openai_messages(messages)]
        body: dict = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            # Anthropic 风格 -> OpenAI 风格:input_schema 改名为 parameters,
            # 没 schema 的工具也补一个空 object,避免 Grok / 严格校验拒掉。
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": (
                            t.get("parameters")
                            or t.get("input_schema")
                            or {"type": "object", "properties": {}}
                        ),
                    },
                }
                for t in tools
            ]

        # Assemble streamed tool_calls by index.
        tool_assembly: dict[int, dict] = {}
        usage_in = 0
        usage_out = 0
        stop_reason = "end_turn"

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    err = await resp.aread()
                    raise RuntimeError(
                        f"openai_compat {resp.status_code}: {err.decode('utf-8','ignore')[:500]}"
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = ev.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta", {}) or {}
                        if "content" in delta and delta["content"]:
                            yield TextDelta(text=delta["content"])
                        for tc in delta.get("tool_calls", []) or []:
                            idx = tc.get("index", 0)
                            slot = tool_assembly.setdefault(
                                idx, {"id": "", "name": "", "args": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function", {}) or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                        fr = choices[0].get("finish_reason")
                        if fr:
                            stop_reason = (
                                "tool_use" if fr == "tool_calls" else
                                "end_turn" if fr == "stop" else
                                "max_tokens" if fr == "length" else
                                "error"
                            )
                    u = ev.get("usage")
                    if u:
                        usage_in = u.get("prompt_tokens", usage_in)
                        usage_out = u.get("completion_tokens", usage_out)

        for slot in tool_assembly.values():
            try:
                inp = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                inp = {"_raw": slot["args"]}
            yield ToolUse(id=slot["id"] or slot["name"], name=slot["name"], input=inp)

        yield Usage(input_tokens=usage_in, output_tokens=usage_out)
        yield Done(stop_reason=stop_reason)  # type: ignore[arg-type]
