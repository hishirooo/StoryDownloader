# -*- coding: utf-8 -*-
# Debug: Kiểm tra raw HTML của chapter 2
import os, re, html, sys
sys.path.insert(0, r'e:\Make_Installer\MyTool\StoryDownloader')

import importlib.util
spec = importlib.util.spec_from_file_location('shizongzui', r'e:\Make_Installer\MyTool\StoryDownloader\shizongzui.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from bs4 import BeautifulSoup, NavigableString, Tag
from copy import deepcopy

url = "https://www.shizongzui.com/diyibu/diyijuan/31.html"
soup = mod._fetch_html(url)

# In raw HTML của span12
content_div = soup.find("div", class_="span12")
raw_html = str(content_div)

# Ghi ra file để xem
with open("debug_raw.html", "w", encoding="utf-8") as f:
    f.write(raw_html)
sys.stdout.buffer.write(b"=== Raw HTML saved to debug_raw.html ===\n")

# Thử extract content với method mới
content_div2 = deepcopy(content_div)

# Xóa các element rác
for tag_name, attrs in [
    ("ul",   {"class": "breadcrumb"}),
    ("ul",   {"class": "pager"}),
    ("div",  {"class": "page-header"}),
    ("div",  {"class": "pagination"}),
    ("div",  {"class": "navbar"}),
    ("div",  {"class": "footer"}),
    ("script", {}),
    ("style",  {}),
]:
    for elem in list(content_div2.find_all(tag_name, attrs)):
        if elem.parent:
            elem.decompose()

# In số descendants sau khi xóa
descendants = list(content_div2.descendants)
sys.stdout.buffer.write(f"Descendants count after cleanup: {len(descendants)}\n".encode())

text_nodes = [n for n in descendants if isinstance(n, NavigableString) and str(n).strip()]
br_nodes   = [n for n in descendants if isinstance(n, Tag) and n.name == "br"]
sys.stdout.buffer.write(f"Text nodes: {len(text_nodes)}, BR nodes: {len(br_nodes)}\n".encode())

# In 5 text nodes đầu tiên
for i, n in enumerate(text_nodes[:5]):
    sys.stdout.buffer.write(f"  TextNode[{i}]: {repr(str(n)[:80])}\n".encode('utf-8'))

# Thử method string replace
inner_html = str(content_div2)
# Thay <br> bằng \n
inner_html2 = re.sub(r'<br\s*/?>', '\n', inner_html)
bs2 = BeautifulSoup(inner_html2, "html.parser")
full_text = bs2.get_text()
lines = [l.strip() for l in full_text.split('\n') if l.strip()]
sys.stdout.buffer.write(f"\nLines after br->newline method: {len(lines)}\n".encode())
for l in lines[:5]:
    sys.stdout.buffer.write(f"  {repr(l[:80])}\n".encode('utf-8'))
