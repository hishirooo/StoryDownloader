# Yêu cầu downloader Piaotia

File này dùng để nhắc lại yêu cầu khi cần tạo hoặc cập nhật downloader cho `piaotia.com`.

## Trang mẫu

- Trang info: `https://www.piaotia.com/bookinfo/10/10902.html`
- Trang mục lục: `https://www.piaotia.com/html/10/10902/index.html`
- Trang chương mẫu: `https://www.piaotia.com/html/10/10902/7839687.html`

## Chức năng bắt buộc

- Lấy thông tin truyện: tên, tác giả, thể loại/trạng thái nếu có, cover, giới thiệu, mục lục và URL chương.
- Tải nội dung từng chương và lưu HTML ngay sau khi tải xong.
- Có cache HTML: chương đã có file thì bỏ qua, trừ khi có chế độ force.
- Tải cover bằng Pillow, convert sang RGB JPEG và resize để EPUB/Kobo đọc tốt.
- Tạo EPUB thủ công bằng `zipfile`, không dùng ebooklib/epublib hay thư viện EPUB ngoài.
- Thư viện ngoài phải import bằng `try/except`; nếu thiếu thì tự gọi `pip install`.
- Log đầy đủ các bước: lấy info, mục lục, cover, từng chương, cache, lưu file, tạo EPUB.
- Log tải chương dùng format chung:
  `[XXX/YYY] [HTTP=200] Chương 0011/0054: 第11章 去眉山居`
- Phần `HTTP=...` phải có màu theo nhóm status: 2xx xanh lá, 3xx xanh cyan, 4xx vàng, 5xx/ERR đỏ, CACHE xanh cyan.

## Menu yêu cầu

```text
Nhập URL:
[1] Tải tất cả ( html + epub ) ( Mặc định )
[2] Tải từ chương X tới chương Y
[3] Tải chương X
[4] Thoát.
```

Sau khi tải/tạo xong:

```text
[1] Nhập Url mới
[2] Thoát ( Mặc định )
```

## Ghi chú kỹ thuật Piaotia

- Encoding trang thường là `gbk` hoặc `gb2312`; nên detect từ `meta charset` rồi fallback `gb18030`.
- Mục lục chính nằm trong `.centent`, link chương dạng `7839675.html`.
- Trang chương có HTML không chuẩn: nội dung nên trích từ HTML thô giữa `div.toplink` và `div.bottomlink` hoặc comment `翻页上AD开始`.
- Cover thường có dạng `/files/article/image/<cat>/<book>/<book>s.jpg`.
- Output nên nằm trong `output/<Tên truyện>/` gồm `book_info.txt`, `book_info.json`, `chapters.json`, `progress.json`, `cover.jpg`, `chapter_0001.html`, và EPUB.

## File hiện tại

- Script: `piaotia.py`
- Default URL: `https://www.piaotia.com/bookinfo/10/10902.html`
