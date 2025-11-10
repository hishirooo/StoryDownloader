# -*- coding: utf-8 -*-
"""
Downloader 3020.devshop.vn → EPUB3 (Kobo-friendly)
- Ưu tiên Next.js (__NEXT_DATA__) từ trang /chapter/<slug>
- Fallback API /api/chapters/get-chapter (OpenSSL "Salted__")
- Tự retry khi "Slow down…"
- Xuất EPUB3

pip install requests beautifulsoup4 pycryptodome pillow
"""

import os, re, time, html, unicodedata, base64, hashlib, json, zipfile, uuid, io, datetime
from pathlib import Path
from typing import List, Dict, Tuple
import requests
from bs4 import BeautifulSoup

# ================= CẤU HÌNH =================
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.2
SLEEP_BETWEEN_CHAPS = 0.05
MAX_COVER_SIZE = (1600, 2400)  # Kobo-friendly
DOMAIN = "https://3020.devshop.vn"
API_LIST_CHAPS = f"{DOMAIN}/api/chapters/list-chapters"
API_GET_CHAP  = f"{DOMAIN}/api/chapters/get-chapter"

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

RETRY_STATUS = {429, 500, 502, 503, 504}

def _sess_get(url, **kw):
    for i in range(6):
        r = SESSION.get(url, timeout=TIMEOUT, **kw)
        if r.status_code in RETRY_STATUS:
            time.sleep(0.6*(i+1)); continue
        r.raise_for_status(); return r
    r.raise_for_status(); return r

def _sess_post(url, **kw):
    for i in range(6):
        r = SESSION.post(url, timeout=TIMEOUT, **kw)
        if r.status_code in RETRY_STATUS:
            time.sleep(0.6*(i+1)); continue
        r.raise_for_status(); return r
    r.raise_for_status(); return r

# Pillow (cover)
try:
    from PIL import Image
    HAS_PILLOW = True
except Exception:
    HAS_PILLOW = False

# ================= TIỆN ÍCH =================
def _fetch_html(url: str) -> BeautifulSoup:
    r = _sess_get(url)
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "html.parser")

def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

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
    return (s or "book")[:80]

# ================= AES/OpenSSL giống web + AES-256-CBC (Next.js) =================
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import unpad

def _openssl_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    d = b""; last = b""
    while len(d) < key_len + iv_len:
        last = hashlib.md5(last + password + salt).digest()
        d += last
    return d[:key_len], d[key_len:key_len+iv_len]

def _aes_encrypt(plaintext: str, passphrase: str) -> str:
    salt = get_random_bytes(8)
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pad = 16 - (len(plaintext.encode()) % 16)
    enc = cipher.encrypt(plaintext.encode() + bytes([pad])*pad)
    return base64.b64encode(b"Salted__" + salt + enc).decode()

def _aes_decrypt(b64: str, passphrase: str) -> str:
    raw = base64.b64decode(b64)
    assert raw[:8] == b"Salted__", "Không đúng định dạng OpenSSL."
    salt = raw[8:16]
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:])
    pad = dec[-1]
    return dec[:-pad].decode()

def _derive_key(obf: str) -> str:
    if "-" in obf:
        left, right = obf.split("-", 1)
    else:
        mid = (len(obf)+1)//2
        left, right = obf[:mid], obf[mid:] or "0000"
    prod = 1
    for ch in right:
        prod = (prod * ord(ch)) % 255
    out = []
    for i, ch in enumerate(left):
        code = ord(ch); r = (prod + i * code) % 26
        if 65 <= code <= 90:
            out.append(chr((code - 65 + r) % 26 + 65))
        elif 97 <= code <= 122:
            out.append(chr((code - 97 + r) % 26 + 97))
        else:
            out.append(ch)
    return "".join(out)

PASS = _derive_key("T5Hr41U5jKTTrtUOXdYZnyx3wjZEKUoxv16Clwwu4D5zIbd0-q9sdfh")

