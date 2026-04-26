"""把底层异常翻译成"人话",外加适当的修复建议。"""
from __future__ import annotations

import re
from urllib.parse import urlparse


def humanize(exc: BaseException, *, hint_provider: str = "", hint_model: str = "",
             hint_base_url: str = "") -> str:
    """返回一段对终端用户友好的多行报错字符串。

    - 网络错误 (ConnectionError / 解析失败 / SSL) → 提示代理 / base_url
    - 4xx → 提示具体含义(401 → key 错;404 → model id 错;422 → schema 错)
    - 5xx → 服务端波动,稍后重试
    """
    text = repr(exc)
    msg = str(exc)
    cls = exc.__class__.__name__

    # ── 1. httpx 一族(ConnectError / ReadError / TimeoutException / ProxyError)
    netty = ("ConnectError", "ReadError", "ConnectTimeout", "ReadTimeout",
            "ProxyError", "RemoteProtocolError", "ConnectionError")
    if cls in netty or "Connection" in cls or "Timeout" in cls:
        host = ""
        try:
            if hint_base_url:
                host = urlparse(hint_base_url).hostname or hint_base_url
        except Exception:
            host = hint_base_url
        lines = [
            f"无法连接到 LLM 服务{f' ({host})' if host else ''}。",
            "可能的原因:",
            "  • 网络不通 / 需要科学上网(尤其是 api.x.ai / api.anthropic.com)",
            f"  • base_url 写错了:当前 = {hint_base_url or '(未知)'}",
            "  • 系统代理变量 HTTP_PROXY / HTTPS_PROXY 没设或设错",
            f"原始错误:{cls}: {msg or '(空)'}",
        ]
        return "\n".join(lines)

    # ── 2. 我们自己包装的 RuntimeError("xxx <code>: <body>")
    m = re.search(r"(\w+)\s+(\d{3}):\s*(.*)", msg, re.DOTALL)
    if m:
        proto, code, body = m.group(1), int(m.group(2)), m.group(3)
        return _explain_http(proto, code, body, hint_provider, hint_model)

    return f"{cls}: {msg or '(no message)'}"


def _explain_http(proto: str, code: int, body: str, provider: str, model: str) -> str:
    head = f"{provider or proto} 返回 {code}"
    body_short = body[:300].strip()

    if code == 401 or code == 403:
        return (f"{head}:API key 无效或已被禁用。\n"
                f"  • 检查刚才输入的 key 是否完整(注意有没有粘贴到空格)\n"
                f"  • 该 provider 的额度是否还有余量\n"
                f"原始 body:{body_short}")
    if code == 404:
        return (f"{head}:接口或模型不存在。\n"
                f"  • 模型 id 当前 = {model!r},确认拼写\n"
                f"  • base_url 是否带了 /v1(OpenAI 兼容协议都需要)\n"
                f"原始 body:{body_short}")
    if code == 422:
        return (f"{head}:请求体被服务端校验拒绝。\n"
                f"  • 通常是工具(tools)字段缺字段、或 messages 顺序不合法\n"
                f"  • 这是 duet 内部 bug,请把下面这段粘给开发者:\n"
                f"原始 body:{body_short}")
    if code == 429:
        return (f"{head}:速率受限或额度耗尽。稍等几秒重试,或切到另一个 provider。\n"
                f"原始 body:{body_short}")
    if 500 <= code < 600:
        return (f"{head}:服务端错误,通常稍等就好。\n"
                f"原始 body:{body_short}")
    return f"{head}\n原始 body:{body_short}"
