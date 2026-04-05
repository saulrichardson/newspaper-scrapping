"""Operator-facing alert extraction for AWS screenshot workers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


BLOCKING_STOP_REASONS = frozenset({"cloudflare_challenge", "auth_required"})


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _summary_updated_epoch(summary_path: Path, summary: dict[str, Any]) -> float:
    updated = summary.get("updated_at_epoch")
    if isinstance(updated, (float, int)):
        return float(updated)
    return summary_path.stat().st_mtime if summary_path.exists() else 0.0


def _extract_stop_reason(worker_summary: dict[str, Any]) -> tuple[str, str]:
    stop_reason = str(worker_summary.get("stopped_reason", "") or "")
    stop_message = ""
    pass_summaries = worker_summary.get("pass_summaries") or []
    if pass_summaries:
        last_pass = pass_summaries[-1] or {}
        if not stop_reason:
            stop_reason = str(last_pass.get("blocking_stop_reason", "") or "")
        stop_message = str(last_pass.get("blocking_stop_message", "") or "")
        if not stop_reason:
            nested = last_pass.get("runner_summary") or {}
            stop_reason = str(nested.get("stopped_reason", "") or "")
    return stop_reason, stop_message


def extract_screenshot_operator_alerts(s3_root: Path) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for instance_dir in sorted(path for path in s3_root.iterdir() if path.is_dir()):
        run_summary_path = instance_dir / "run" / "summary.json"
        runner_summary_path = instance_dir / "run" / "runner_summary.json"
        run_summary = _load_json(run_summary_path)
        runner_summary = _load_json(runner_summary_path)
        if not run_summary and not runner_summary:
            continue
        completed_summary = run_summary or runner_summary or {}
        runner_state = runner_summary or run_summary or {}

        dcv_connection = _load_json(instance_dir / "state" / "dcv_connection.json") or {}
        updated_epoch = max(
            _summary_updated_epoch(run_summary_path, run_summary or {}),
            _summary_updated_epoch(runner_summary_path, runner_summary or {}),
        )

        active_workers = {
            str(item.get("worker_name", "") or "")
            for item in (runner_state.get("active_workers") or [])
            if isinstance(item, dict)
        }
        failed_workers = {
            str(name or "") for name in (runner_state.get("failed_workers") or [])
        }
        delayed_workers = {
            str(name or "") for name in (runner_state.get("delayed_workers") or [])
        }
        pending_workers = {
            str(name or "") for name in (runner_state.get("pending_workers") or [])
        }
        blocking_workers = failed_workers | delayed_workers
        non_blocking_workers = active_workers | pending_workers

        completed = completed_summary.get("completed_worker_summaries") or {}
        for worker_name, worker_summary in completed.items():
            if not isinstance(worker_summary, dict):
                continue
            # Only emit an operator alert when the current runner state still treats
            # this worker as blocked. Old stop reasons remain in completed summaries
            # after manual intervention and resume, so suppress them once the worker
            # is active/pending again.
            if worker_name in non_blocking_workers:
                continue
            if blocking_workers and worker_name not in blocking_workers:
                continue
            stop_reason, stop_message = _extract_stop_reason(worker_summary)
            if stop_reason not in BLOCKING_STOP_REASONS:
                continue

            public_ip = str(
                dcv_connection.get("public_ip")
                or dcv_connection.get("web_url", "").replace("https://", "").split(":")[0]
                or ""
            )
            web_url = str(dcv_connection.get("web_url", "") or "")
            alert_key = (
                f"{instance_dir.name}:{worker_name}:{stop_reason}:{int(updated_epoch)}"
            )
            alerts.append(
                {
                    "alert_key": alert_key,
                    "instance_id": instance_dir.name,
                    "worker_name": worker_name,
                    "stop_reason": stop_reason,
                    "stop_message": stop_message,
                    "updated_at_epoch": updated_epoch,
                    "public_ip": public_ip,
                    "web_url": web_url,
                    "session_id": str(dcv_connection.get("session_id", "") or ""),
                    "captured_rows": int(worker_summary.get("captured_rows", 0) or 0),
                    "failed_rows": int(worker_summary.get("failed_rows", 0) or 0),
                    "pages_per_minute_estimate": worker_summary.get(
                        "pages_per_minute_estimate"
                    ),
                    "remaining_failures_manifest_csv": str(
                        worker_summary.get("remaining_failures_manifest_csv", "") or ""
                    ),
                }
            )

    alerts.sort(key=lambda item: (item["updated_at_epoch"], item["instance_id"], item["worker_name"]))
    return alerts


def load_operator_alert_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_alert_keys": []}
    payload = json.loads(path.read_text())
    seen_keys = payload.get("seen_alert_keys")
    if not isinstance(seen_keys, list):
        seen_keys = []
    return {"seen_alert_keys": [str(key) for key in seen_keys]}


def select_new_operator_alerts(
    alerts: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seen = set(str(key) for key in (state.get("seen_alert_keys") or []))
    new_alerts = [alert for alert in alerts if str(alert["alert_key"]) not in seen]
    updated_state = {"seen_alert_keys": sorted(seen | {str(alert["alert_key"]) for alert in alerts})}
    return new_alerts, updated_state
