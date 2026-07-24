import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from src.collector import CollectionError, collect, load_data, normalise_url, sort_articles


NOW = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)


class UrlNormalisationTests(unittest.TestCase):
    def test_removes_tracking_fragment_default_port_and_trailing_slash(self):
        actual = normalise_url(
            "HTTPS://Example.COM:443/story/?utm_source=newsletter&b=2&a=1#section"
        )
        self.assertEqual(actual, "https://example.com/story?a=1&b=2")

    def test_preserves_meaningful_query_parameters(self):
        self.assertEqual(
            normalise_url("https://example.com/search?q=agro&lang=da"),
            "https://example.com/search?lang=da&q=agro",
        )


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "search_terms": ["Department of Agroecology"],
            "lookback": "1month",
            "max_records_per_term": 250,
        }
        self.empty = {"last_successful_update": None, "articles": []}

    def test_duplicate_urls_are_not_inserted(self):
        def fetcher(url):
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["query"], ['"Department of Agroecology"'])
            return {
                "articles": [
                    {
                        "title": "Research story",
                        "url": "https://news.example/story?utm_source=x",
                        "domain": "news.example",
                        "seendate": "20260723T120000Z",
                    },
                    {
                        "title": "Research story duplicate",
                        "url": "https://news.example/story?fbclid=123",
                        "domain": "news.example",
                        "seendate": "20260723T120000Z",
                    },
                ]
            }

        data, count = collect(self.config, self.empty, fetcher=fetcher, now=lambda: NOW)
        self.assertEqual(count, 1)
        self.assertEqual(len(data["articles"]), 1)
        self.assertEqual(data["articles"][0]["url"], "https://news.example/story")

    def test_all_api_failures_leave_existing_file_unchanged(self):
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
