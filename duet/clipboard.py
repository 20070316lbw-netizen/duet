"""/copy 实现:优先剪贴板,失败回写到文件,提示用户去看。"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """尽力把 text 放进系统剪贴板。

    Returns (ok, detail)。detail 是用户能看的一句话。
    """
    if not text:
        return False, "(无内容)"

    # macOS
    if shutil.which("pbcopy"):
        try:
            p = subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=3)
            return True, "已复制到剪贴板 (pbcopy)"
        except Exception as e:  # noqa: BLE001
            return False, f"pbcopy 失败:{e!r}"

    # Linux: prefer wl-copy (Wayland), fallback xclip / xsel
    for cmd, args in (
        ("wl-copy", ["wl-copy"]),
        ("xclip",   ["xclip", "-selection", "clipboard"]),
        ("xsel",    ["xsel", "--clipboard", "--input"]),
    ):
        if shutil.which(cmd):
            try:
                subprocess.run(args, input=text.encode("utf-8"), check=True, timeout=3)
                return True, f"已复制到剪贴板 ({cmd})"
            except Exception as e:  # noqa: BLE001
                return False, f"{cmd} 失败:{e!r}"

    # Windows
    if os.name == "nt" and shutil.which("clip"):
        try:
            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True, timeout=3)
            return True, "已复制到剪贴板 (clip)"
        except Exception as e:  # noqa: BLE001
            return False, f"clip 失败:{e!r}"

    return False, "未找到剪贴板工具(macOS:pbcopy / Linux:xclip|xsel|wl-copy / Windows:clip)"


def copy_to_file(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
