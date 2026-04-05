from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path


def _load_progress_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "aws"
        / "watch_screenshot_progress.py"
    )
    spec = importlib.util.spec_from_file_location("watch_screenshot_progress", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_results_csv(path: Path) -> None:
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
                "status",
                "output_path",
                "elapsed_seconds",
                "selected_strategy",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "issue_id": "issue-1",
                "issue_date": "2005-01-01",
                "page_num": "1",
                "preferred_image_id": "111",
                "preferred_image_page_url": "https://example.com/image/111/",
                "status": "captured",
                "output_path": "/opt/newscom/run/workers/worker_01/passes/pass_01/issue-1/0001__111/111_viewer.png",
                "elapsed_seconds": "7.2",
                "selected_strategy": "synthetic_tile_canvas",
            }
        )
        writer.writerow(
            {
                "issue_id": "issue-2",
                "issue_date": "2005-01-01",
                "page_num": "2",
                "preferred_image_id": "222",
                "preferred_image_page_url": "https://example.com/image/222/",
                "status": "failed",
                "output_path": "",
                "elapsed_seconds": "",
                "selected_strategy": "",
            }
        )


def test_build_global_report_treats_unfinished_backlog_as_remaining_rows(tmp_path: Path) -> None:
    module = _load_progress_module()

    instance_dir = tmp_path / "i-abc123"
    run_dir = instance_dir / "run"
    state_dir = instance_dir / "state"
    worker_results = run_dir / "workers" / "worker_01" / "passes" / "pass_01" / "results.csv"
    _write_results_csv(worker_results)

    state_dir.mkdir(parents=True)
    (state_dir / "dcv_connection.json").write_text(
        json.dumps(
            {
                "public_ip": "203.0.113.10",
                "web_url": "https://203.0.113.10:8443/#newscom-shot-01",
                "session_id": "newscom-shot-01",
            }
        )
    )
    (run_dir / "runner_summary.json").write_text(
        json.dumps(
            {
                "active_workers": [{"worker_name": "worker_01"}],
                "completed_worker_summaries": {
                    "worker_01": {
                        "captured_rows": 4606,
                        "failed_rows": 13794,
                        "subset_rows": 18400,
                        "pages_per_minute_estimate": 8.0,
                    }
                },
            }
        )
    )

    report = module.build_global_report(tmp_path, "results/test-prefix")
    assert report is not None

    worker = report["workers"][0]
    assert worker["capture_count"] == 4606
    assert worker["attempt_failure_count"] == 1
    assert worker["remaining_count"] == 13794

    totals = report["totals"]
    assert totals["captured_total"] == 4606
    assert totals["attempt_failures_total"] == 1
    assert totals["remaining_total"] == 13794


def test_build_message_labels_attempt_failures_and_remaining_rows() -> None:
    module = _load_progress_module()

    report = {
        "totals": {
            "workers_total": 1,
            "workers_active": 1,
            "workers_completed": 0,
            "workers_blocked": 0,
            "workers_pending": 0,
            "captured_total": 4606,
            "attempt_failures_total": 1,
            "remaining_total": 13794,
            "manifest_total": 18400,
            "ppm_total": 8.0,
        },
        "workers": [
            {
                "worker_name": "worker_01",
                "instance_id": "i-abc123",
                "state": "active",
                "capture_count": 4606,
                "attempt_failure_count": 1,
                "remaining_count": 13794,
                "manifest_total": 18400,
                "ppm": 8.0,
            }
        ],
    }

    _subject, message = module.build_message(report, [])

    assert "Attempt failures total: 1" in message
    assert "Remaining rows total: 13794" in message
    assert "13794 remaining, 1 attempt failures so far" in message