def _decrypt_next_content(enc_b64: str, key_b64: str) -> str:
    enc = base64.b64decode(enc_b64)
    seed = base64.b64decode(key_b64)
    key  = hashlib.sha256(seed).digest()
    iv   = enc[:16]
    dec  = AES.new(key, AES.MODE_CBC, iv).decrypt(enc[16:])
    dec  = unpad(dec, AES.block_size)
    return dec.decode("utf-8", errors="ignore")

# ================= LẤY INFO TRUYỆN =================
def get_story_id(soup: BeautifulSoup) -> int:
    tag = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if not tag or not tag.string:
        raise RuntimeError("Không tìm thấy __NEXT_DATA__")
    data = json.loads(tag.string)
    return int(data["props"]["pageProps"]["story"]["id"])

def get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    title = _text(soup.find("h1", class_="story_book-info__title__1jpSQ"))
    author = _text(soup.find("div", class_="story_book-info__author__lPhnG")).replace("Tác giả: ","")
    genres = " - ".join(_text(a) for a in soup.select(".story_book-info__category__B1RPT a"))
    desc = _text(soup.find("div", class_="story_card-content__NO3Br"))
    cover = ""
    imgtag = soup.select_one(".story_book__qq6xd img")
    if imgtag and imgtag.get("src"):
        cover = DOMAIN + imgtag["src"]
    return {"title": title, "author": author, "genre": genres, "description": desc, "cover": cover}

# ================= DANH SÁCH CHƯƠNG =================
def get_list_chapters(story_url: str) -> List[Dict[str,str]]:
    soup = _fetch_html(story_url)
    sid = get_story_id(soup)
    chapters, page, per_page = [], 1, 50

    while True:
        payload = {
            "id_story": _aes_encrypt(str(sid), PASS),
            "page": _aes_encrypt(str(page), PASS),
            "items_per_page": _aes_encrypt(str(per_page), PASS),
            "order": _aes_encrypt("asc", PASS),
        }
        r = _sess_post(API_LIST_CHAPS, json=payload)
        data = json.loads(_aes_decrypt(r.json()["data"], PASS))
        rows = data if isinstance(data, list) else (data.get("chapters") or data.get("data") or [])
        if not rows: break

        for ch in rows:
            slug = ch.get("slug") or ch.get("slug_chapter") or ""
            url  = f"{DOMAIN}/chapter/{slug}" if slug else ""
            chapters.append({"title": ch.get("name","").strip(), "url": url, "slug": slug})

        if len(rows) < per_page: break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters

