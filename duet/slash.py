"""Slash 命令注册表:解析 + 补全候选。

目的:让 cli.py 的巨大 if/elif 链变成"加一个命令 = 加一个 register 调用"。
还顺便给 TUI 提供"输入 / 时弹候选"的数据源。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class SlashCommand:
    name: str                                 # "/cost"
    summary: str                              # 一行说明,中文
    usage: str = ""                           # "/agent <名字> on|off"
    handler: Callable[..., Awaitable[None]] | None = None


class SlashRegistry:
    def __init__(self) -> None:
        self._cmds: dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand) -> None:
        self._cmds[cmd.name] = cmd

    def get(self, name: str) -> SlashCommand | None:
        return self._cmds.get(name)

    def all(self) -> list[SlashCommand]:
        return sorted(self._cmds.values(), key=lambda c: c.name)

    def complete(self, prefix: str) -> list[SlashCommand]:
        """返回 name 以 prefix 开头的命令(按字母序)。"""
        return [c for c in self.all() if c.name.startswith(prefix)]
