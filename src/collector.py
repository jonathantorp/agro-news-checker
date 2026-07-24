#!/usr/bin/env python3
"""Collect Department of Agroecology mentions from the GDELT DOC API."""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "search_terms.json"
DEFAULT_DATA = ROOT / "data" / "articles.json"
GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ref",
    "ref_src",
}

LOGGER = logging.getLogger("agro-news")


class CollectionError(RuntimeError):
    """Raised when no configured search can be completed."""


def normalise_url(url: str) -> str:
    """Return a stable URL without fragments or common tracking parameters."""
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return value

    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"

    clean_query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, hostname, path, urlencode(sorted(clean_query)), ""))


def parse_publication_date(value: str | None) -> str | None:
    """Convert GDELT date formats to an ISO-8601 timestamp when possible."""
    if not value:
        return None
    text = value.strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
            return parsed.isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).isoformat().replace("+00:00", "Z")
    except ValueError:
        LOGGER.warning("Could not parse publication date %r", value)
        return None


def publication_sort_key(article: dict[str, Any]) -> str:
    """Return a sortable publication date; missing dates sort last."""
    return article.get("publication_date") or ""


def sort_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort newest publication date first."""
    return sorted(articles, key=publication_sort_key, reverse=True)


def fetch_json(
    url: str,
    *,
    timeout: float = 20,
    retries: int = 3,
    opener: Callable[..., Any] = urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch JSON with bounded exponential-backoff retries."""
    request = Request(url, headers={"User-Agent": "agro-news-checker/1.0"})
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with opener(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as error:
            last_error = error
            LOGGER.warning("Request failed (attempt %d/%d): %s", attempt, retries, error)
            if attempt < retries:
                sleep(2 ** (attempt - 1))
    raise CollectionError(f"GDELT request failed after {retries} attempts: {last_error}")


def build_api_url(term: str, lookback: str, max_records: int) -> str:
    """Build a GDELT DOC API query for an exact phrase."""
    params = {
        "query": f'"{term}"',
        "mode": "ArtList",
        "maxrecords": str(max_records),
        "format": "json",
        "sort": "DateDesc",
        "timespan": lookback,
    }
    return f"{GDELT_ENDPOINT}?{urlencode(params)}"


def load_data(path: Path) -> dict[str, Any]:
    """Load and minimally validate the persistent article store."""
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
    fetcher: Callable[[str], dict[str, Any]] = fetch_json,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[dict[str, Any], int]:
    """Fetch all terms, merge new unique URLs, and return data and new count."""
    terms = config.get("search_terms", [])
    if not terms:
        raise ValueError("No search terms configured")

    articles = list(existing["articles"])
    known_urls = {
        normalise_url(article["url"])
        for article in articles
        if isinstance(article, dict) and article.get("url")
    }
    discovered_at = now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    success_count = 0
    new_count = 0

    for term in terms:
        url = build_api_url(
            term,
            str(config.get("lookback", "1month")),
            int(config.get("max_records_per_term", 250)),
        )
        try:
            response = fetcher(url)
        except Exception as error:
            LOGGER.error("Search failed for %r: %s", term, error)
            continue

        success_count += 1
        results = response.get("articles", [])
        if not isinstance(results, list):
            LOGGER.error("Unexpected GDELT response for %r: 'articles' is not a list", term)
            continue

        LOGGER.info("Search %r returned %d article(s)", term, len(results))
        for result in results:
            raw_url = result.get("url")
            title = result.get("title")
            if not raw_url or not title:
                continue
            clean_url = normalise_url(raw_url)
            if clean_url in known_urls:
                continue
            articles.append(
                {
                    "headline": title.strip(),
                    "url": clean_url,
                    "publisher": (result.get("domain") or urlsplit(clean_url).hostname or "").strip(),
                    "publication_date": parse_publication_date(result.get("seendate")),
                    "first_discovered": discovered_at,
                    "search_term": term,
                }
            )
            known_urls.add(clean_url)
            new_count += 1

    if success_count == 0:
        raise CollectionError("All GDELT searches failed; existing data was left unchanged")

    return {
        "last_successful_update": discovered_at,
        "articles": sort_articles(articles),
    }, new_count


def write_data_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically so interruption cannot truncate the data file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        existing = load_data(args.data)

        def configured_fetcher(url: str) -> dict[str, Any]:
            return fetch_json(url, timeout=args.timeout, retries=args.retries)

        updated, new_count = collect(config, existing, fetcher=configured_fetcher)
        write_data_atomic(args.data, updated)
        LOGGER.info("Collection complete: %d new article(s), %d total", new_count, len(updated["articles"]))
        print(f"new_articles={new_count}")
        return 0
    except (CollectionError, OSError, ValueError, json.JSONDecodeError) as error:
        LOGGER.error("Collection aborted: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
