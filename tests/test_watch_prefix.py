from __future__ import annotations

import pytest

from newspaper_scrapper.application import watch_prefix
from newspaper_scrapper.application.watch_prefix import select_latest_screenshot_prefix


def test_list_child_prefixes_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    responses = [
        {
            "IsTruncated": True,
            "NextContinuationToken": "token-1",
            "CommonPrefixes": [
                {"Prefix": "results/screenshot-a/"},
                {"Prefix": ""},
                {},
            ],
        },
        {
            "IsTruncated": False,
            "CommonPrefixes": [
                {"Prefix": "results/screenshot-b/"},
            ],
        },
    ]

    def fake_run_aws_json(command: list[str]) -> dict[str, object]:
        calls.append(command)
        return responses.pop(0)

    monkeypatch.setattr(watch_prefix, "_run_aws_json", fake_run_aws_json)

    prefixes = watch_prefix.list_child_prefixes("bucket-name", "results")

    assert prefixes == ["results/screenshot-a/", "results/screenshot-b/"]
    assert "--continuation-token" not in calls[0]
    assert calls[1][-2:] == ["--continuation-token", "token-1"]


def test_list_child_prefixes_rejects_truncated_response_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_aws_json(_command: list[str]) -> dict[str, object]:
        return {
            "IsTruncated": True,
            "CommonPrefixes": [{"Prefix": "results/screenshot-a/"}],
        }

    monkeypatch.setattr(watch_prefix, "_run_aws_json", fake_run_aws_json)

    with pytest.raises(RuntimeError, match="without NextContinuationToken"):
        watch_prefix.list_child_prefixes("bucket-name", "results")


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
