"""启动时的 Provider / API Key 选择面板。

设计要点:
- key 仅存进当前进程的 cfg.providers 中,**不写盘**;退出即失效。
- 默认选项是 cfg.planner.provider;如果环境变量已 set 对应 api_key_env,
  额外提示"已检测到环境变量"。
- 用 prompt_toolkit 做一个全屏的简单菜单,而不是引入 Textual,
  因为 Textual app 启动后接管终端,选择 → 切到主 TUI 不太顺。
- 没装 prompt_toolkit 也能用:回退到 input() 文本菜单。
"""
from __future__ import annotations

import getpass
import os
import sys

from duet import config as cfgmod


def _supported_models(kind: str) -> list[str]:
    if kind == "anthropic":
        return ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"]
    return [
        "deepseek-v4-flash", "deepseek-v4-pro",
        "gpt-4o", "gpt-4o-mini",
        "grok-3", "grok-3-fast",
    ]


def _read_int(prompt: str, lo: int, hi: int, default: int | None = None) -> int:
    while True:
        suffix = f" [回车=默认 {default}]" if default is not None else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            n = int(raw)
            if lo <= n <= hi:
                return n
        except ValueError:
            pass
        print(f"  请输入 {lo}-{hi} 之间的数字。")


def _read_str(prompt: str, default: str = "") -> str:
    suffix = f" [回车=默认 {default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or default


def _read_secret(prompt: str) -> str:
    """密码式输入,不回显。"""
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)


def select_provider(cfg: cfgmod.Config) -> cfgmod.Config:
    """交互式选 provider + 输 api key。返回修改后的 cfg(in-memory only)。

    如果某个 provider 已经从 config.toml 或 env 拿到了 api_key,
    会把它列为"已就绪",直接选编号即可跳过输入 key。
    """
    providers = list(cfg.providers.values())
    if not providers:
        print("[duet] 配置里一个 provider 都没有,先去填 ~/.duet/config.toml")
        sys.exit(2)

    print()
    print("┌─────────────────────────────────────────────────────────────")
    print("│  duet 启动 — 选择 LLM 供应商")
    print("│  (key 只在本次会话有效,退出后需要重新输入)")
    print("└─────────────────────────────────────────────────────────────")
    print()

    # 默认指向 planner.provider
    default_idx = 1
    for i, p in enumerate(providers, 1):
        ready = "✓ 就绪" if p.api_key else "  需要 key"
        marker = "★" if p.name == cfg.planner.provider else " "
        if p.name == cfg.planner.provider:
            default_idx = i
        print(f"  {marker} {i}. {p.name:<14} kind={p.kind:<14} {ready}")
    print()

    idx = _read_int("选择编号", 1, len(providers), default=default_idx)
    chosen = providers[idx - 1]

    # 如果还没 key,要求输入
    if not chosen.api_key:
        env = os.environ.get(_env_var_for(chosen.name, cfg)) or ""
        if env:
            print(f"  检测到环境变量已设置,直接使用。")
            chosen.api_key = env
        else:
            chosen.api_key = _read_secret(
                f"输入 {chosen.name} 的 API key (不会显示,不会写盘)"
            )
            if not chosen.api_key:
                print("  未输入 key,退出。")
                sys.exit(2)
    else:
        # 已有 key,问是否覆盖
        ans = _read_str("已检测到 key,是否覆盖? (y/N)", default="N")
        if ans.lower() in ("y", "yes"):
            chosen.api_key = _read_secret(f"输入 {chosen.name} 的 API key")

    # 选模型
    models = _supported_models(chosen.kind)
    print()
    print(f"  常用模型 (也可以手动输入其它):")
    for i, m in enumerate(models, 1):
        marker = "★" if m == cfg.planner.model else " "
        print(f"    {marker} {i}. {m}")
    raw = input(f"  选编号或直接输入模型名 [回车=默认 {cfg.planner.model}]: ").strip()
    if not raw:
        model = cfg.planner.model
    elif raw.isdigit() and 1 <= int(raw) <= len(models):
        model = models[int(raw) - 1]
    else:
        model = raw

    # 把选择写回 cfg(仅内存)
    cfg.planner.provider = chosen.name
    cfg.planner.model = model
    # supervisor 默认跟 planner 同一 provider,模型留 config 里写的
    if cfg.supervisor.provider not in cfg.providers or \
            not cfg.providers[cfg.supervisor.provider].api_key:
        cfg.supervisor.provider = chosen.name

    print()
    print(f"  ✓ 使用 {chosen.name} / {model}")
    print()
    return cfg


def _env_var_for(provider_name: str, cfg: cfgmod.Config) -> str:
    """在原始 toml 里查一下 api_key_env 字段。MVP 简单:按命名约定。"""
    return {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
        "deepseek":  "DEEPSEEK_API_KEY",
        "grok":      "XAI_API_KEY",
    }.get(provider_name, f"{provider_name.upper()}_API_KEY")
