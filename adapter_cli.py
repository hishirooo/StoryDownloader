# -*- coding: utf-8 -*-
"""Shared non-interactive CLI for story adapters.

This module is intentionally small and conservative. It is used when an
adapter is executed directly with command-line arguments, for example:

    python some_adapter.py URL -y --start 1 --end 10

Adapters keep their old interactive menu when run without arguments.
"""

from __future__ import annotations

import argparse
import html
import inspect
import io
import os
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup


def _safe_print(message: str = "") -> None:
    try:
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    except Exception:
        print(message, flush=True)


def _safe_filename(name: str, max_length: int = 150) -> str:
    safe = re.sub(r'[\\/:*?"<>|]+', " - ", str(name or "book"))
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    return safe[:max_length].strip() or "book"


def _call_variants(fn: Any, variants: List[Tuple[Any, ...]]) -> Any:
    last_error: Optional[Exception] = None
    for args in variants:
        try:
            return fn(*args)
        except TypeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    raise TypeError("No call variants supplied")


def _first_param_name(fn: Any) -> str:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return ""
    if not params:
        return ""
    return params[0].name.lower()


def _call_page_parser(fn: Any, soup: Any, url: str) -> Any:
    first = _first_param_name(fn)
    if "url" in first or first in {"source", "book", "story"}:
        variants = [(url,), (soup, url), (soup,)]
    else:
        variants = [(soup, url), (soup,), (url,)]
    return _call_variants(fn, variants)


def _normalize_chapters(raw: Any) -> List[Dict[str, Any]]:
    if not raw:
        return []
    result: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            title = item.get("title") or item.get("name") or item.get("chapter_name") or item.get("text") or ""
            url = item.get("url") or item.get("link") or item.get("href") or ""
            normalized = dict(item)
            if title and not normalized.get("title"):
                normalized["title"] = title
            if url and not normalized.get("url"):
                normalized["url"] = url
            result.append(normalized)
            continue
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            title, url = item[0], item[1]
            result.append({"title": str(title or ""), "url": str(url or "")})
    return result


def _book_info_from_data(data: Dict[str, Any], url: str) -> Dict[str, Any]:
    info = dict(data or {})
    nested = info.get("info")
    if isinstance(nested, dict):
        base = dict(nested)
        for key, value in info.items():
            if key not in {"info", "chapters"} and key not in base:
                base[key] = value
        info = base

    title = info.get("title") or info.get("Title") or info.get("name") or "Book"
    author = info.get("author") or info.get("Author") or info.get("creator") or "Unknown"
    category = info.get("category") or info.get("genre") or info.get("Genre") or info.get("genres") or info.get("tags") or ""
    intro = info.get("intro") or info.get("description") or info.get("Description") or ""
    cover_url = info.get("cover_url") or info.get("cover") or info.get("CoverURL") or ""
    return {
        **info,
        "title": title,
        "author": author,
        "category": category,
        "intro": intro,
        "cover_url": cover_url,
        "url": info.get("url") or info.get("book_url") or url,
    }


def _context_from_tuple(value: Any) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]], Path, Optional[bytes], Optional[str], str]]:
    if not isinstance(value, tuple):
        return None
    if len(value) >= 5 and isinstance(value[0], dict) and isinstance(value[1], list):
        book_info = _book_info_from_data(value[0], str(value[0].get("url") or ""))
        chapters = _normalize_chapters(value[1])
        return book_info, chapters, Path(value[2]), value[3], value[4], str(book_info.get("url") or "")
    if len(value) >= 6 and isinstance(value[3], dict) and isinstance(value[4], list):
        # truyenmo-style: source, story_url, is_local, info, chapters, out_dir
        story_url = str(value[1] or "")
        book_info = _book_info_from_data(value[3], story_url)
        chapters = _normalize_chapters(value[4])
        return book_info, chapters, Path(value[5]), None, None, story_url
    return None


def _module_output_base(module: Any) -> Path:
    for name in ("OUTPUT_BASE", "OUTPUT_ROOT", "OUTPUT_DIR"):
        value = getattr(module, name, None)
        if value:
            return Path(value)
    return Path("output")


