# -*- coding: utf-8 -*-
from urllib.parse import urljoin, urlparse
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from bs4 import BeautifulSoup, Comment
import requests, urllib.request
import subprocess, shutil, time, re, os, io, zipfile, html, datetime, unicodedata

try:
    from PIL import Image
    HAS_PILLOW = True
except Exception:
    HAS_PILLOW = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
TIMEOUT = 25
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
SLEEP_BETWEEN_CHAPS = 0.35
MAX_COVER_SIZE = (1600, 2400)
DEBUG_DIR = Path("debug")


def _text(el) -> str:
    try:
        return el.get_text(" ", strip=True) if el else ""
    except Exception:
        return ""


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug_folder(s: str) -> str:
    s = (s or "Truyen").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "Truyen"


def _slugify_vi(s: str) -> str:
    s = (s or "book").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-_")
    return (s or "book")[:100]


def _clean_chapter_title(raw_title: str, chapter_idx: int) -> str:
    cleaned_title = (raw_title or "").strip()
    cleaned_title = re.sub(r"\[[^\]]+\]", "", cleaned_title).strip()
    cleaned_title = re.sub(r"\s+", " ", cleaned_title).strip()
    m = re.search(r"(chương\s*[\d._-]+)", cleaned_title, re.IGNORECASE)
    if m:
        return m.group(1).strip().capitalize()
    return cleaned_title or f"Chương {chapter_idx}"


def _normalize_url(u: str) -> str:
    return (u or "").strip().rstrip("/")


def _chapter_sort_key(title: str, url: str, fallback_idx: int = 10**9):
    t = (title or "").strip()

    m = re.search(r"chương\s*(\d+)(?:\s*[-._]\s*(\d+))?", t, re.I)
    if m:
        main = int(m.group(1))
        sub = int(m.group(2)) if m.group(2) else 0
        return (main, sub, url)

    m = re.search(r"chapter\s*(\d+)(?:\s*[-._]\s*(\d+))?", t, re.I)
    if m:
        main = int(m.group(1))
        sub = int(m.group(2)) if m.group(2) else 0
        return (main, sub, url)

    m = re.search(r"chuong[-_ ]*(\d+)(?:[-_.](\d+))?", url, re.I)
    if m:
        main = int(m.group(1))
        sub = int(m.group(2)) if m.group(2) else 0
        return (main, sub, url)

    nums = re.findall(r"\d+", url or "")
    if nums:
        return (int(nums[-1]), 0, url)

    return (fallback_idx, 0, url)


def _is_probable_chapter_text(text: str) -> bool:
    t = html.unescape((text or "").strip()).lower()
    if not t:
        return False
    if re.search(r"\b(chương|chapter|phiên ngoại|extra)\b", t, re.I):
        return True
    return False


def _save_debug_html(name: str, content: str):
    try:
        ensure_dir(DEBUG_DIR)
        p = DEBUG_DIR / name
        p.write_text(content or "", encoding="utf-8", errors="ignore")
    except Exception:
        pass


def _try_requests(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    if r.status_code in RETRY_STATUS:
        r.raise_for_status()
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return r.text


def _try_urllib(url: str, referer: Optional[str] = None) -> str:
    req_headers = dict(HEADERS)
    if referer:
        req_headers["Referer"] = referer
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
    for enc in ("utf-8", "utf-8-sig", "cp1258"):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def _try_curl(url: str, referer: Optional[str] = None) -> str:
    curl_bin = shutil.which("curl") or shutil.which("curl.exe")
    if not curl_bin:
        raise RuntimeError("curl not found")

    cmd = [
        curl_bin,
        "-L",
        "--compressed",
        "-A", HEADERS["User-Agent"],
        "-H", f"Accept: {HEADERS['Accept']}",
        "-H", f"Accept-Language: {HEADERS['Accept-Language']}",
        url,
    ]
    if referer:
        cmd.extend(["-e", referer])

    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "curl failed")
    return p.stdout


