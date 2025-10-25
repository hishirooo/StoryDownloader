import re, html, requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ---------- Base helpers ----------
def _fetch_html(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def _text(el):
    return (el.get_text(strip=True) if el else "").strip()

# ---------- CSS helpers ----------
_IMPORT_RE = re.compile(r'@import\s+(?:url\()?["\']?([^"\')]+)["\']?\)?\s*;', re.I)

def _css_decode_content(s: str) -> str:
    s = s.strip()
    s = s.replace(r"\A", "\n").replace(r"\a", "\n")
    def repl_hex(m):
        try:
            return chr(int(m.group(1), 16))
        except Exception:
            return m.group(0)
    s = re.sub(r"\\([0-9a-fA-F]{1,6})\s?", repl_hex, s)
    s = s.replace(r"\'", "'").replace(r"\"", '"').replace(r"\\", "\\")
    return s

def _collect_css_texts(soup: BeautifulSoup, base_url: str):
    css_texts = []

    # 1) inline <style>
    for st in soup.find_all("style"):
        if st.string:
            css_texts.append(st.string)

    # 2) <link> gồm cả rel=stylesheet / preload as=style / href *.css
    for link in soup.find_all("link"):
        rel = (link.get("rel") or [])
        rel = [x.lower() for x in rel]
        as_attr = (link.get("as") or "").lower()
        href = link.get("href")
        if not href:
            continue
        take = False
        if any("stylesheet" in x for x in rel):
            take = True
        if ("preload" in rel and as_attr == "style"):
            take = True
        if href.endswith(".css"):
            take = True
        if not take:
            continue

        css_url = urljoin(base_url, href)
        try:
            r = requests.get(css_url, headers=HEADERS, timeout=30)
            if r.ok:
                text = r.text
                css_texts.append(text)
                # 3) theo @import đệ quy (một tầng là đủ ở đây)
                for imp in _IMPORT_RE.findall(text):
                    imp_url = urljoin(css_url, imp)
                    try:
                        r2 = requests.get(imp_url, headers=HEADERS, timeout=30)
                        if r2.ok:
                            css_texts.append(r2.text)
                        # không cần sâu quá nhiều tầng
                    except requests.RequestException:
                        pass
        except requests.RequestException:
            pass

    return css_texts

def _build_span_map_from_css(css_texts):
    """
    Hỗ trợ:
      .abc::before { content: "x" "y" "\006B\0068\00F4\006E\0067" }
      .abc:after  { content: '...' !important }
      .a:before,.b:before{content:'...'}
    """
    mapping = {}

    # Bóc từng block có thuộc tính content:
    block_re = re.compile(r'(?P<selectors>[^{]+){(?P<body>[^{}]*content\s*:[^;]+;[^}]*)}', re.S)
    str_token_re = re.compile(r'("([^"]*)"|\'([^\']*)\')')  # lấy tất cả chuỗi trong content:

    for css in css_texts:
        for blk in block_re.finditer(css):
            selectors = blk.group("selectors")
            body = blk.group("body")

            # Chỉ quan tâm :before / ::before / :after / ::after
            sel_list = [s.strip() for s in selectors.split(",")]
            sel_classes = []
            for s in sel_list:
                m = re.search(r'\.([A-Za-z0-9_-]+)\s*::?be?fore\b', s)
                if not m:
                    m = re.search(r'\.([A-Za-z0-9_-]+)\s*::?after\b', s)
                if m:
                    sel_classes.append(m.group(1))
            if not sel_classes:
                continue

            # Lấy tất cả string tokens trong phần content:
            joined = ""
            for sm in str_token_re.finditer(body):
                piece = sm.group(2) if sm.group(2) is not None else sm.group(3)
                joined += _css_decode_content(piece)

            if not joined:
                continue

            for cls in sel_classes:
                # chỉ gán nếu chưa có (ưu tiên rule cụ thể trước; nếu muốn ghi đè thì bỏ if)
                if cls not in mapping:
                    mapping[cls] = joined
    return mapping

def _replace_spans_with_text(root: BeautifulSoup, cls_map: dict):
    container = root.select_one("div#chapter-content-render")
    if not container:
        return
    for sp in container.find_all("span"):
        classes = sp.get("class") or []
        buf = []
        for c in classes:
            t = cls_map.get(c)
            if t:
                buf.append(t)
        if buf:
            sp.replace_with("".join(buf))

def _html_to_text(p_tag):
    for br in p_tag.find_all("br"):
        br.replace_with("\n")
    txt = p_tag.get_text().replace("\xa0", " ")
    return txt.strip("\n")

# ---------- Public API ----------
def _get_content_from_chapter_url(url: str) -> str:
    soup = _fetch_html(url)

    title = _text(soup.find("h1", class_="card-title"))
    print("Chapter title:", title)

    css_texts = _collect_css_texts(soup, url)
    cls_map = _build_span_map_from_css(css_texts)

    # (debug) nếu thiếu map, bạn có thể in ra vài class đầu tiên
    # print("map size:", len(cls_map))
    # print(list(cls_map.items())[:10])

    _replace_spans_with_text(soup, cls_map)

    ps = soup.select("div#chapter-content-render p")
    blocks = []
    for p in ps:
        t = _html_to_text(p)
        if t.strip() and t.strip() != "---":
            blocks.append(t)
    return "\n\n".join(blocks)


# --- Ví dụ dùng ---
text = _get_content_from_chapter_url("https://monkeydtruyen.com/vo-chong-phao-hoi-hom-nay-muon-lam-giau/chuong-1.html")
print(text)
