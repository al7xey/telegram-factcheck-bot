"""Fetch top news from Google News RSS."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Iterable
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests

from config import config


_TOP_URL = "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru"
_SEARCH_URL = "https://news.google.com/rss/search?q={query}&hl=ru&gl=RU&ceid=RU:ru"


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
        headers={"User-Agent": "factcheck-bot/1.0"},
    )
    response.raise_for_status()

    root = ElementTree.fromstring(response.content)
    items: list[NewsItem] = []

    for item in _iter_items(root):
        title = _clean_text(item.findtext("title"))
        link = _clean_text(item.findtext("link"))
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
