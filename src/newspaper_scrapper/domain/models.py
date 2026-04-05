"""Domain models for scraper manifests."""

from __future__ import annotations

from dataclasses import dataclass


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.7680.80 Safari/537.36"
)


@dataclass(frozen=True)
class IssueSeedRow:
    issue_id: str
    issue_date: str
    newspaper_display_name: str
    city: str
    state: str
    search_query: str
    raw_row: dict[str, str]


@dataclass(frozen=True)
class ConfirmedIssue:
    issue_id: str
    issue_date: str
    newspaper_display_name: str
    matched_paper_url: str
    exact_issue_url: str


@dataclass(frozen=True)
class BrowseBranch:
    node_type: str
    name: str
    display_name: str


@dataclass(frozen=True)
class ManifestRow:
    issue_id: str
    issue_date: str
    page_num: str
    preferred_image_id: str
    preferred_image_page_url: str
    raw_row: dict[str, str]
