# -*- coding: utf-8 -*-
"""Shared console log formatting for StoryDownloader."""

from __future__ import annotations

import os
import sys
from typing import Any


RESET = "\033[0m"
COLORS = {
    "green": "\033[32m",
    "cyan": "\033[36m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "dim": "\033[2m",
}


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return True


def colorize(text: str, color: str) -> str:
    if not _color_enabled():
        return text
    return f"{COLORS.get(color, '')}{text}{RESET}"


def _status_to_int(status: Any) -> int | None:
    if isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        match = "".join(ch for ch in status if ch.isdigit())
        if match:
            try:
                return int(match)
            except ValueError:
                return None
    return None


def status_label(status: Any) -> str:
    """Return colored status text such as HTTP=200, HTTP=404, CACHE, ERR."""
    if status is None or status == "":
        status = "ERR"

    if isinstance(status, str):
        normalized = status.strip().upper()
        if normalized == "CACHE":
            return colorize("CACHE", "cyan")
        if normalized in {"ERR", "ERROR", "FAIL", "FAILED"}:
            return colorize("ERR", "red")

    code = _status_to_int(status)
    if code is None:
        return colorize(str(status), "red")

    label = f"HTTP={code}"
    if 200 <= code < 300:
        return colorize(label, "green")
    if 300 <= code < 400:
        return colorize(label, "cyan")
    if 400 <= code < 500:
        return colorize(label, "yellow")
    if code >= 500:
        return colorize(label, "red")
    return colorize(label, "dim")


def _pad(value: int, total: int, min_width: int) -> str:
    width = max(min_width, len(str(max(total, value, 0))))
    return f"{value:0{width}d}"


def chapter_log_line(
    done: int,
    selected_total: int,
    status: Any,
    chapter_index: int,
    chapter_total: int,
    title: str,
) -> str:
    """Format chapter progress logs consistently across downloader modules."""
    progress = f"[{_pad(done, selected_total, 3)}/{_pad(selected_total, selected_total, 3)}]"
    chapter_no = f"{_pad(chapter_index, chapter_total, 4)}/{_pad(chapter_total, chapter_total, 4)}"
    return f"{progress} [{status_label(status)}] Chương {chapter_no}: {title}"


def print_chapter_log(
    done: int,
    selected_total: int,
    status: Any,
    chapter_index: int,
    chapter_total: int,
    title: str,
) -> None:
    print(chapter_log_line(done, selected_total, status, chapter_index, chapter_total, title), flush=True)