# ================= LẤY CHƯƠNG: Next.js first =================
def get_chapter_via_next(slug: str, story_url: str | None = None, tries: int = 6, warmup: bool = True) -> dict | None:
    """
    - Warm-up: mở trang chapter để lấy cookie + __NEXT_DATA__
    - Giải mã contentEncrypt bằng key_encrypt
    - Nếu gặp "Slow down…" → backoff rồi retry
    """
    delay = 0.7
    for attempt in range(1, tries+1):
        try:
            if warmup:
                resp = _sess_get(f"{DOMAIN}/chapter/{slug}", headers={"referer": story_url or DOMAIN})
                html_text = resp.text
                if "Slow down" in html_text or "Load lại" in html_text:
                    raise RuntimeError("Throttle")

                soup = BeautifulSoup(html_text, "html.parser")
                s = soup.find("script", id="__NEXT_DATA__", type="application/json")
                if s and s.string:
                    data = json.loads(s.string)
                    ch  = (data.get("props",{}).get("pageProps",{}) or {}).get("chapter") or {}
                    key = ch.get("key_encrypt")
                    enc = ch.get("contentEncrypt") or ch.get("content_comp") or ch.get("content")
                    title_raw = ch.get("title") or ch.get("name") or slug
                    if key and enc:
                        title = _decrypt_next_content(title_raw, key) if isinstance(title_raw, str) and title_raw.startswith("U2Fsd") else title_raw
                        body  = _decrypt_next_content(enc, key)
                        if body.strip():
                            return {"title": title or slug, "content": body, "slug": slug}

            # Fallback: gọi _next/data với buildId lấy từ chính trang chapter (nếu có trong script)
            if not s or not s.string:
                # lấy buildId nhanh từ story/home
                host_soup = _fetch_html(story_url or DOMAIN)
                s2 = host_soup.find("script", id="__NEXT_DATA__", type="application/json")
                build_id = None
                if s2 and s2.string:
                    build_id = json.loads(s2.string).get("buildId")
                if build_id:
                    url = f"{DOMAIN}/_next/data/{build_id}/chapter/{slug}.json?slug={slug}"
                    r2 = _sess_get(url, headers={"x-nextjs-data": "1", "referer": story_url or f"{DOMAIN}/chapter/{slug}"})
                    j2 = r2.json()
                    ch2 = (j2.get("pageProps") or {}).get("chapter") or {}
                    key = ch2.get("key_encrypt")
                    enc = ch2.get("contentEncrypt") or ch2.get("content_comp") or ch2.get("content")
                    title_raw = ch2.get("title") or slug
                    if key and enc:
                        title = _decrypt_next_content(title_raw, key) if isinstance(title_raw, str) and title_raw.startswith("U2Fsd") else title_raw
                        body  = _decrypt_next_content(enc, key)
                        if body.strip():
                            return {"title": title or slug, "content": body, "slug": slug}

            # nếu tới đây xem như không có nội dung → retry
            raise RuntimeError("No content via Next")

        except Exception as e:
            if attempt == tries:
                return None
            time.sleep(delay); delay = min(delay*1.8, 6.0)
            continue

# Helpers duyệt sâu JSON cho fallback API
def _find_encrypted_payload(obj):
    if isinstance(obj, dict):
        key_enc = obj.get("key_encrypt")
        enc = obj.get("contentEncrypt") or obj.get("content_comp") or obj.get("content")
        ttl = obj.get("title") or obj.get("name")
        if key_enc and isinstance(enc, str) and enc.startswith("U2Fsd"):
            return (ttl, enc, key_enc)
        for v in obj.values():
            r = _find_encrypted_payload(v)
            if r: return r
    elif isinstance(obj, list):
        for it in obj:
            r = _find_encrypted_payload(it)
            if r: return r
    return None

def _extract_plain_content(obj, slug: str):
    if isinstance(obj, dict):
        title = obj.get("name") or obj.get("title") or slug
        html_body = obj.get("content") or obj.get("chapter_content") or obj.get("html")
        if isinstance(html_body, str) and html_body.strip():
            return (title, html_body)
        for v in obj.values():
            r = _extract_plain_content(v, slug)
            if r: return r
    elif isinstance(obj, list):
        for it in obj:
            r = _extract_plain_content(it, slug)
            if r: return r
    return None

def get_chapter_by_slug(slug: str, story_url: str | None = None, max_retry_api: int = 4) -> dict:
    # 1) Next.js (ổn định nhất)
    data = get_chapter_via_next(slug, story_url=story_url)
    if data and data.get("content"):
        return data

    # 2) Fallback API get-chapter (OpenSSL)
    delay = 0.6
    last_err = None
    for attempt in range(1, max_retry_api+1):
        try:
            payload = {"slug": _aes_encrypt(slug, PASS)}
            r = _sess_post(API_GET_CHAP, json=payload)
            j = r.json()
            dec = _aes_decrypt(j["data"], PASS).strip()

            # server trả HTML trực tiếp
            if dec.lower().startswith("<"):
                if "slow down" in dec.lower():  # throttle
                    raise RuntimeError("Server throttle")
                return {"title": slug, "content": dec, "slug": slug}

            parsed = json.loads(dec)

            enc_pack = _find_encrypted_payload(parsed)
            if enc_pack:
                ttl, enc_b64, key_b64 = enc_pack
                html_body = _decrypt_next_content(enc_b64, key_b64)
                if isinstance(ttl, str) and ttl.startswith("U2Fsd"):
                    try: ttl = _decrypt_next_content(ttl, key_b64)
                    except: ttl = slug
                return {"title": ttl or slug, "content": html_body, "slug": slug}

            plain_pack = _extract_plain_content(parsed, slug)
            if plain_pack:
                ttl, html_body = plain_pack
                return {"title": ttl or slug, "content": html_body, "slug": slug}

            raise RuntimeError("Chưa có nội dung")

        except Exception as e:
            last_err = e
            if attempt == max_retry_api:
                break
            time.sleep(delay)
            delay = min(delay * 1.8, 6.0)

    raise RuntimeError(f"Không lấy được nội dung (slug={slug}): {last_err}")

