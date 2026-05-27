'''
Tải truyện từ website https://www.czbooks.net/
Ứng dụng sẽ:
1. Nhận URL truyện từ người dùng.
2. Tự động lấy thông tin truyện và bìa.
3. Tải cover và chuyển đổi sang định dạng phù hợp cho EPUB.
4. Tải truyện theo chương, lưu từng chương ngay khi tải xong.
5. Lưu html chương dưới dạng chapter_xxxx.html trong Output\\Tên truyện\\
6. Tạo file EPUB tại Output\\Tên truyện.epub
'''

import io
import os
import re
import sys
import time
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup, Comment
try:
    from PIL import Image
except ImportError:
    os.system('pip install Pillow')
    from PIL import Image

from epub_builder import create_epub


def _print(msg: str) -> None:
    """In thông báo an toàn (tránh lỗi encoding trên Windows console)."""
    try:
        sys.stdout.buffer.write((msg + '\n').encode('utf-8', errors='replace'))
        sys.stdout.flush()
    except Exception:
        try:
            print(msg)
        except Exception:
            pass

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,vi;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

class CzbooksScraper:
    def __init__(self, novel_url):
        self.novel_url = novel_url.strip()
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.novel_data = {
            'title': None,
            'author': None,
            'description': None,
            'cover_url': None,
            'cover_bytes': None,
            'cover_ext': '.jpg',
        }
        self.output_base = 'Output'
        self.book_dir = None

    @staticmethod
    def _safe_filename(value: str) -> str:
        if not value:
            return 'Unknown'
        value = re.sub(r'[\\/:*?"<>|]+', ' ', value)
        value = re.sub(r'\s+', ' ', value).strip().rstrip('.')
        return value or 'Unknown'

    def _ensure_output_dir(self):
        os.makedirs(self.output_base, exist_ok=True)
        title_safe = self._safe_filename(self.novel_data['title'] or 'Unknown')
        self.book_dir = os.path.join(self.output_base, title_safe)
        os.makedirs(self.book_dir, exist_ok=True)
        _print(f'📁 Thư mục lưu: {self.book_dir}')

    def _http_get(self, url, referer=None, retries=3, backoff=1.0):
        headers = {}
        if referer:
            headers['Referer'] = referer

        for attempt in range(1, retries + 1):
            try:
                response = self.session.get(url, headers=headers, timeout=20)
                response.encoding = 'utf-8'
                if response.status_code == 403 and attempt < retries:
                    _print(f'  ⚠ 403 - thử lại {attempt}/{retries}...')
                    time.sleep(backoff * attempt)
                    if referer is None:
                        headers['Referer'] = self.novel_url
                    if attempt == 1 and self.novel_url:
                        self.session.get(self.novel_url, timeout=20)
                    continue
                if response.status_code == 403 and attempt == retries:
                    _print(f'  ⚠ 403 cuối cùng, thử session mới...')
                    fresh_sess = requests.Session()
                    fresh_sess.headers.update(HEADERS)
                    if self.novel_url:
                        fresh_sess.get(self.novel_url, timeout=20)
                    fresh_resp = fresh_sess.get(url, headers=headers, timeout=20)
                    if fresh_resp.status_code == 200:
                        fresh_resp.encoding = 'utf-8'
                        return fresh_resp
                return response
            except requests.RequestException as exc:
                _print(f'  ✗ Lỗi HTTP: {exc}')
                if attempt < retries:
                    time.sleep(backoff)
                    continue
                return None
        return None

    def scrape_novel(self):
        _print(f'\n{"="*50}')
        _print(f'📖 Đang lấy thông tin truyện...')
        _print(f'{"="*50}')
        response = self._http_get(self.novel_url)
        if not response or response.status_code != 200:
            raise RuntimeError(f'Không thể tải trang truyện. Status: {getattr(response, "status_code", None)}')

        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        title_tag = soup.find('span', class_='title')
        author_tag = soup.find('span', class_='author')
        description_tag = soup.find('div', class_='description')
        cover_img = soup.select_one('div.thumbnail img')

        self.novel_data['title'] = title_tag.get_text(strip=True) if title_tag else 'Unknown Title'
        self.novel_data['author'] = author_tag.get_text(' ', strip=True) if author_tag else 'Unknown Author'
        self.novel_data['description'] = description_tag.get_text(' ', strip=True) if description_tag else ''
        self.novel_data['cover_url'] = urljoin(self.novel_url, cover_img['src']) if cover_img and cover_img.get('src') else None

        _print(f'  Tiêu đề : {self.novel_data["title"]}')
        _print(f'  Tác giả : {self.novel_data["author"]}')

        self._ensure_output_dir()
        self.download_cover()

    def download_cover(self):
        cover_url = self.novel_data.get('cover_url')
        if not cover_url:
            _print('  ⚠ Không tìm thấy URL bìa, bỏ qua.')
            return

        _print(f'🖼️ Đang tải bìa truyện...')
        response = self.session.get(cover_url, timeout=20)
        if response.status_code != 200:
            _print(f'  ✗ Tải bìa thất bại (status {response.status_code})')
            return

        try:
            image = Image.open(io.BytesIO(response.content))
            image = image.convert('RGB')
            image.thumbnail((1200, 1600), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format='JPEG', quality=85)
            self.novel_data['cover_bytes'] = buffer.getvalue()
            self.novel_data['cover_ext'] = '.jpg'

            cover_path = os.path.join(self.book_dir, 'cover.jpg')
            with open(cover_path, 'wb') as f:
                f.write(self.novel_data['cover_bytes'])
            _print(f'  ✓ Đã lưu bìa: cover.jpg')
        except Exception as exc:
            _print(f'  ✗ Lỗi xử lý bìa: {exc}')

    def get_list_chapters(self):
        _print(f'\n📋 Đang lấy danh sách chương...')
        response = self._http_get(self.novel_url)
        if not response or response.status_code != 200:
            raise RuntimeError(f'Failed to retrieve novel page. Status code: {getattr(response, "status_code", None)}')

        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        chapter_links = []

        for a in soup.select('ul.nav.chapter-list a'):
            href = a.get('href')
            if not href:
                continue
            chapter_links.append({
                'title': a.get_text(strip=True) or 'Chương',
                'url': urljoin(self.novel_url, href)
            })

        if not chapter_links:
            for a in soup.select('a'):
                href = a.get('href')
                if href and 'chapter' in href and a.get_text(strip=True):
                    chapter_links.append({
                        'title': a.get_text(strip=True),
                        'url': urljoin(self.novel_url, href)
                    })

        unique = []
        seen = set()
        for item in chapter_links:
            if item['url'] not in seen:
                seen.add(item['url'])
                unique.append(item)

        _print(f'  ✓ Tìm thấy {len(unique)} chương')
        return unique

    def _clean_content(self, content_node):
        if not content_node:
            return ''

        for tag in content_node.find_all(['script', 'style', 'noscript', 'iframe', 'header', 'footer', 'form', 'button', 'input', 'textarea', 'svg', 'ads', 'aside']):
            tag.decompose()

        for comment in content_node.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        for tag in content_node.find_all(True):
            if tag.name not in {'div', 'p', 'br', 'strong', 'b', 'em', 'i', 'u', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'ul', 'ol', 'li'}:
                tag.unwrap()
            else:
                tag.attrs = {}

        for div in content_node.find_all('div'):
            if not div.find(['div', 'p', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
                text = div.get_text(strip=True)
                if text:
                    div.name = 'p'
                else:
                    div.decompose()

        return ''.join(str(child) for child in content_node.contents if str(child).strip())

    def _wrap_html(self, title, content_html):
        title_safe = self._safe_filename(title)
        return (
            '<!DOCTYPE html>\n'
            '<html lang="vi">\n'
            '<head>\n'
            '  <meta charset="utf-8"/>\n'
            f'  <title>{title_safe}</title>\n'
            '</head>\n'
            '<body>\n'
            f'  <article id="chapter">\n'
            f'    <h1>{title_safe}</h1>\n'
            f'{content_html}\n'
            '  </article>\n'
            '</body>\n'
            '</html>\n'
        )

    def save_chapter_file(self, index, chapter_title, content_html):
        filename = f'{index:04d}.html'
        path = os.path.join(self.book_dir, filename)
        if os.path.exists(path):
            return path
        html_text = self._wrap_html(chapter_title, content_html)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html_text)
        return path

    def get_content_chapter(self, chapter_url, fallback_title=None):
        response = self._http_get(chapter_url, referer=self.novel_url)
        if not response or response.status_code != 200:
            _print(f'  ✗ Lỗi tải chương (status {getattr(response, "status_code", None)})')
            return None

        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        
        #<div class="name">《大鍋炒》夏夜暗湧2</div>
        title_tag = soup.find('div', class_='name') or soup.find('h1')
        title = title_tag.get_text(strip=True) if title_tag else fallback_title or 'Chương'
        # <div class="content">幾乎在同一時間，三樓的另一間臥室裡。<br>
        #     <br>
        #     奶奶吳梅也還沒睡。她心裡惦記著孫子，白天看李秀赫好像有點累，就端了一杯溫牛奶上來。她敲了敲李秀赫的房門。<br>
        #     <br>
        #     「誰啊？」裡面傳來李秀赫渾厚的聲音。<br>
        #     <br>
        #     「秀赫啊，是奶奶，給你端杯牛奶。」<br>
        content_node = soup.find('div', class_='content')
        
            
        content_html = self._clean_content(content_node)
        if not content_html:
            paragraphs = [p.get_text(strip=True) for p in soup.find_all('p') if p.get_text(strip=True)]
            content_html = ''.join(f'<p>{p}</p>' for p in paragraphs)

        return {'title': title, 'content_html': content_html, 'url': chapter_url}

    def download_all_chapters(self, chapters):
        import download_policy

        def fetch_one(url, retries=1, fallback_title='', book_title=''):
            data = self.get_content_chapter(url, fallback_title)
            if not data:
                return {
                    'title': fallback_title or 'Chapter error',
                    'content_html': '',
                    'text': '',
                    'url': url,
                    'status_code': 'ERR',
                    'ok': False,
                    'error': 'No chapter content',
                }
            data.setdefault('status_code', 200)
            return data

        self._policy_last_download_result = download_policy.download_chapters_with_retries(
            module=self,
            book_title=self.novel_data.get('title') or 'Book',
            chapters=chapters,
            out_dir=self.book_dir,
            fetch_fn=fetch_one,
            start=1,
            end=None,
            book_url=self.novel_url,
        )

    def _fetch_chapter_for_epub(self, chapter_url):
        """Wrapper cho get_content_chapter để dùng làm fetch_fn cho epub_builder."""
        return self.get_content_chapter(chapter_url)

    def build_epub(self, chapters):
        epub_title = self.novel_data['title']
        epub_author = self.novel_data['author']
        epub_path = os.path.join(self.output_base, f'{self._safe_filename(epub_title)}.epub')
        policy_result = getattr(self, '_policy_last_download_result', None)
        has_policy_result = isinstance(policy_result, dict)

        create_epub(
            book_url=self.novel_url,
            book_title=epub_title,
            author=epub_author,
            chapters=policy_result.get('chapters') if has_policy_result else chapters,
            fetch_fn=None if has_policy_result else self._fetch_chapter_for_epub,
            cover_bytes=self.novel_data.get('cover_bytes'),
            cover_ext=self.novel_data.get('cover_ext', '.jpg'),
            language='zh',
            out_epub_path=epub_path,
            html_cache_dir=None if has_policy_result else self.book_dir,
            chapters_data=policy_result.get('chapters_data') if has_policy_result else None,
            tags=self.novel_data,
            book_info=self.novel_data,
        )

        _print(f'\n✅ Đã tạo EPUB: {epub_path}')
        return epub_path

    def run(self):
        self.scrape_novel()
        chapters = self.get_list_chapters()
        if not chapters:
            raise RuntimeError('Không tìm thấy chương nào để tải.')

        self.download_all_chapters(chapters)
        self.build_epub(chapters)


def main():
    url = input('Nhập URL truyện CZBooks: ').strip()
    if not url:
        print('URL không được để trống.')
        return

    scraper = CzbooksScraper(url)
    try:
        scraper.run()
    except Exception as exc:
        _print(f'\n❌ Đã xảy ra lỗi: {exc}')



try:
    from download_policy import install_adapter_policy as _install_adapter_policy
    _install_adapter_policy(globals())
except Exception:
    pass
if __name__ == '__main__':
    main()
        
