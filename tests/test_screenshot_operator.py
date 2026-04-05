from __future__ import annotations

import json
from pathlib import Path

from newspaper_scrapper.application import screenshot_operator


def test_extract_screenshot_operator_alerts_detects_cloudflare_block(tmp_path: Path) -> None:
    instance_dir = tmp_path / "i-abc123"
    (instance_dir / "run").mkdir(parents=True)
    (instance_dir / "state").mkdir(parents=True)

    (instance_dir / "state" / "dcv_connection.json").write_text(
        json.dumps(
            {
                "public_ip": "203.0.113.10",
                "session_id": "newscom-shot-01",
                "web_url": "https://203.0.113.10:8443/#newscom-shot-01",
            }
        )
    )
    (instance_dir / "run" / "summary.json").write_text(
        json.dumps(
            {
                "updated_at_epoch": 1234567890.0,
                "completed_worker_summaries": {
                    "worker_01": {
                        "stopped_reason": "cloudflare_challenge",
                        "captured_rows": 207,
                        "failed_rows": 293,
                        "pages_per_minute_estimate": 7.4,
                        "pass_summaries": [
                            {
                                "blocking_stop_reason": "cloudflare_challenge",
                                "blocking_stop_message": "Cloudflare challenge while waiting for image page DOM",
                            }
                        ],
                    }
                },
            }
        )
    )

    alerts = screenshot_operator.extract_screenshot_operator_alerts(tmp_path)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["stop_reason"] == "cloudflare_challenge"
    assert alert["web_url"] == "https://203.0.113.10:8443/#newscom-shot-01"
    assert alert["captured_rows"] == 207
    assert alert["alert_key"] == "i-abc123:worker_01:cloudflare_challenge:1234567890"


def test_select_new_operator_alerts_dedupes_seen_keys() -> None:
    alerts = [
        {"alert_key": "a"},
        {"alert_key": "b"},
    ]

    new_alerts, updated_state = screenshot_operator.select_new_operator_alerts(
        alerts,
        {"seen_alert_keys": ["a"]},
    )

    assert new_alerts == [{"alert_key": "b"}]
    assert updated_state == {"seen_alert_keys": ["a", "b"]}


def test_extract_screenshot_operator_alerts_ignores_resolved_block_when_worker_active(
    tmp_path: Path,
) -> None:
    instance_dir = tmp_path / "i-abc123"
    (instance_dir / "run").mkdir(parents=True)
    (instance_dir / "state").mkdir(parents=True)

    (instance_dir / "state" / "dcv_connection.json").write_text(
        json.dumps(
            {
                "public_ip": "203.0.113.10",
                "session_id": "newscom-shot-01",
                "web_url": "https://203.0.113.10:8443/#newscom-shot-01",
            }
        )
    )
    (instance_dir / "run" / "summary.json").write_text(
        json.dumps(
            {
                "updated_at_epoch": 1234567891.0,
                "active_workers": [{"worker_name": "worker_01", "pid": 999}],
                "failed_workers": [],
                "completed_worker_summaries": {
                    "worker_01": {
                        "stopped_reason": "cloudflare_challenge",
                        "captured_rows": 207,
                        "failed_rows": 293,
                        "pages_per_minute_estimate": 7.4,
                        "pass_summaries": [
                            {
                                "blocking_stop_reason": "cloudflare_challenge",
                                "blocking_stop_message": "Cloudflare challenge while waiting for image page DOM",
                            }
                        ],
                    }
                },
            }
        )
    )

    alerts = screenshot_operator.extract_screenshot_operator_alerts(tmp_path)

    assert alerts == []


def test_extract_screenshot_operator_alerts_prefers_live_runner_state_over_stale_run_summary(
    tmp_path: Path,
) -> None:
    instance_dir = tmp_path / "i-abc123"
    (instance_dir / "run").mkdir(parents=True)
    (instance_dir / "state").mkdir(parents=True)

    (instance_dir / "state" / "dcv_connection.json").write_text(
        json.dumps(
            {
                "public_ip": "203.0.113.10",
                "session_id": "newscom-shot-01",
                "web_url": "https://203.0.113.10:8443/#newscom-shot-01",
            }
        )
    )
    (instance_dir / "run" / "summary.json").write_text(
        json.dumps(
            {
                "updated_at_epoch": 1234567890.0,
                "failed_workers": ["worker_01"],
                "completed_worker_summaries": {
                    "worker_01": {
                        "stopped_reason": "cloudflare_challenge",
                        "captured_rows": 207,
                        "failed_rows": 293,
                        "pages_per_minute_estimate": 7.4,
                        "pass_summaries": [
                            {
                                "blocking_stop_reason": "cloudflare_challenge",
                                "blocking_stop_message": "Cloudflare challenge while waiting for image page DOM",
                            }
                        ],
                    }
                },
            }
        )
    )
    (instance_dir / "run" / "runner_summary.json").write_text(
        json.dumps(
            {
                "updated_at_epoch": 1234567999.0,
                "active_workers": [{"worker_name": "worker_01", "pid": 999}],
                "failed_workers": [],
                "completed_worker_summaries": {},
            }
        )
    )

    alerts = screenshot_operator.extract_screenshot_operator_alerts(tmp_path)

    assert alerts == []
