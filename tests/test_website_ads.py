import unittest

from monitor.website_ads import (
    WebsiteAdSourceError,
    count_website_published_ads,
    website_published_ads_report,
)


SOURCE_CONFIG = {
    "source": "test_api",
    "api_url": "https://api.example.test/products",
    "website_origin": "https://qatarsale.com",
    "website_host": "qatarsale.com",
    "category_path_prefix": "/ar/products/",
    "publication_date_field": "startDate",
    "id_field": "id",
    "page_size": 10,
    "request_interval_seconds": 0,
}

SCRAPERS = [
    {"name": "cars", "url": "https://qatarsale.com/ar/products/cars"},
    {"name": "property", "url": "https://qatarsale.com/ar/products/property"},
    {"name": "jobs", "url": "https://qatarsale.com/ar/jobs/opportunity/list"},
    {"name": "other-host", "url": "https://example.test/ar/products/ignored"},
]


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []
        self.closed = False

    def post(self, url, json, headers, timeout):
        self.urls.append(json["url"])
        return FakeResponse(self.pages[json["url"]])

    def close(self):
        self.closed = True


def api_page(page, pages, rows, total=None):
    return {
        "currentPage": page,
        "pagesCount": pages,
        "totalCount": len(rows) if total is None else total,
        "list": rows,
    }


class WebsiteAdsTests(unittest.TestCase):
    def test_filters_by_utc_publication_date_deduplicates_and_scans_categories(self):
        cars_url = "https://qatarsale.com/ar/products/cars?page=0"
        cars_page_1 = "https://qatarsale.com/ar/products/cars?page=1"
        property_url = "https://qatarsale.com/ar/products/property?page=0"
        pages = {
            cars_url: api_page(0, 2, [
                {"id": 11, "startDate": "2026-06-02T00:30:00+03:00"},
                {"id": 12, "startDate": "2026-06-02T23:59:59Z"},
            ], total=3),
            cars_page_1: api_page(1, 2, [
                {"id": 11, "startDate": "2026-06-02T00:30:00+03:00"},
            ], total=3),
            property_url: api_page(0, 1, [
                {"id": 11, "startDate": "2026-06-02T00:30:00+03:00"},
                {"id": 13, "startDate": "2026-06-02T22:00:00Z"},
            ]),
        }
        session = FakeSession(pages)

        result = count_website_published_ads(
            SOURCE_CONFIG, SCRAPERS, "2026-06-01", session=session
        )

        self.assertEqual(result["website_published_ads"], 1)
        self.assertEqual(result["website_published_ads_status"], "ok")
        self.assertEqual(result["website_published_ads_categories_scanned"], 2)
        self.assertEqual(result["website_published_ads_pages_scanned"], 3)
        self.assertEqual(result["website_published_ads_source_listings_scanned"], 5)
        self.assertEqual(len(session.urls), 3)

    def test_empty_success_is_zero(self):
        category_url = "https://qatarsale.com/ar/products/cars?page=0"
        session = FakeSession({
            category_url: api_page(0, 1, [], total=0),
        })

        result = count_website_published_ads(
            SOURCE_CONFIG, SCRAPERS[:1], "2026-06-01", session=session
        )

        self.assertEqual(result["website_published_ads"], 0)
        self.assertEqual(result["website_published_ads_status"], "ok")

    def test_source_failure_raises_instead_of_returning_partial_total(self):
        class FailingSession:
            def post(self, *args, **kwargs):
                raise OSError("offline")

        report = website_published_ads_report(
            SOURCE_CONFIG,
            SCRAPERS[:1],
            "2026-06-01",
            session=FailingSession(),
            sleep_fn=lambda _seconds: None,
        )
        self.assertIsNone(report["website_published_ads"])
        self.assertEqual(report["website_published_ads_status"], "error")
        self.assertIn("website request failed", report["website_published_ads_error"])

    def test_invalid_page_fails_complete_scan(self):
        category_url = "https://qatarsale.com/ar/products/cars?page=0"
        session = FakeSession({
            category_url: api_page(0, 2, [{"id": 1, "startDate": "2026-06-01T00:00:00Z"}], total=2),
            "https://qatarsale.com/ar/products/cars?page=1": api_page(1, 2, [], total=2),
        })

        with self.assertRaisesRegex(WebsiteAdSourceError, "empty page"):
            count_website_published_ads(
                SOURCE_CONFIG, SCRAPERS[:1], "2026-06-01", session=session
            )


if __name__ == "__main__":
    unittest.main()
