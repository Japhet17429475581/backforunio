from __future__ import annotations

import sys
import time
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


class VspuNewsParser:
    BASE_URL = "https://www.vspu.ru"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
    TIMEOUT = 15
    MAX_PAGES = 50
    MAX_ITEMS = 50
    SLEEP_BETWEEN = 0.25
    MAX_RETRIES = 2

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.USER_AGENT})

    def _resolve_url(self, href: str | None) -> str | None:
        if not href:
            return None
        href = href.strip()
        if href.startswith("//"):
            return "https:" + href
        return urljoin(self.base_url + "/", href)

    def fetch(self, path: str) -> str:
        url = self._resolve_url(path)
        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=self.TIMEOUT)
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.MAX_RETRIES:
                    break
                time.sleep(0.5 * (attempt + 1))
        raise last_exc or RuntimeError("Не удалось загрузить страницу")

    @staticmethod
    def _parse_date(value: str | None):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    def _get_image(self, img):
        if not img:
            return None
        srcset = img.get("srcset")
        if srcset:
            variants = []
            for item in srcset.split(","):
                parts = item.strip().split()
                if not parts:
                    continue
                width = 0
                if len(parts) > 1:
                    try:
                        width = int(parts[1].rstrip("w"))
                    except ValueError:
                        pass
                variants.append((width, parts[0]))
            if variants:
                variants.sort(reverse=True)
                return self._resolve_url(variants[0][1])
        return self._resolve_url(img.get("src"))

    def parse_list(self, html: str):
        soup = BeautifulSoup(html, "html.parser")
        container = soup.select_one(".view-vspu-allnews .view-content")
        if not container:
            return []

        result = []
        for item in container.select(".views-view-responsive-grid__item"):
            link = item.select_one("a.newstitle")
            if not link:
                continue
            url = self._resolve_url(link.get("href"))
            if not url:
                continue

            title_tag = item.select_one(".viewfield3") or link
            title = title_tag.get_text(" ", strip=True) or link.get('aria-label','').strip()
            date_tag = item.select_one("time.datetime")
            published_at = self._parse_date(date_tag.get("datetime")) if date_tag else None
            image = self._get_image(item.select_one("img"))
            external_id = url.rstrip("/").split("/")[-1]
            if not external_id:
                continue

            result.append({
                "external_id": external_id,
                "title": title,
                "source_url": url,
                "image_url": image,
                "published_at": published_at,
            })
        return result

    def parse_article(self, html: str, external_id: str):
        soup = BeautifulSoup(html, "html.parser")
        article = soup.select_one("article.node--type-vspu-news")
        if not article:
            return None

        # Drupal renders the page heading outside the article body on vspu.ru.
        title=''
        for tag in [article.select_one('h1.title, h1, .field--name-title'),
                    soup.select_one('h1.title, h1.page-title, #page-title')]:
            if tag and tag.get_text(' ',strip=True):
                title=tag.get_text(' ',strip=True)
                break
        if not title:
            meta=soup.find('meta',attrs={'property':'og:title'}) or soup.find('meta',attrs={'name':'twitter:title'})
            title=(meta.get('content','').strip() if meta else '')
        canonical = soup.select_one('link[rel="canonical"]')
        source_url = self._resolve_url(canonical.get('href')) if canonical else f"{self.base_url}/news-article/{external_id}"
        source_url = source_url or f"{self.base_url}/news-article/{external_id}"
        date_tag = article.select_one(".newsdate time.datetime")
        published_at = self._parse_date(date_tag.get("datetime")) if date_tag else None
        image = self._get_image(article.select_one(".field--name-field-vspu-responsive-image img"))

        content_parts = []
        for block in article.select(".field--name-field-vspu-article-text"):
            text = block.get_text(" ", strip=True)
            if text:
                content_parts.append(text)
        content = "\n\n".join(content_parts)

        return {
            "external_id": external_id,
            "title": title,
            "description": content_parts[0] if content_parts else None,
            "content": content,
            "image_url": image,
            "source_url": source_url,
            "published_at": published_at,
        }

    def get_news(self, max_pages: int | None = None, max_items: int | None = None, known_urls: set[str] | None = None):
        max_pages = max_pages or self.MAX_PAGES
        max_items = max_items or self.MAX_ITEMS
        known_urls = known_urls or set()
        result = []
        seen = set()

        for page in range(max_pages):
            if len(result) >= max_items:
                break
            path = "/news" if page == 0 else f"/news?page={page}"
            try:
                html = self.fetch(path)
            except requests.RequestException as exc:
                sys.stderr.write("news_page_failed\n")
                break

            items = self.parse_list(html)
            if not items:
                break

            hit_known = False
            for item in items:
                url = item["source_url"]
                if url in seen:
                    continue
                seen.add(url)
                if url in known_urls:
                    # Лента ВГСПУ отсортирована от новых к старым.
                    # Как только встретили известную статью, дальше идут ещё более старые.
                    hit_known = True
                    break
                result.append(item)
                if len(result) >= max_items:
                    break

            if len(result) >= max_items or hit_known:
                break
            time.sleep(self.SLEEP_BETWEEN)

        result.sort(key=lambda item: item.get("published_at") or datetime.min, reverse=True)
        return result[:max_items]

    def get_article(self, external_id: str):
        return self.parse_article(self.fetch(f"/news-article/{external_id}"), external_id)
