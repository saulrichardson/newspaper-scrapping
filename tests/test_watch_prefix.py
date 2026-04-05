from __future__ import annotations

from newspaper_scrapper.application.watch_prefix import select_latest_screenshot_prefix


def test_select_latest_screenshot_prefix_prefers_active_runs() -> None:
    selected = select_latest_screenshot_prefix(
        [
            {
                "prefix": "results/screenshot-old",
                "active_worker_count": 0,
                "updated_at_epoch": 200.0,
            },
            {
                "prefix": "results/screenshot-current",
                "active_worker_count": 1,
                "updated_at_epoch": 100.0,
            },
        ]
    )

    assert selected == "results/screenshot-current"


def test_select_latest_screenshot_prefix_falls_back_to_newest_update() -> None:
    selected = select_latest_screenshot_prefix(
        [
            {
                "prefix": "results/screenshot-a",
                "active_worker_count": 0,
                "updated_at_epoch": 100.0,
            },
            {
                "prefix": "results/screenshot-b",
                "active_worker_count": 0,
                "updated_at_epoch": 200.0,
            },
        ]
    )

    assert selected == "results/screenshot-b"
