
# -*- coding: utf-8 -*-
import os, re, time, html, base64, hashlib, json
from typing import List, Dict
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

TIMEOUT  = 20
SLEEP_BETWEEN_PAGES = 0.2

def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _fetch_html(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=_make_headers(url), timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "html.parser")

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

def _aes_decrypt_openssl_raw(b64: str, passphrase: str) -> bytes:
    raw = base64.b64decode(b64)
    if not raw.startswith(b"Salted__"):
        raise ValueError("Không đúng format OpenSSL (Salted__).")
    salt = raw[8:16]
    key, iv = _openssl_bytes_to_key(passphrase.encode(), salt, 32, 16)
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:])
    pad = dec[-1]
    if pad < 1 or pad > 16:
        raise ValueError("Padding không hợp lệ.")
    return dec[:-pad]

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

def _base_host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url)
    host = m.group(1).lower() if m else "3020.devshop.vn"
    # Hợp thức: 3020.devshop.vn hoặc metruyen.xyz
    if "metruyen" in host:
        return "metruyen.xyz"
    return "3020.devshop.vn"

def _api_base(url: str) -> str:
    host = _base_host(url)
    return f"https://{host}"

def _make_headers(url: str) -> Dict[str, str]:
    base = _api_base(url)
    return {
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": base,
        "Referer": url if "/story/" in url or "/chapter/" in url else base,
    }

def get_story_id(soup: BeautifulSoup) -> int:
    tag = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if tag and tag.string:
        data = json.loads(tag.string)
        return int(data["props"]["pageProps"]["story"]["id"])
    raise RuntimeError("Không tìm thấy story.id")

def get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    title = _text(soup.find("h1", class_="story_book-info__title__1jpSQ"))
    author = _text(soup.find("div", class_="story_book-info__author__lPhnG")).replace("Tác giả: ","")
    genres = " - ".join(_text(a) for a in soup.select(".story_book-info__category__B1RPT a"))
    cover_node = soup.select_one(".story_book__qq6xd img")
    cover = ""
    if cover_node and cover_node.get("src"):
        cover = _api_base(cover_node.get("src")) + cover_node["src"] if cover_node["src"].startswith("/") else cover_node["src"]
    desc = _text(soup.find("div", class_="story_card-content__NO3Br"))
    return {"title": title, "author": author, "genre": genres, "desc": desc, "cover": cover}

