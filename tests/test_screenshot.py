from __future__ import annotations

import csv
import json
from pathlib import Path

from newspaper_scrapper.application import screenshot
from newspaper_scrapper.config import Settings


def test_classify_blocking_stop_reason_detects_auth_required() -> None:
    exc = RuntimeError(
        'Timed out waiting for image page DOM to expose signed image metadata: '
        '{"bodySnippet":"You need a subscription to view this page"}'
    )

    assert (
        screenshot._classify_blocking_stop_reason(exc)
        == screenshot.STOP_REASON_AUTH_REQUIRED
    )


def test_classify_blocking_stop_reason_detects_cloudflare_challenge() -> None:
    exc = RuntimeError("Cloudflare challenge while waiting for image page DOM")

    assert (
        screenshot._classify_blocking_stop_reason(exc)
        == screenshot.STOP_REASON_CLOUDFLARE_CHALLENGE
    )


def test_adaptive_sleep_controller_decreases_after_clean_streak() -> None:
    controller = screenshot.AdaptiveSleepController(
        enabled=True,
        current_sleep_seconds=1.5,
        min_sleep_seconds=0.5,
        max_sleep_seconds=2.0,
        step_seconds=0.25,
        clean_streak_threshold=2,
        slow_page_threshold_seconds=10.0,
    )

    first = controller.record_page_result(
        elapsed_seconds=7.0,
        page_attempts=1,
        had_retryable_error=False,
    )
    second = controller.record_page_result(
        elapsed_seconds=6.5,
        page_attempts=1,
        had_retryable_error=False,
    )

    assert first == 1.5
    assert second == 1.25
    assert controller.last_adjustment_reason == "decreased_after_clean_streak"


def test_adaptive_sleep_controller_increases_after_slow_page() -> None:
    controller = screenshot.AdaptiveSleepController(
        enabled=True,
        current_sleep_seconds=1.0,
        min_sleep_seconds=0.5,
        max_sleep_seconds=2.0,
        step_seconds=0.25,
        clean_streak_threshold=3,
        slow_page_threshold_seconds=10.0,
    )

    next_sleep = controller.record_page_result(
        elapsed_seconds=13.0,
        page_attempts=1,
        had_retryable_error=False,
    )

    assert next_sleep == 1.25
    assert controller.last_adjustment_reason == "increased_after_slow_or_retried_page"


def test_capture_pages_from_manifest_clears_stale_stopped_reason_after_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manifest_csv = tmp_path / "manifest.csv"
    with manifest_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=screenshot.MANIFEST_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "issue_id": "issue-1",
                "issue_date": "2005-01-01",
                "page_num": "1",
                "preferred_image_id": "111",
                "preferred_image_page_url": "https://example.com/image/111/",
            }
        )
        writer.writerow(
            {
                "issue_id": "issue-2",
                "issue_date": "2005-01-01",
                "page_num": "2",
                "preferred_image_id": "222",
                "preferred_image_page_url": "https://example.com/image/222/",
            }
        )

    output_dir = tmp_path / "output"
    settings = Settings(
        data_dir=tmp_path / "data",
        chrome_profile_dir=tmp_path / "profile",
    )

    calls = {"count": 0}
    summary_writes: list[dict[str, object]] = []

    def fake_capture_viewer_screenshot(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise FileNotFoundError("temporary local output mismatch")
        page_dir = kwargs["output_dir"]
        page_dir.mkdir(parents=True, exist_ok=True)
        image_path = page_dir / "222_viewer.png"
        image_path.write_text("fake image placeholder")
        return {
            "output_path": image_path,
            "elapsed_seconds": 6.0,
            "selected_output_stem": "viewer",
            "selected_strategy": screenshot.STRATEGY_TILES,
            "strategy_runs": [
                {
                    "output_stem": "viewer",
                    "strategy": screenshot.STRATEGY_TILES,
                    "render_state": {"strategy": "synthetic_tile_canvas"},
                    "render_progress": [{"naturalWidth": 1000, "naturalHeight": 2000}],
                    "attempt": {"metrics": {"mean_luma": 123.0, "bright240_fraction": 0.1}},
                    "timings": {
                        "render_seconds": 2.0,
                        "settle_seconds": 0.1,
                        "capture_seconds": 1.0,
                        "validation_seconds": 0.0,
                    },
                }
            ],
            "timings": {
                "probe_seconds": 0.4,
                "hydrate_seconds": 0.2,
            },
        }

    monkeypatch.setattr(screenshot, "capture_viewer_screenshot", fake_capture_viewer_screenshot)
    monkeypatch.setattr(screenshot.time, "sleep", lambda *_args, **_kwargs: None)
    original_write_summary = screenshot._write_summary

    def recording_write_summary(path: Path, summary: dict[str, object]) -> None:
        summary_writes.append(dict(summary))
        original_write_summary(path, summary)

    monkeypatch.setattr(screenshot, "_write_summary", recording_write_summary)

    result = screenshot.capture_pages_from_manifest(
        settings,
        manifest_csv=manifest_csv,
        output_dir=output_dir,
        page_load_seconds=1.0,
        render_wait_seconds=1.0,
        sleep_between_pages=0.0,
        sleep_jitter_seconds=0.0,
        continue_on_error=True,
        reusable_ws_url="ws://example.test/devtools/page/1",
        reusable_target_id="target-1",
    )

    assert result["captured_this_run"] == 1
    assert result["failed_this_run"] == 1
    assert result["stopped_reason"] == "completed_run"

    summary = json.loads((output_dir / "summary.json").read_text())
    assert summary["stopped_reason"] == "completed_run"
    assert any(
        candidate.get("captured_this_run") == 1
        and candidate.get("failed_this_run") == 1
        and candidate.get("stopped_reason") == ""
        for candidate in summary_writes
    )
