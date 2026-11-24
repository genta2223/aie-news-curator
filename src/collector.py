"""RSS collector module for AIE News Curator.

Fetches article metadata solely from configured RSS/Atom feeds without
performing any web scraping of target article pages.
"""

from __future__ import annotations

import html
import logging
import re
from calendar import timegm
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_SOURCES_PATH = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


class ArticleMetadata(BaseModel):
    """Metadata for a single collected RSS article."""

    title: str
    url: str
    source: str
    published_at: datetime | None = None
    summary: str = Field(default="")


def load_sources_config(config_path: Path | str = DEFAULT_SOURCES_PATH) -> dict[str, Any]:
    """Load RSS sources and exclude keywords from YAML configuration."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        "sources": data.get("sources", []),
        "exclude_keywords": data.get("exclude_keywords", []),
    }


def clean_summary(raw_text: str, max_length: int = 500) -> str:
    """Strip HTML tags and normalize whitespace from an RSS summary."""
    if not raw_text:
        return ""
    unescaped = html.unescape(raw_text)
    stripped = _TAG_RE.sub(" ", unescaped)
    normalized = _WHITESPACE_RE.sub(" ", stripped).strip()
    if len(normalized) > max_length:
        return normalized[:max_length].rstrip() + "..."
    return normalized


def parse_entry_datetime(entry: Any) -> datetime | None:
    """Extract a UTC-aware datetime from a feedparser entry if available."""
    parsed_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed_struct:
        try:
            timestamp = timegm(parsed_struct)
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass

    raw_date = entry.get("published") or entry.get("updated")
    if isinstance(raw_date, str) and raw_date.strip():
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def is_excluded(title: str, summary: str, exclude_keywords: list[str]) -> bool:
    """Return True if title or summary contains any exclude keyword."""
    if not exclude_keywords:
        return False
    combined = f"{title}\n{summary}".lower()
    for kw in exclude_keywords:
        kw_clean = kw.strip().lower()
        if not kw_clean:
            continue
        # Match short ASCII tokens like "PR" on word boundaries to avoid false positives
        if kw_clean.isascii() and len(kw_clean) <= 3 and kw_clean.isalnum():
            if re.search(rf"\b{re.escape(kw_clean)}\b", combined):
                return True
        elif kw_clean in combined:
            return True
    return False


def filter_and_deduplicate(
    articles: list[ArticleMetadata],
    exclude_keywords: list[str],
    hours: int = 48,
    now: datetime | None = None,
) -> list[ArticleMetadata]:
    """Filter articles by recency and exclude keywords, then deduplicate by URL."""
    reference_now = now or datetime.now(timezone.utc)
    cutoff = reference_now - timedelta(hours=hours)

    seen_urls: set[str] = set()
    result: list[ArticleMetadata] = []

    for article in articles:
        normalized_url = article.url.strip()
        if not normalized_url or normalized_url in seen_urls:
            continue
        if article.published_at is not None and article.published_at < cutoff:
            continue
        if is_excluded(article.title, article.summary, exclude_keywords):
            continue

        seen_urls.add(normalized_url)
        result.append(article)

    return result


def fetch_feed_articles(source: dict[str, Any]) -> list[ArticleMetadata]:
    """Fetch and parse entries from a single RSS source configuration."""
    name = str(source.get("name", "Unknown"))
    url = str(source.get("url", "")).strip()
    max_items = int(source.get("max_items", 20))
    if not url:
        return []

    parsed = feedparser.parse(url)
    if getattr(parsed, "bozo", False) and not parsed.entries:
        logger.warning("Failed to parse feed %s (%s): %s", name, url, getattr(parsed, "bozo_exception", ""))
        return []

    articles: list[ArticleMetadata] = []
    for entry in parsed.entries[:max_items]:
        title = _WHITESPACE_RE.sub(" ", html.unescape(str(entry.get("title", "")))).strip()
        link = str(entry.get("link", "")).strip()
        if not title or not link:
            continue

        raw_summary = entry.get("summary") or entry.get("description") or ""
        summary = clean_summary(str(raw_summary))
        published_at = parse_entry_datetime(entry)

        articles.append(
            ArticleMetadata(
                title=title,
                url=link,
                source=name,
                published_at=published_at,
                summary=summary,
            )
        )
    return articles


def collect_articles(
    config_path: Path | str = DEFAULT_SOURCES_PATH,
    hours: int = 48,
    now: datetime | None = None,
) -> list[ArticleMetadata]:
    """Collect, filter, and deduplicate articles from all configured RSS sources."""
    config = load_sources_config(config_path)
    raw_articles: list[ArticleMetadata] = []

    for source in config["sources"]:
        try:
            raw_articles.extend(fetch_feed_articles(source))
        except Exception as exc:  # pragma: no cover - defensive against network issues
            logger.warning("Error fetching source %s: %s", source.get("name"), exc)

    filtered = filter_and_deduplicate(
        raw_articles,
        exclude_keywords=config["exclude_keywords"],
        hours=hours,
        now=now,
    )
    filtered.sort(
        key=lambda a: a.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return filtered


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    items = collect_articles(hours=48)
    print(f"Collected {len(items)} articles (last 48h):")
    for idx, item in enumerate(items[:10], start=1):
        pub = item.published_at.isoformat() if item.published_at else "N/A"
        print(f"[{idx}] ({item.source}) {item.title}")
        print(f"    URL: {item.url}")
        print(f"    Published: {pub}")