def _module_slug(module: Any, title: str) -> str:
    for name in ("slugify_vi", "_slugify_vi", "_slug_folder", "slugify", "_safe_filename", "safe_filename", "slugify_filename"):
        fn = getattr(module, name, None)
        if callable(fn):
            try:
                return str(fn(title)) or _safe_filename(title)
            except Exception:
                continue
    return _safe_filename(title)


def _fetch_soup(module: Any, url: str):
    fetch_html = getattr(module, "_fetch_html", None) or getattr(module, "fetch_html", None)
    if callable(fetch_html):
        return _call_variants(fetch_html, [(url,), (url, 3)])
    http = getattr(module, "HTTP", None)
    if http is not None and callable(getattr(http, "get_html", None)):
        return http.get_html(url)
    raise AttributeError("Adapter does not expose _fetch_html/fetch_html/HTTP.get_html")


def _context_generic(module: Any, url: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Path, Optional[bytes], Optional[str], str]:
    if callable(getattr(module, "getText", None)):
        data = module.getText(url)
        if not isinstance(data, dict):
            raise RuntimeError("getText did not return a dict")
        book_info = _book_info_from_data(data, url)
        chapters = _normalize_chapters(data.get("chapters") or [])
    elif callable(getattr(module, "getinfo", None)):
        data = module.getinfo(url)
        if not isinstance(data, dict):
            raise RuntimeError("getinfo did not return a dict")
        book_info = _book_info_from_data(data, url)
        chapters = _normalize_chapters(data.get("chapters") or [])
    else:
        soup = _fetch_soup(module, url)
        info_fn = (
            getattr(module, "_get_book_info", None)
            or getattr(module, "get_book_info", None)
            or getattr(module, "get_info_from_index", None)
            or getattr(module, "parse_book_info", None)
        )
        chapters_fn = (
            getattr(module, "_get_list_chapters", None)
            or getattr(module, "_get_list_chapter", None)
            or getattr(module, "get_chapter_list", None)
            or getattr(module, "get_chapters", None)
        )
        if not callable(info_fn) or not callable(chapters_fn):
            raise AttributeError("Adapter does not expose a supported book/chapter-list API")
        info = _call_page_parser(info_fn, soup, url)
        chapters_raw = _call_page_parser(chapters_fn, soup, url)
        book_info = _book_info_from_data(info if isinstance(info, dict) else {}, url)
        chapters = _normalize_chapters(chapters_raw)

    out_dir = _module_output_base(module) / _module_slug(module, str(book_info.get("title") or "Book"))
    out_dir.mkdir(parents=True, exist_ok=True)
    return book_info, chapters, out_dir, None, None, str(book_info.get("url") or url)


def _load_context(module: Any, url: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Path, Optional[bytes], Optional[str], str]:
    for name in ("_load_book_context", "_prepare_book_context"):
        fn = getattr(module, name, None)
        if callable(fn):
            try:
                context = _context_from_tuple(fn(url))
                if context:
                    return context
            except TypeError:
                pass
    return _context_generic(module, url)


def _fetch_cover(module: Any, book_info: Dict[str, Any], book_url: str) -> Tuple[Optional[bytes], Optional[str]]:
    for name in ("fetch_cover_from_book_page", "_fetch_cover_from_book_page"):
        fn = getattr(module, name, None)
        if callable(fn):
            try:
                value = fn(book_url)
                if isinstance(value, tuple) and len(value) >= 2 and value[0]:
                    return value[0], value[1] or ".jpg"
            except Exception:
                pass
    return None, None


def _resolve_fetch_fn(module: Any, password: str = ""):
    import download_policy

    direct = getattr(module, "fetch_chapter_content", None)
    if password and callable(direct):
        try:
            params = inspect.signature(direct).parameters
            if "password" in params:
                def fetch_with_password(url: str, **kwargs: Any) -> Dict[str, Any]:
                    return direct(url, password=password)
                return fetch_with_password
        except (TypeError, ValueError):
            pass

    content_fn = getattr(module, "_get_content_chapter", None)
    if callable(content_fn) and ("url" in _first_param_name(content_fn) or _first_param_name(content_fn) in {"link", "href"}):
        def fetch_from_content_url(url: str, **kwargs: Any) -> Dict[str, Any]:
            value = content_fn(url)
            if isinstance(value, dict):
                return value
            content_html = str(value or "")
            fallback_title = str(kwargs.get("fallback_title") or "Chapter")
            text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
            return {
                "title": fallback_title,
                "content_html": content_html,
                "text": text,
                "url": url,
                "status_code": 200 if text else "ERR_CONTENT",
            }
        return fetch_from_content_url

    fetch = download_policy.resolve_fetch_fn(module)
    return fetch


