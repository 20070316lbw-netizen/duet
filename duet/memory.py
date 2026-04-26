"""Memory 层:用户偏好/项目笔记的持久化。

数据模型(MVP):
- 一个 SQLite 文件 `~/.duet/memory.db`,表 `memories(id, kind, scope, key, value, ts)`。
- kind ∈ {user, project, reference};scope = workspace 路径(project 类)或 "*"(user 类)。
- 注入时机:每次 agent.run 前,把命中当前 workspace 的 user + project 记忆拼成
  "USER MEMORIES" 段落,prepend 到 system_prompt。
- 命令:
    /memory          列出所有记忆
    /memory add <kind> <key>=<value>     添加(kind 默认 user)
    /forget <id>     删除一条
    /forget all      清空当前 workspace 的 project 记忆
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Memory:
    id: int
    kind: str         # "user" | "project" | "reference"
    scope: str        # workspace path or "*"
    key: str
    value: str
    ts: float


class MemoryStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                scope TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                ts REAL NOT NULL
            )
            """
        )
        self.conn.commit()

    def add(self, kind: str, scope: str, key: str, value: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO memories (kind, scope, key, value, ts) VALUES (?,?,?,?,?)",
            (kind, scope, key, value, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def delete(self, id: int) -> bool:
        cur = self.conn.execute("DELETE FROM memories WHERE id = ?", (id,))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_project(self, scope: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM memories WHERE kind='project' AND scope=?", (scope,),
        )
        self.conn.commit()
        return cur.rowcount

    def list_all(self) -> list[Memory]:
        rows = self.conn.execute(
            "SELECT id, kind, scope, key, value, ts FROM memories ORDER BY id"
        ).fetchall()
        return [Memory(*r) for r in rows]

    def for_workspace(self, workspace: str) -> list[Memory]:
        """命中当前 workspace 的记忆:user(scope='*') + project(scope=workspace)。"""
        rows = self.conn.execute(
            """
            SELECT id, kind, scope, key, value, ts FROM memories
            WHERE (kind='user' AND scope='*') OR (kind='project' AND scope=?)
            ORDER BY kind, id
            """,
            (workspace,),
        ).fetchall()
        return [Memory(*r) for r in rows]

    def render_for_prompt(self, workspace: str) -> str:
        mems = self.for_workspace(workspace)
        if not mems:
            return ""
        lines = ["## USER MEMORIES (持久偏好,优先级高于默认行为)"]
        for m in mems:
            lines.append(f"- [{m.kind}] {m.key}: {m.value}")
        return "\n".join(lines) + "\n"

    def close(self) -> None:
        self.conn.close()
