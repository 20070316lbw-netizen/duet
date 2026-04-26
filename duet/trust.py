"""TrustPolicy:管理"哪类操作不再每次问审批"。

- write 规则:按 path glob 匹配。`*.py`、`docs/**`、`!secret*` 这种。
- shell 规则:按命令首词(argv[0])白名单。`pytest` / `npm test` 之类。
- 命令前 ! 表示禁止(优先级高于允许)。
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TrustRule:
    pattern: str
    deny: bool = False     # `!xxx` 形式

    @classmethod
    def parse(cls, raw: str) -> "TrustRule":
        raw = raw.strip()
        if raw.startswith("!"):
            return cls(pattern=raw[1:], deny=True)
        return cls(pattern=raw)

    def matches_path(self, path: str) -> bool:
        # 支持 `**/foo.py` 这种递归 glob
        return fnmatch.fnmatch(path, self.pattern) or \
               fnmatch.fnmatch(Path(path).name, self.pattern)

    def matches_cmd(self, head: str) -> bool:
        return fnmatch.fnmatch(head, self.pattern)


@dataclass
class TrustPolicy:
    write_rules: list[TrustRule] = field(default_factory=list)
    shell_rules: list[TrustRule] = field(default_factory=list)
    auto_write: bool = False     # /trust write all
    auto_shell: bool = False

    # ---- 决策入口 ----
    def auto_approve_write(self, path: str) -> bool:
        # deny 优先
        for r in self.write_rules:
            if r.deny and r.matches_path(path):
                return False
        if self.auto_write:
            return True
        for r in self.write_rules:
            if not r.deny and r.matches_path(path):
                return True
        return False

    def auto_approve_shell(self, cmd: str) -> bool:
        head = (cmd.strip().split() or [""])[0]
        for r in self.shell_rules:
            if r.deny and r.matches_cmd(head):
                return False
        if self.auto_shell:
            return True
        for r in self.shell_rules:
            if not r.deny and r.matches_cmd(head):
                return True
        return False

    # ---- /trust 命令 ----
    def add(self, kind: str, pattern: str) -> str:
        if kind == "write":
            if pattern == "all":
                self.auto_write = True
                return "已开启:所有写入操作自动通过(危险!)"
            self.write_rules.append(TrustRule.parse(pattern))
        elif kind == "shell":
            if pattern == "all":
                self.auto_shell = True
                return "已开启:所有 shell 命令自动通过(危险!)"
            self.shell_rules.append(TrustRule.parse(pattern))
        else:
            return f"未知类型:{kind}(应为 write 或 shell)"
        return f"已添加 {kind} 规则:{pattern}"

    def remove(self, kind: str, pattern: str) -> str:
        rules = self.write_rules if kind == "write" else self.shell_rules
        before = len(rules)
        rules[:] = [r for r in rules if r.pattern != pattern.lstrip("!")]
        return f"删除了 {before - len(rules)} 条 {kind} 规则"

    def clear(self) -> None:
        self.write_rules.clear()
        self.shell_rules.clear()
        self.auto_write = False
        self.auto_shell = False

    def summary(self) -> list[str]:
        lines: list[str] = []
        lines.append(f"write 全自动: {'开' if self.auto_write else '关'}")
        for r in self.write_rules:
            lines.append(f"  {'禁止' if r.deny else '允许'}: {r.pattern}")
        lines.append(f"shell 全自动: {'开' if self.auto_shell else '关'}")
        for r in self.shell_rules:
            lines.append(f"  {'禁止' if r.deny else '允许'}: {r.pattern}")
        if not self.write_rules and not self.shell_rules \
                and not self.auto_write and not self.auto_shell:
            lines.append("(没有信任规则,所有写入和 shell 都会询问)")
        return lines