def run_adapter_cli(module: Any, default_url: str = "", argv: Optional[List[str]] = None, description: str = "") -> Dict[str, Any]:
    parser = argparse.ArgumentParser(description=description or f"Run {getattr(module, '__name__', 'adapter')} non-interactively")
    parser.add_argument("url", nargs="?", default=default_url, help="Book URL")
    parser.add_argument("--start", type=int, default=1, help="Start chapter index")
    parser.add_argument("--end", type=int, default=None, help="End chapter index")
    parser.add_argument("--no-epub", action="store_true", help="Only download/cache HTML")
    parser.add_argument("--force", action="store_true", help="Accepted for compatibility")
    parser.add_argument("--password", default="", help="Chapter password for adapters that need it")
    parser.add_argument("-y", "--yes", action="store_true", help="Run non-interactively")
    args = parser.parse_args(argv)

    if not args.url:
        parser.error("URL is required")

    import download_policy
    import epub_builder

    book_info, chapters, out_dir, cover_bytes, cover_ext, book_url = _load_context(module, args.url)
    if not chapters:
        raise RuntimeError("No chapters found")

    fetch_fn = _resolve_fetch_fn(module, password=args.password)
    if fetch_fn is None:
        raise AttributeError("Adapter does not expose a supported chapter fetch function")

    title = str(book_info.get("title") or "Book")
    author = str(book_info.get("author") or "Unknown")
    _safe_print("\n-----------------Book info-----------------")
    _safe_print(f"Title   : {title}")
    _safe_print(f"Author  : {author}")
    if book_info.get("category") or book_info.get("genre") or book_info.get("genres"):
        _safe_print(f"Category: {book_info.get('category') or book_info.get('genre') or book_info.get('genres')}")
    _safe_print(f"Chapters: {len(chapters)}")
    _safe_print(f"Output  : {out_dir}")

    result = download_policy.download_chapters_with_retries(
        module=module,
        book_title=title,
        chapters=chapters,
        out_dir=out_dir,
        fetch_fn=fetch_fn,
        start=args.start,
        end=args.end,
        book_url=book_url or args.url,
    )

    epub_path = None
    if not args.no_epub:
        if not cover_bytes:
            cover_bytes, cover_ext = _fetch_cover(module, book_info, book_url or args.url)
        start = int(result.get("start") or 1)
        end = int(result.get("end") or len(chapters))
        suffix = "" if start == 1 and end == len(chapters) else f"_{start:04d}-{end:04d}"
        epub_path = Path(out_dir) / f"{_safe_filename(title)}{suffix}.epub"
        noise = io.StringIO()
        with redirect_stdout(noise):
            epub_builder.create_epub(
                book_url=book_url or args.url,
                book_title=title,
                author=author,
                chapters=result.get("chapters") or [],
                fetch_fn=None,
                html_cache_dir=None,
                chapters_data=result.get("chapters_data") or [],
                cover_bytes=cover_bytes,
                cover_ext=cover_ext or ".jpg",
                out_epub_path=str(epub_path),
                tags=book_info.get("category") or book_info.get("genre") or book_info.get("genres") or book_info.get("tags"),
                book_info=book_info,
            )
        _safe_print(f"[Epub] Created: {epub_path}")

    return {**result, "epub_path": str(epub_path) if epub_path else None, "out_dir": str(out_dir)}


def dispatch_or_menu(module: Any, menu_fn: Any, default_url: str = "") -> None:
    if len(sys.argv) > 1:
        run_adapter_cli(module, default_url=default_url)
    else:
        menu_fn()
