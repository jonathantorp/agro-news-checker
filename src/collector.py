#!/usr/bin/env python3
"""Collect Department of Agroecology mentions from broad RSS web/news searches."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import socket
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "search_terms.json"
DEFAULT_DATA = ROOT / "data" / "articles.json"
BING_NEWS_ENDPOINT = "https://www.bing.com/news/search"
BING_WEB_ENDPOINT = "https://www.bing.com/search"
MAX_RESPONSE_BYTES = 3_000_000
TRACKING_PARAMETERS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "mc_cid", "mc_eid",
    "msclkid", "ref", "ref_src",
}
LOGGER = logging.getLogger("agro-news")


class CollectionError(RuntimeError):
    """Raised when no configured search can be completed."""


class MetadataParser(HTMLParser):
    """Extract common publication-date metadata without parsing article text."""

    DATE_KEYS = {
        "article:published_time", "date", "datepublished", "datecreated",
        "dc.date", "dc.date.issued", "publishdate", "pubdate",
    }

    def __init__(self) -> None:
        super().__init__()
        self.dates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {key.lower(): value for key, value in attrs if value}
        key = (values.get("property") or values.get("name") or values.get("itemprop") or "").lower()
        if key in self.DATE_KEYS and values.get("content"):
            self.dates.append(values["content"])


def normalise_url(url: str) -> str:
    """Return a direct, stable URL without fragments or tracking parameters."""
    value = url.strip()
    parts = urlsplit(value)
    if parts.hostname and parts.hostname.lower().endswith("bing.com"):
        target = parse_qs(parts.query).get("url", [None])[0]
        if target:
            return normalise_url(target)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return value

    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    query = [
        (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, hostname, path, urlencode(sorted(query)), ""))


def parse_publication_date(value: str | None) -> str | None:
    """Convert RSS, ISO, and GDELT-style dates to UTC ISO-8601."""
    if not value:
        return None
    text = value.strip()
    try:
        parsed = parsedate_to_datetime(text)
        if parsed:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        pass
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
            return parsed.isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def sort_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort newest publication date first; unknown dates sort last."""
    return sorted(articles, key=lambda article: article.get("publication_date") or "", reverse=True)


