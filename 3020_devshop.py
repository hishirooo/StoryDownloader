# -*- coding: utf-8 -*-
"""
3020_devshop_uc_full.py

Tích hợp:
- Lấy danh sách chương (API AES/OpenSSL) bằng requests + BeautifulSoup
- Lấy nội dung chương bằng undetected-chromedriver (Selenium) với bypass anti-bot

Hướng dẫn:
    pip install undetected-chromedriver selenium beautifulsoup4 requests pycryptodome webdriver-manager
    # nếu cần: tải chromedriver tương ứng hoặc để webdriver-manager tự tải

Chạy:
    python 3020_devshop_uc_full.py

Nhập URL truyện khi được yêu cầu.
"""

import os
import re
import time
import html
import base64
import hashlib
import json
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
import unicodedata
# === Cấu hình chung ===
TIMEOUT = 20
SLEEP_BETWEEN_PAGES = 0.2

# ===== tiện ích nhỏ =====

def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _slugify(text: str, maxlen: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "truyen")[:maxlen]

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
    enc = cipher.encrypt(plaintext.encode() + bytes([pad]) * pad)
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
        mid = (len(obf) + 1) // 2
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


def get_book_info(soup: BeautifulSoup) -> Dict[str, str]:
    title = _text(soup.find("h1", class_="story_book-info__title__1jpSQ"))
    author = _text(soup.find("div", class_="story_book-info__author__lPhnG")).replace("Tác giả: ", "")
    genres = " - ".join(_text(a) for a in soup.select(".story_book-info__category__B1RPT a"))
    cover_node = soup.select_one(".story_book__qq6xd img")
    cover = ""
    if cover_node and cover_node.get("src"):
        cover = _api_base(cover_node.get("src")) + cover_node["src"] if cover_node["src"].startswith("/") else cover_node["src"]
    desc = _text(soup.find("div", class_="story_card-content__NO3Br"))
    return {"title": title, "author": author, "genre": genres, "desc": desc, "cover": cover}


def get_list_chapters(story_url: str) -> List[Dict[str, str]]:
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
            chapters.append({"title": ch.get("name", ""), "url": url})

        if len(rows) < 50:
            break
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)
    return chapters

# ==========================
#  undetected-chromedriver bypass functions
# ==========================

import random
try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
except Exception:
    uc = None


def _mk_driver(headless: bool = False, page_load_timeout: int = 60):
    import undetected_chromedriver as uc
    from selenium.common.exceptions import WebDriverException

    opts = uc.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")

    opts.add_argument("--start-maximized")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )
    # KHÔNG set excludeSwitches / useAutomationExtension để tránh InvalidArgumentException

    def _launch(o):
        return uc.Chrome(options=o)

    try:
        driver = _launch(opts)
    except WebDriverException as e:
        if headless and "--headless=new" in " ".join(opts.arguments):
            opts2 = uc.ChromeOptions()
            for a in opts.arguments:
                if a != "--headless=new":
                    opts2.add_argument(a)
            opts2.add_argument("--headless")  # fallback headless cũ
            driver = _launch(opts2)
        else:
            raise e

    driver.set_page_load_timeout(page_load_timeout)
    return driver



def _human_pause(a=0.35, b=0.9):
    time.sleep(random.uniform(a, b))


def _slow_scroll(driver, total_ms=1500):
    t0 = time.time()
    try:
        h = driver.execute_script("return document.body.scrollHeight || 2000;")
    except Exception:
        h = 2000
    y = 0
    while (time.time() - t0) * 1000 < total_ms:
        y += random.randint(120, 380)
        try:
            driver.execute_script(f"window.scrollTo(0,{min(y,h)});")
        except Exception:
            pass
        _human_pause(0.08, 0.2)


