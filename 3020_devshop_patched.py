# -*- coding: utf-8 -*-
import asyncio, sys, re, json, time
from pathlib import Path
from typing import Dict, List, Optional
import base64, hashlib
import requests
from bs4 import BeautifulSoup

# Playwright
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    import os
    os.system("pip install playwright")
    from playwright.sync_api import sync_playwright

TIMEOUT = 25
SLEEP_BETWEEN_PAGES = 0.15
ITEMS_PER_PAGE = 50

# ====== phần bạn đã dùng để gọi API list-chapters (giữ lại, không cần giải mã trả về) ======
def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _base_host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url)
    host = m.group(1).lower() if m else "3020.devshop.vn"
    if "metruyen" in host:
        return "metruyen.xyz"
    return "3020.devshop.vn"

def _api_base(url: str) -> str:
    return f"https://{_base_host(url)}"

def _make_headers(url: str) -> Dict[str, str]:
    base = _api_base(url)
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": base,
        "Referer": url if "/story/" in url or "/chapter/" in url else base,
    }

def _fetch_html(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=_make_headers(url), timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "html.parser")

def get_story_id(soup: BeautifulSoup) -> int:
    tag = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if tag and tag.string:
        data = json.loads(tag.string)
        return int(data["props"]["pageProps"]["story"]["id"])
    raise RuntimeError("Không tìm thấy story.id")

# === dùng AES-OpenSSL để GỬI payload (server yêu cầu), KHÔNG giải mã response ===
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

def _openssl_bytes_to_key(password: bytes, salt: bytes, key_len: int, iv_len: int):
    d = b""; last = b""
    while len(d) < key_len + iv_len:
        last = hashlib.md5(last + password + salt).digest()
        d += last
    return d[:key_len], d[key_len:key_len+iv_len]

def _aes_encrypt_openssl(plaintext: str, passphrase: str) -> str:
    salt = get_random_bytes(8)
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pad = 16 - (len(plaintext.encode()) % 16)
    enc = cipher.encrypt(plaintext.encode() + bytes([pad])*pad)
    return base64.b64encode(b"Salted__" + salt + enc).decode()

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

def get_book_info(story_url: str) -> Dict[str, str]:
    soup = _fetch_html(story_url)
    title = _text(soup.find("h1", class_="story_book-info__title__1jpSQ"))
    author = _text(soup.find("div", class_="story_book-info__author__lPhnG")).replace("Tác giả: ","")
    genres = " - ".join(_text(a) for a in soup.select(".story_book-info__category__B1RPT a"))
    return {"title": title, "author": author, "genres": genres}

def get_list_chapters(story_url: str) -> List[Dict[str, str]]:
    soup = _fetch_html(story_url)
    sid = get_story_id(soup)
    api = _api_base(story_url) + "/api/chapters/list-chapters"

    chapters, page = [], 1
    while True:
        payload = {
            "id_story": _aes_encrypt_openssl(str(sid), PASS),
            "page": _aes_encrypt_openssl(str(page), PASS),
            "items_per_page": _aes_encrypt_openssl(str(ITEMS_PER_PAGE), PASS),
            "order": _aes_encrypt_openssl("asc", PASS),
        }
        r = requests.post(api, json=payload, headers=_make_headers(story_url), timeout=TIMEOUT)
        r.raise_for_status()
        # KHÔNG giải mã response — ta chỉ cần slug/name
        try:
            js = r.json()
            # response["data"] là openssl-b64 → bó tay, nên để server trả JSON list ở nơi khác (nhiều site trả thêm data phụ),
            # hoặc fallback dùng trang HTML list nếu có. Ở đây ta thử branch phụ:
            rows = js.get("chapters") or js.get("data") or []
            if not rows:
                # fallback: lấy list trên HTML nếu API không trả
                rows = []
                for a in soup.select('a[href*="/chapter/"]'):
                    slug = a.get("href", "")
                    if "/chapter/" in slug:
                        title = _text(a)
                        u = slug if slug.startswith("http") else _api_base(story_url) + slug
                        rows.append({"name": title, "slug": slug.replace("/chapter/","")})
        except Exception:
            rows = []

        if not rows:
            break

        for ch in rows:
            slug = ch.get("slug") or ch.get("slug_chapter") or ""
            if not slug and isinstance(ch.get("url"), str) and "/chapter/" in ch["url"]:
                slug = ch["url"].split("/chapter/")[-1]
            url = f"{_api_base(story_url)}/chapter/{slug}" if slug else ""
            if url:
                chapters.append({"title": ch.get("name","").strip() or slug, "url": url})

        if len(rows) < ITEMS_PER_PAGE:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters

# ====== Playwright: mở chương, cho JS chạy, lấy DOM đã giải mã ======
CONTENT_SELECTORS = [
    ".chapter-content, .reader-content, article, #chapter-content",
    "div[class*=chapter]:has(p), div[id*=chapter]:has(p)",
]

def _sanitize_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "chapter"

async def fetch_chapter_with_browser(pw, url: str, out_dir: Path) -> Path:
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    page.set_default_timeout(45000)

    await page.goto(url, wait_until="domcontentloaded")
    # nhiều trang lazy-load khi cuộn
    await page.evaluate("window.scrollBy(0, 600);")

    used = "body(fallback)"
    html_inner = None
    for sel in CONTENT_SELECTORS:
        try:
            await page.wait_for_selector(sel, state="visible", timeout=30000)
            node = await page.query_selector(sel)
            if node:
                html_inner = await node.inner_html()
                used = sel
                break
        except Exception:
            continue
    if not html_inner:
        node = await page.query_selector("body")
        html_inner = await node.inner_html() if node else ""

    title = await page.title()
    await browser.close()

    # Lưu html + txt
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_title = _sanitize_filename(title)
    html_path = out_dir / f"{safe_title}.html"
    txt_path  = out_dir / f"{safe_title}.txt"

    wrapper = f"""<!doctype html><meta charset="utf-8">
<!-- grabbed via Playwright; sel: {used} -->
<title>{title}</title>
<body>{html_inner}</body>"""

    html_path.write_text(wrapper, encoding="utf-8")

    # rút text đơn giản
    from bs4 import BeautifulSoup as _BS
    tx = _BS(html_inner, "html.parser").get_text("\n", strip=True)
    txt_path.write_text(tx, encoding="utf-8")

    return html_path

async def run(story_url: str):
    # chuẩn hoá host
    if story_url.startswith("https://3020.devshop.vn/story/"):
        story_url = story_url.replace("https://3020.devshop.vn/story/","https://metruyen.xyz/story/")

    info = get_book_info(story_url)
    print(f"[INFO] {info.get('title','(không tiêu đề)')} — {info.get('author','')} — {info.get('genres','')}")

    chaps = get_list_chapters(story_url)
    print(f"[CHAPS] Tìm thấy: {len(chaps)}")

    if not chaps:
        print("Không tìm thấy chương (API không trả list; kiểm tra lại URL).")
        return

    out_dir = Path("output") / _sanitize_filename(info.get("title","story"))
    async with async_playwright() as pw:
        for i, ch in enumerate(chaps, 1):
            print(f"Tải {i}/{len(chaps)}: {ch['title'] or ch['url']}")
            try:
                path = await fetch_chapter_with_browser(pw, ch["url"], out_dir)
                print(f" -> OK: {path.name}")
            except Exception as e:
                print(" -> LỖI:", e)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python grab_story_playwright.py <story_url>")
        sys.exit(1)
    asyncio.run(run(sys.argv[1]))
