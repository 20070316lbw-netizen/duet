"""Path-sandbox helpers — refuse anything outside write_paths or matching deny_paths."""
from __future__ import annotations

import fnmatch
from pathlib import Path

# Hard denylist that always wins, even if user explicitly allows.
ALWAYS_DENY = [
    ".env", ".env.*", "**/.env", "**/.env.*",
    "**/*_rsa", "**/*_rsa.pub", "**/*.pem", "**/*.key",
    "**/secrets/**", "**/.ssh/**", "**/.aws/**", "**/.gnupg/**",
]


def _match_any(rel: str, patterns: list[str]) -> bool:
    rel_norm = rel.replace("\\", "/")
    for pat in patterns:
        # fnmatch handles ** by collapsing to *; good enough for our purposes here.
        if fnmatch.fnmatch(rel_norm, pat):
            return True
        # also try matching the basename
        if fnmatch.fnmatch(Path(rel_norm).name, pat):
            return True
    return False


def resolve_safe(
    raw_path: str,
    *,
    workspace_root: Path,
    write_paths: list[str],
    deny_paths: list[str],
    for_write: bool,
) -> Path:
    """Resolve raw_path against workspace_root and validate.

    Raises PermissionError on any sandbox violation.
    """
    p = (workspace_root / raw_path).resolve()
    try:
        rel = p.relative_to(workspace_root.resolve())
    except ValueError as e:
        raise PermissionError(
            f"path escapes workspace: {raw_path}"
        ) from e

    rel_str = str(rel)
    if _match_any(rel_str, ALWAYS_DENY):
        raise PermissionError(f"sandbox: hard-denied path: {rel_str}")
    if _match_any(rel_str, deny_paths):
        raise PermissionError(f"sandbox: deny_paths match: {rel_str}")

    if for_write:
        ok = False
        for wp in write_paths:
            wp_root = (workspace_root / wp).resolve()
            try:
                p.relative_to(wp_root)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            raise PermissionError(
                f"sandbox: {rel_str} is not inside write_paths={write_paths}"
            )
    return p
