"""Textual TUI: 左 Lobby + 右 Diff/Shell 卡片。审批与对话使用同一输入框。"""
from __future__ import annotations

import asyncio
import re

from rich.console import Group
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, RichLog, Static


# ── 角色配色 ────────────────────────────────────────────────────────────────
AUTHOR_STYLE = {
    "user":       "bold cyan",
    "planner":    "bold green",
    "supervisor": "bold magenta",
    "tool":       "yellow",
    "system":     "dim white",
}

AUTHOR_LABEL = {
    "user":       "你",
    "planner":    "Planner",
    "supervisor": "Supervisor",
    "tool":       "工具",
    "system":     "系统",
}


class DiffCard(Static):
    """右栏一张独立卡片:细单线边框 + 浅色标题条。"""

    DEFAULT_CSS = """
    DiffCard {
        margin: 0 0 1 0;
        padding: 0 1;
        border: hkey #2a3a55;
        background: #12161e;
    }
    DiffCard.shell { border: hkey #5a4a2a; }
    DiffCard.write { border: hkey #2a4a3a; }
    """

    def __init__(self, title: str, body, *, kind: str = "write"):
        super().__init__()
        self._title = title
        self._body = body
        self.add_class(kind)

    def render(self):
        return Group(
            Text(f"  {self._title}", style="bold #b8c4d8"),
            Text("─" * 60, style="#2a2f3a"),
            self._body,
        )


# ── GitHub 风格 diff 渲染 ────────────────────────────────────────────────────
HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def render_github_diff(diff_text: str, lang: str | None = None) -> Table:
    """把 unified diff 渲染成 GitHub 风格的两列行号 + 着色行。

    输入:`difflib.unified_diff` 的输出(包含 `--- / +++ / @@` 和 `+/-/ ` 行)。
    """
    table = Table(
        show_header=False, show_edge=False, pad_edge=False,
        box=None, padding=(0, 1, 0, 1), expand=True,
    )
    table.add_column(justify="right", style="dim", width=4, no_wrap=True)
    table.add_column(justify="right", style="dim", width=4, no_wrap=True)
    table.add_column(no_wrap=False, overflow="fold")

    old_no = new_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("---") or raw.startswith("+++"):
            continue  # 文件头我们自己在标题里展示了
        if raw.startswith("@@"):
            m = HUNK_RE.search(raw)
            if m:
                old_no = int(m.group(1))
                new_no = int(m.group(2))
            table.add_row(
                Text("…", style="dim"),
                Text("…", style="dim"),
                Text(raw, style="cyan dim"),
            )
            continue
        if not raw:
            table.add_row("", "", "")
            continue
        sign, body = raw[0], raw[1:]
        if sign == "+":
            table.add_row(
                "",
                Text(str(new_no), style="green"),
                Text(body, style="white on #15351c"),
            )
            new_no += 1
        elif sign == "-":
            table.add_row(
                Text(str(old_no), style="red"),
                "",
                Text(body, style="white on #4a1414"),
            )
            old_no += 1
        else:
            # 上下文行(以空格开头)
            table.add_row(
                Text(str(old_no), style="dim"),
                Text(str(new_no), style="dim"),
                Text(body, style="white"),
            )
            old_no += 1
            new_no += 1
    return table


