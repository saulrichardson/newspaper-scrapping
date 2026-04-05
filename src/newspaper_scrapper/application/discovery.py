"""Issue discovery through the live `/papers/` page."""

from __future__ import annotations

import csv
import json
import time
from collections import defaultdict
from pathlib import Path

from newspaper_scrapper.adapters.chrome import cdp
from newspaper_scrapper.adapters.newspapers import browse, papers
from newspaper_scrapper.application.auth import launch_browser
from newspaper_scrapper.config import Settings


def read_issue_rows(base_csv: Path, confirmed_csv: Path | None) -> list[dict[str, str]]:
    confirmed_ids: set[str] = set()
    if confirmed_csv and confirmed_csv.exists():
        confirmed_ids = {row["issue_id"] for row in csv.DictReader(confirmed_csv.open())}
    rows: list[dict[str, str]] = []
    with base_csv.open(newline="") as handle:
        for row in csv.DictReader(handle):
            issue_id = row["issue_id"].strip()
            if issue_id in confirmed_ids:
                continue
            rows.append(
                {
                    "issue_id": issue_id,
                    "issue_date": row["issue_date"].strip(),
                    "newspaper_display_name": row["newspaper_display_name"].strip(),
                    "city": row.get("newspaperarchive_publication_city_name", "").strip(),
                    "state": row.get("newspaperarchive_publication_state_abbr", "").strip(),
                    "search_query": row.get("search_query", row["newspaper_display_name"]).strip(),
                }
            )
    return rows


def rank_families(rows: list[dict[str, str]]) -> list[tuple[str, list[dict[str, str]]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["newspaper_display_name"]].append(row)
    return sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0].lower()))


