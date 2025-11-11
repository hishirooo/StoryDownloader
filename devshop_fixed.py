# -*- coding: utf-8 -*-
import os, re, json, requests
from pathlib import Path
from typing import Dict
from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

# === Added helper utilities (robust key extraction) ===
def _pick(d: dict, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return None

def _deep_find(d, *keys):
    stack = [d]
    seen = set()
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, dict):
            v = _pick(cur, *keys)
            if v:
                return v
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None
# === End helpers ===

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 15
PASS = "devshop"
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

def dump(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(data, (dict, list)):
                json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                f.write(str(data))
    except Exception:
        pass

def _base_from_url(url: str):
    m = re.match(r"https?://[^/]+", url)
    return m.group(0) if m else ""

def _fetch_html(url: str):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    return BeautifulSoup(r.text, "html.parser")

def _aes_encrypt_openssl_str(s: str, password: str) -> str:
    key = password.encode("utf-8")[:16]
    iv = get_random_bytes(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pad = 16 - len(s.encode("utf-8")) % 16
    ct = cipher.encrypt(s.encode("utf-8") + bytes([pad])*pad)
    import base64
    return base64.b64encode(iv + ct).decode("utf-8")

def _aes_decrypt_openssl_to_bytes(enc: str, password: str) -> bytes:
    import base64
    b = base64.b64decode(enc)
    iv, ct = b[:16], b[16:]
    key = password.encode("utf-8")[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    dec = cipher.decrypt(ct)
    pad = dec[-1]
    return dec[:-pad]

def _get_chapter_via_api(chap_url: str) -> Dict[str, str]:
    base = _base_from_url(chap_url)
    slug = chap_url.split("/chapter/")[-1]
    api = f"{base}/api/chapters/get-chapter"
    headers = {**HEADERS, "Origin": base, "Referer": chap_url, "Content-Type": "application/json"}
    payload = {"slug": _aes_encrypt_openssl_str(slug, PASS)}
    r = requests.post(api, json=payload, headers=headers, timeout=TIMEOUT)
    dump(DEBUG_DIR / f"get_{slug}.json", r.text)
    try:
        js = r.json()
    except Exception:
        return {}
    enc = js.get("data")
    if not enc:
        return {}
    try:
        raw = _aes_decrypt_openssl_to_bytes(enc, PASS).decode("utf-8", "ignore")
        data = json.loads(raw)
    except Exception:
        return {}
    if isinstance(data, list) and data:
        data = data[0]
    d = data.get("chapter") or data
    return {
        "title": _pick(d, "title", "name"),
        "content_comp": _pick(d, "content_comp", "contentEncrypt", "content", "contentComp"),
        "key_encrypt": _pick(d, "key_encrypt", "keyEncrypt", "key", "encryptKey", "password"),
    }

def _get_chapter_via_html(chap_url: str) -> Dict[str, str]:
    soup = _fetch_html(chap_url)
    node = soup.find("script", id="__NEXT_DATA__", type="application/json") or soup.find("script", id="__NEXT_DATA__")
    if not node or not node.string:
        return {}
    data = json.loads(node.string)
    dump(DEBUG_DIR / "chapter_nextdata.json", data)
    pp = data.get("props", {}).get("pageProps", {}) if isinstance(data, dict) else {}
    ch = (
        (pp.get("chapter") if isinstance(pp, dict) else None)
        or (pp.get("data", {}).get("chapter") if isinstance(pp.get("data", {}), dict) else None)
        or (pp.get("data") if isinstance(pp.get("data", {}), dict) else None)
        or {}
    )
    if not isinstance(ch, dict):
        ch = {}
    title = _pick(ch, "title", "name")
    content_comp = _pick(ch, "content_comp", "contentEncrypt", "content", "contentComp")
    key_encrypt = _pick(ch, "key_encrypt", "keyEncrypt", "key", "encryptKey", "password")
    if (not content_comp or not key_encrypt) and isinstance(data, dict):
        if not content_comp:
            content_comp = _deep_find(data, "content_comp", "contentEncrypt", "content", "contentComp")
        if not key_encrypt:
            key_encrypt = _deep_find(data, "key_encrypt", "keyEncrypt", "key", "encryptKey", "password")
        if not title:
            title = _deep_find(data, "title", "name")
    return {"title": title, "content_comp": content_comp, "key_encrypt": key_encrypt}

def get_chapter(chap_url: str) -> Dict[str, str]:
    d = _get_chapter_via_api(chap_url)
    if not d.get("content_comp") or not d.get("key_encrypt"):
        h = _get_chapter_via_html(chap_url)
        for k, v in h.items():
            if v and not d.get(k):
                d[k] = v
    if not d.get("content_comp") or not d.get("key_encrypt"):
        raise RuntimeError("Chưa có nội dung (thiếu content_comp / key_encrypt).")
    return d

def main():
    print("Script loaded successfully.")

if __name__ == "__main__":
    main()
