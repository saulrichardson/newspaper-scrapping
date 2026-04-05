from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from newspaper_scrapper.application import screenshot
from newspaper_scrapper.application import screenshot_workers


def _write_manifest(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
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
        writer.writerows(rows)


def test_plan_screenshot_workers_groups_issue_pages_together(tmp_path: Path) -> None:
    manifest_csv = tmp_path / "manifest.csv"
    _write_manifest(
        manifest_csv,
        [
            {
                "issue_id": "issue-a",
                "issue_date": "1973-01-02",
                "page_num": "1",
                "preferred_image_id": "img-a-1",
                "preferred_image_page_url": "https://www.newspapers.com/image/img-a-1/",
            },
            {
                "issue_id": "issue-a",
                "issue_date": "1973-01-02",
                "page_num": "2",
                "preferred_image_id": "img-a-2",
                "preferred_image_page_url": "https://www.newspapers.com/image/img-a-2/",
            },
            {
                "issue_id": "issue-b",
                "issue_date": "1973-01-03",
                "page_num": "4",
                "preferred_image_id": "img-b-4",
                "preferred_image_page_url": "https://www.newspapers.com/image/img-b-4/",
            },
        ],
    )

    result = screenshot_workers.plan_screenshot_workers(
        manifest_csv=manifest_csv,
        output_dir=tmp_path / "plan",
        worker_count=2,
        grouping_mode="issue",
        base_debug_port=9701,
        profile_root=None,
        cookies_json=None,
        strategy="synthetic_tiles",
        page_load_seconds=6.0,
        render_wait_seconds=8.0,
        sleep_between_pages=0.0,
        sleep_jitter_seconds=0.0,
        adaptive_sleep=True,
        min_sleep_between_pages=0.25,
        max_sleep_between_pages=1.5,
        sleep_step_seconds=0.25,
        clean_streak_threshold=3,
        slow_page_threshold_seconds=10.0,
        post_render_settle_seconds=0.1,
        recycle_browser_every_pages=100,
        max_passes=3,
        pass_page_load_increment=0.75,
        pass_render_wait_increment=2.0,
        stop_on_stall=True,
        restart_browser_before_run=True,
        restart_browser_each_pass=True,
    )

    assert result["total_pages"] == 3
    assert result["total_groups"] == 2
    plan_csv = Path(result["worker_plan_csv"])
    with plan_csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["adaptive_sleep"] == "true"
    assert rows[0]["post_render_settle_seconds"] == "0.1"
    assert rows[0]["recycle_browser_every_pages"] == "100"

    manifest_rows_by_worker: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        manifest_path = Path(row["manifest_csv"])
        with manifest_path.open(newline="") as handle:
            manifest_rows_by_worker[row["worker_name"]] = list(csv.DictReader(handle))

    issue_a_workers = [
        worker
        for worker, rows_for_worker in manifest_rows_by_worker.items()
        if any(row["issue_id"] == "issue-a" for row in rows_for_worker)
    ]
    assert issue_a_workers == [issue_a_workers[0]]
    assert len(issue_a_workers) == 1


def test_merge_screenshot_workers_prefers_captured_rows(tmp_path: Path) -> None:
    workers_root = tmp_path / "workers"
    worker_1 = workers_root / "worker_01" / "run"
    worker_2 = workers_root / "worker_02" / "run"
    worker_1.mkdir(parents=True)
    worker_2.mkdir(parents=True)

    fieldnames = screenshot_workers.PRODUCTION_RESULT_FIELDNAMES
    with (worker_1 / "final_results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "run_index": "1",
                "issue_id": "issue-a",
                "issue_date": "1973-01-02",
                "page_num": "1",
                "preferred_image_id": "img-a-1",
                "preferred_image_page_url": "https://www.newspapers.com/image/img-a-1/",
                "status": "failed",
                "output_path": "",
                "selected_strategy": "",
                "mean_luma": "",
                "bright240_fraction": "",
                "natural_width": "",
                "natural_height": "",
                "elapsed_seconds": "",
                "error_type": "RuntimeError",
                "error_message": "bad",
                "pass_index": "1",
                "pass_name": "pass_01",
            }
        )
    with (worker_2 / "final_results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "run_index": "1",
                "issue_id": "issue-a",
                "issue_date": "1973-01-02",
                "page_num": "1",
                "preferred_image_id": "img-a-1",
                "preferred_image_page_url": "https://www.newspapers.com/image/img-a-1/",
                "status": "captured",
                "output_path": "/tmp/img-a-1.png",
                "selected_strategy": "synthetic_tiles",
                "mean_luma": "180",
                "bright240_fraction": "0.01",
                "natural_width": "3000",
                "natural_height": "5000",
                "elapsed_seconds": "5.0",
                "error_type": "",
                "error_message": "",
                "pass_index": "2",
                "pass_name": "pass_02",
            }
        )
    (worker_1 / "summary.json").write_text(json.dumps({"captured_rows": 0, "failed_rows": 1}))
    (worker_2 / "summary.json").write_text(json.dumps({"captured_rows": 1, "failed_rows": 0}))

    result = screenshot_workers.merge_screenshot_workers(
        workers_root=workers_root,
        output_dir=tmp_path / "merged",
    )

    assert result["captured_rows"] == 1
    assert result["failed_rows"] == 0
    captured_manifest = Path(result["captured_manifest_merged_csv"])
    with captured_manifest.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["preferred_image_page_url"] == "https://www.newspapers.com/image/img-a-1/"


