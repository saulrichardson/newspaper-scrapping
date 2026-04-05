from __future__ import annotations

from newspaper_scrapper.application import screenshot
from newspaper_scrapper.domain.models import ManifestRow


def _row(issue_id: str, page_num: str) -> ManifestRow:
    return ManifestRow(
        issue_id=issue_id,
        issue_date="1973-01-02",
        page_num=page_num,
        preferred_image_id=f"img-{issue_id}-{page_num}",
        preferred_image_page_url=f"https://www.newspapers.com/image/{issue_id}-{page_num}/",
        raw_row={},
    )


def test_build_failed_manifest_rows_keeps_failed_and_unattempted() -> None:
    manifest_rows = [_row("issue-a", "1"), _row("issue-a", "2"), _row("issue-b", "9")]
    result_rows = [
        {
            "issue_id": "issue-a",
            "page_num": "1",
            "status": "captured",
        },
        {
            "issue_id": "issue-a",
            "page_num": "2",
            "status": "failed",
        },
    ]

    remaining = screenshot._build_failed_manifest_rows(manifest_rows, result_rows)

    assert [(row.issue_id, row.page_num) for row in remaining] == [
        ("issue-a", "2"),
        ("issue-b", "9"),
    ]


def test_merge_production_rows_prefers_later_capture_and_preserves_failures() -> None:
    manifest_rows = [_row("issue-a", "1"), _row("issue-a", "2")]
    pass_rows = [
        (
            1,
            "pass_01",
            [
                {
                    "run_index": "1",
                    "issue_id": "issue-a",
                    "issue_date": "1973-01-02",
                    "page_num": "1",
                    "preferred_image_id": "img-issue-a-1",
                    "preferred_image_page_url": "https://www.newspapers.com/image/issue-a-1/",
                    "status": "failed",
                    "output_path": "",
                    "selected_strategy": "",
                    "mean_luma": "",
                    "bright240_fraction": "",
                    "natural_width": "",
                    "natural_height": "",
                    "elapsed_seconds": "",
                    "error_type": "RuntimeError",
                    "error_message": "bad first pass",
                },
                {
                    "run_index": "2",
                    "issue_id": "issue-a",
                    "issue_date": "1973-01-02",
                    "page_num": "2",
                    "preferred_image_id": "img-issue-a-2",
                    "preferred_image_page_url": "https://www.newspapers.com/image/issue-a-2/",
                    "status": "failed",
                    "output_path": "",
                    "selected_strategy": "",
                    "mean_luma": "",
                    "bright240_fraction": "",
                    "natural_width": "",
                    "natural_height": "",
                    "elapsed_seconds": "",
                    "error_type": "RuntimeError",
                    "error_message": "still failed",
                },
            ],
        ),
        (
            2,
            "pass_02",
            [
                {
                    "run_index": "1",
                    "issue_id": "issue-a",
                    "issue_date": "1973-01-02",
                    "page_num": "1",
                    "preferred_image_id": "img-issue-a-1",
                    "preferred_image_page_url": "https://www.newspapers.com/image/issue-a-1/",
                    "status": "captured",
                    "output_path": "/tmp/issue-a-1.png",
                    "selected_strategy": "synthetic_full_image",
                    "mean_luma": "200",
                    "bright240_fraction": "0.1",
                    "natural_width": "4000",
                    "natural_height": "6000",
                    "elapsed_seconds": "8.0",
                    "error_type": "",
                    "error_message": "",
                }
            ],
        ),
    ]

    merged = screenshot._merge_production_rows(manifest_rows, pass_rows)

    assert merged[0]["status"] == "captured"
    assert merged[0]["pass_index"] == "2"
    assert merged[0]["pass_name"] == "pass_02"
    assert merged[1]["status"] == "failed"
    assert merged[1]["error_message"] == "still failed"
    assert merged[1]["pass_index"] == "1"
