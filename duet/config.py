"""Configuration loader: ~/.duet/config.toml + env override + secret scan."""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.path.expanduser("~/.duet"))
CONFIG_PATH = CONFIG_DIR / "config.toml"

# Heuristic patterns for plaintext secrets we never want sitting in the workspace.
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xai-[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
]
SCAN_GLOBS = ("*.toml", "*.env", "*.json", "*.yaml", "*.yml")
SCAN_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".duet"}


@dataclass
class ProviderCfg:
    name: str          # logical name used in [planner].provider
    kind: str          # wire protocol: "anthropic" | "openai_compat"
    api_key: str
    base_url: str

    def redact(self) -> str:
        k = self.api_key
        if not k or len(k) < 12:
            return "<unset>"
        return f"{k[:8]}***{k[-4:]}"


@dataclass
class AgentCfg:
    provider: str
    model: str
    enabled: bool = True


@dataclass
class Config:
    default_route: str
    write_paths: list[str]
    deny_paths: list[str]
    planner: AgentCfg
    supervisor: AgentCfg
    providers: dict[str, ProviderCfg] = field(default_factory=dict)
    trust_write: str = "manual"
    trust_shell: str = "manual"
    shell_denylist: list[str] = field(default_factory=list)
    theme: str = "dark"


def load() -> Config:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"[duet] no config at {CONFIG_PATH}. "
            f"Copy config.example.toml there to start."
        )
    with open(CONFIG_PATH, "rb") as f:
        raw = tomllib.load(f)

    providers: dict[str, ProviderCfg] = {}
    # New format: [providers.<name>] tables
    for name, sec in (raw.get("providers", {}) or {}).items():
        env = sec.get("api_key_env", "")
        key = os.environ.get(env, "") if env else ""
        if not key:
            key = sec.get("api_key", "") or ""
        providers[name] = ProviderCfg(
            name=name,
            kind=sec.get("kind", "openai_compat"),
            api_key=key,
            base_url=sec.get("base_url", ""),
        )
    # Back-compat: old top-level [anthropic] / [openai_compat] tables
    for legacy_name, kind in (("anthropic", "anthropic"),
                              ("openai_compat", "openai_compat")):
        if legacy_name in providers:
            continue
        sec = raw.get(legacy_name)
        if not sec:
            continue
        env = sec.get("api_key_env", "")
        key = os.environ.get(env, "") if env else ""
        if not key:
            key = sec.get("api_key", "") or ""
        providers[legacy_name] = ProviderCfg(
            name=legacy_name, kind=kind, api_key=key,
            base_url=sec.get("base_url", ""),
        )

    p = raw.get("planner", {})
    s = raw.get("supervisor", {})
    ws = raw.get("workspace", {})
    tr = raw.get("trust", {})
    ui = raw.get("ui", {})

    return Config(
        default_route=raw.get("default_route", "@planner"),
        write_paths=ws.get("write_paths", ["./"]),
        deny_paths=ws.get("deny_paths", []),
        planner=AgentCfg(
            provider=p.get("provider", "anthropic"),
            model=p.get("model", "claude-sonnet-4-5"),
            enabled=True,
        ),
        supervisor=AgentCfg(
            provider=s.get("provider", "anthropic"),
            model=s.get("model", "claude-sonnet-4-5"),
            enabled=s.get("enabled", False),
        ),
        providers=providers,
        trust_write=tr.get("write", "manual"),
        trust_shell=tr.get("shell", "manual"),
        shell_denylist=tr.get("shell_denylist", []),
        theme=ui.get("theme", "dark"),
    )


def scan_workspace_for_secrets(root: Path) -> list[tuple[Path, str]]:
    """Return list of (path, matched_prefix) findings. Empty = clean."""
    hits: list[tuple[Path, str]] = []
    for path in _iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in SECRET_PATTERNS:
            m = pat.search(text)
            if m:
                hits.append((path, m.group(0)[:12] + "***"))
                break
    return hits


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP_DIRS]
        for fn in filenames:
            for g in SCAN_GLOBS:
                if Path(fn).match(g):
                    yield Path(dirpath) / fn
                    break