# ================= COVER =================
def prepare_cover(cover_mode: str, auto_url: str) -> bytes | None:
    if cover_mode not in ("auto","local","url",""):
        cover_mode = "auto"
    b = None
    try:
        if cover_mode == "auto" and auto_url:
            b = _sess_get(auto_url).content
        elif cover_mode == "url":
            link = input("Nhập link ảnh cover: ").strip()
            if link: b = _sess_get(link).content
        elif cover_mode == "local":
            path = input("Nhập đường dẫn ảnh cover (trên máy): ").strip().strip('"')
            if path and os.path.exists(path):
                with open(path, "rb") as f: b = f.read()
    except Exception:
        b = None

    if not b or not HAS_PILLOW:
        return b
    try:
        im = Image.open(io.BytesIO(b)).convert("RGB")
        im.thumbnail(MAX_COVER_SIZE)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=88)
        return out.getvalue()
    except Exception:
        return b

# ================= EPUB3 =================
def _absolute_img_src(html_fragment: str) -> str:
    return re.sub(r'(<img[^>]+src=")(/[^"]+)"', rf'\1{DOMAIN}\2"', html_fragment)

def _clean_chapter_html(raw_html: str, chapter_title: str = "Chương") -> str:
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for tag in soup.find_all(True): tag.attrs = {}
    for sp in soup.find_all("span"): sp.unwrap()
    for p in list(soup.find_all("p")):
        txt = p.get_text(strip=True)
        only_br = all((getattr(ch, "name", None) == "br") or (str(ch).strip()== "") for ch in p.children)
        if txt == "" and only_br: p.decompose()
    html_str = str(soup)
    html_str = re.sub(r"(?:<br/?>\s*){3,}", "<br/>\n<br/>", html_str, flags=re.I)
    html_str = re.sub(r"[ \t]+\n", "\n", html_str)
    html_str = _absolute_img_src(html_str)

    xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="vi" lang="vi">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(chapter_title or "Chương")}</title>
</head>
<body>
  <section id="chapter" class="chapter-content">
{BeautifulSoup(html_str, "html.parser").prettify()}
  </section>
</body>
</html>""".strip()
    return xhtml

def build_epub3(out_folder: Path, title: str, author: str, chapters_xhtml: List[Tuple[str, str]], cover_bytes: bytes | None):
    out_folder.mkdir(parents=True, exist_ok=True)
    epub_path = out_folder / f"{_slugify_vi(title)}.epub"

    OEBPS_text = [(name, data) for name, data in chapters_xhtml]
    manifest_items = []
    spine_items = []

    uid = str(uuid.uuid4())
    now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    with zipfile.ZipFile(epub_path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)

        container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
        z.writestr("META-INF/container.xml", container_xml)

        cover_item = ""
        if cover_bytes:
            z.writestr("OEBPS/images/cover.jpg", cover_bytes)
            cover_item = '<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>'

        for idx, (fname, xhtml) in enumerate(OEBPS_text, 1):
            z.writestr(f"OEBPS/text/{fname}", xhtml)
            manifest_items.append(f'<item id="chap{idx:04d}" href="text/{fname}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="chap{idx:04d}"/>')

        nav_items = []
        for idx, (fname, xhtml) in enumerate(OEBPS_text, 1):
            m = re.search(r"<title>(.*?)</title>", xhtml, flags=re.I|re.S)
            t = html.escape(m.group(1).strip()) if m else f"Chương {idx}"
            nav_items.append(f'<li><a href="text/{fname}">{t}</a></li>')
        nav_xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="vi">
<head><meta charset="utf-8"/><title>Mục lục</title></head>
<body>
<nav epub:type="toc" id="toc">
  <h2>Mục lục</h2>
  <ol>
    {' '.join(nav_items)}
  </ol>
</nav>
</body></html>"""
        z.writestr("OEBPS/nav.xhtml", nav_xhtml)
        manifest_items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')

        manifest = "\n    ".join(([cover_item] if cover_item else []) + manifest_items)
        spine = "\n    ".join(spine_items)
        content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{uid}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author or "Unknown")}</dc:creator>
    <dc:language>vi</dc:language>
    <dc:publisher>Hishiro</dc:publisher>
    <meta property="dcterms:modified">{now_iso}</meta>
  </metadata>
  <manifest>
    {manifest}
  </manifest>
  <spine>
    {spine}
  </spine>