def get_list_chapters(story_url: str) -> List[Dict[str,str]]:
    soup = _fetch_html(story_url)
    sid = get_story_id(soup)
    api = _api_base(story_url) + "/api/chapters/list-chapters"

    chapters, page = [], 1
    while True:
        payload = {
            "id_story": _aes_encrypt_openssl(str(sid), PASS),
            "page": _aes_encrypt_openssl(str(page), PASS),
            "items_per_page": _aes_encrypt_openssl("50", PASS),
            "order": _aes_encrypt_openssl("asc", PASS),
        }
        r = requests.post(api, json=payload, headers=_make_headers(story_url), timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        raw = _aes_decrypt_openssl_raw(js["data"], PASS).decode("utf-8", "ignore")

        try:
            data = json.loads(raw)
        except Exception:
            data = []

        rows = data if isinstance(data, list) else (data.get("chapters") or data.get("data") or [])
        if not rows:
            break

        for ch in rows:
            slug = ch.get("slug") or ch.get("slug_chapter") or ""
            url = f"{_api_base(story_url)}/chapter/{slug}" if slug else ""
            chapters.append({"title": ch.get("name",""), "url": url})

        if len(rows) < 50:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters

def _aes_cbc_decrypt_b64_ivprefix(b64_ct: str, key_bytes: bytes) -> str:
    """content_comp dạng base64, 16 byte đầu là IV"""
    raw = base64.b64decode(b64_ct)
    iv, enc = raw[:16], raw[16:]
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    dec = cipher.decrypt(enc)
    pad = dec[-1]
    if pad < 1 or pad > 16:
        raise ValueError("Padding không hợp lệ.")
    return dec[:-pad].decode("utf-8", "ignore")

def get_chapter_content_from_html(chapter_url: str) -> Dict[str, str]:
    soup = _fetch_html(chapter_url)
    node = soup.find("script", id="__NEXT_DATA__", type="application/json")
    if not node or not node.string:
        raise RuntimeError("Không tìm thấy __NEXT_DATA__")

    data = json.loads(node.string)
    pp = data["props"]["pageProps"]
    chap = pp.get("chapter") or {}
    title = chap.get("title") or chap.get("name") or ""

    content_comp = chap.get("content_comp") or pp.get("contentEncrypt") or chap.get("content")
    key_encrypt  = chap.get("key_encrypt")

    if not content_comp:
        raise RuntimeError("Chưa có nội dung.")

    # Cách A: seed = decrypt(key_encrypt, PASS)
    tried = []
    def try_decrypt_with_seed(seed_bytes: bytes) -> str:
        key = hashlib.sha256(seed_bytes).digest()
        return _aes_cbc_decrypt_b64_ivprefix(content_comp, key)

    if key_encrypt:
        try:
            seed = _aes_decrypt_openssl_raw(key_encrypt, PASS)
            tried.append("A")
            return {"title": title, "content": try_decrypt_with_seed(seed)}
        except Exception:
            pass

    # Cách B: dùng ri -> rs[index]
    ri = pp.get("ri"); rs = pp.get("rs")
    if key_encrypt and isinstance(rs, dict) and isinstance(ri, str):
        try:
            ri_num = int(_aes_decrypt_openssl_raw(ri, PASS).decode("utf-8", "ignore").strip() or "0")
            # chọn 1 khóa phụ từ rs theo modulo
            keys = sorted(rs.keys(), key=lambda x: int(x) if x.isdigit() else x)
            if keys:
                pick = rs[keys[ri_num % len(keys)]]
                # đôi khi pick là 1 lớp OpenSSL nữa -> seed
                seed = _aes_decrypt_openssl_raw(pick, PASS)
                tried.append("B1")
                return {"title": title, "content": try_decrypt_with_seed(seed)}
        except Exception:
            # fallback B2: pick có thể đã là seed thô (base64)
            try:
                seed = base64.b64decode(pick)
                tried.append("B2")
                return {"title": title, "content": try_decrypt_with_seed(seed)}
            except Exception:
                pass

    # Cách C: key_encrypt là base64 seed
    if key_encrypt:
        try:
            seed = base64.b64decode(key_encrypt)
            tried.append("C")
            return {"title": title, "content": try_decrypt_with_seed(seed)}
        except Exception:
            pass

    raise RuntimeError("Chưa có nội dung.")
# ================= PLAYWRIGHT FETCH CONTENT =================
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

async def fetch_chapter_content_browser(chapter_url: str, out_dir="debug_chapter"):
    from pathlib import Path
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,   # bật để debug
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print(f"[Browser] Mở chương: {chapter_url}")
        await page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)

        # === BƯỚC 1: PHÁT HIỆN NÚT LOAD LẠI ===
        load_btn = None
        try:
            load_btn = await page.wait_for_selector(
                "text=Load lại",
                timeout=5000
            )
            print("⚠️ Phát hiện trang anti-bot, chờ 3s rồi click...")
        except:
            print("✔ Không thấy nút Load lại (có thể đã pass)")

        # === BƯỚC 2: CHỜ & CLICK ===
        if load_btn:
            await page.wait_for_timeout(3200)  # BẮT BUỘC
            await load_btn.click()
            print("🖱 Đã click Load lại")

        # === BƯỚC 3: CHỜ NỘI DUNG THẬT ===
        await page.wait_for_timeout(2000)

        html_content = ""
        title = ""

        for i in range(30):
            title = await page.evaluate("""
                () => document.querySelector("h1")?.innerText || ""
            """)

            html_content = await page.evaluate("""
                () => {
                    const el = document.querySelector(
                        "div.chapter__content, #chapter__content"
                    );
                    if (el && el.innerText && el.innerText.trim().length > 200) {
                        return el.innerHTML;
                    }
                    return "";
                }
            """)

            if html_content:
                print(f"✔ Nội dung load thành công sau {i+1}s")
                break

            await page.wait_for_timeout(1000)

        await browser.close()

    if not html_content:
        raise RuntimeError("❌ Đã click Load lại nhưng vẫn không có nội dung")

    # === GHI FILE DEBUG ===
    safe_title = re.sub(r"[\\\\/:*?\"<>|]+", "_", title).strip() or "chapter"
    html_path = Path(out_dir) / f"{safe_title}.html"
    txt_path  = Path(out_dir) / f"{safe_title}.txt"

    html_path.write_text(
        f"""<!doctype html>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<body>{html_content}</body>
""",
        encoding="utf-8"
    )

    text = BeautifulSoup(html_content, "html.parser")\
            .get_text("\n", strip=True)

    txt_path.write_text(text, encoding="utf-8")

    print("✔ Đã ghi file:")
    print(f"  - {html_path}")
    print(f"  - {txt_path}")

    return {"title": title, "content": text}


# ================= DEMO DEBUG =================
if __name__ == "__main__":
    story = "https://metruyen.xyz/story/bat-dau-tu-trang-do"

    soup = _fetch_html(story)
    info = get_book_info(soup)

    print("---- THÔNG TIN ----")
    for k, v in info.items():
        print(f"{k}: {v}")

    print("\n---- DANH SÁCH CHƯƠNG ----")
    chaps = get_list_chapters(story)
    print(f"Tổng số chương: {len(chaps)}")

    for ch in chaps[:5]:
        print(f"- {ch['title']} -> {ch['url']}")

    if not chaps:
        print("❌ Không có chương")
        exit(0)

    # ===== TEST 1 CHƯƠNG =====
    print("\n---- DEBUG 1 CHƯƠNG ----")
    r = asyncio.run(
        fetch_chapter_content_browser(
            chaps[0]["url"],
            out_dir="debug_chapter"
        )
    )

    print("\nTiêu đề:", r["title"])
    print("\nNội dung (500 ký tự đầu):\n")
    print(r["content"][:500])
