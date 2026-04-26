"""Anthropic Messages API — direct HTTP via httpx, SSE-parsed."""
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


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com"):
        if not api_key:
            raise ValueError("anthropic api_key is empty (set ANTHROPIC_API_KEY)")
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
        url = f"{self.base_url}/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools

        # In-flight assembly for tool_use blocks (their input arrives as JSON deltas).
        tool_assembly: dict[int, dict] = {}
        usage_in = 0
        usage_out = 0
        stop_reason = "end_turn"

        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    err = await resp.aread()
                    raise RuntimeError(
                        f"anthropic {resp.status_code}: {err.decode('utf-8', 'ignore')[:500]}"
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        ev = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    et = ev.get("type")
                    if et == "message_start":
                        u = ev.get("message", {}).get("usage", {})
                        usage_in = u.get("input_tokens", 0)
                    elif et == "content_block_start":
                        idx = ev.get("index", 0)
                        block = ev.get("content_block", {})
                        if block.get("type") == "tool_use":
                            tool_assembly[idx] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "json": "",
                            }
                    elif et == "content_block_delta":
                        idx = ev.get("index", 0)
                        d = ev.get("delta", {})
                        dt = d.get("type")
                        if dt == "text_delta":
                            yield TextDelta(text=d.get("text", ""))
                        elif dt == "input_json_delta":
                            if idx in tool_assembly:
                                tool_assembly[idx]["json"] += d.get("partial_json", "")
                    elif et == "content_block_stop":
                        idx = ev.get("index", 0)
                        if idx in tool_assembly:
                            t = tool_assembly.pop(idx)
                            try:
                                inp = json.loads(t["json"]) if t["json"] else {}
                            except json.JSONDecodeError:
                                inp = {"_raw": t["json"]}
                            yield ToolUse(id=t["id"], name=t["name"], input=inp)
                    elif et == "message_delta":
                        d = ev.get("delta", {})
                        if "stop_reason" in d and d["stop_reason"]:
                            stop_reason = d["stop_reason"]
                        u = ev.get("usage", {})
                        if "output_tokens" in u:
                            usage_out = u["output_tokens"]
                    elif et == "message_stop":
                        break
                    elif et == "error":
                        err = ev.get("error", {})
                        raise RuntimeError(
                            f"anthropic stream error: {err.get('type')}: {err.get('message')}"
                        )

        yield Usage(input_tokens=usage_in, output_tokens=usage_out)
        yield Done(stop_reason=stop_reason)  # type: ignore[arg-type]