</package>"""
        z.writestr("OEBPS/content.opf", content_opf)

    print(f"✅ Đã tạo EPUB: {epub_path}")

# ================= MAIN =================
def main():
    story_url = input("Nhập URL truyện (https://3020.devshop.vn/story/<slug>): ").strip()
    if not story_url:
        print("URL rỗng."); return

    soup = _fetch_html(story_url)
    info = get_book_info(soup)
    title = info["title"] or "Truyện"
    author = info["author"] or ""
    print("\n--- THÔNG TIN TRUYỆN ---")
    print("Title :", title)
    print("Author:", author)
    print("Genres:", info["genre"])
    print("Cover :", info["cover"])

    mode = input("Chọn cover [auto/local/url/none] (mặc định auto): ").strip().lower() or "auto"
    if mode == "none": mode = ""
    cover_bytes = prepare_cover(mode, info.get("cover",""))

    chapters = get_list_chapters(story_url)
    print(f"Tổng số chương: {len(chapters)}")

    rng = input("Chọn phạm vi chương (vd 1-50 | 100- | -200 | Enter = tất cả): ").strip()
    start_idx, end_idx = 1, len(chapters)
    m = re.fullmatch(r"\s*(\d+)?\s*-\s*(\d+)?\s*", rng) if rng else None
    if m:
        if m.group(1): start_idx = max(1, int(m.group(1)))
        if m.group(2): end_idx = min(len(chapters), int(m.group(2)))
    if rng and not m:
        print("❓ Không hiểu phạm vi, tải tất cả.")
    sel = chapters[start_idx-1:end_idx]
    print(f"Tải {len(sel)} chương: [{start_idx}..{end_idx}]")

    out_root = Path("output") / _slug_folder(title)
    chap_folder = out_root / "chapters"
    chap_folder.mkdir(parents=True, exist_ok=True)

    texts: List[Tuple[str,str]] = []
    total = len(sel)
    for k, ch in enumerate(sel, start=start_idx):
        slug = ch["slug"]
        try:
            data = get_chapter_by_slug(slug, story_url=story_url)
            xhtml = _clean_chapter_html(data["content"], data["title"])
            fname = f"{k:04d}-{_slugify_vi(data['title'] or f'Chuong-{k}')}.xhtml"
            texts.append((fname, xhtml))
            (chap_folder / fname).write_text(xhtml, encoding="utf-8")
            print(f"[{k}/{end_idx}] ✓ {data['title']}", flush=True)
        except Exception as e:
            print(f"[{k}/{end_idx}] ⚠ Lỗi chương {k}: {e}", flush=True)
            err_file = chap_folder / f"{k:04d}-ERROR.xhtml"
            with open(err_file, "w", encoding="utf-8") as f:
                f.write(f"<!-- Error: {e} -->\n")
        time.sleep(SLEEP_BETWEEN_CHAPS)

    build_epub3(out_root, title, author, texts, cover_bytes)

if __name__ == "__main__":
    main()
