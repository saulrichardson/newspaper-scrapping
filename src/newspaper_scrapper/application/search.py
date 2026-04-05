"""Keyword search via the authenticated Newspapers.com search API."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

from newspaper_scrapper.adapters.chrome import cdp
from newspaper_scrapper.adapters.newspapers import search as search_adapter
from newspaper_scrapper.application.auth import launch_browser
from newspaper_scrapper.config import Settings


RESULT_FIELDNAMES = [
    "query_keyword",
    "query_date",
    "query_location",
    "entity_types",
    "search_api_url",
    "api_page_index",
    "api_page_size",
    "search_record_index",
    "record_type",
    "publication_id",
    "publication_canonical_id",
    "newspaper_display_name",
    "publication_location",
    "issue_id",
    "issue_date",
    "page_num",
    "image_id",
    "image_page_url",
    "viewer_url",
]

PAGE_MANIFEST_FIELDNAMES = [
    "issue_id",
    "issue_date",
    "page_num",
    "preferred_image_id",
    "preferred_image_page_url",
    "query_keyword",
    "query_date",
    "query_location",
    "entity_types",
    "publication_id",
    "publication_canonical_id",
    "newspaper_display_name",
    "publication_location",
    "first_api_page_index",
    "first_search_record_index",
    "viewer_url",
]


def append_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    write_header = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return sum(1 for _ in reader)


def load_existing_image_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            str(row.get("preferred_image_id", "")).strip()
            for row in reader
            if str(row.get("preferred_image_id", "")).strip()
        }


def load_existing_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def find_newspapers_tab_ws(settings: Settings) -> str:
    pages = cdp.list_page_tabs(settings.chrome_debug_base)
    for page in pages:
        if "newspapers.com" in str(page.get("url", "")):
            ws_url = page.get("webSocketDebuggerUrl")
            if ws_url:
                return str(ws_url)
    raise RuntimeError("No open Newspapers.com Chrome tab found")


def normalize_record_row(
    *,
    record: dict[str, Any],
    keyword: str,
    date: str | None,
    date_start: str | None,
    date_end: str | None,
    location: str | None,
    entity_types: str,
    search_api_url: str,
    api_page_index: int,
    api_page_size: int,
    search_record_index: int,
) -> dict[str, str] | None:
    page = record.get("page") or {}
    publication = record.get("publication") or {}

    image_id = str(page.get("id", "")).strip()
    issue_date = str(page.get("date", "")).strip()
    page_num = str(page.get("pageNumber", "")).strip()
    publication_name = str(publication.get("name", "")).strip()
    publication_location = str(publication.get("location", "")).strip()
    publication_id = str(publication.get("id", "")).strip()
    publication_canonical_id = str(publication.get("canonicalId", "")).strip()
    viewer_url = str(page.get("viewerUrl", "")).strip()

    if not image_id or not issue_date or not page_num or not publication_name:
        return None

    issue_id = search_adapter.build_search_issue_id(
        publication_name=publication_name,
        issue_date=issue_date,
        publication_canonical_id=publication_canonical_id or publication_id,
    )

    return {
        "query_keyword": keyword,
        "query_date": build_query_date_value(
            date=date,
            date_start=date_start,
            date_end=date_end,
        ),
        "query_location": location or "",
        "entity_types": entity_types,
        "search_api_url": search_api_url,
        "api_page_index": str(api_page_index),
        "api_page_size": str(api_page_size),
        "search_record_index": str(search_record_index),
        "record_type": str(record.get("type", "")).strip(),
        "publication_id": publication_id,
        "publication_canonical_id": publication_canonical_id,
        "newspaper_display_name": publication_name,
        "publication_location": publication_location,
        "issue_id": issue_id,
        "issue_date": issue_date,
        "page_num": page_num,
        "image_id": image_id,
        "image_page_url": search_adapter.canonical_image_page_url(image_id),
        "viewer_url": viewer_url,
    }


def page_manifest_row_from_record(record_row: dict[str, str]) -> dict[str, str]:
    return {
        "issue_id": record_row["issue_id"],
        "issue_date": record_row["issue_date"],
        "page_num": record_row["page_num"],
        "preferred_image_id": record_row["image_id"],
        "preferred_image_page_url": record_row["image_page_url"],
        "query_keyword": record_row["query_keyword"],
        "query_date": record_row["query_date"],
        "query_location": record_row["query_location"],
        "entity_types": record_row["entity_types"],
        "publication_id": record_row["publication_id"],
        "publication_canonical_id": record_row["publication_canonical_id"],
        "newspaper_display_name": record_row["newspaper_display_name"],
        "publication_location": record_row["publication_location"],
        "first_api_page_index": record_row["api_page_index"],
        "first_search_record_index": record_row["search_record_index"],
        "viewer_url": record_row["viewer_url"],
    }


def build_query_date_value(
    *,
    date: str | None,
    date_start: str | None,
    date_end: str | None,
) -> str:
    if date:
        return date
    if date_start or date_end:
        return f"{date_start or ''}_{date_end or ''}"
    return ""


def validate_resume(
    summary: dict[str, Any],
    *,
    keyword: str,
    date: str | None,
    date_start: str | None,
    date_end: str | None,
    location: str | None,
    entity_types: str,
) -> None:
    if not summary:
        return
    expected = {
        "query_keyword": keyword,
        "query_date": build_query_date_value(
            date=date,
            date_start=date_start,
            date_end=date_end,
        ),
        "query_location": location or "",
        "entity_types": entity_types,
    }
    for key, value in expected.items():
        existing = str(summary.get(key, ""))
        if existing and existing != value:
            raise RuntimeError(
                f"Existing output dir was created for {key}={existing!r}, not {value!r}"
            )


def search_content(
    settings: Settings,
    *,
    keyword: str,
    output_dir: Path,
    date: str | None,
    date_start: str | None,
    date_end: str | None,
    location: str | None,
    entity_types: str,
    max_pages: int,
    count_per_request: int,
    page_load_seconds: float,
    sleep_between_requests: float,
    start_token: str | None,
    resume: bool,
    max_api_retries: int,
    api_backoff_seconds: float,
    navigate_search_results: bool,
) -> dict[str, object]:
    launch_browser(settings)
    target_ws = find_newspapers_tab_ws(settings)

    search_results_url = search_adapter.build_search_results_url(
        keyword=keyword,
        date=date,
        date_start=date_start,
        date_end=date_end,
        location=location,
    )
    if navigate_search_results:
        cdp.navigate(target_ws, search_results_url)
        time.sleep(page_load_seconds)

    output_dir.mkdir(parents=True, exist_ok=True)
    results_csv = output_dir / "results.csv"
    page_manifest_csv = output_dir / "page_manifest.csv"
    summary_json = output_dir / "summary.json"

    existing_summary = load_existing_summary(summary_json) if resume else {}
    validate_resume(
        existing_summary,
        keyword=keyword,
        date=date,
        date_start=date_start,
        date_end=date_end,
        location=location,
        entity_types=entity_types,
    )

    if existing_summary.get("completed") is True and not start_token:
        return existing_summary

    existing_image_ids = load_existing_image_ids(page_manifest_csv) if resume else set()
    records_written = count_csv_rows(results_csv) if resume else 0
    unique_pages_written = len(existing_image_ids)
    api_pages_fetched = int(existing_summary.get("api_pages_fetched", 0)) if resume else 0
    total_record_count = int(existing_summary.get("record_count", 0)) if resume else 0

    next_start = (
        start_token
        if start_token is not None
        else str(existing_summary.get("last_next_start", "")).strip() or "*"
    )
    last_search_api_url = str(existing_summary.get("last_search_api_url", "")).strip()
    if not last_search_api_url and next_start:
        last_search_api_url = search_adapter.build_search_api_url(
            keyword=keyword,
            date=date,
            date_start=date_start,
            date_end=date_end,
            location=location,
            entity_types=entity_types,
            count=count_per_request,
            start=next_start,
        )

    summary: dict[str, Any] = {
        "query_keyword": keyword,
        "query_date": build_query_date_value(
            date=date,
            date_start=date_start,
            date_end=date_end,
        ),
        "query_location": location or "",
        "entity_types": entity_types,
        "search_results_url": search_results_url,
        "results_csv": str(results_csv),
        "page_manifest_csv": str(page_manifest_csv),
        "count_per_request": count_per_request,
        "api_pages_fetched": api_pages_fetched,
        "records_written": records_written,
        "unique_pages_written": unique_pages_written,
        "record_count": total_record_count,
        "last_next_start": next_start,
        "last_search_api_url": last_search_api_url,
        "completed": False,
        "sleep_between_requests": sleep_between_requests,
        "max_api_retries": max_api_retries,
        "api_backoff_seconds": api_backoff_seconds,
    }
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))

    while next_start and api_pages_fetched < max_pages:
        api_page_index = api_pages_fetched + 1
        search_api_url = search_adapter.build_search_api_url(
            keyword=keyword,
            date=date,
            date_start=date_start,
            date_end=date_end,
            location=location,
            entity_types=entity_types,
            count=count_per_request,
            start=next_start,
        )
        fetch_state: dict[str, Any] | None = None
        for attempt in range(1, max_api_retries + 2):
            fetch_state = cdp.evaluate_json(
                target_ws,
                search_adapter.search_api_fetch_expression(search_api_url),
                await_promise=True,
            )
            status = int(fetch_state.get("status", 0))
            payload = fetch_state.get("payload")
            text_snippet = str(fetch_state.get("textSnippet", ""))
            if status == 200 and isinstance(payload, dict):
                break
            if "Cloudflare" in text_snippet or "Access denied" in text_snippet:
                raise RuntimeError(
                    f"Cloudflare challenge while fetching search API page {api_page_index}"
                )
            if status == 429 and attempt <= max_api_retries:
                summary.update(
                    {
                        "last_error": f"429 on api_page_index={api_page_index} attempt={attempt}",
                        "last_search_api_url": search_api_url,
                    }
                )
                summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))
                if api_backoff_seconds > 0:
                    time.sleep(api_backoff_seconds * attempt)
                continue
            raise RuntimeError(
                f"Unexpected search API response status={status} for {search_api_url}: "
                f"{text_snippet[:500]}"
            )

        assert fetch_state is not None
        payload = fetch_state.get("payload")

        records = payload.get("records") or []
        record_rows: list[dict[str, str]] = []
        page_rows: list[dict[str, str]] = []
        for idx, record in enumerate(records, start=1):
            record_row = normalize_record_row(
                record=record,
                keyword=keyword,
                date=date,
                date_start=date_start,
                date_end=date_end,
                location=location,
                entity_types=entity_types,
                search_api_url=search_api_url,
                api_page_index=api_page_index,
                api_page_size=count_per_request,
                search_record_index=records_written + idx,
            )
            if not record_row:
                continue
            record_rows.append(record_row)
            image_id = record_row["image_id"]
            if image_id not in existing_image_ids:
                existing_image_ids.add(image_id)
                page_rows.append(page_manifest_row_from_record(record_row))

        append_csv(results_csv, RESULT_FIELDNAMES, record_rows)
        append_csv(page_manifest_csv, PAGE_MANIFEST_FIELDNAMES, page_rows)

        records_written += len(record_rows)
        unique_pages_written += len(page_rows)
        api_pages_fetched += 1
        total_record_count = int(payload.get("recordCount", total_record_count) or total_record_count)
        next_start = str(payload.get("nextStart", "")).strip()

        summary.update(
            {
                "api_pages_fetched": api_pages_fetched,
                "records_written": records_written,
                "unique_pages_written": unique_pages_written,
                "record_count": total_record_count,
                "last_next_start": next_start,
                "last_search_api_url": search_api_url,
                "last_error": "",
            }
        )
        summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))

        if total_record_count == 0 and not record_rows:
            next_start = ""
            summary["last_next_start"] = ""
            summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))
            break

        if total_record_count > 0 and records_written >= total_record_count:
            next_start = ""
            summary["last_next_start"] = ""
            summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))
            break

        if not next_start:
            break
        if sleep_between_requests > 0:
            time.sleep(sleep_between_requests)

    summary["completed"] = not bool(next_start)
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))
    return json.loads(summary_json.read_text())