def append_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    write_header = not path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def discover_issues_via_papers(
    settings: Settings,
    *,
    base_csv: Path,
    confirmed_csv: Path | None,
    output_dir: Path,
    family_limit: int,
    family_offset: int,
    page_load_seconds: float,
    sleep_between_families: float,
    max_api_retries: int,
    api_backoff_seconds: float,
) -> dict[str, object]:
    launch_browser(settings)
    rows = read_issue_rows(base_csv, confirmed_csv)
    families = rank_families(rows)
    families = families[family_offset : family_offset + family_limit]
    if not families:
        raise RuntimeError("No newspaper families selected for this run")

    output_dir.mkdir(parents=True, exist_ok=True)
    family_csv = output_dir / "family_results.csv"
    issue_csv = output_dir / "issue_results.csv"
    summary_json = output_dir / "summary.json"

    papers_pages = cdp.list_page_tabs(settings.chrome_debug_base)
    papers_tab = None
    for page in papers_pages:
        if str(page.get("url", "")).startswith("https://www.newspapers.com/papers/"):
            papers_tab = page
            break
    if papers_tab is None:
        for page in papers_pages:
            if "newspapers.com" in str(page.get("url", "")):
                papers_tab = page
                cdp.navigate(page["webSocketDebuggerUrl"], "https://www.newspapers.com/papers/")
                time.sleep(page_load_seconds)
                break
    if papers_tab is None:
        raise RuntimeError("Could not find any open Newspapers.com tab to reuse")
    ws_url = papers_tab["webSocketDebuggerUrl"]

    summary: dict[str, object] = {
        "family_limit": family_limit,
        "family_offset": family_offset,
        "families_selected": len(families),
        "families_processed": 0,
        "issues_marked_available": 0,
        "issues_marked_unavailable": 0,
        "issues_unresolved": 0,
        "current_family": "",
    }
    summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))

    family_fieldnames = [
        "newspaper_display_name",
        "family_issue_count",
        "query_string",
        "papers_search_url",
        "papers_result_status",
        "papers_showing_count",
        "selected_paper_url",
        "selected_paper_text",
        "browse_base",
    ]
    issue_fieldnames = [
        "issue_id",
        "issue_date",
        "newspaper_display_name",
        "query_string",
        "selected_paper_url",
        "browse_base",
        "exact_issue_status",
        "exact_issue_url",
        "exact_issue_detail",
    ]

    for family_name, family_rows in families:
        summary["current_family"] = family_name
        summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))

        query_string = family_name
        search_url = papers.papers_search_url(query_string)
        cdp.navigate(ws_url, search_url)
        time.sleep(page_load_seconds)
        search_state = cdp.evaluate_json(ws_url, papers.papers_search_expression())
        title = str(search_state.get("title", ""))
        if "Access denied" in title or "Cloudflare" in title:
            raise RuntimeError(f"Cloudflare challenge on /papers/ search page: {title}")

        cards = search_state.get("cards") or []
        result_status, selected_card = papers.choose_card(cards, family_rows)
        selected_paper_url = "" if selected_card is None else selected_card["href"]
        selected_paper_text = "" if selected_card is None else selected_card["text"]
        browse_base = ""

        if selected_card is not None:
            cdp.navigate(ws_url, selected_paper_url)
            time.sleep(page_load_seconds)
            paper_state = cdp.evaluate_json(ws_url, papers.paper_page_expression())
            paper_title = str(paper_state.get("title", ""))
            if "Access denied" in paper_title or "Cloudflare" in paper_title:
                raise RuntimeError(f"Cloudflare challenge on paper page: {paper_title}")
            browse_base = (
                papers.choose_browse_base(paper_state.get("browseLinks") or [], selected_paper_url)
                or ""
            )
            if not browse_base:
                result_status = "selected_without_browse_base"

        append_csv(
            family_csv,
            family_fieldnames,
            [
                {
                    "newspaper_display_name": family_name,
                    "family_issue_count": str(len(family_rows)),
                    "query_string": query_string,
                    "papers_search_url": search_url,
                    "papers_result_status": result_status,
                    "papers_showing_count": (
                        "" if search_state.get("showing") is None else str(search_state["showing"])
                    ),
                    "selected_paper_url": selected_paper_url,
                    "selected_paper_text": selected_paper_text,
                    "browse_base": browse_base,
                }
            ],
        )

        issue_rows_out: list[dict[str, str]] = []
        if browse_base:
            selected_year_range = papers.parse_year_range(selected_paper_text)
            for issue in family_rows:
                issue_year = int(issue["issue_date"][:4])
                if selected_year_range is not None:
                    start, end = selected_year_range
                    if not (start <= issue_year <= end):
                        exact_status, exact_issue_url, detail = (
                            "out_of_range_for_selected_paper",
                            "",
                            f"{start}-{end}",
                        )
                    else:
                        exact_status, exact_issue_url, detail = browse.confirm_exact_issue(
                            browse_base,
                            issue["issue_date"],
                            max_retries=max_api_retries,
                            retry_backoff_seconds=api_backoff_seconds,
                        )
                else:
                    exact_status, exact_issue_url, detail = browse.confirm_exact_issue(
                        browse_base,
                        issue["issue_date"],
                        max_retries=max_api_retries,
                        retry_backoff_seconds=api_backoff_seconds,
                    )
                issue_rows_out.append(
                    {
                        "issue_id": issue["issue_id"],
                        "issue_date": issue["issue_date"],
                        "newspaper_display_name": issue["newspaper_display_name"],
                        "query_string": query_string,
                        "selected_paper_url": selected_paper_url,
                        "browse_base": browse_base,
                        "exact_issue_status": exact_status,
                        "exact_issue_url": exact_issue_url,
                        "exact_issue_detail": detail,
                    }
                )
        else:
            for issue in family_rows:
                issue_rows_out.append(
                    {
                        "issue_id": issue["issue_id"],
                        "issue_date": issue["issue_date"],
                        "newspaper_display_name": issue["newspaper_display_name"],
                        "query_string": query_string,
                        "selected_paper_url": selected_paper_url,
                        "browse_base": browse_base,
                        "exact_issue_status": "unresolved_family_match",
                        "exact_issue_url": "",
                        "exact_issue_detail": result_status,
                    }
                )

        append_csv(issue_csv, issue_fieldnames, issue_rows_out)
        summary["families_processed"] = int(summary["families_processed"]) + 1
        for row in issue_rows_out:
            status = row["exact_issue_status"]
            if status == "available":
                summary["issues_marked_available"] = int(summary["issues_marked_available"]) + 1
            elif status == "unavailable":
                summary["issues_marked_unavailable"] = int(summary["issues_marked_unavailable"]) + 1
            else:
                summary["issues_unresolved"] = int(summary["issues_unresolved"]) + 1
        summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True))
        time.sleep(sleep_between_families)

    return summary