def _click_reload_if_needed(driver, wait: WebDriverWait, post_wait_range=(5.0, 10.0)) -> bool:
    """
    Gặp màn 'Slow down... Load lại' thì:
      - đợi >=3s theo yêu cầu site
      - click nút 'Load lại'
      - CHỜ THÊM 5–10s (có thể chỉnh qua post_wait_range) cho JS giải mã xong
    Trả về True nếu đã xử lý reload; False nếu không thấy banner.
    """
    try:
        btn = WebDriverWait(driver, 1.5).until(
            EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Load lại')]"))
        )
    except TimeoutException:
        return False

    # Đợi tối thiểu 3s trước khi bấm
    time.sleep(3.2)
    try:
        btn.click()
    except Exception:
        try:
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            return False

    # Sau khi click: CHỜ 5–10s để web tự render lại
    import random
    delay = random.uniform(*post_wait_range)
    time.sleep(delay)

    # Kéo nhẹ kích hoạt lazy-load
    try:
        driver.execute_script("window.scrollBy(0, 800);")
    except Exception:
        pass

    return True


def _extract_text(driver) -> str:
    # Try several likely containers, then fallback
    candidates = [
        ".chapter-content__read",
        ".chapter_chapter-content__read__4UYM5",
        ".chapter-content",
        ".reader-content",
        "article",
        "#chapter-content",
    ]
    for sel in candidates:
        try:
            js = f"const el = document.querySelector('{sel}'); if(!el) return ''; el.querySelectorAll('.word_63, .position-absolute.hidden, p.hidden').forEach(n=>n.remove()); return el.innerText.trim();"
            text = driver.execute_script(js)
            if text and len(text.strip()) > 100:
                return text.strip()
        except Exception:
            continue
    # fallback entire body
    try:
        return driver.execute_script("return document.body.innerText || '';").strip()
    except Exception:
        return ""

