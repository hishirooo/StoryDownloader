import requests


import os, re, html, unicodedata, ssl, time, random
from typing import List, Dict, Optional, Tuple, Set

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin

url = "https://truyen.tangthuvien.vn/story/chapters"

querystring = {"story_id":"38786"}

payload = ""
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
}

response = requests.request("GET", url, data=payload, headers=headers, params=querystring)

doc = BeautifulSoup(response.text, "html.parser")
chapters = []

for a_tag in doc.find_all('a'):
    link = a_tag.get('href').strip()
    title = a_tag.get('title')
    chapters.append({'title': title, 'link': link})
print(len(chapters))
# In kết quả
# for chap in chapters:
#     print(f"{chap['title']} -> {chap['link']}")
