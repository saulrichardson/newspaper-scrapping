"""Issue page inventory cataloging."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from newspaper_scrapper.adapters.newspapers import browse


def catalog_issue_pages(
    *,
    confirmed_csv: Path,
    output_dir: Path,
    target_page_manifest: Path | None,
    sleep_seconds: float,
    max_retries: int,
    retry_backoff_seconds: float,
) -> dict[str, object]:
    issues = browse.read_confirmed_issues(confirmed_csv)
    output_dir.mkdir(parents=True, exist_ok=True)

    issue_summary_path = output_dir / "issue_page_inventory_summary.csv"
    page_inventory_path = output_dir / "issue_page_inventory.csv"
    processed_issue_ids = set()
    if issue_summary_path.exists():
        processed_issue_ids = {
            row["issue_id"]
            for row in browse.read_csv_rows(issue_summary_path)
            if row.get("issue_id")
        }

    newly_enumerated_count = 0
    for idx, issue in enumerate(issues, start=1):
        if issue.issue_id in processed_issue_ids:
            continue
        issue_summary, issue_pages = browse.enumerate_issue_pages(
            issue,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        browse.append_csv_rows(issue_summary_path, [issue_summary])
        browse.append_csv_rows(page_inventory_path, issue_pages)
        processed_issue_ids.add(issue.issue_id)
        newly_enumerated_count += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)
        if idx % 25 == 0:
            print(
                f"Checked {idx}/{len(issues)} issues; enumerated {len(processed_issue_ids)} total",
                file=sys.stderr,
            )

    issue_summary_rows = browse.read_csv_rows(issue_summary_path)
    page_rows = browse.read_csv_rows(page_inventory_path)
    summary: dict[str, object] = {
        "confirmed_issue_count": len(issues),
        "issue_page_inventory_rows": len(page_rows),
        "issues_enumerated_successfully": len(issue_summary_rows),
        "issues_enumerated_in_this_run": newly_enumerated_count,
    }

    if target_page_manifest:
        target_rows = browse.read_target_manifest(target_page_manifest)
        joined_rows, joined_summary = browse.build_target_join_rows(target_rows, page_rows)
        browse.write_csv(output_dir / "target_page_image_manifest.csv", joined_rows)
        preferred_only_rows = [
            row for row in joined_rows if row["matched_issue_page"] == "true"
        ]
        if preferred_only_rows:
            browse.write_csv(
                output_dir / "target_page_image_manifest_preferred_only.csv",
                preferred_only_rows,
            )
        summary["target_page_join"] = joined_summary

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary
