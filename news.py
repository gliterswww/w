"""Pulls market headlines from free, no-key RSS feeds and filters for NQ-relevant stories."""
from __future__ import annotations

import feedparser

FEEDS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^NDX&region=US&lang=en-US",
    "https://finance.yahoo.com/news/rssindex",
    "https://www.marketwatch.com/rss/topstories",
]

# Headlines mentioning any of these are treated as relevant to Nasdaq/NQ futures.
KEYWORDS = [
    "fed", "rate", "inflation", "cpi", "ppi", "jobs report", "nonfarm", "unemployment",
    "nasdaq", "nvidia", "apple", "microsoft", "amazon", "meta", "alphabet", "google",
    "tesla", "tech stock", "semiconductor", "chip", "ai ", "earnings", "fomc", "powell",
    "yield", "treasury", "s&p", "market",
]

MAX_ITEMS = 8


def _is_relevant(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in KEYWORDS)


def get_news() -> list[dict]:
    items: list[dict] = []
    seen_titles: set[str] = set()

    for url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception:
            continue
        for entry in parsed.get("entries", []):
            title = entry.get("title", "").strip()
            if not title or title in seen_titles:
                continue
            if not _is_relevant(title):
                continue
            seen_titles.add(title)
            items.append({
                "title": title,
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
                "source": parsed.get("feed", {}).get("title", "Market News"),
            })
            if len(items) >= MAX_ITEMS:
                break
        if len(items) >= MAX_ITEMS:
            break

    return items
