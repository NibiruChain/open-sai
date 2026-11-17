"""Read and clear the system clipboard through platform commands."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    """A clipboard command pair: one reads, the other empties.

    The clear command gets empty stdin, which `pbcopy` and `xclip` store as
    the new, empty clipboard.
    """

    read: tuple[str, ...]
    clear: tuple[str, ...]


# Order: macOS, Wayland, X11, then WSL and Windows.
TOOLS = (
    Tool(read=("pbpaste",), clear=("pbcopy",)),
    Tool(read=("wl-paste", "--no-newline"), clear=("wl-copy", "--clear")),
    Tool(
        read=("xclip", "-selection", "clipboard", "-o"),
        clear=("xclip", "-selection", "clipboard"),
    ),
    Tool(
        # Windows PowerShell writes the console code page unless told to
        # use UTF-8; -Raw keeps line breaks instead of an array of lines.
        read=(
            "powershell.exe",
            "-NoProfile",
            "-Command",
            (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8;"
                " Get-Clipboard -Raw"
            ),
        ),
        clear=(
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Set-Clipboard -Value $null",
        ),
    ),
)

NAMES = ", ".join(tool.read[0] for tool in TOOLS)


def available() -> list[Tool]:
    return [tool for tool in TOOLS if shutil.which(tool.read[0])]


def read() -> tuple[str, Tool] | None:
    """Return the clipboard text and the tool that read it.

    Tries each installed tool in order and skips one that fails, such as
    `wl-paste` without a Wayland display, or whose output is not UTF-8
    text, such as a copied image. Returns None when none works.
    """
    for tool in available():
        try:
            proc = subprocess.run(
                tool.read,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=True,
            )
            text = proc.stdout.decode("utf-8-sig")
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            continue
        return text.strip(), tool
    return None


def clear(tool: Tool) -> bool:
    """Empty the clipboard with TOOL. Best effort; returns success.

    Output goes to /dev/null because `xclip` forks to own the selection and
    would hold a captured pipe open.
    """
    try:
        subprocess.run(
            tool.clear,
            input="",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