def fetch_bytes(
    url: str,
    *,
    timeout: float = 15,
    retries: int = 3,
    opener: Callable[..., Any] = urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Fetch a bounded response with exponential-backoff retries."""
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; agro-news-checker/2.0; +https://github.com/jonathantorp/agro-news-checker)",
            "Accept": "application/rss+xml, application/xml, text/html;q=0.8",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with opener(request, timeout=timeout) as response:
                return response.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
        except (HTTPError, URLError, TimeoutError, socket.timeout) as error:
            last_error = error
            LOGGER.warning("Request failed (attempt %d/%d): %s", attempt, retries, error)
            if attempt < retries:
                sleep(2 ** (attempt - 1))
    raise CollectionError(f"Request failed after {retries} attempts: {last_error}")


def build_search_url(endpoint: str, query: str, page: int, page_size: int) -> str:
    """Build a Bing RSS URL with a stable result offset."""
    params = {
        "q": query,
        "format": "rss",
        "setlang": "da",
        "cc": "dk",
        "count": str(page_size),
        "first": str(page * page_size + 1),
    }
    return f"{endpoint}?{urlencode(params)}"


def parse_rss(payload: bytes) -> list[dict[str, str | None]]:
    """Parse RSS items into the common article shape."""
    root = ET.fromstring(payload)
    items: list[dict[str, str | None]] = []
    for node in root.findall(".//item"):
        def value(name: str) -> str | None:
            element = node.find(name)
            return element.text.strip() if element is not None and element.text else None

        title = value("title")
        link = value("link")
        if not title or not link:
            continue
        source = value("source")
        items.append(
            {
                "headline": html.unescape(title),
                "url": normalise_url(link),
                "publisher": html.unescape(source) if source else None,
                "publication_date": parse_publication_date(value("pubDate")),
            }
        )
    return items


def extract_date_from_html(payload: bytes) -> str | None:
    """Extract a publication date from standard metadata or JSON-LD."""
    text = payload.decode("utf-8", errors="ignore")
    parser = MetadataParser()
    parser.feed(text)
    for candidate in parser.dates:
        parsed = parse_publication_date(candidate)
        if parsed:
            return parsed
    match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', text, flags=re.IGNORECASE)
    return parse_publication_date(html.unescape(match.group(1))) if match else None


def load_data(path: Path) -> dict[str, Any]:
    """Load and minimally validate the permanent article archive."""
    if not path.exists():
        return {"last_successful_update": None, "articles": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
        raise ValueError(f"Invalid data structure in {path}")
    payload.setdefault("last_successful_update", None)
    return payload


def collect(
    config: dict[str, Any],
    existing: dict[str, Any],
    *,
    backfill: bool = False,
    fetcher: Callable[[str], bytes] = fetch_bytes,
    article_fetcher: Callable[[str], bytes] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], int]:
    """Search news and web RSS feeds, then merge unique direct article URLs."""
    queries = config.get("search_queries", config.get("search_terms", []))
    if not queries:
        raise ValueError("No search queries configured")
    page_size = int(config.get("results_per_page", 50))
    pages = int(config.get("backfill_pages" if backfill else "daily_pages", 1))
    delay = float(config.get("request_delay_seconds", 1))
    endpoints = [
        ("Bing News", BING_NEWS_ENDPOINT),
        ("Bing Web", BING_WEB_ENDPOINT),
    ]

    articles = list(existing["articles"])
    known_urls = {
        normalise_url(article["url"]) for article in articles
        if isinstance(article, dict) and article.get("url")
    }
    discovered_at = now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    successful_searches = 0
    new_count = 0

    for query in queries:
        for source_name, endpoint in endpoints:
            for page in range(pages):
                url = build_search_url(endpoint, query, page, page_size)
                try:
                    results = parse_rss(fetcher(url))
                    successful_searches += 1
                except (CollectionError, ET.ParseError, ValueError) as error:
                    LOGGER.error("%s search failed for %r page %d: %s", source_name, query, page + 1, error)
                    continue
                LOGGER.info("%s %r page %d returned %d result(s)", source_name, query, page + 1, len(results))
                for result in results:
                    clean_url = normalise_url(result["url"] or "")
                    if not clean_url or clean_url in known_urls:
                        continue
                    published = result["publication_date"]
                    if not published and article_fetcher:
                        try:
                            published = extract_date_from_html(article_fetcher(clean_url))
                        except (CollectionError, ValueError) as error:
                            LOGGER.info("Could not read publication date from %s: %s", clean_url, error)
                    publisher = result["publisher"] or urlsplit(clean_url).hostname or ""
                    articles.append(
                        {
                            "headline": (result["headline"] or "").strip(),
                            "url": clean_url,
                            "publisher": publisher.strip(),
                            "publication_date": published,
                            "first_discovered": discovered_at,
                            "search_term": query,
                            "source": source_name,
                        }
                    )
                    known_urls.add(clean_url)
                    new_count += 1
                if delay:
                    sleep(delay)

    if successful_searches == 0:
        raise CollectionError("All search sources failed; existing data was left unchanged")
    return {
        "last_successful_update": discovered_at,
        "articles": sort_articles(articles),
    }, new_count


def write_data_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically so interruption cannot truncate the archive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backfill", action="store_true", help="Search additional result pages")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        existing = load_data(args.data)

        def configured_fetcher(url: str) -> bytes:
            return fetch_bytes(url, timeout=args.timeout, retries=args.retries)

        updated, new_count = collect(
            config,
            existing,
            backfill=args.backfill,
            fetcher=configured_fetcher,
            article_fetcher=configured_fetcher,
        )
        write_data_atomic(args.data, updated)
        LOGGER.info("Collection complete: %d new article(s), %d total", new_count, len(updated["articles"]))
        print(f"new_articles={new_count}")
        return 0
    except (CollectionError, OSError, ValueError, json.JSONDecodeError) as error:
        LOGGER.error("Collection aborted: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
