"""Signed page image download flows."""

from __future__ import annotations

import csv
import json
import random
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

from newspaper_scrapper.adapters.chrome import cdp
from newspaper_scrapper.adapters.newspapers import browse, image
from newspaper_scrapper.application.auth import launch_browser
from newspaper_scrapper.config import Settings
from newspaper_scrapper.domain.models import ConfirmedIssue, ManifestRow


RESULT_FIELDNAMES = [
    "run_index",
    "issue_id",
    "issue_date",
    "page_num",
    "preferred_image_id",
    "preferred_image_page_url",
    "status",
    "output_path",
    "byte_count",
    "content_type",
    "page_title",
    "error_type",
    "error_message",
]


def read_manifest(path: Path) -> list[ManifestRow]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "issue_id",
            "issue_date",
            "page_num",
            "preferred_image_id",
            "preferred_image_page_url",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing required columns: {sorted(missing)}")
        rows = []
        for row in reader:
            rows.append(
                ManifestRow(
                    issue_id=row["issue_id"].strip(),
                    issue_date=row["issue_date"].strip(),
                    page_num=row["page_num"].strip(),
                    preferred_image_id=row["preferred_image_id"].strip(),
                    preferred_image_page_url=row["preferred_image_page_url"].strip(),
                    raw_row={k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()},
                )
            )
        return rows


def load_completed_keys(results_csv: Path) -> set[tuple[str, str]]:
    if not results_csv.exists():
        return set()
    with results_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        completed: set[tuple[str, str]] = set()
        for row in reader:
            if row.get("status") == "downloaded":
                completed.add((row["issue_id"], row["page_num"]))
        return completed


def append_result(results_csv: Path, row: dict[str, str]) -> None:
    write_header = not results_csv.exists()
    with results_csv.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.write_text(json.dumps(summary, indent=2, sort_keys=True))


def error_type_and_message(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, HTTPError):
        body = b""
        try:
            body = exc.read()
        except Exception:
            body = b""
        body_text = body.decode("utf-8", errors="replace")
        if exc.code == 429:
            return "rate_limited", body_text or str(exc)
        return f"http_{exc.code}", body_text or str(exc)
    return exc.__class__.__name__, str(exc)


def build_output_path(base_dir: Path, row: ManifestRow) -> Path:
    issue_dir = base_dir / row.issue_id
    issue_dir.mkdir(parents=True, exist_ok=True)
    return issue_dir / f"{row.page_num.zfill(4)}__{row.preferred_image_id}.jpg"


def find_newspapers_tab_ws(settings: Settings) -> str:
    pages = cdp.list_page_tabs(settings.chrome_debug_base)
    for page in pages:
        if "newspapers.com" in str(page.get("url", "")):
            ws_url = page.get("webSocketDebuggerUrl")
            if ws_url:
                return str(ws_url)
    raise RuntimeError("No open Newspapers.com Chrome tab found")


def probe_live_page(
    row: ManifestRow,
    *,
    target_ws: str,
    settings: Settings,
    page_load_seconds: float,
) -> dict[str, Any]:
    cdp.navigate(target_ws, row.preferred_image_page_url)
    time.sleep(page_load_seconds)
    live_probe = cdp.evaluate_json(target_ws, image.page_probe_expression())
    title = str(live_probe.get("title", ""))
    body = str(live_probe.get("bodySnippet", ""))
    if "Access denied" in title or "Cloudflare" in title:
        raise RuntimeError(f"Cloudflare challenge page detected: {title}")
    if "Sign in to Newspapers.com" in title or "Sign in to Newspapers.com" in body:
        raise RuntimeError("Chrome session is no longer signed into Newspapers.com")
    if "/image/" not in str(live_probe.get("url", "")):
        raise RuntimeError(
            f"Unexpected page after navigation for {row.preferred_image_page_url}: {title}"
        )
    return live_probe


