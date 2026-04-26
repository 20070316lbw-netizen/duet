"""Lobby Router: parse leading @ tokens; default route = @planner.
Supervisor is silent unless explicitly @supervisor AND its `enabled` flag is True.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

KNOWN_AGENTS = ("planner", "supervisor")
TOKEN_RE = re.compile(r"^\s*(@(?:planner|supervisor|both))\b", re.IGNORECASE)


@dataclass
class Route:
    targets: list[str]            # subset of KNOWN_AGENTS
    body: str                     # message text with @-prefix stripped


def parse(text: str, *, default: str = "@planner") -> Route:
    m = TOKEN_RE.match(text)
    if m:
        token = m.group(1).lower()
        body = text[m.end():].lstrip()
        if token == "@both":
            return Route(targets=list(KNOWN_AGENTS), body=body)
        return Route(targets=[token[1:]], body=body)
    # No @ — fall back to default.
    tag = default.lstrip("@").lower()
    if tag == "both":
        return Route(targets=list(KNOWN_AGENTS), body=text)
    return Route(targets=[tag], body=text)


def filter_by_enabled(route: Route, enabled: dict[str, bool]) -> Route:
    """Drop targets whose agent is currently disabled (e.g. supervisor off)."""
    keep = [t for t in route.targets if enabled.get(t, False)]
    return Route(targets=keep, body=route.body)
