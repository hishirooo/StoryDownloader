# Domain Adapter Config

Purpose: reusable checklist/spec for adding a new novel domain downloader.

## Adapter Contract

Each domain file should expose these stable functions:

```python
getText(url: str) -> dict
fetch_chapter_content(url: str, retries: int = 5, **kwargs) -> dict
download_chapters(book_info: dict, chapters: list[dict], book_dir: str | Path, *, start=1, end=None, force=False) -> list[dict]
build_epub(book_info: dict, chapters: list[dict], book_dir: str | Path, *, start=1, end=None, cover_bytes=None, cover_ext=None) -> Path
main() -> None
```

Return shape:

```python
book_info = {
    "title": "...",
    "author": "...",
    "status": "...",
    "category": "...",
    "update_time": "...",
    "latest_chapter": "...",
    "latest_chapter_url": "...",
    "intro": "...",
    "cover_url": "...",
    "url": "...",
}

chapter = {"title": "...", "url": "..."}

chapter_data = {
    "title": "...",
    "content_html": "<p>...</p>",
    "text": "...",
    "url": "...",
    "status_code": 200 | "CACHE" | "ERR",
}
```

## Implementation Steps

1. Constants:
   - `BASE_URL`, `DEFAULT_URL`, `OUTPUT_BASE`.
   - Browser-like headers, timeout, retry count, sleeps.
2. Transport:
   - `_http_get()`, `_fetch_html_with_status()`, encoding detection.
   - Retry `403`, `429`, `5xx`.
   - Optional `curl_cffi` fallback for Cloudflare-heavy sites.
3. URL helpers:
   - `_ensure_url()`
   - `_absolute_url(page_url, href)`
   - domain-specific URL validators, e.g. `_is_chapter_url()`.
4. Book page parser:
   - Prefer `meta[property="og:*"]`.
   - Fallback to visible selectors.
   - Extract: title, author, category, status, update time, latest chapter, cover, intro, catalog URL.
5. Catalog parser:
   - Read all paginated index pages, not only the visible first page.
   - Support both real `href` and JS `onclick="location.href='...'"`.
   - Deduplicate by normalized absolute URL while preserving order.
6. Chapter parser:
   - Title selectors first, title tag fallback.
   - Content selector first, largest text block fallback.
   - Strip scripts, styles, nav, ads, report-error links, site promo links.
   - Emit clean paragraph-only XHTML-safe HTML.
7. Cache:
   - Save chapters under `output/<book>/html/0001 - title.html`.
   - Reuse valid cache before network fetch.
   - Treat failed cache as retryable.
8. EPUB:
   - Use shared `epub_builder.create_epub()`.
   - Pass `chapters_data` from cache/download.
   - Set correct language, tags, intro, cover bytes/ext.
9. CLI/menu:
   - Interactive default.
   - Optional non-interactive args for automation.
10. Verification:
   - Parse sample info/catalog/chapter HTML.
   - `python -m py_compile <adapter>.py`
   - Build a small EPUB from cached/sample chapter data.

## zhaoshuyuan.net Notes

Current skin labels itself as `uu看书`, but source URLs use `www.zhaoshuyuan.net`.

Selectors:

```text
book title:      meta og:novel:book_name, .book .booktitle h1
author:          meta og:novel:author, .bookdes text "作者"
category:        meta og:novel:category, breadcrumb category
status:          meta og:novel:status, .bookdes text "状态"
intro:           meta og:description, .bookintro
cover:           meta og:image, .cover img
catalog link:    /index/<num>/<book_key>/<page>.html/
catalog list:    .chapterlist .all ul li a
chapter href:    href or onclick location.href='/read/<book_key>/<chapter>.html'
chapter title:   .read .booktitle h1
chapter content: #chaptercontent
```

Known cleanup:

```text
remove: 69shuba links, report-error paragraph, readpage/nav, scripts/styles
trash text: uu看书, zhaoshuyuan.net, 上一章, 下一章, 目录, 加书签, 如遇章节错误, 广告, Copyright
```

## biququ.co Notes

Selectors:

```text
book title:      meta og:novel:book_name, #info h1
author:          meta og:novel:author, #info text "作者"
category:        meta og:novel:category, .con_top category, #info text "类别"
status:          meta og:novel:status, #info text "状态"
intro:           meta og:description, #intro
cover:           meta og:image, #fmimg img[data-original|src]
catalog list:    #list after dt containing "全部章节目录"
chapter links:   #list a[rel="chapter"][href], including hidden .hc links
chapter title:   h1.bookname
chapter content: #booktxt, fallback #chaptercontent
next page:       #next_url text "下一页" or same chapter id with suffix _2/_3...
```

Known cleanup:

```text
remove: read setting controls, prev/next nav, add bookmark, simplified/traditional toggle, scripts/styles/ads
trash text: 笔趣阁, biququ.co, 上一章, 下一章, 下一页, 章节目录, 加入书签, 点击切换, 繁体版, 简体版, 广告
```

## xqiushubang.com Notes

Selectors:

```text
book title:      meta og:novel:book_name, .info .top h1
author:          meta og:novel:author, .info/.fix p text "作者"
category:        meta og:novel:category, .info/.fix p text "类别"
status:          meta og:novel:status, .info/.fix p text "状态"
intro:           meta og:description, .desc.xs-hidden, .m-desc
cover:           meta og:image, .imgbox img[src|data-original]
catalog pages:   select#indexselect option[value], /index/<book_id>/<page>/
catalog list:    h2.layout-tit containing "正文" -> next .section-box ul.section-list a[href]
chapter URL:     /read/<book_id>/<chapter_id>.html
chapter title:   h1.title
chapter content: #content
next page:       #next_url text "下一页" or same chapter id with suffix _2/_3...
```

Known cleanup:

```text
remove: reader setting selects, prev/next nav, add bookmark, simplified/traditional toggle, scripts/styles/ads
trash text: 求书帮, xqiushubang.com, 上一章, 下一章, 下一页, 目录, 加入书签, 最近阅读, 推荐本书, 广告
```