def get_content_chapters(
    chapter_url: str,
    headless: bool = True,
    max_attempts: int = 3,
    wait_seconds: int = 45,
    allow_headful_fallback: bool = True
) -> str:
    """
    Mở chương, tự xử lý banner 'Slow down... Load lại', đợi nội dung render.
    Khi chạy headless thất bại 2 lần liên tiếp, tự fallback sang headful để vượt chặn.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from bs4 import BeautifulSoup
    import random, os

    def _wait_ready(drv, timeout=30):
        WebDriverWait(drv, timeout).until(
            lambda x: x.execute_script("return document.readyState") == "complete"
        )

    def _human_pause(a=0.3, b=0.8):
        time.sleep(random.uniform(a, b))

    def _slow_scroll(drv, total_ms=1500):
        t0 = time.time()
        try:
            h = drv.execute_script("return document.body.scrollHeight || 2000;")
        except Exception:
            h = 2000
        y = 0
        while (time.time() - t0) * 1000 < total_ms:
            y += random.randint(160, 420)
            try:
                drv.execute_script(f"window.scrollTo(0, {min(y,h)});")
            except Exception:
                break
            _human_pause(0.06, 0.18)

    def _click_reload_if_needed(drv, wait) -> bool:
        # xử lý nhiều lần nếu banner còn hiện
        for _ in range(3):
            try:
                btn = WebDriverWait(drv, 2).until(
                    EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Load lại')]"))
                )
            except TimeoutException:
                return False
            time.sleep(3.2)  # web yêu cầu ≥3s
            try:
                btn.click()
            except Exception:
                drv.execute_script("arguments[0].click();", btn)
            _human_pause(0.5, 1.0)
            # chờ lại ready + cuộn 1 chút
            try:
                _wait_ready(drv, timeout=20)
            except Exception:
                pass
            _slow_scroll(drv, total_ms=900)
            # thử xem vẫn còn nút không, nếu không còn → break
            try:
                drv.find_element(By.XPATH, "//button[contains(., 'Load lại')]")
            except Exception:
                return True
        return True

    def _extract_text(drv) -> str:
        candidates = [
            ".chapter-content__read",
            ".chapter_chapter-content__read__",
            ".chapter-content",
            ".reader-content",
            "article",
            "#chapter-content",
        ]
        for sel in candidates:
            try:
                js = (
                    f"const el=document.querySelector('{sel}');"
                    "if(!el) return '';"
                    "el.querySelectorAll('.word_63, .position-absolute.hidden, p.hidden').forEach(n=>n.remove());"
                    "return el.innerText.trim();"
                )
                txt = drv.execute_script(js) or ""
                if len(txt) > 200:
                    return txt
            except Exception:
                continue
        try:
            return drv.execute_script("return document.body.innerText || ''") or ""
        except Exception:
            return ""

    # chạy nhiều attempt; nếu headless lỗi 2 lần đầu → thử headful 1 lần
    used_headless = headless
    for attempt in range(1, max_attempts + 1):
        try:
            if allow_headful_fallback and headless and attempt >= 3:
                print("↩️  Fallback sang headful (hiển thị) do headless liên tiếp thất bại.")
                used_headless = False

            driver = _mk_driver(headless=used_headless, page_load_timeout=60)
            print(f"🔗 Mở: {chapter_url}  (headless={used_headless})")
            driver.get(chapter_url)

            # đợi DOM ok, cuộn nhẹ để kích hoạt lazy
            try:
                _wait_ready(driver, timeout=30)
            except Exception:
                pass
            _slow_scroll(driver, total_ms=1000)

            # xử lý banner 'Load lại' nếu xuất hiện (lặp tối đa 3 lần internal)
            wait = WebDriverWait(driver, 25)
            _click_reload_if_needed(driver, wait)
            _slow_scroll(driver, total_ms=800)

            # chia nhỏ thời gian chờ: mỗi bước 5s, tổng ~wait_seconds
            remain = wait_seconds
            txt = ""
            while remain > 0:
                try:
                    WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((
                            By.CSS_SELECTOR,
                            ".chapter-content__read, .chapter_chapter-content__read__, "
                            ".chapter-content, .reader-content, article, #chapter-content"
                        ))
                    )
                except TimeoutException:
                    pass
                _slow_scroll(driver, total_ms=700)
                txt = _extract_text(driver)
                if len(txt) > 300:
                    break
                remain -= 5

            # debug save
            os.makedirs("debug", exist_ok=True)
            try:
                driver.save_screenshot(f"debug/chapter_screen_uc_{int(time.time())}.png")
                with open(f"debug/chapter_dom_uc_{int(time.time())}.html", "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
            except Exception:
                pass

            if len(txt) > 300:
                print("✅ Lấy nội dung xong.")
                # làm sạch xuống dòng dư
                txt = re.sub(r"\n\s*\n+", "\n\n", txt).strip()
                driver.quit()
                return txt

            print("⏳ Nội dung quá ngắn, thử lại attempt khác...")
            driver.quit()
            time.sleep(random.uniform(1.5, 3.0))

        except (TimeoutException, WebDriverException) as e:
            print("⚠️ Lỗi điều hướng/đợi:", e)
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(random.uniform(2.0, 4.0))
            continue
        except Exception as e:
            print("❌ Lỗi không mong muốn:", e)
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(2)
            continue

    raise TimeoutException("Hết số lần thử mà vẫn chưa lấy được nội dung.")


import unicodedata
from pathlib import Path

def _slugify(text: str, maxlen: int = 80) -> str:
    """Slug ASCII an toàn cho tên file."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if not text:
        text = "chapter"
    return text[:maxlen]

def _text_to_xhtml(title: str, body_text: str) -> str:
    """Đóng gói nội dung chương thành XHTML hợp lệ cho EPUB."""
    # tách đoạn theo 2 xuống dòng
    paras = [p.strip() for p in re.split(r"\n\s*\n", body_text) if p.strip()]
    p_html = "\n".join(f"<p>{html.escape(p)}</p>" for p in paras)
    return f"""<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="vi" xml:lang="vi">
  <head>
    <meta charset="utf-8" />
    <title>{html.escape(title)}</title>
  </head>
  <body>
    <h1>{html.escape(title)}</h1>
    <div class="content">
{p_html}
    </div>
  </body>
</html>""".strip()

def save_chapter_xhtml(out_dir: Path, index: int, title: str, text: str) -> Path:
    """Lưu 1 chương ra file XHTML: 003-ten-chuong.xhtml"""
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(title or f"chuong-{index}")
    fname = f"{index:03d}-{slug}.xhtml"
    xhtml = _text_to_xhtml(title or f"Chương {index}", text)
    path = out_dir / fname
    path.write_text(xhtml, encoding="utf-8", newline="\n")
    return path