def test_merge_screenshot_workers_accepts_flattened_worker_layout(tmp_path: Path) -> None:
    workers_root = tmp_path / "workers"
    worker_dir = workers_root / "i-abc123__worker_01"
    worker_dir.mkdir(parents=True)

    fieldnames = screenshot_workers.PRODUCTION_RESULT_FIELDNAMES
    with (worker_dir / "final_results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "run_index": "1",
                "issue_id": "issue-flat",
                "issue_date": "2005-01-01",
                "page_num": "23",
                "preferred_image_id": "183939628",
                "preferred_image_page_url": "https://www.newspapers.com/image/183939628/",
                "status": "captured",
                "output_path": "/tmp/183939628_viewer.png",
                "selected_strategy": "synthetic_tiles",
                "mean_luma": "175",
                "bright240_fraction": "0.02",
                "natural_width": "3641",
                "natural_height": "6993",
                "elapsed_seconds": "7.5",
                "error_type": "",
                "error_message": "",
                "pass_index": "1",
                "pass_name": "pass_01",
            }
        )
    (worker_dir / "summary.json").write_text(json.dumps({"captured_rows": 1, "failed_rows": 0}))

    result = screenshot_workers.merge_screenshot_workers(
        workers_root=workers_root,
        output_dir=tmp_path / "merged",
    )

    assert result["captured_rows"] == 1
    assert result["unique_pages"] == 1


def test_merge_screenshot_workers_uses_latest_pass_results_when_final_missing(
    tmp_path: Path,
) -> None:
    workers_root = tmp_path / "workers"
    worker_dir = workers_root / "i-live__worker_01"
    pass_dir = worker_dir / "passes" / "pass_01"
    pass_dir.mkdir(parents=True)

    with (pass_dir / "results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=screenshot.RESULT_FIELDNAMES)
        writer.writeheader()
        writer.writerow(
            {
                "run_index": "1",
                "issue_id": "issue-live",
                "issue_date": "2005-01-03",
                "page_num": "13",
                "preferred_image_id": "145008306",
                "preferred_image_page_url": "https://www.newspapers.com/image/145008306/",
                "status": "captured",
                "output_path": "/tmp/145008306_viewer.png",
                "selected_strategy": "synthetic_tile_canvas",
                "mean_luma": "171.9",
                "bright240_fraction": "0.01",
                "natural_width": "",
                "natural_height": "",
                "elapsed_seconds": "6.6",
                "error_type": "",
                "error_message": "",
            }
        )
    (pass_dir / "summary.json").write_text(
        json.dumps({"captured_this_run": 1, "failed_this_run": 0, "stopped_reason": ""})
    )

    result = screenshot_workers.merge_screenshot_workers(
        workers_root=workers_root,
        output_dir=tmp_path / "merged",
    )

    assert result["captured_rows"] == 1
    assert result["unique_pages"] == 1
    final_results = Path(result["final_results_merged_csv"])
    with final_results.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["pass_index"] == "1"
    assert rows[0]["pass_name"] == "pass_01"


def test_run_screenshot_workers_stops_on_cloudflare_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_csv = tmp_path / "worker_plan.csv"
    plan_csv.write_text("worker_name\nworker_01\n")
    worker_root = tmp_path / "worker_01"
    manifest_csv = worker_root / "input_manifest.csv"
    manifest_csv.parent.mkdir(parents=True)
    manifest_csv.write_text("issue_id,issue_date,page_num,preferred_image_id,preferred_image_page_url\n")

    row = {
        "worker_name": "worker_01",
        "manifest_csv": str(manifest_csv),
        "output_dir": str(worker_root / "run"),
        "chrome_debug_port": "9701",
        "chrome_profile_dir": str(tmp_path / "chrome-profile"),
        "strategy": "synthetic_tiles",
        "page_load_seconds": "6.0",
        "render_wait_seconds": "8.0",
        "sleep_between_pages": "0.0",
        "sleep_jitter_seconds": "0.0",
        "max_passes": "3",
        "pass_page_load_increment": "0.75",
        "pass_render_wait_increment": "2.0",
        "stop_on_stall": "true",
        "restart_browser_before_run": "false",
        "restart_browser_each_pass": "false",
    }

    monkeypatch.setattr(screenshot_workers, "_load_worker_plan", lambda _: [row])
    monkeypatch.setattr(
        screenshot_workers,
        "build_worker_shell_script",
        lambda *args, **kwargs: "exit 0",
    )

    class _FakeProcess:
        pid = 12345

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(screenshot_workers.subprocess, "Popen", lambda *args, **kwargs: _FakeProcess())
    monkeypatch.setattr(
        screenshot_workers,
        "_load_worker_completion_summary",
        lambda _: {"stopped_reason": screenshot.STOP_REASON_CLOUDFLARE_CHALLENGE},
    )
    monkeypatch.setattr(screenshot_workers.time, "sleep", lambda _: None)

    result = screenshot_workers.run_screenshot_workers(
        plan_csv=plan_csv,
        output_dir=tmp_path / "runner",
        max_concurrent_workers=1,
        worker_stagger_seconds=0.0,
        retry_cooldown_seconds=300.0,
        max_worker_attempts=10,
        cookies_json=None,
        poll_seconds=0.0,
    )

    assert result["completed_worker_count"] == 0
    assert result["failed_worker_count"] == 1
    assert result["failed_workers"] == ["worker_01"]
