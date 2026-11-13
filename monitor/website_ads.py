"""Website-sourced published-ad counts for the daily site monitor."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger("monitor")

DEFAULT_TIMEOUT = (10, 30)
MAX_RETRIES = 3


class WebsiteAdSourceError(Exception):
    """Raised when a complete website listing scan cannot be verified."""


def _configured_category_urls(
    source_config: Dict[str, Any], scraper_configs: Iterable[Dict[str, Any]]
) -> List[str]:
    website_host = str(source_config["website_host"]).lower()
    path_prefix = str(source_config.get("category_path_prefix", "/ar/products/"))
    categories: Set[str] = set()

    for scraper in scraper_configs:
        if not isinstance(scraper, dict):
            continue
        url = scraper.get("url")
        if not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        if (parsed.hostname or "").lower() not in {
            website_host,
            f"www.{website_host}",
        }:
            continue
        if not parsed.path.startswith(path_prefix):
            continue
        categories.add(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")))

    return sorted(categories)


def _page_url(
    category_url: str, page: int, default_query_params: Dict[str, Any]
) -> str:
    parsed = urlsplit(category_url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "page"]
    query_keys = {key for key, _value in query}
    query.extend(
        (key, str(value))
        for key, value in default_query_params.items()
        if key not in query_keys
    )
    query.append(("page", str(page)))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, safe=":"), parsed.fragment)
    )


def _non_negative_int(value: Any, label: str, category: str, page: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WebsiteAdSourceError(
            f"{category} page {page}: invalid {label} in website response"
        )
    return value


def _publication_date(value: Any, category: str, page: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebsiteAdSourceError(
            f"{category} page {page}: missing publication date in website response"
        )
    try:
        published = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise WebsiteAdSourceError(
            f"{category} page {page}: invalid publication date {value!r}"
        ) from exc
    if published.tzinfo is None:
        raise WebsiteAdSourceError(
            f"{category} page {page}: publication date has no timezone"
        )
    return published.astimezone(timezone.utc).date().isoformat()


def _request_page(
    session: requests.Session,
    api_url: str,
    website_origin: str,
    category_url: str,
    page: int,
    page_size: int,
    default_query_params: Dict[str, Any],
    sleep_fn,
) -> Dict[str, Any]:
    payload = {
        "url": _page_url(category_url, page, default_query_params),
        "includeFavs": False,
        "pageSize": page_size,
    }
    last_error: Optional[Exception] = None

    for attempt in range(MAX_RETRIES):
        try:
            response = session.post(
                api_url,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": website_origin.rstrip("/"),
                    "Referer": website_origin.rstrip("/") + "/",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                    ),
                },
                timeout=DEFAULT_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise WebsiteAdSourceError(
                    f"{category_url} page {page}: expected a JSON object"
                )
            return data
        except WebsiteAdSourceError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 < MAX_RETRIES:
                sleep_fn(attempt + 1)

    raise WebsiteAdSourceError(
        f"{category_url} page {page}: website request failed: {last_error}"
    ) from last_error


def count_website_published_ads(
    source_config: Dict[str, Any],
    scraper_configs: Iterable[Dict[str, Any]],
    listing_date: str,
    session: Optional[requests.Session] = None,
    sleep_fn=time.sleep,
) -> Dict[str, Any]:
    """Count unique configured product ads published on *listing_date* (UTC)."""
    try:
        datetime.strptime(listing_date, "%Y-%m-%d")
        if not isinstance(source_config, dict):
            raise TypeError("source configuration must be an object")

        api_url = source_config["api_url"]
        source_id = source_config["source"]
        website_origin = source_config["website_origin"]
        id_field = source_config.get("id_field", "id")
        date_field = source_config.get("publication_date_field", "startDate")
        page_size = source_config.get("page_size", 500)
        default_query_params = source_config.get("category_query_defaults", {})
        request_interval = source_config.get("request_interval_seconds", 1.2)
        if not isinstance(api_url, str) or not api_url.startswith("https://"):
            raise ValueError("api_url must be an HTTPS URL")
        if not isinstance(website_origin, str) or not website_origin.startswith("https://"):
            raise ValueError("website_origin must be an HTTPS URL")
        if not isinstance(source_config.get("website_host"), str) or not source_config["website_host"]:
            raise ValueError("website_host must be a non-empty host name")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source must be a non-empty identifier")
        if not isinstance(id_field, str) or not id_field:
            raise ValueError("id_field must be a non-empty field name")
        if not isinstance(date_field, str) or not date_field:
            raise ValueError("publication_date_field must be a non-empty field name")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 500:
            raise ValueError("page_size must be an integer between 1 and 500")
        if not isinstance(default_query_params, dict):
            raise ValueError("category_query_defaults must be an object")
        if (
            isinstance(request_interval, bool)
            or not isinstance(request_interval, (int, float))
            or request_interval < 0
        ):
            raise ValueError("request_interval_seconds must be non-negative")
    except (KeyError, TypeError, ValueError) as exc:
        raise WebsiteAdSourceError(f"invalid website published-ad configuration: {exc}") from exc

    categories = _configured_category_urls(source_config, scraper_configs)
    if not categories:
        raise WebsiteAdSourceError("no product categories configured for website published-ad scan")

    http = session or requests.Session()
    own_session = session is None
    matching_ids: Set[str] = set()
    id_dates: Dict[str, str] = {}
    pages_scanned = 0
    source_ads_scanned = 0
    previous_request = 0.0
    try:
        for category_url in categories:
            expected_pages: Optional[int] = None
            expected_total: Optional[int] = None
            for page in range(10000):
                if previous_request:
                    elapsed = time.monotonic() - previous_request
                    wait = float(request_interval) - elapsed
                    if wait > 0:
                        sleep_fn(wait)

                data = _request_page(
                    http,
                    api_url,
                    website_origin,
                    category_url,
                    page,
                    page_size,
                    default_query_params,
                    sleep_fn,
                )
                previous_request = time.monotonic()
                pages_scanned += 1

                response_page = data.get("currentPage")
                if response_page is not None and response_page != page:
                    raise WebsiteAdSourceError(
                        f"{category_url} page {page}: API returned page {response_page}"
                    )

                pages = _non_negative_int(data.get("pagesCount"), "pagesCount", category_url, page)
                total = _non_negative_int(data.get("totalCount"), "totalCount", category_url, page)
                rows = data.get("list")
                if not isinstance(rows, list):
                    raise WebsiteAdSourceError(
                        f"{category_url} page {page}: expected list in website response"
                    )

                if expected_pages is None:
                    expected_pages = pages
                    expected_total = total
                    if pages > 10000:
                        raise WebsiteAdSourceError(
                            f"{category_url}: API reports an unreasonable page count ({pages})"
                        )
                elif pages != expected_pages or total != expected_total:
                    raise WebsiteAdSourceError(
                        f"{category_url} page {page}: pagination totals changed during scan"
                    )

                if pages == 0:
                    if total != 0 or rows:
                        raise WebsiteAdSourceError(
                            f"{category_url} page {page}: empty pagination conflicts with response data"
                        )
                    break
                if page >= pages:
                    raise WebsiteAdSourceError(
                        f"{category_url} page {page}: API returned more pages than pagesCount"
                    )
                if total and not rows:
                    raise WebsiteAdSourceError(
                        f"{category_url} page {page}: empty page before scan completed"
                    )
                if not total and rows:
                    raise WebsiteAdSourceError(
                        f"{category_url} page {page}: listings returned with totalCount 0"
                    )
                if len(rows) > page_size:
                    raise WebsiteAdSourceError(
                        f"{category_url} page {page}: response exceeds configured page size"
                    )

                source_ads_scanned += len(rows)
                for row in rows:
                    if not isinstance(row, dict):
                        raise WebsiteAdSourceError(
                            f"{category_url} page {page}: malformed listing entry"
                        )
                    ad_id = row.get(id_field)
                    if ad_id is None or isinstance(ad_id, bool) or not str(ad_id).strip():
                        raise WebsiteAdSourceError(
                            f"{category_url} page {page}: listing has no stable {id_field}"
                        )
                    normalized_id = str(ad_id).strip()
                    published_date = _publication_date(row.get(date_field), category_url, page)
                    previous_date = id_dates.get(normalized_id)
                    if previous_date is not None and previous_date != published_date:
                        raise WebsiteAdSourceError(
                            f"{category_url} page {page}: listing {normalized_id} has conflicting publication dates"
                        )
                    id_dates[normalized_id] = published_date
                    if published_date == listing_date:
                        matching_ids.add(normalized_id)

                if page + 1 == pages:
                    break
            else:
                raise WebsiteAdSourceError(
                    f"{category_url}: scan exceeded the maximum page count"
                )
    finally:
        if own_session:
            http.close()

    log.info(
        "Website published-ad scan completed: %s categories, %s pages, %s source listings, "
        "%s unique ads for %s",
        len(categories),
        pages_scanned,
        source_ads_scanned,
        len(matching_ids),
        listing_date,
    )
    return {
        "website_published_ads": len(matching_ids),
        "website_published_ads_date": listing_date,
        "website_published_ads_source": source_id,
        "website_published_ads_status": "ok",
        "website_published_ads_categories_scanned": len(categories),
        "website_published_ads_pages_scanned": pages_scanned,
        "website_published_ads_source_listings_scanned": source_ads_scanned,
    }


def website_published_ads_report(
    source_config: Any,
    scraper_configs: Iterable[Dict[str, Any]],
    listing_date: str,
    session: Optional[requests.Session] = None,
    sleep_fn=time.sleep,
) -> Dict[str, Any]:
    """Return report fields for a complete scan or an explicit error result."""
    source_id = source_config.get("source") if isinstance(source_config, dict) else None
    report = {
        "website_published_ads": None,
        "website_published_ads_date": listing_date,
        "website_published_ads_source": source_id,
        "website_published_ads_status": "error",
    }
    try:
        report.update(
            count_website_published_ads(
                source_config, scraper_configs, listing_date, session=session, sleep_fn=sleep_fn
            )
        )
    except Exception as exc:
        error = str(exc).strip() or exc.__class__.__name__
        report["website_published_ads_error"] = error[:300]
        log.error("Website published-ad scan failed for %s: %s", listing_date, error)
    return report