def download_chapters_as_xhtml(
    story_url: str,
    headless: bool = True,
    start: int = 1,
    end: int | None = None,
    wait_seconds: int = 40,
    per_session: int = 1,  # 1 chương/ phiên (đúng yêu cầu hiện tại)
    sleep_between: tuple[float, float] = (1.2, 2.2),  # nghỉ giữa chương
):
    """
    Tải các chương và lưu dạng XHTML chuẩn EPUB.
    - headless: True để chạy ẩn (ổn định hơn khi batch)
    - per_session: số chương xử lý trong cùng một phiên (giữ mặc định = 1)
    """
    # chuẩn hoá host
    if story_url.startswith("https://3020.devshop.vn/story/"):
        story_url = story_url.replace("https://3020.devshop.vn/story/", "https://metruyen.xyz/story/")

    soup = _fetch_html(story_url)
    info = get_book_info(soup)
    book_title = info.get("title") or "truyen"
    print(f"[INFO] {book_title} — {info.get('author','')} — {info.get('genre','')}")

    chaps = get_list_chapters(story_url)
    total = len(chaps)
    print(f"[CHAPS] Tìm thấy {total} chương")
    if total == 0:
        print("❌ Không tìm thấy chương.")
        return

    if end is None or end > total:
        end = total
    start = max(1, start)
    if start > end:
        start, end = 1, total

    out_root = Path("output") / _slugify(book_title, 60)
    out_dir = out_root / "chapters"
    out_dir.mkdir(parents=True, exist_ok=True)

    ok = 0
    fail = 0

    batch = []
    for idx, ch in enumerate(chaps[start-1:end], start=start):
        batch.append((idx, ch))
        # xử lý theo lô (per_session), hiện bạn muốn =1
        if len(batch) < per_session:
            # gom đủ rồi mới chạy, nếu per_session=1 thì chạy ngay bên dưới
            pass

        # chạy batch
        if len(batch) == per_session or idx == end:
            for i, chapter in batch:
                title = (chapter.get("title") or f"Chương {i}").strip()
                url   = chapter.get("url")
                print(f"→ [{i}/{total}] {title} — {url}")

                try:
                    # mỗi chương mở 1 trình duyệt mới (chính là bên trong get_content_chapters)
                    text = get_content_chapters(url, headless=headless, wait_seconds=wait_seconds)
                    if not text or len(text) < 100:
                        raise RuntimeError("Nội dung rỗng/ quá ngắn.")

                    path = save_chapter_xhtml(out_dir, i, title, text)
                    ok += 1
                    print(f"   ✓ Đã lưu: {path.relative_to(Path.cwd())}")
                except Exception as e:
                    fail += 1
                    print(f"   ✗ LỖI: {e}")

                # nghỉ ngẫu nhiên giữa chương để tránh bị chặn
                delay = random.uniform(*sleep_between)
                time.sleep(delay)

            batch = []  # clear lô

    print(f"\n== TÓM TẮT ==")
    print(f"✓ Thành công: {ok}  |  ✗ Thất bại: {fail}")
    print(f"📁 Thư mục: {out_dir}")
    print("👉 Các file .xhtml này đã sẵn sàng để build EPUB (chỉ cần đóng gói theo chuẩn OPS/OEBPS).")


# ================= DEMO CLI =================
if __name__ == "__main__":
    story = input("URL truyen: ").strip()
    if story.startswith("https://3020.devshop.vn/story/"):
        story = story.replace("https://3020.devshop.vn/story/", "https://metruyen.xyz/story/")

    try:
        soup = _fetch_html(story)
    except Exception as e:
        print("❌ Không thể tải trang truyện:", e)
        raise

    info = get_book_info(soup)
    print("---- THÔNG TIN ----")
    for k, v in info.items():
        print(f"{k}: {v}")

    print("\n---- DANH SÁCH CHƯƠNG ----")
    chaps = get_list_chapters(story)
    print(f"Tổng số chương: {len(chaps)}")
    for ch in chaps[:10]:
        print(f"- {ch['title']} -> {ch['url']}")

    download_chapters_as_xhtml(
        story_url=story,
        headless=False,
        start=1,
        end=None,       # toàn bộ
        wait_seconds=10,
        per_session=1,  # 1 chương / phiên (giảm fingerprint)
        sleep_between=(1.5, 3.0),
    )