def _looks_blocked(text: str) -> bool:
    low = (text or "").lower()
    bad_markers = [
        "just a moment",
        "attention required",
        "access denied",
        "forbidden",
        "wordpress.com",
    ]
    if len((text or "").strip()) < 200:
        return True
    if "<html" not in low and "<!doctype" not in low:
        return False
    if any(x in low for x in bad_markers) and "entry-content" not in low and "wp-block-table" not in low:
        return True
    return False


def _fetch_text(url: str, referer: Optional[str] = None, tries: int = 3, warm_home: bool = True) -> str:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # wordpress.com thường ổn hơn với curl trước
    prefer_curl_first = parsed.netloc.endswith("wordpress.com")

    last_err = None
    methods = []
    if prefer_curl_first:
        methods = [
            lambda: _try_curl(url, referer=referer or origin + "/"),
            lambda: _try_requests(_make_session(url, referer), url),
            lambda: _try_urllib(url, referer=referer or origin + "/"),
        ]
    else:
        methods = [
            lambda: _try_requests(_make_session(url, referer), url),
            lambda: _try_urllib(url, referer=referer or origin + "/"),
            lambda: _try_curl(url, referer=referer or origin + "/"),
        ]

    for fn in methods:
        for k in range(tries):
            try:
                text = fn()
                if _looks_blocked(text):
                    raise RuntimeError("blocked-or-empty-html")
                return text
            except Exception as e:
                last_err = e
                time.sleep(0.5 * (k + 1))

    raise last_err if last_err else RuntimeError(f"Cannot fetch: {url}")


