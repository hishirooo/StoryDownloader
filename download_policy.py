# -*- coding: utf-8 -*-
"""Shared chapter download policy.

This module intentionally keeps the retry loop at the batch level:
pass 1 downloads every selected chapter, pass 2 and pass 3 only retry
chapters that failed in earlier passes. Failed chapters are not written as
normal HTML cache files.
"""

from __future__ import annotations

import html
import inspect
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from download_logger import chapter_log_line

CACHE_STATUS = {"CACHE", "FILE"}
DEFAULT_MAX_PASSES = 3

FAILED_TEXT_MARKERS = (
    "khong tai duoc noi dung",
    "khong co noi dung",
    "khong tim thay noi dung",
    "khong tải được nội dung",
    "không tải được nội dung",
    "không có nội dung",
    "không tìm thấy nội dung",
    "chapter error",
    "chuong loi",
    "chương lỗi",
    "err_content",
    "(err)",
)


def _safe_print(message: str = "") -> None:
    try:
        sys.stdout.buffer.write((message + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()
    except Exception:
        print(message, flush=True)


def _clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def _status_to_int(status: Any) -> Optional[int]:
    if isinstance(status, bool) or status is None:
        return None
    if isinstance(status, int):
        return status
    if isinstance(status, str):
        match = re.search(r"\d{3}", status)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return None
    return None


def is_success_status(status: Any) -> bool:
    if isinstance(status, str) and status.strip().upper() in CACHE_STATUS:
        return True
    return _status_to_int(status) == 200


def _chapter_url(chapter: Dict[str, Any]) -> str:
    return str(chapter.get("url") or chapter.get("link") or chapter.get("href") or "").strip()


def _safe_filename(name: str, max_len: int = 120) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", " - ", name or "chapter")
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return (name[:max_len] or "chapter")


def _chapter_html_path(book_dir: str | Path, idx: int, title: str) -> Path:
    return Path(book_dir) / f"{idx:04d} - {_safe_filename(title)}.html"


def find_cached_chapter_path(book_dir: str | Path, idx: int) -> Optional[Path]:
    root = Path(book_dir)
    patterns = (f"{idx:04d} - *.html", f"{idx:04d}.html", f"chapter_{idx:04d}.html")
    for directory in (root, root / "html"):
        if not directory.is_dir():
            continue
        for pattern in patterns:
            matches = sorted(directory.glob(pattern))
            if matches:
                return matches[0]
    return None


def _content_text(data: Dict[str, Any]) -> str:
    text = str(data.get("text") or "").strip()
    if text:
        return text
    return BeautifulSoup(str(data.get("content_html") or ""), "html.parser").get_text("\n", strip=True)


def _looks_like_failed_content(text: str) -> bool:
    cleaned = _clean_spaces(text).lower()
    if not cleaned:
        return True
    return any(marker in cleaned for marker in FAILED_TEXT_MARKERS)


def _content_html_from_text(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(f"<p>{html.escape(line)}</p>" for line in lines)


def read_cached_chapter(html_path: str | Path) -> Dict[str, Any]:
    path = Path(html_path)
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    title = (
        (soup.find("h1") and soup.find("h1").get_text(" ", strip=True))
        or (soup.find("title") and soup.find("title").get_text(" ", strip=True))
        or path.stem
    )
    node = soup.select_one("article.chapter") or soup.select_one("article") or soup.body or soup
    node = BeautifulSoup(str(node), "html.parser")
    for bad in node.select(".source"):
        bad.decompose()
    for h1 in node.find_all("h1"):
        h1.decompose()
    content_html = "\n".join(str(child) for child in node.contents).strip()
    text = BeautifulSoup(content_html, "html.parser").get_text("\n", strip=True)
    return {
        "title": title,
        "content_html": content_html,
        "text": text,
        "url": str(path),
        "status_code": "CACHE",
        "html_path": str(path),
        "ok": True,
    }


def normalize_chapter_data(
    data: Any,
    *,
    fallback_title: str,
    url: str,
    default_status: Any = 200,
) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return make_error_chapter(
            fallback_title=fallback_title,
            url=url,
            status="ERR",
            error=f"fetch returned {type(data).__name__}, expected dict",
        )

    result = dict(data)
    result.setdefault("title", fallback_title or "Chapter")
    result.setdefault("url", url)

    if not result.get("content_html") and result.get("text"):
        result["content_html"] = _content_html_from_text(str(result.get("text") or ""))
    if not result.get("text") and result.get("content_html"):
        result["text"] = BeautifulSoup(str(result.get("content_html") or ""), "html.parser").get_text("\n", strip=True)

    raw_status = result.get("status_code", default_status)
    if isinstance(raw_status, str):
        upper = raw_status.strip().upper()
        code = _status_to_int(raw_status)
        if upper in CACHE_STATUS:
            status = upper
        elif code is not None:
            status = code
        else:
            status = upper or "ERR"
    else:
        status = raw_status

    result["status_code"] = status
    existing_ok = result.get("ok")
    content_failed = _looks_like_failed_content(_content_text(result))
    ok = existing_ok is not False and is_success_status(status) and not content_failed
    result["ok"] = bool(ok)

    if not ok:
        if is_success_status(status) and content_failed:
            result.setdefault("http_status", status)
            result["status_code"] = "ERR_CONTENT"
            result.setdefault("error", "HTTP 200 but chapter content is empty or invalid")
        else:
            result.setdefault("error", f"HTTP/status not successful: {status}")
    else:
        result.setdefault("error", "")

    return result


def make_error_chapter(
    *,
    fallback_title: str,
    url: str,
    status: Any = "ERR",
    error: str = "",
    attempts: int = 1,
) -> Dict[str, Any]:
    status = status if status not in (None, "") else "ERR"
    code = _status_to_int(status)
    if code is not None:
        status = code
    return {
        "title": fallback_title or "Chapter error",
        "content_html": "",
        "text": "",
        "url": url,
        "status_code": status,
        "ok": False,
        "error": error or f"Download failed: {status}",
        "attempts": attempts,
    }


def _status_from_exception(exc: Exception) -> Any:
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None):
        return getattr(response, "status_code")
    return "ERR"


def _call_fetch(
    fetch_fn: Callable[..., Any],
    url: str,
    chapter: Dict[str, Any],
    *,
    book_title: str,
    book_url: str = "",
) -> Any:
    title = str(chapter.get("title") or "").strip()
    kwargs: Dict[str, Any] = {}
    try:
        signature = inspect.signature(fetch_fn)
        params = signature.parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

        def accepts(name: str) -> bool:
            return accepts_kwargs or name in params

        if accepts("retries"):
            kwargs["retries"] = 1
        if accepts("fallback_title"):
            kwargs["fallback_title"] = title
        if accepts("book_title"):
            kwargs["book_title"] = book_title
        if accepts("story_url"):
            kwargs["story_url"] = book_url or ""
        if accepts("referer") and book_url:
            kwargs["referer"] = book_url
    except (TypeError, ValueError):
        kwargs = {}

    return fetch_fn(url, **kwargs)


def resolve_fetch_fn(module: Any) -> Optional[Callable[..., Any]]:
    for name in ("fetch_chapter_content", "get_chapter", "fetch_chapter", "extract_chapter_content"):
        fn = getattr(module, name, None)
        if callable(fn):
            return fn

    fetch_html = getattr(module, "_fetch_html", None)
    get_content = getattr(module, "_get_content_chapter", None)
    if callable(fetch_html) and callable(get_content):
        def fetch_from_html(url: str, **_kwargs: Any) -> Dict[str, Any]:
            soup = fetch_html(url)
            content_html = get_content(soup) if soup else ""
            title = _kwargs.get("fallback_title") or "Chapter"
            return {
                "title": title,
                "content_html": content_html,
                "text": BeautifulSoup(content_html or "", "html.parser").get_text("\n", strip=True),
                "url": url,
                "status_code": 200 if content_html else "ERR_CONTENT",
            }
        return fetch_from_html

    http_client = getattr(module, "HTTP", None)
    clean_chapter_html = getattr(module, "clean_chapter_html", None)
    if http_client is not None and callable(getattr(http_client, "get_html", None)) and callable(clean_chapter_html):
        maybe_unlock = getattr(module, "maybe_unlock_protected", None)
        def fetch_wordpress_html(url: str, **_kwargs: Any) -> Dict[str, Any]:
            soup = http_client.get_html(url)
            if callable(maybe_unlock):
                soup = maybe_unlock(soup, url)
            content_html = clean_chapter_html(soup) or ""
            title = _kwargs.get("fallback_title") or "Chapter"
            return {
                "title": title,
                "content_html": content_html,
                "text": BeautifulSoup(content_html or "", "html.parser").get_text("\n", strip=True),
                "url": url,
                "status_code": 200 if content_html else "ERR_CONTENT",
            }
        return fetch_wordpress_html

    return None


def _chapter_html_doc(title: str, content_html: str, source_url: str = "") -> str:
    source = ""
    if source_url:
        escaped = html.escape(source_url, quote=True)
        source = f'<p class="source"><a href="{escaped}">{html.escape(source_url)}</a></p>'
    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <article class="chapter">
{content_html}
  </article>
  {source}
</body>
</html>
"""


def _write_chapter_html(data: Dict[str, Any], book_dir: str | Path, idx: int, source_url: str) -> Path:
    title = str(data.get("title") or f"Chapter {idx}")
    path = _chapter_html_path(book_dir, idx, title)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_chapter_html_doc(title, str(data.get("content_html") or ""), source_url), encoding="utf-8")
    data["html_path"] = str(path)
    return path


def _failure_record(idx: int, chapter: Dict[str, Any], data: Dict[str, Any], pass_no: int) -> Dict[str, Any]:
    return {
        "index": idx,
        "title": data.get("title") or chapter.get("title") or f"Chapter {idx}",
        "url": data.get("url") or _chapter_url(chapter),
        "status_code": data.get("status_code", "ERR"),
        "error": data.get("error", ""),
        "last_pass": pass_no,
    }


def _write_failed_report(book_dir: str | Path, failures: List[Dict[str, Any]]) -> Optional[Path]:
    path = Path(book_dir) / "failed_chapters.json"
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "failed_count": len(failures),
        "failures": failures,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def normalize_range(total: int, start: int = 1, end: Optional[int] = None) -> Tuple[int, int]:
    if total <= 0:
        return 1, 0
    start = max(1, int(start or 1))
    end = total if end is None else int(end)
    end = min(total, max(start, end))
    return start, end


def download_chapters_with_retries(
    *,
    module: Any,
    book_title: str,
    chapters: List[Dict[str, Any]],
    out_dir: str | Path,
    fetch_fn: Callable[..., Any],
    start: int = 1,
    end: Optional[int] = None,
    book_url: str = "",
    max_passes: int = DEFAULT_MAX_PASSES,
) -> Dict[str, Any]:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    start, end = normalize_range(len(chapters), start, end)
    selected = [(idx, chapters[idx - 1]) for idx in range(start, end + 1)]
    completed: Dict[int, Dict[str, Any]] = {}
    failures_by_index: Dict[int, Dict[str, Any]] = {}
    pending = selected

    if not selected:
        return {"items": [], "chapters": [], "chapters_data": [], "failures": []}

    for pass_no in range(1, max_passes + 1):
        if not pending:
            break

        selected_total = len(pending)
        label = "initial" if pass_no == 1 else f"retry {pass_no - 1}"
        _safe_print(f"\n[Download] Pass {pass_no}/{max_passes} ({label}): {selected_total} chapter(s)")
        next_pending: List[Tuple[int, Dict[str, Any]]] = []

        for done, (idx, chapter) in enumerate(pending, 1):
            title = str(chapter.get("title") or f"Chapter {idx}")
            url = _chapter_url(chapter)

            if pass_no == 1:
                cached_path = find_cached_chapter_path(out_dir, idx)
                if cached_path:
                    cached = normalize_chapter_data(read_cached_chapter(cached_path), fallback_title=title, url=str(cached_path))
                    if cached.get("ok"):
                        completed[idx] = cached
                        _safe_print(chapter_log_line(done, selected_total, "CACHE", idx, len(chapters), cached.get("title") or title))
                        continue

            if not url:
                data = make_error_chapter(fallback_title=title, url="", status="ERR", error="Missing chapter URL", attempts=pass_no)
            else:
                try:
                    raw = _call_fetch(fetch_fn, url, chapter, book_title=book_title, book_url=book_url)
                    data = normalize_chapter_data(raw, fallback_title=title, url=url)
                    data["attempts"] = pass_no
                except Exception as exc:
                    data = make_error_chapter(
                        fallback_title=title,
                        url=url,
                        status=_status_from_exception(exc),
                        error=str(exc),
                        attempts=pass_no,
                    )

            if data.get("ok"):
                source_url = url or str(data.get("url") or "")
                _write_chapter_html(data, out_dir, idx, source_url)
                completed[idx] = data
                failures_by_index.pop(idx, None)
                _safe_print(chapter_log_line(done, selected_total, data.get("status_code", 200), idx, len(chapters), data.get("title") or title))
            else:
                failures_by_index[idx] = _failure_record(idx, chapter, data, pass_no)
                next_pending.append((idx, chapter))
                log_title = data.get("title") or title
                error = str(data.get("error") or "").strip()
                if error:
                    log_title = f"{log_title} ({error})"
                _safe_print(chapter_log_line(done, selected_total, data.get("status_code", "ERR"), idx, len(chapters), log_title))

            sleep_s = getattr(module, "SLEEP_BETWEEN_CHAPS", 0) or 0
            try:
                sleep_s = min(float(sleep_s), 1.0)
            except (TypeError, ValueError):
                sleep_s = 0
            if sleep_s > 0:
                time.sleep(sleep_s)

        pending = next_pending

    failures = [failures_by_index[idx] for idx, _chapter in selected if idx in failures_by_index]
    report_path = _write_failed_report(out_dir, failures)

    if failures:
        _safe_print(f"\n[Warning] {len(failures)} chapter(s) still failed after {max_passes} pass(es).")
        for failure in failures[:20]:
            _safe_print(
                f"  - #{failure['index']:04d}: {failure.get('status_code', 'ERR')} | "
                f"{failure.get('title', '')} | {failure.get('url', '')}"
            )
        if len(failures) > 20:
            _safe_print(f"  ... and {len(failures) - 20} more")
        _safe_print(f"[Warning] Failed chapter report: {report_path}")
    else:
        _safe_print(f"\n[Download] All selected chapters completed after retry policy.")

    ordered_success = [(idx, chapter, completed[idx]) for idx, chapter in selected if idx in completed]
    epub_chapters: List[Dict[str, Any]] = []
    for idx, chapter, data in ordered_success:
        item = dict(chapter)
        item["url"] = _chapter_url(chapter) or str(data.get("url") or data.get("html_path") or "")
        item["title"] = item.get("title") or data.get("title") or f"Chapter {idx}"
        epub_chapters.append(item)
    return {
        "items": [item[2] for item in ordered_success],
        "chapters": epub_chapters,
        "chapters_data": [item[2] for item in ordered_success],
        "html_paths": [str(item[2].get("html_path")) for item in ordered_success if item[2].get("html_path")],
        "failures": failures,
        "failed_report": str(report_path),
        "selected_count": len(selected),
        "success_count": len(ordered_success),
        "start": start,
        "end": end,
    }


def save_all_chapters_to_html_with_retries(
    module: Any,
    book_title: str,
    chapters: List[Dict[str, Any]],
    out_dir: str | Path,
    start: int = 1,
    end: Optional[int] = None,
    *,
    fetch_fn: Optional[Callable[..., Any]] = None,
    book_url: str = "",
    max_passes: int = DEFAULT_MAX_PASSES,
) -> List[str]:
    fetch = fetch_fn or resolve_fetch_fn(module)
    if fetch is None:
        raise AttributeError("Adapter does not expose a supported chapter fetch function")
    result = download_chapters_with_retries(
        module=module,
        book_title=book_title,
        chapters=chapters,
        out_dir=out_dir,
        fetch_fn=fetch,
        start=start,
        end=end,
        book_url=book_url,
        max_passes=max_passes,
    )
    return result["html_paths"]


def _namespace_proxy(namespace: Dict[str, Any]) -> Any:
    class NamespaceProxy(SimpleNamespace):
        def __getattr__(self, name: str) -> Any:
            try:
                return namespace[name]
            except KeyError as exc:
                raise AttributeError(name) from exc

    return NamespaceProxy(**namespace)


def _write_txt_exports(book_dir: str | Path, items: List[Dict[str, Any]]) -> None:
    txt_dir = Path(book_dir) / "txt"
    txt_dir.mkdir(parents=True, exist_ok=True)
    for idx, data in enumerate(items, 1):
        title = str(data.get("title") or f"Chapter {idx}")
        text = _content_text(data)
        path = txt_dir / f"{idx:04d}.txt"
        path.write_text(f"{title}\n\n{text}\n", encoding="utf-8")


def install_adapter_policy(namespace: Dict[str, Any]) -> None:
    """Install the shared retry policy into a legacy adapter namespace.

    This is intentionally conservative: it only replaces well-known batch
    functions and keeps the adapter-specific single-chapter parser intact.
    """
    if namespace.get("__policy_installed__"):
        return

    module = _namespace_proxy(namespace)
    originals = namespace.setdefault("__policy_originals__", {})
    fetch = resolve_fetch_fn(module)
    if fetch is None:
        namespace["__policy_installed__"] = True
        return

    if callable(namespace.get("save_all_chapters_to_html")):
        originals.setdefault("save_all_chapters_to_html", namespace["save_all_chapters_to_html"])

        def policy_save_all_chapters_to_html(
            book_title: str,
            chapters: List[Dict[str, Any]],
            out_dir: str | Path,
            start: int = 1,
            end: Optional[int] = None,
            *args: Any,
            **kwargs: Any,
        ) -> List[str]:
            result = download_chapters_with_retries(
                module=module,
                book_title=book_title,
                chapters=chapters,
                out_dir=out_dir,
                fetch_fn=fetch,
                start=start,
                end=end,
                book_url=str(kwargs.get("book_url") or ""),
            )
            namespace["__policy_last_download_result__"] = result
            return result["html_paths"]

        namespace["save_all_chapters_to_html"] = policy_save_all_chapters_to_html

    download_chapters_fn = namespace.get("download_chapters")
    can_wrap_standard_download = False
    can_wrap_mode_download = False
    if callable(download_chapters_fn):
        try:
            param_names = list(inspect.signature(download_chapters_fn).parameters)
            can_wrap_standard_download = len(param_names) >= 3 and param_names[:3] == ["book_info", "chapters", "book_dir"]
            can_wrap_mode_download = len(param_names) >= 4 and param_names[:4] == ["book_title", "chapters", "out_dir", "mode"]
        except (TypeError, ValueError):
            can_wrap_standard_download = False
            can_wrap_mode_download = False

    if can_wrap_standard_download:
        originals.setdefault("download_chapters", namespace["download_chapters"])

        def policy_download_chapters(
            book_info: Any,
            chapters: List[Dict[str, Any]],
            book_dir: str | Path,
            *args: Any,
            start: int = 1,
            end: Optional[int] = None,
            force: bool = False,
            **kwargs: Any,
        ) -> List[Dict[str, Any]]:
            if isinstance(book_info, dict):
                book_title = str(book_info.get("title") or "Book")
                book_url = str(book_info.get("url") or "")
            else:
                book_title = str(book_info or "Book")
                book_url = ""
            result = download_chapters_with_retries(
                module=module,
                book_title=book_title,
                chapters=chapters,
                out_dir=book_dir,
                fetch_fn=fetch,
                start=start,
                end=end,
                book_url=book_url,
            )
            namespace["__policy_last_download_result__"] = result
            return result["chapters_data"]

        namespace["download_chapters"] = policy_download_chapters

    elif can_wrap_mode_download:
        originals.setdefault("download_chapters", namespace["download_chapters"])

        def policy_download_chapters_by_mode(
            book_title: str,
            chapters: List[Dict[str, Any]],
            out_dir: str | Path,
            mode: str,
            start: int = 1,
            end: Optional[int] = None,
            *args: Any,
            force: bool = False,
            **kwargs: Any,
        ) -> Tuple[List[str], List[str], List[Dict[str, Any]]]:
            result = download_chapters_with_retries(
                module=module,
                book_title=str(book_title or "Book"),
                chapters=chapters,
                out_dir=out_dir,
                fetch_fn=fetch,
                start=start,
                end=end,
            )
            namespace["__policy_last_download_result__"] = result
            html_paths = result["html_paths"] if str(mode) in {"1", "3", "4", "6"} else []
            txt_paths: List[str] = []
            if str(mode) in {"2", "3", "5", "6"}:
                _write_txt_exports(out_dir, result["chapters_data"])
                txt_paths = [str(path) for path in sorted((Path(out_dir) / "txt").glob("*.txt"))]
            return html_paths, txt_paths, result["chapters_data"]

        namespace["download_chapters"] = policy_download_chapters_by_mode

    if callable(namespace.get("download_all")):
        originals.setdefault("download_all", namespace["download_all"])

        def policy_download_all(
            chapters: List[Dict[str, Any]],
            out_dir: str | Path,
            logger: Any = None,
            save_html_flag: bool = True,
            save_txt_flag: bool = False,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            result = download_chapters_with_retries(
                module=module,
                book_title=Path(out_dir).name or "Book",
                chapters=chapters,
                out_dir=out_dir,
                fetch_fn=fetch,
                start=1,
                end=None,
            )
            namespace["__policy_last_download_result__"] = result
            if save_txt_flag:
                _write_txt_exports(out_dir, result["chapters_data"])
            if logger and hasattr(logger, "log"):
                logger.log(
                    f"Policy download complete: {result['success_count']}/{result['selected_count']} ok, "
                    f"{len(result['failures'])} failed"
                )

        namespace["download_all"] = policy_download_all

    if callable(namespace.get("download_manager")):
        originals.setdefault("download_manager", namespace["download_manager"])

        def policy_download_manager(book_dir: str | Path, selected_chaps: List[Dict[str, Any]], mode: str, *args: Any, **kwargs: Any) -> None:
            result = download_chapters_with_retries(
                module=module,
                book_title=Path(book_dir).name or "Book",
                chapters=selected_chaps,
                out_dir=book_dir,
                fetch_fn=fetch,
                start=1,
                end=None,
            )
            namespace["__policy_last_download_result__"] = result
            for chapter, data in zip(selected_chaps, result["chapters_data"]):
                chapter["content"] = data.get("content_html", "")
            if str(mode) in {"2", "3"}:
                _write_txt_exports(book_dir, result["chapters_data"])

        namespace["download_manager"] = policy_download_manager

    if callable(namespace.get("download_htmls")):
        originals.setdefault("download_htmls", namespace["download_htmls"])

        def policy_download_htmls(
            index_url: str,
            out_base: str | Path = "output",
            start: int = 1,
            end: Optional[int] = None,
            resume: bool = False,
            *args: Any,
            **kwargs: Any,
        ) -> Tuple[str, Dict[str, Any], List[str]]:
            http = namespace.get("HTTP")
            get_info = namespace.get("get_info_from_index")
            get_chapters = namespace.get("get_chapter_list")
            slugify = namespace.get("_slugify_vi") or _safe_filename
            if not http or not callable(getattr(http, "get_html", None)) or not callable(get_info) or not callable(get_chapters):
                return originals["download_htmls"](index_url, out_base=out_base, start=start, end=end, resume=resume)
            soup = http.get_html(index_url)
            info = get_info(soup)
            book_title = str(info.get("title") or "Book")
            html_dir = Path(out_base) / str(slugify(book_title))
            raw_chapters = get_chapters(soup)
            chapters = [{"title": title, "url": url} for title, url in raw_chapters]
            result = download_chapters_with_retries(
                module=module,
                book_title=book_title,
                chapters=chapters,
                out_dir=html_dir,
                fetch_fn=fetch,
                start=start,
                end=end,
                book_url=index_url,
            )
            namespace["__policy_last_download_result__"] = result
            return str(html_dir), info, result["html_paths"]

        namespace["download_htmls"] = policy_download_htmls

    build_epub_fn = namespace.get("build_epub")
    can_wrap_build_epub = False
    if callable(build_epub_fn):
        try:
            param_names = list(inspect.signature(build_epub_fn).parameters)
            can_wrap_build_epub = (
                len(param_names) >= 3
                and param_names[0] in {"book_info", "info", "data"}
                and "chapter" in param_names[1]
                and param_names[2] in {"book_dir", "out_dir", "output_dir"}
            )
        except (TypeError, ValueError):
            can_wrap_build_epub = False

    if can_wrap_build_epub:
        originals.setdefault("build_epub", namespace["build_epub"])

        def policy_build_epub(
            book_info: Any,
            chapters: List[Dict[str, Any]],
            book_dir: str | Path,
            *args: Any,
            start: int = 1,
            end: Optional[int] = None,
            cover_bytes: Optional[bytes] = None,
            cover_ext: Optional[str] = None,
            **kwargs: Any,
        ) -> Path:
            import epub_builder

            if isinstance(book_info, dict):
                book_title = str(book_info.get("title") or "Book")
                author = str(book_info.get("author") or "Unknown")
                book_url = str(book_info.get("url") or "")
                tags = book_info.get("category") or book_info.get("genre") or book_info.get("genres") or book_info.get("tags")
            else:
                book_title = str(book_info or "Book")
                author = "Unknown"
                book_url = ""
                tags = None

            start_norm, end_norm = normalize_range(len(chapters), start, end)
            result = namespace.get("__policy_last_download_result__")
            result_matches_range = (
                isinstance(result, dict)
                and int(result.get("start") or 0) == start_norm
                and int(result.get("end") or 0) == end_norm
            )
            if not result_matches_range:
                result = download_chapters_with_retries(
                    module=module,
                    book_title=book_title,
                    chapters=chapters,
                    out_dir=book_dir,
                    fetch_fn=fetch,
                    start=start,
                    end=end,
                    book_url=book_url,
                )
                namespace["__policy_last_download_result__"] = result

            suffix = "" if start_norm == 1 and end_norm == len(chapters) else f"_{start_norm:04d}-{end_norm:04d}"
            epub_path = Path(book_dir) / f"{_safe_filename(book_title)}{suffix}.epub"
            epub_builder.create_epub(
                book_url=book_url,
                book_title=book_title,
                author=author,
                chapters=result.get("chapters") or [],
                fetch_fn=None,
                cover_bytes=cover_bytes,
                cover_ext=cover_ext or ".jpg",
                out_epub_path=str(epub_path),
                html_cache_dir=None,
                chapters_data=result.get("chapters_data") or [],
                tags=tags,
                book_info=book_info if isinstance(book_info, dict) else None,
            )
            _safe_print(f"[Epub] Created: {epub_path}")
            return epub_path

        namespace["build_epub"] = policy_build_epub

    namespace["__policy_installed__"] = True
