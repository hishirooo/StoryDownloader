# -*- coding: utf-8 -*-
"""
Plugin cho domain: truyenfull.vision
- Lấy meta truyện (title/author/genres) và toàn bộ danh sách chương (mọi trang)
- Tải nội dung từng chương, lưu .html
- Tải ảnh bìa và đóng gói EPUB (metadata đầy đủ)
"""

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from datetime import datetime, timezone
import requests, re, time, os, html, unicodedata, zipfile
from typing import Optional


def getinfo(url: str) -> dict:
    """Lấy thông tin truyện và danh sách chương từ tangthuvien.vn"""
    soup = _fetch_html(url)
    book_info = _get_book_info(soup) # "title": title, "author": author, "genre": genre, "status": status
    chapters = _get_list_chapters(soup)
    #book_info["chapters"] = chapters
    #
    print("-----------------THÔNG TIN TRUYỆN:-------------------")
    print(f"Title :  {book_info['title']}")
    print(f"Author: {book_info['author']}")
    print(f"Genre :  {book_info['genre']}")
    print(f"Status: {book_info['status']}")
    print(f"Đã lấy {len(chapters)} link chương.")
    
def _fetch_html(url: str) -> BeautifulSoup:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
    }
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return BeautifulSoup(r.text, "html.parser")
def _get_book_info(soup: BeautifulSoup) -> dict:
    # title = soup.select_one("h1", class_= "book-info").get_text(strip=True)
    # author_tag = soup.select_one("p",class_="tag")
    # author = author_tag.find("a",class_="blue").get_text(strip=True) if author_tag else "N/A"
    # genre = author_tag.find("a", class_="red").get_text(strip=True) if author_tag else "N/A" 
    # #status = author_tag.select_one('div.book-info p.blue span')
    book_info = soup.find('div', class_='book-info')
    title = book_info.find('h1').get_text(strip=True)
    tag_section = book_info.find('p', class_='tag')
    author = tag_section.find('a', class_='blue').get_text(strip=True)
    status = tag_section.find('span', class_='blue').get_text(strip=True)
    genre = tag_section.find('a', class_='red').get_text(strip=True)

    return {
        "title": title,
        "author": author,
        "genre": genre,
         "status": status
    }
    

def _get_list_chapters(soup: BeautifulSoup) -> list:
    chapters = []
    chapter_list_div = soup.find("div", class_="chapter-list")
    if not chapter_list_div:
        return chapters
    for a_tag in chapter_list_div.find_all("a", href=True):
        link = urljoin("https://truyen.tangthuvien.vn", a_tag["href"])
        title = a_tag.get_text(strip=True)
        chapters.append({"title": title, "link": link})
    return chapters         
    
    
getinfo("https://truyen.tangthuvien.vn/doc-truyen/song-sot-trong-tro-choi-voi-tu-cach-mot-barbarian")