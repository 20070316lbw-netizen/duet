"""Transcript: append-only DAG. MVP only ever uses branch_id='main'."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


Author = Literal["user", "planner", "supervisor", "tool", "system"]


@dataclass
class LobbyMessage:
    id: str
    parent_id: str | None
    branch_id: str
    author: Author
    payload: dict                # {"text": "..."} or {"tool_use": {...}} etc.
    ts: float = field(default_factory=time.time)
    route: list[str] = field(default_factory=list)   # which agents this was routed to


class Transcript:
    """Append-only message log, persisted as JSONL per branch."""

    def __init__(self, session_dir: Path, branch_id: str = "main"):
        self.session_dir = session_dir
        self.branch_id = branch_id
        self.path = session_dir / "branches" / branch_id / "messages.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head: str | None = None
        self.messages: list[LobbyMessage] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                self.messages.append(LobbyMessage(**d))
        if self.messages:
            self._head = self.messages[-1].id

    def append(self, *, author: Author, payload: dict, route: list[str] | None = None) -> LobbyMessage:
        msg = LobbyMessage(
            id=uuid.uuid4().hex[:12],
            parent_id=self._head,
            branch_id=self.branch_id,
            author=author,
            payload=payload,
            route=route or [],
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")
            f.flush()
        self._head = msg.id
        self.messages.append(msg)
        return msg

    def head(self) -> str | None:
        return self._head


def session_dir(root: Path, session_id: str) -> Path:
    p = root / "sessions" / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p