class DuetApp(App):
    """单屏 MVP:左大厅 + 右 diff/shell 卡片栈。视觉:细线条 + 衬线感字符。"""

    CSS = """
    Screen { layout: vertical; background: #0f1115; }
    #panes { height: 1fr; padding: 1 1 0 1; }
    #lobby_wrap {
        width: 1fr;
        border: hkey #2a2f3a;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    #diff_wrap  {
        width: 1fr;
        border: hkey #2a2f3a;
        padding: 0 1;
    }
    #lobby_log { background: #0f1115; color: #d8dde6; }
    #diff_scroll { background: #0f1115; padding: 1 0; }
    #prompt {
        dock: bottom;
        border: hkey #3a4050;
        margin: 0 1 1 1;
        background: #0f1115;
        color: #e6eaf2;
    }
    #prompt.approving {
        border: hkey #c08a3a;
    }
    Header { background: #0f1115; color: #e6eaf2; }
    Footer { background: #0f1115; color: #8089a0; }
    #autocomplete {
        dock: bottom;
        height: auto;
        max-height: 8;
        border: hkey #3a4050;
        margin: 0 1 0 1;
        background: #161a22;
        color: #d8dde6;
        padding: 0 1;
        display: none;
    }
    #autocomplete.visible { display: block; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出"),
        Binding("escape", "interrupt", "中断"),
    ]

    def __init__(self, on_submit, on_interrupt):
        super().__init__()
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self._pending_approval: asyncio.Future | None = None
        self._busy_task: asyncio.Task | None = None
        # 流式输出累积:模型一次给几个字符,累到换行/结束才"定稿"。
        self._stream_buf: str = ""
        self._stream_author: str | None = None
        self._stream_widget: Static | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="panes"):
            with Vertical(id="lobby_wrap"):
                yield RichLog(id="lobby_log", wrap=True, highlight=False, markup=True)
            with Vertical(id="diff_wrap"):
                yield VerticalScroll(id="diff_scroll")
        yield Static("", id="autocomplete")
        yield Input(
            placeholder="输入消息，可用 @planner / @supervisor 指定谁回复（默认 @planner）",
            id="prompt",
        )
        yield Footer()

    # ── 命令补全:slash registry 注入 ──────────────────────────────────────
    def set_slash_registry(self, registry) -> None:
        self._slash_registry = registry

    def on_input_changed(self, event) -> None:
        if event.input.id != "prompt":
            return
        ac = self.query_one("#autocomplete", Static)
        text = event.value
        reg = getattr(self, "_slash_registry", None)
        if reg is None or not text.startswith("/"):
            ac.update("")
            ac.remove_class("visible")
            return
        prefix = text.split()[0] if text else "/"
        cands = reg.complete(prefix)
        if not cands:
            ac.update(Text("（无匹配命令）", style="dim"))
            ac.add_class("visible")
            return
        rt = Text()
        for c in cands[:8]:
            rt.append(f"  {c.name:<14}", style="bold #8acaff")
            rt.append(f" {c.summary}", style="#a8b0bf")
            if c.usage:
                rt.append(f"   {c.usage}", style="dim")
            rt.append("\n")
        ac.update(rt)
        ac.add_class("visible")

    # ── 左栏:大厅 ─────────────────────────────────────────────────────────
    def lobby_say(self, author: str, text: str) -> None:
        """非流式条目:一条完整记录。会先 flush 掉正在流式中的内容。"""
        self._flush_stream()
        log: RichLog = self.query_one("#lobby_log", RichLog)
        style = AUTHOR_STYLE.get(author, "white")
        label = AUTHOR_LABEL.get(author, author)
        line = Text()
        line.append(f"{label}", style=style)
        line.append("  ")
        line.append(text)
        log.write(line)

    def lobby_stream_begin(self, author: str) -> None:
        """开始一段流式输出,标记 author。"""
        self._flush_stream()
        self._stream_author = author
        self._stream_buf = ""

    def lobby_stream(self, text: str) -> None:
        """累积流式片段。整段不断增长,只在收到换行时把已完成的行真正写入 log。"""
        if self._stream_author is None:
            self.lobby_stream_begin("planner")
        self._stream_buf += text
        # 每次收到新片段:把 buf 切成 "完整行 + 残段"。
        # 完整行直接写进 RichLog,残段留下来等下次。
        while "\n" in self._stream_buf:
            line, _, rest = self._stream_buf.partition("\n")
            self._write_stream_line(line, final=True)
            self._stream_buf = rest
        # 残段:更新或新建一个临时 widget 显示当前未完成行
        self._render_partial()

    def lobby_stream_end(self) -> None:
        """模型本轮发言结束:flush 最后一行(如果有)。"""
        self._flush_stream()

    def _write_stream_line(self, line: str, *, final: bool) -> None:
        """把一行流式文本写入 RichLog(带作者前缀,只在该 stream 的第一行带)。"""
        log: RichLog = self.query_one("#lobby_log", RichLog)
        author = self._stream_author or "planner"
        style = AUTHOR_STYLE.get(author, "white")
        label = AUTHOR_LABEL.get(author, author)
        rt = Text()
        # 第一行带作者标签,后续行缩进对齐
        if not getattr(self, "_stream_started", False):
            rt.append(f"{label}", style=style)
            rt.append("  ")
            self._stream_started = True
        else:
            rt.append("    ")  # 续行缩进
        rt.append(line)
        log.write(rt)

    def _render_partial(self) -> None:
        """把当前未完成的残段实时显示出来(光标行)。

        实现:每次重新生成一行写入 RichLog 太吵,改为不显示残段,
        靠"换行才提交"的方式自然出现。残段会在下次换行/end 时一起出。
        """
        # 留空:RichLog 不支持原地更新最后一行,实时残段会带来闪烁。
        # 模型本身输出 token 流速很快,用户基本感觉不到延迟到下一个换行。
        pass

    def _flush_stream(self) -> None:
        if self._stream_author is None and not self._stream_buf:
            return
        if self._stream_buf:
            self._write_stream_line(self._stream_buf, final=True)
            self._stream_buf = ""
        self._stream_author = None
        self._stream_started = False

    # ── 右栏:diff / shell 卡片 ────────────────────────────────────────────
    def _push_card(self, card: DiffCard) -> None:
        scroll = self.query_one("#diff_scroll", VerticalScroll)
        scroll.mount(card)
        scroll.scroll_end(animate=False)

    def diff_show(self, label: str, diff_text: str) -> None:
        if diff_text.strip():
            body = render_github_diff(diff_text)
        else:
            body = Text("(无差异)", style="dim italic")
        self._push_card(DiffCard(f"✎ {label}", body, kind="write"))

    def diff_shell(self, label: str, output: str) -> None:
        body = Text(output or "(无输出)", style="white")
        self._push_card(DiffCard(f"$ {label}", body, kind="shell"))

    # ── 审批 ────────────────────────────────────────────────────────────────
    async def ask_approval(self, label: str, artifact: dict) -> bool:
        if "diff" in artifact:
            self.diff_show(label, artifact["diff"])
        elif "cmd" in artifact:
            self.diff_shell(label, artifact["cmd"])
        self.lobby_say("system", "是否批准？回车输入 a 同意 / r 拒绝")
        prompt = self.query_one("#prompt", Input)
        prompt.placeholder = "审批中：输入 a 同意 / r 拒绝（其它输入将被忽略）"
        prompt.add_class("approving")
        prompt.focus()
        loop = asyncio.get_running_loop()
        self._pending_approval = loop.create_future()
        try:
            return await self._pending_approval
        finally:
            self._pending_approval = None
            prompt.remove_class("approving")
            prompt.placeholder = (
                "输入消息，可用 @planner / @supervisor 指定谁回复（默认 @planner）"
            )

    # ── 输入处理 ────────────────────────────────────────────────────────────
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        # 1) 审批挂起时,优先吃掉 a/r
        if self._pending_approval is not None and not self._pending_approval.done():
            t = text.lower()
            if t in ("a", "accept", "y", "yes", "同意", "好"):
                self._pending_approval.set_result(True)
                self.lobby_say("system", "已批准。")
            elif t in ("r", "reject", "n", "no", "拒绝", "不"):
                self._pending_approval.set_result(False)
                self.lobby_say("system", "已拒绝。")
            else:
                self.lobby_say("system", "请输入 a 批准或 r 拒绝。")
            return

        # 2) 普通消息:fire-and-forget,不阻塞输入框
        if self._busy_task is not None and not self._busy_task.done():
            self.lobby_say("system", "上一轮还在跑，按 Esc 中断后再发。")
            return

        async def _run():
            try:
                await self._on_submit(text)
            except Exception as e:  # noqa: BLE001
                self.lobby_say("system", f"内部错误：{e!r}")

        self._busy_task = asyncio.create_task(_run())

    def action_interrupt(self) -> None:
        self._on_interrupt()
        if self._busy_task is not None and not self._busy_task.done():
            self._busy_task.cancel()
