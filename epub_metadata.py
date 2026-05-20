# -*- coding: utf-8 -*-
"""Helpers chung cho metadata EPUB."""

from __future__ import annotations

import html
import re
from typing import Any, List

PUBLISHER = "Hishiro"

_EMPTY_TAGS = {
    "",
    "-",
    "--",
    "—",
    "n/a",
    "na",
    "none",
    "unknown",
    "không rõ",
    "khong ro",
    "không có",
    "n/a",
}


def normalize_tags(tags: Any) -> List[str]:
    """Chuẩn hóa thể loại/tags thành list chuỗi để ghi dc:subject."""
    if tags is None:
        return []

    if isinstance(tags, dict):
        for key in ("category", "genre", "genres", "tags", "Genre", "Genres"):
            value = tags.get(key)
            result = normalize_tags(value)
            if result:
                return result
        return []

    raw_items: List[str] = []
    if isinstance(tags, str):
        value = html.unescape(tags)
        value = re.sub(r"\s+-\s+", ",", value)
        raw_items = re.split(r"\s*[,;|/]\s*", value)
    elif isinstance(tags, (list, tuple, set)):
        for item in tags:
            raw_items.extend(normalize_tags(item))
    else:
        raw_items = [str(tags)]

    cleaned: List[str] = []
    seen = set()
    for item in raw_items:
        tag = re.sub(r"\s+", " ", str(item or "")).strip()
        if tag.lower() in _EMPTY_TAGS:
            continue
        if tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
    return cleaned


def subject_xml(tags: Any, indent: str = "    ") -> str:
    """Trả về các dòng <dc:subject> cho OPF."""
    lines = [f"{indent}<dc:subject>{html.escape(tag)}</dc:subject>" for tag in normalize_tags(tags)]
    return ("\n".join(lines) + "\n") if lines else ""
