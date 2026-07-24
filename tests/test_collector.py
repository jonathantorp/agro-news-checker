import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from src.collector import (
    CollectionError,
    collect,
    extract_date_from_html,
    load_data,
    normalise_url,
    parse_rss,
    sort_articles,
)

NOW = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)


def rss(*items):
    entries = "".join(
        f"""
        <item>
          <title>{item['title']}</title>
          <link>{item['url'].replace('&', '&amp;')}</link>
          <source>{item.get('source', 'Example News')}</source>
          {f"<pubDate>{item['date']}</pubDate>" if item.get('date') else ""}
        </item>
        """
        for item in items
    )
    return f"<?xml version='1.0'?><rss><channel>{entries}</channel></rss>".encode()


class UrlNormalisationTests(unittest.TestCase):
    def test_removes_tracking_fragment_default_port_and_trailing_slash(self):
        actual = normalise_url(
            "HTTPS://Example.COM:443/story/?utm_source=newsletter&b=2&a=1#section"
        )
        self.assertEqual(actual, "https://example.com/story?a=1&b=2")

    def test_extracts_direct_article_from_bing_redirect(self):
        article = "https://news.example/story?utm_source=bing"
        bing = f"https://www.bing.com/news/apiclick.aspx?url={quote(article, safe='')}&tid=x"
        self.assertEqual(normalise_url(bing), "https://news.example/story")


class ParsingTests(unittest.TestCase):
    def test_rss_provides_headline_date_publisher_and_direct_link(self):
        items = parse_rss(
            rss(
                {
                    "title": "New research &amp; results",
                    "url": "https://news.example/story?utm_medium=rss",
                    "source": "Example News",
                    "date": "Thu, 23 Jul 2026 12:00:00 GMT",
                }
            )
        )
        self.assertEqual(items[0]["headline"], "New research & results")
        self.assertEqual(items[0]["url"], "https://news.example/story")
        self.assertEqual(items[0]["publisher"], "Example News")
        self.assertEqual(items[0]["publication_date"], "2026-07-23T12:00:00Z")

    def test_extracts_article_date_from_metadata(self):
        page = b'<meta property="article:published_time" content="2025-03-04T09:30:00+01:00">'
        self.assertEqual(extract_date_from_html(page), "2025-03-04T08:30:00Z")


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "search_queries": ['"Aarhus Universitet" agroøkologi'],
            "daily_pages": 1,
            "backfill_pages": 3,
            "results_per_page": 50,
            "request_delay_seconds": 0,
        }
        self.empty = {"last_successful_update": None, "articles": []}

    def test_duplicate_urls_across_sources_are_not_inserted(self):
        payload = rss(
            {
                "title": "Research story",
                "url": "https://news.example/story?utm_source=x",
                "date": "Thu, 23 Jul 2026 12:00:00 GMT",
            },
            {
                "title": "Research story duplicate",
                "url": "https://news.example/story?fbclid=123",
                "date": "Thu, 23 Jul 2026 12:00:00 GMT",
            },
        )
        data, count = collect(
            self.config, self.empty, fetcher=lambda _url: payload, now=lambda: NOW
        )
        self.assertEqual(count, 1)
        self.assertEqual(len(data["articles"]), 1)
        self.assertEqual(data["articles"][0]["url"], "https://news.example/story")

    def test_backfill_requests_configured_number_of_pages_for_both_sources(self):
        urls = []

        def fetcher(url):
            urls.append(url)
            return rss()

        collect(self.config, self.empty, backfill=True, fetcher=fetcher, now=lambda: NOW)
        self.assertEqual(len(urls), 6)
        offsets = [parse_qs(urlsplit(url).query)["first"][0] for url in urls]
        self.assertEqual(offsets, ["1", "51", "101", "1", "51", "101"])

    def test_all_source_failures_leave_existing_file_unchanged(self):
        original = {
            "last_successful_update": "2026-07-20T00:00:00Z",
            "articles": [{"headline": "Existing", "url": "https://example.com/a"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "articles.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaises(CollectionError):
                collect(
                    self.config,
                    load_data(path),
                    fetcher=lambda _url: (_ for _ in ()).throw(CollectionError("offline")),
                    now=lambda: NOW,
                )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_articles_are_sorted_by_publication_date(self):
        articles = [
            {"headline": "Old", "publication_date": "2026-01-01T00:00:00Z"},
            {"headline": "Unknown", "publication_date": None},
            {"headline": "New", "publication_date": "2026-07-01T00:00:00Z"},
        ]
        self.assertEqual(
            [article["headline"] for article in sort_articles(articles)],
            ["New", "Old", "Unknown"],
        )


if __name__ == "__main__":
    unittest.main()
