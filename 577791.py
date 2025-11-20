'''
    Author : Hishiro
    Date: 13-11-2025
    
    Tải truyện tiếng trung tại 577791.com
    Lưu thành txt để sữ dụng cho tool dịch tự động bằng AI.
    Mỗi chương sẽ được lưu bằng 1 file txt
    thư mục lưu : output/Tentruyen ( Dịch tên tiếng trung thành tiếng việt không dấu)
'''
# -*- coding: utf-8 -*-
"""
Tool dịch bằng Gemini, tự cài thư viện nếu thiếu.
Chỉ cần có file .env cùng folder:
GEMINI_API_KEY=AIza... (API key của bạn)
"""

import sys
import subprocess

# ========= TỰ CÀI THƯ VIỆN NẾU THIẾU ========= #

REQUIRED_PACKAGES = [
    ("requests", "requests"),
    ("beautifulsoup4", "bs4"),
    ("python-dotenv", "dotenv"),
    ("google-genai", "google"),
]

def install_missing_packages():
    for package, module_name in REQUIRED_PACKAGES:
        try:
            __import__(module_name)
        except ImportError:
            print(f"[INFO] Chưa có gói '{package}', đang cài đặt...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])

install_missing_packages()

# ========= IMPORT SAU KHI ĐẢM BẢO ĐỦ LIB ========= #

import os
import time
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

# ========= CẤU HÌNH GEMINI ========= #

# Tải biến môi trường từ file .env
load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise ValueError(
        "Không tìm thấy GEMINI_API_KEY trong biến môi trường.\n"
        "Hãy tạo file .env cùng thư mục với nội dung:\n"
        "GEMINI_API_KEY=AIza....(API key của bạn)"
    )

# Khởi tạo client Gemini
client = genai.Client(api_key=API_KEY)

def translate_text(text: str, source_lang: str = "", target_lang: str = "vi") -> str:
    """
    Dịch đoạn text sang tiếng Việt bằng Gemini.
    - text: đoạn cần dịch
    - source_lang: ngôn ngữ gốc (có thể để trống, Gemini tự đoán)
    - target_lang: ngôn ngữ đích (mặc định: 'vi' = tiếng Việt)
    """
    prompt = f"""
Bạn là dịch giả chuyên nghiệp.
Hãy dịch đoạn văn sau sang TIẾNG VIỆT, giữ nguyên tên riêng, giữ đúng nghĩa, văn phong tự nhiên:

[NGÔN NGỮ GỐC]: {source_lang if source_lang else "Không rõ, tự nhận diện"}
[ĐOẠN GỐC]:
{text}

[BẢN DỊCH TIẾNG VIỆT]:
"""

    response = client.models.generate_content(
        model="gemini-2.0-flash",   # có thể đổi thành gemini-1.5-pro nếu bạn muốn
        contents=prompt
    )

    return response.text.strip()

# ========= VÍ DỤ DÙNG THỬ / CLI ========= #

def main():
    print("=== Tool dịch Gemini (gõ trống để thoát) ===")
    while True:
        print("\nNhập đoạn cần dịch:")
        src = input("> ").strip()
        if not src:
            print("Thoát.")
            break

        print("\nĐang dịch, vui lòng đợi...")
        try:
            vi = translate_text(src)
            print("\n[BẢN DỊCH]:")
            print(vi)
        except Exception as e:
            print("Có lỗi xảy ra khi gọi API:", e)

if __name__ == "__main__":
    main()

    