def download_pages_from_manifest(
    settings: Settings,
    *,
    manifest_csv: Path,
    output_dir: Path,
    page_load_seconds: float,
    sleep_between_pages: float,
    sleep_jitter_seconds: float,
    limit: int | None,
    start_offset: int,
) -> dict[str, Any]:
    launch_browser(settings)
    target_ws = find_newspapers_tab_ws(settings)
    manifest_rows = read_manifest(manifest_csv)
    output_dir.mkdir(parents=True, exist_ok=True)
    results_csv = output_dir / "results.csv"
    summary_json = output_dir / "summary.json"

    completed = load_completed_keys(results_csv)
    remaining = [
        row for row in manifest_rows if (row.issue_id, row.page_num) not in completed
    ]
    if start_offset:
        remaining = remaining[start_offset:]
    if limit is not None:
        remaining = remaining[:limit]

    summary: dict[str, Any] = {
        "manifest_csv": str(manifest_csv),
        "output_dir": str(output_dir),
        "total_manifest_rows": len(manifest_rows),
        "already_downloaded_rows": len(completed),
        "run_candidate_rows": len(remaining),
        "downloaded_this_run": 0,
        "stopped_reason": "",
        "last_issue_id": "",
        "last_page_num": "",
        "sleep_between_pages": sleep_between_pages,
        "sleep_jitter_seconds": sleep_jitter_seconds,
        "page_load_seconds": page_load_seconds,
    }
    write_summary(summary_json, summary)

    for run_index, row in enumerate(remaining, start=1):
        output_path = build_output_path(output_dir, row)
        try:
            live_probe = probe_live_page(
                row,
                target_ws=target_ws,
                settings=settings,
                page_load_seconds=page_load_seconds,
            )
            full_image_url = image.build_full_image_url(live_probe)
            download_meta = image.download_binary(full_image_url, output_path)
            result = {
                "run_index": str(run_index),
                "issue_id": row.issue_id,
                "issue_date": row.issue_date,
                "page_num": row.page_num,
                "preferred_image_id": row.preferred_image_id,
                "preferred_image_page_url": row.preferred_image_page_url,
                "status": "downloaded",
                "output_path": str(output_path),
                "byte_count": str(download_meta["byte_count"]),
                "content_type": str(download_meta["content_type"]),
                "page_title": str(live_probe.get("title", "")),
                "error_type": "",
                "error_message": "",
            }
            append_result(results_csv, result)
            summary["downloaded_this_run"] += 1
            summary["last_issue_id"] = row.issue_id
            summary["last_page_num"] = row.page_num
            write_summary(summary_json, summary)
            sleep_seconds = sleep_between_pages + (
                random.uniform(0.0, sleep_jitter_seconds)
                if sleep_jitter_seconds > 0
                else 0.0
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except Exception as exc:
            err_type, err_msg = error_type_and_message(exc)
            result = {
                "run_index": str(run_index),
                "issue_id": row.issue_id,
                "issue_date": row.issue_date,
                "page_num": row.page_num,
                "preferred_image_id": row.preferred_image_id,
                "preferred_image_page_url": row.preferred_image_page_url,
                "status": "failed",
                "output_path": str(output_path),
                "byte_count": "",
                "content_type": "",
                "page_title": "",
                "error_type": err_type,
                "error_message": err_msg,
            }
            append_result(results_csv, result)
            summary["stopped_reason"] = err_type
            summary["last_issue_id"] = row.issue_id
            summary["last_page_num"] = row.page_num
            write_summary(summary_json, summary)
            raise

    summary["stopped_reason"] = "completed_run"
    write_summary(summary_json, summary)
    return summary


def parse_page_selection(selection: str, available_pages: list[str]) -> list[str]:
    if selection == "all":
        return sorted(available_pages, key=lambda value: int(value))
    chosen: set[str] = set()
    for token in selection.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_str, end_str = token.split("-", 1)
            start = int(start_str)
            end = int(end_str)
            for value in range(start, end + 1):
                chosen.add(str(value))
        else:
            chosen.add(str(int(token)))
    return [page for page in sorted(available_pages, key=lambda value: int(value)) if page in chosen]


def download_issue(
    settings: Settings,
    *,
    exact_issue_url: str,
    issue_id: str,
    issue_date: str,
    newspaper_display_name: str,
    matched_paper_url: str,
    output_dir: Path,
    pages: str,
    page_load_seconds: float,
    sleep_between_pages: float,
    sleep_jitter_seconds: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> dict[str, Any]:
    confirmed_issue = ConfirmedIssue(
        issue_id=issue_id,
        issue_date=issue_date,
        newspaper_display_name=newspaper_display_name,
        matched_paper_url=matched_paper_url,
        exact_issue_url=exact_issue_url,
    )
    _, page_rows = browse.enumerate_issue_pages(
        confirmed_issue,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    available_pages = [row["page_num"] for row in page_rows if row["page_num"].isdigit()]
    target_pages = parse_page_selection(pages, available_pages)
    manifest_rows = [
        ManifestRow(
            issue_id=issue_id,
            issue_date=issue_date,
            page_num=row["page_num"],
            preferred_image_id=row["image_id"],
            preferred_image_page_url=row["image_page_url"],
            raw_row=row,
        )
        for row in page_rows
        if row["page_num"] in target_pages
    ]
    manifest_csv = output_dir / "issue_manifest.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    with manifest_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "issue_id",
                "issue_date",
                "page_num",
                "preferred_image_id",
                "preferred_image_page_url",
            ],
        )
        writer.writeheader()
        for row in manifest_rows:
            writer.writerow(
                {
                    "issue_id": row.issue_id,
                    "issue_date": row.issue_date,
                    "page_num": row.page_num,
                    "preferred_image_id": row.preferred_image_id,
                    "preferred_image_page_url": row.preferred_image_page_url,
                }
            )
    return download_pages_from_manifest(
        settings,
        manifest_csv=manifest_csv,
        output_dir=output_dir,
        page_load_seconds=page_load_seconds,
        sleep_between_pages=sleep_between_pages,
        sleep_jitter_seconds=sleep_jitter_seconds,
        limit=None,
        start_offset=0,
    )
