"""Fetch top news from Google News RSS, returning direct source links."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Iterable
from urllib.parse import quote_plus, unquote, urlparse, parse_qs
from xml.etree import ElementTree

import requests

from config import config


_TOP_URL = "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru"
_SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=ru&gl=RU&ceid=RU:ru"
_USER_AGENT = "factcheck-bot/1.0"
_GOOGLE_HOSTS = {"news.google.com", "google.com", "www.google.com"}


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    summary: str


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_google_host(host: str) -> bool:
    normalized = host.lower()
    return normalized in _GOOGLE_HOSTS or any(
        normalized.endswith(f".{base}") for base in _GOOGLE_HOSTS
    )


def _is_google_link(url: str) -> bool:
    try:
        host = urlparse(url).netloc
    except ValueError:
        return False
    return bool(host) and _is_google_host(host)


def _is_http_url(url: str) -> bool:
    lower = url.lower()
    return lower.startswith("http://") or lower.startswith("https://")


def _extract_url_param(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    query = parse_qs(parsed.query)
    for key in ("url", "u", "q"):
        if key not in query:
            continue
        candidate = query[key][0]
        if not candidate:
            continue
        candidate = unquote(candidate)
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
    return None


def _follow_redirect(url: str) -> str | None:
    for method in (requests.head, requests.get):
        response = None
        try:
            response = method(
                url,
                allow_redirects=True,
                timeout=config.request_timeout,
                headers={"User-Agent": _USER_AGENT},
                stream=True,
            )
            final_url = response.url
            if final_url:
                return final_url
        except requests.RequestException:
            continue
        finally:
            if response is not None:
                response.close()
    return None


def resolve_direct_link(url: str) -> str:
    cleaned = url.strip()
    if not cleaned or not _is_http_url(cleaned):
        return cleaned
    direct = _extract_url_param(cleaned)
    if direct and not _is_google_link(direct):
        return direct
    if not _is_google_link(cleaned):
        return cleaned
    final_url = _follow_redirect(cleaned)
    if not final_url:
        return cleaned
    direct = _extract_url_param(final_url)
    if direct and not _is_google_link(direct):
        return direct
    if not _is_google_link(final_url):
        return final_url
    return cleaned


def normalize_direct_link(url: str) -> str | None:
    resolved = resolve_direct_link(url)
    if not resolved or not _is_http_url(resolved):
        return None
    if _is_google_link(resolved):
        return None
    return resolved


def normalize_direct_links(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for url in urls:
        direct = normalize_direct_link(str(url).strip())
        if not direct or direct in seen:
            continue
        seen.add(direct)
        normalized.append(direct)
    return normalized


def _truncate(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    trimmed = text[: max_len - 1].rstrip()
    return f"{trimmed}..."


def _iter_items(root: ElementTree.Element) -> Iterable[ElementTree.Element]:
    return root.findall(".//item")


def fetch_top_news(topic: str | None, limit: int = 5) -> list[NewsItem]:
    url = _TOP_URL if not topic else _SEARCH_URL.format(query=quote_plus(topic))

    response = requests.get(
        url,
        timeout=config.request_timeout,
        headers={"User-Agent": _USER_AGENT},
    )
    response.raise_for_status()

    root = ElementTree.fromstring(response.content)
    items: list[NewsItem] = []

    for item in _iter_items(root):
        title = _clean_text(item.findtext("title"))
        link = _clean_text(item.findtext("link"))
        if link:
            direct = normalize_direct_link(link)
            link = direct or (link if _is_http_url(link) else None)
        else:
            link = None
        source = _clean_text(item.findtext("source"))
        description = _clean_text(item.findtext("description"))

        summary = _truncate(description or title)
        if not title or not link:
            continue
        items.append(
            NewsItem(
                title=title,
                link=link,
                source=source,
                summary=summary,
            )
        )
        if len(items) >= limit:
            break

    return items
