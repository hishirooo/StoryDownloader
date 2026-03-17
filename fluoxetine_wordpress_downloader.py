import requests
from bs4 import BeautifulSoup
import re

headers = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://luclacnho2810.wordpress.com/"
}

def get_soup(url):
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def get_chapters(url):
    soup = get_soup(url)

    chapters = []

    table = soup.select_one("figure.wp-block-table table")

    for a in table.select("a"):
        title = a.get_text(strip=True)
        link = a["href"]

        if "Chương" in title:
            num = int(re.search(r"\d+", title).group())
            chapters.append((num, title, link))

    chapters.sort()

    return chapters


def get_content(url):
    soup = get_soup(url)

    content = soup.select_one(
        "div.entry-content, div.wp-block-post-content"
    )

    for tag in content.select("script,style"):
        tag.decompose()

    return content.get_text("\n", strip=True)


def main():

    url = input("URL truyện: ")

    chapters = get_chapters(url)

    print("Tổng chương:", len(chapters))

    for num, title, link in chapters:

        print(f"Tải {title}")

        text = get_content(link)

        with open(f"C{num}.txt", "w", encoding="utf8") as f:
            f.write(title + "\n\n")
            f.write(text)


if __name__ == "__main__":
    main()