def _make_session(url: str, referer: Optional[str] = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    session.headers.update({
        "Referer": referer or origin + "/",
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    })
    try:
        session.get(origin + "/", timeout=TIMEOUT)
    except Exception:
        pass
    return session


def _fetch_html(url: str, referer: Optional[str] = None) -> Tuple[str, BeautifulSoup]:
    text = _fetch_text(url, referer=referer)
    return text, BeautifulSoup(text, "html.parser")


def _find_cover_url(soup: BeautifulSoup, page_url: str, raw_html: str = "") -> str:
    candidates = []
    for sel in [
        "figure.aligncenter img",
        ".entry-content figure img",
        ".entry-content img",
        ".wp-block-post-content img",
        "article img",
        "img.wp-image",
    ]:
        candidates.extend(soup.select(sel))

    for img in candidates:
        srcset = img.get("srcset", "")
        srcset_first = ""
        if srcset:
            try:
                srcset_first = srcset.split(",")[0].strip().split(" ")[0].strip()
            except Exception:
                srcset_first = ""
        for key in ("data-orig-file", "data-large-file", "data-medium-file", "data-src", "src"):
            val = img.get(key)
            if val:
                return urljoin(page_url, val)
        if srcset_first:
            return urljoin(page_url, srcset_first)

    # regex fallback
    for pat in [
        r'data-orig-file="([^"]+)"',
        r'data-large-file="([^"]+)"',
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
        r'<meta[^>]+name="twitter:image"[^>]+content="([^"]+)"',
    ]:
        m = re.search(pat, raw_html or "", re.I)
        if m:
            return urljoin(page_url, html.unescape(m.group(1)))

    return ""


def _get_book_info(soup: BeautifulSoup, book_url: str, raw_html: str = "") -> dict:
    info = {
        "Title": "Unknown",
        "Author": "Unknown",
        "Genre": "N/A",
        "Status": "N/A",
        "CoverURL": "",
    }

    title = (
        _text(soup.select_one("h1.entry-title"))
        or _text(soup.select_one("h1.wp-block-post-title"))
        or _text(soup.select_one(".post-title h1"))
        or _text(soup.select_one("article h1"))
        or _text(soup.select_one("h1"))
    )
    if not title:
        m = re.search(r"<title>(.*?)</title>", raw_html or "", re.I | re.S)
        if m:
            title = html.unescape(re.sub(r"\s+", " ", BeautifulSoup(m.group(1), "html.parser").get_text(" ", strip=True)))

    info["Title"] = title or "Unknown"

    author = (
        _text(soup.select_one(".entry-author"))
        or _text(soup.select_one(".author"))
        or _text(soup.select_one("a[rel='author']"))
        or _text(soup.select_one(".byline a"))
    )
    info["Author"] = author or "Unknown"
    info["CoverURL"] = _find_cover_url(soup, book_url, raw_html=raw_html)
    return info


def _collect_links_from_raw_html(raw_html: str, book_url: str) -> List[Dict[str, str]]:
    chapters = []
    seen = set()
    idx = 0

    # anchor text dạng html
    pattern = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    for href, inner in pattern.findall(raw_html or ""):
        idx += 1
        text = BeautifulSoup(inner, "html.parser").get_text(" ", strip=True)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        full = urljoin(book_url, html.unescape(href).strip())
        if not full or not _is_probable_chapter_text(text):
            continue
        key = (_normalize_url(full), text.lower())
        if key in seen:
            continue
        seen.add(key)
        chapters.append({"title": text, "url": full, "_idx": idx})

    chapters.sort(key=lambda ch: (_chapter_sort_key(ch["title"], ch["url"], ch["_idx"]), ch["_idx"]))
    for ch in chapters:
        ch.pop("_idx", None)
    return chapters


def _detect_wp_table_toc(soup: BeautifulSoup, book_url: str) -> List[Dict[str, str]]:
    chapters = []
    seen = set()
    idx = 0
    tables = soup.select("figure.wp-block-table table")
    if not tables:
        return []

    for table in tables:
        for a in table.select("a[href]"):
            idx += 1
            title = _text(a)
            href = urljoin(book_url, a.get("href", "").strip())
            if not href or not _is_probable_chapter_text(title):
                continue
            key = (_normalize_url(href), title.lower())
            if key in seen:
                continue
            seen.add(key)
            chapters.append({"title": title.strip(), "url": href, "_idx": idx})

    chapters.sort(key=lambda ch: (_chapter_sort_key(ch["title"], ch["url"], ch["_idx"]), ch["_idx"]))
    for ch in chapters:
        ch.pop("_idx", None)
    return chapters


def _detect_wp_list_toc(soup: BeautifulSoup, book_url: str) -> List[Dict[str, str]]:
    chapters = []
    seen = set()
    idx = 0

    for container in [soup.select_one(".entry-content"), soup.select_one(".wp-block-post-content"), soup.select_one("article"), soup]:
        if not container:
            continue
        for a in container.select("ul a[href], ol a[href], p a[href], div a[href]"):
            idx += 1
            title = _text(a)
            href = urljoin(book_url, a.get("href", "").strip())
            if not href or not _is_probable_chapter_text(title):
                continue
            key = (_normalize_url(href), title.lower())
            if key in seen:
                continue
            seen.add(key)
            chapters.append({"title": title.strip(), "url": href, "_idx": idx})
        if chapters:
            break

    chapters.sort(key=lambda ch: (_chapter_sort_key(ch["title"], ch["url"], ch["_idx"]), ch["_idx"]))
    for ch in chapters:
        ch.pop("_idx", None)
    return chapters


def _detect_wp_manga_ajax(book_url: str) -> List[Dict[str, str]]:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("wpmanga-adault", "1")

    ajax_url = book_url.rstrip("/") + "/ajax/chapters/"
    parsed = urlparse(book_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": book_url,
        "Origin": origin,
    }

    chapters = []
    try:
        r = session.post(ajax_url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        html_text = (r.text or "").strip()
        if not html_text:
            return []
        soup = BeautifulSoup(html_text, "html.parser")
        idx = 0
        for a in soup.select("li.wp-manga-chapter a[href]"):
            idx += 1
            href = urljoin(book_url, a.get("href", "").strip())
            title = _text(a)
            if href and title:
                chapters.append({"title": title.strip(), "url": href, "_idx": idx})
        chapters.sort(key=lambda ch: (_chapter_sort_key(ch["title"], ch["url"], ch["_idx"]), ch["_idx"]))
        for ch in chapters:
            ch.pop("_idx", None)
        return chapters
    except Exception:
        return []


def _get_list_chapters(book_url: str, soup: BeautifulSoup, raw_html: str) -> list:
    detectors = [
        ("wp_table_toc", lambda: _detect_wp_table_toc(soup, book_url)),
        ("wp_list_toc", lambda: _detect_wp_list_toc(soup, book_url)),
        ("raw_anchor_regex", lambda: _collect_links_from_raw_html(raw_html, book_url)),
        ("wp_manga_ajax", lambda: _detect_wp_manga_ajax(book_url)),
    ]

    for name, fn in detectors:
        try:
            chapters = fn()
            if chapters:
                print(f"✅ Detected chapter source: {name}")
                return chapters
        except Exception as e:
            print(f"⚠ Detector {name} lỗi: {e}")

    _save_debug_html("book_page_debug.html", raw_html)
    return []


def _extract_chapter_title(soup: BeautifulSoup, url: str, raw_html: str = "") -> str:
    title = (
        _text(soup.select_one("#chapter-heading"))
        or _text(soup.select_one("h1.entry-title"))
        or _text(soup.select_one("h1.wp-block-post-title"))
        or _text(soup.select_one("article h1"))
        or _text(soup.select_one("h1"))
    )
    if title:
        return title.strip()

    m = re.search(r"<title>(.*?)</title>", raw_html or "", re.I | re.S)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", BeautifulSoup(m.group(1), "html.parser").get_text(" ", strip=True)))
        if title:
            return title

    m = re.search(r"(chuong[-\s_]*\d+(?:[-_.]\d+)?)", url, re.I)
    if m:
        return m.group(1).replace("-", " ").replace("_", " ").title()

    return "Chương"


def _looks_like_noise(text: str) -> bool:
    low = html.unescape((text or "").strip()).lower()
    if not low:
        return True
    exact_bad = {
        "đọc tiếp", "mục lục", "hết chương", "quay lại", "next chapter", "previous chapter",
        "chương sau", "chương trước", "ads", "quảng cáo"
    }
    if low in exact_bad:
        return True
    patterns = [
        r"^nguồn[:\s]",
        r"^editor[:\s]",
        r"^beta[:\s]",
        r"^wpdiscuz",
        r"^share this",
        r"^related posts?",
    ]
    return any(re.search(p, low, re.I) for p in patterns)


def _clean_content_node(content_node):
    for tag in content_node.find_all(["script", "style", "noscript", "iframe", "form", "button", "svg", "canvas"]):
        tag.decompose()

    for c in content_node.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    junk_selectors = [
        ".sharedaddy", ".jp-relatedposts", ".sd-sharing", ".post-navigation", ".comments-area",
        ".entry-meta", ".post-meta", ".author-box", ".sidebar", ".widget", ".code-block",
        "nav", "footer", ".wp-block-buttons", ".wp-block-separator", ".wp-block-social-links",
        ".wp-block-latest-posts",
    ]
    for sel in junk_selectors:
        for tag in content_node.select(sel):
            tag.decompose()

    for p in list(content_node.find_all(["p", "div", "span", "li"])):
        txt = p.get_text(" ", strip=True)
        if not txt and not p.find("img"):
            p.decompose()
            continue
        if _looks_like_noise(txt):
            p.decompose()

    allowed = {"p", "img", "br", "a", "strong", "b", "em", "i", "span", "u", "blockquote", "h2", "h3", "h4", "ul", "ol", "li"}
    for tag in list(content_node.find_all()):
        if tag.name not in allowed:
            try:
                tag.unwrap()
            except Exception:
                pass

    return content_node


def _get_content_chapter(url: str) -> str:
    raw_html, soup = _fetch_html(url, referer=url)
    chapter_title = _extract_chapter_title(soup, url, raw_html=raw_html)

    content_node = (
        soup.select_one(".reading-content")
        or soup.select_one(".text-left")
        or soup.select_one("div.entry-content")
        or soup.select_one("div.wp-block-post-content")
        or soup.select_one("article .post-content")
        or soup.select_one("article")
    )

    if not content_node:
        _save_debug_html("chapter_debug.html", raw_html)
        print(f"⚠ Không tìm thấy nội dung tại {url}")
        return ""

    content_node = _clean_content_node(content_node)

    first_heading = content_node.find(["h1", "h2", "h3"])
    if first_heading and _text(first_heading).strip().lower() == chapter_title.strip().lower():
        first_heading.decompose()

    content_html = "\n".join(str(c) for c in content_node.children if str(c).strip())
    content_html = re.sub(r'<\/?(html|head|body|title|doctype)[^>]*>', '', content_html, flags=re.I)

    xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" lang="vi">
  <head>
    <meta charset="utf-8" />
    <title>{html.escape(chapter_title)}</title>
  </head>
  <body>
    <h1>{html.escape(chapter_title)}</h1>
    <section id="chapter" class="chapter-content">
{BeautifulSoup(content_html, "html.parser").prettify()}
    </section>
  </body>
</html>""".strip()

    return xhtml


def _get_content_chapters(chap_list: List[Dict[str, str]], book_title: str | None = None, base_output_dir: str | Path = "output") -> List[Tuple[int, str, str]]:
    if not chap_list:
        print("⚠ Không có chương nào trong chap_list.")
        return []

    book_dir_name = _slug_folder(book_title or "book")
    out_dir = ensure_dir(Path(base_output_dir) / book_dir_name)

    total = len(chap_list)
    results: List[Tuple[int, str, str]] = []
    pad = max(3, len(str(total)))

    print(f"\n--- Bắt đầu tải {total} chương ---")
    for i, chap in enumerate(chap_list, start=1):
        raw_title = chap.get("title") or f"Chương {i}"
        url = chap.get("url")
        try:
            xhtml = _get_content_chapter(url)
            if not xhtml:
                continue

            clean_title = _clean_chapter_title(raw_title, i)
            fname = f"{str(i).zfill(pad)}.xhtml"
            fpath = out_dir / fname

            with open(fpath, "w", encoding="utf-8") as f:
                f.write(xhtml)

            print(f"[{str(i).zfill(pad)}/{str(total).zfill(pad)}] Saved - {clean_title}")
            results.append((i, clean_title, xhtml))
            time.sleep(SLEEP_BETWEEN_CHAPS)
        except Exception as e:
            print(f"[{str(i).zfill(pad)}/{str(total).zfill(pad)}] ❌ Lỗi '{raw_title}' ({url}): {e}")

    return results


def _sniff_image_type(data: bytes):
    if not data or len(data) < 12:
        return (".bin", "application/octet-stream")
    if data[:3] == b"\xff\xd8\xff":
        return (".jpg", "image/jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (".png", "image/png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return (".webp", "image/webp")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return (".gif", "image/gif")
    return (".bin", "application/octet-stream")


def _ensure_jpeg_cover(img_bytes: bytes):
    if not img_bytes:
        return (None, None, None)

    if not HAS_PILLOW:
        ext, mime = _sniff_image_type(img_bytes)
        return (img_bytes, ext, mime)

    try:
        im = Image.open(io.BytesIO(img_bytes))
        resample_filter = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        if im.size[0] > MAX_COVER_SIZE[0] or im.size[1] > MAX_COVER_SIZE[1]:
            im.thumbnail(MAX_COVER_SIZE, resample_filter)

        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")

        out = io.BytesIO()
        im.save(out, format="JPEG", quality=90, optimize=True)
        return (out.getvalue(), ".jpg", "image/jpeg")
    except Exception as e:
        ext, mime = _sniff_image_type(img_bytes)
        print(f"⚠ Cover convert error: {e}. Dùng ảnh gốc {ext}.")
        return (img_bytes, ext, mime)


def _download_binary(url: str, referer: Optional[str] = None) -> bytes:
    # ưu tiên curl cho wordpress.com ảnh
    parsed = urlparse(url)
    if parsed.netloc.endswith("wordpress.com"):
        try:
            curl_bin = shutil.which("curl") or shutil.which("curl.exe")
            if curl_bin:
                cmd = [curl_bin, "-L", "--compressed", "-A", HEADERS["User-Agent"], url]
                if referer:
                    cmd.extend(["-e", referer])
                p = subprocess.run(cmd, capture_output=True, timeout=60)
                if p.returncode == 0 and p.stdout:
                    return p.stdout
        except Exception:
            pass

    session = requests.Session()
    session.headers.update(HEADERS)
    if referer:
        session.headers["Referer"] = referer
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def _load_cover_from_input(cover_in: str, story_url: str, info_cover_url: str):
    data = None

    if cover_in:
        try:
            if cover_in.lower().startswith(("http://", "https://")):
                data = _download_binary(cover_in, referer=story_url)
            else:
                with open(cover_in, "rb") as f:
                    data = f.read()
        except Exception as e:
            print(f"⚠ Không tải/đọc được cover '{cover_in}': {e}")

    if data is None and info_cover_url:
        try:
            print(f"Tự động lấy cover: {info_cover_url}")
            data = _download_binary(info_cover_url, referer=story_url)
        except Exception as e:
            print(f"⚠ Không tải được CoverURL mặc định: {e}")

    if data:
        return _ensure_jpeg_cover(data)
    return (None, None, None)


def _epub_write(zipf, arcname, data_bytes, compress=True):
    zinfo = zipfile.ZipInfo(arcname)
    zinfo.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    zipf.writestr(zinfo, data_bytes)


def create_epub_epub3_for_kobo(book_title: str, author: str, items: list, out_epub_dir: str, cover_bytes=None, cover_ext=None, cover_mime=None, language="vi", publisher="Hishiro"):
    os.makedirs(out_epub_dir, exist_ok=True)
    out_file = os.path.join(out_epub_dir, f"{_slugify_vi(book_title)}.epub")
    cover_rel = f"Images/cover{cover_ext}" if (cover_bytes and cover_ext) else None

    with zipfile.ZipFile(out_file, "w") as z:
        _epub_write(z, "mimetype", b"application/epub+zip", compress=False)

        container_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>'
        ).encode("utf-8")
        _epub_write(z, "META-INF/container.xml", container_xml)

        _epub_write(z, "OEBPS/Styles/style.css", b"body{font-family:serif;line-height:1.6} img{max-width:100%;height:auto} h1{text-align:center}")

        manifest_items = []
        spine_items = []
        toc_entries = []

        if cover_rel:
            cover_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi">\n'
                '<head><title>Cover</title><meta charset="utf-8"/></head>\n'
                '<body>\n'
                f'  <img src="../{cover_rel}" alt="cover" style="max-width:100%;height:auto;display:block;margin:0 auto;"/>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")
            _epub_write(z, "OEBPS/Text/cover.xhtml", cover_xhtml)
            manifest_items.append('<item id="cover-html" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>')
            spine_items.append('<itemref idref="cover-html" linear="no"/>')
            _epub_write(z, f"OEBPS/{cover_rel}", cover_bytes)

        for i, c in enumerate(items, 1):
            fn = f"Text/chapter_{i:04d}.xhtml"
            chap_soup = BeautifulSoup(c.get("xhtml_content") or "", "html.parser")
            node = chap_soup.find("section", id="chapter") or chap_soup.find("body")
            h1_title = _text(chap_soup.find("h1"))
            content_html = "".join(str(x) for x in node.children) if node else ""

            xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">\n'
                '<head>\n'
                f'  <title>{html.escape(c.get("title") or f"Chương {i}")}</title>\n'
                '  <meta charset="utf-8"/>\n'
                '  <link href="../Styles/style.css" rel="stylesheet" type="text/css"/>\n'
                '</head>\n'
                '<body>\n'
                f'  <h1>{html.escape(h1_title or c.get("title") or f"Chương {i}")}</h1>\n'
                f'  <div>{content_html}</div>\n'
                '</body>\n'
                '</html>'
            ).encode("utf-8")

            _epub_write(z, f"OEBPS/{fn}", xhtml)
            manifest_items.append(f'<item id="chap{i}" href="{fn}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{i}"/>')
            toc_entries.append((fn, c.get("title") or f"Chương {i}"))

        nav_html = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" xmlns:epub="http://www.idpf.org/2007/ops">\n'
            '<head><meta charset="utf-8"/><title>TOC</title></head>\n'
            '<body>\n'
            '  <nav epub:type="toc" id="toc">\n'
            f'    <h1>{html.escape(book_title)}</h1>\n'
            '    <ol>\n' +
            "\n".join(f'      <li><a href=\"{fn}\">{html.escape(t)}</a></li>' for fn, t in toc_entries) +
            '\n    </ol>\n'
            '  </nav>\n'
            '</body>\n'
            '</html>'
        ).encode("utf-8")
        _epub_write(z, "OEBPS/nav.xhtml", nav_html)

        dt_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cover_item_line = (
            f'<item id="cover-img" href="{cover_rel}" media-type="{cover_mime or "image/jpeg"}" properties="cover-image"/>'
            if cover_rel else ""
        )
        manifest_str = "\n    ".join([
            '<item id="css" href="Styles/style.css" media-type="text/css"/>',
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            *manifest_items,
            *([cover_item_line] if cover_item_line else [])
        ])
        spine_str = "\n    ".join(spine_items)

        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="3.0">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:identifier id="BookID">urn:uuid:{_slugify_vi(book_title)}-{int(time.time())}</dc:identifier>\n'
            f'    <dc:title>{html.escape(book_title)}</dc:title>\n'
            f'    <dc:creator>{html.escape(author or "—")}</dc:creator>\n'
            f'    <dc:publisher>{html.escape(publisher)}</dc:publisher>\n'
            f'    <dc:language>{language}</dc:language>\n'
            f'    <meta property="dcterms:modified">{dt_utc}</meta>\n'
            '  </metadata>\n'
            '  <manifest>\n'
            f'    {manifest_str}\n'
            '  </manifest>\n'
            '  <spine>\n'
            f'    {spine_str}\n'
            '  </spine>\n'
            '</package>'
        ).encode("utf-8")
        _epub_write(z, "OEBPS/content.opf", opf)

    return out_file


def download_and_build_epub(url_story: str):
    raw_html, soup = _fetch_html(url_story)
    _save_debug_html("last_book_page.html", raw_html)

    info = _get_book_info(soup, url_story, raw_html=raw_html)

    title = info.get("Title") or "Truyện"
    author = info.get("Author") or "—"
    cover_auto_url = info.get("CoverURL") or ""

    print("--------------------------Thông tin truyện--------------------------")
    for k, v in info.items():
        print(f"{k}: {v}")

    chapters = _get_list_chapters(url_story, soup, raw_html)
    if not chapters:
        print("❌ Không tìm thấy chương nào.")
        print("📁 Đã lưu debug HTML tại: debug/last_book_page.html")
        return

    print(f"Total chapters: {len(chapters)}")

    cover_bytes, cover_ext, cover_mime = _load_cover_from_input("", url_story, cover_auto_url)
    print(f"Cover: {'OK' if cover_bytes else 'MISSING'} ({cover_mime or '-'})")

    out_dir = os.path.join("output", _slug_folder(title))
    os.makedirs(out_dir, exist_ok=True)

    chaps_saved = _get_content_chapters(chapters, book_title=title)

    all_items = [{"title": chap_title, "xhtml_content": xhtml} for _, chap_title, xhtml in chaps_saved]
    if not all_items:
        print("❌ Không có chương nào tải thành công.")
        return

    print("\n--- Bắt đầu đóng gói EPUB ---")
    epub_path = create_epub_epub3_for_kobo(
        book_title=title,
        author=author,
        items=all_items,
        out_epub_dir="output",
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        cover_mime=cover_mime,
        language="vi",
        publisher="Hishiro"
    )
    print(f"✅ EPUB: {epub_path}")
    print(f"📁 Thư mục chương đã lưu: {out_dir}")


if __name__ == "__main__":
    url = input("Nhập URL truyện: ").strip()
    if not url:
        print("❌ URL trống.")
    else:
        download_and_build_epub(url)
