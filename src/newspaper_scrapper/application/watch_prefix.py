"""Helpers for resolving screenshot monitor S3 prefixes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


PREFIX_AUTO_LATEST_ACTIVE_SCREENSHOT = "latest-active-screenshot"


def _normalize_prefix(prefix: str) -> str:
    return prefix.rstrip("/")


def _run_aws_json(command: list[str]) -> Any:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown aws cli error"
        raise RuntimeError(message)
    payload = result.stdout.strip()
    return json.loads(payload) if payload else {}


def list_child_prefixes(bucket: str, prefix: str) -> list[str]:
    normalized = _normalize_prefix(prefix) + "/"
    command_base = [
        "aws",
        "s3api",
        "list-objects-v2",
        "--bucket",
        bucket,
        "--prefix",
        normalized,
        "--delimiter",
        "/",
    ]
    prefixes: list[str] = []
    continuation_token: str | None = None
    while True:
        command = list(command_base)
        if continuation_token:
            command.extend(["--continuation-token", continuation_token])
        payload = _run_aws_json(command)
        prefixes.extend(
            str(item.get("Prefix", "") or "")
            for item in (payload.get("CommonPrefixes") or [])
            if str(item.get("Prefix", "") or "")
        )

        if not payload.get("IsTruncated"):
            return prefixes
        next_token = str(payload.get("NextContinuationToken", "") or "")
        if not next_token:
            raise RuntimeError(
                f"aws s3api list-objects-v2 returned a truncated response without NextContinuationToken for s3://{bucket}/{normalized}"
            )
        continuation_token = next_token


def fetch_s3_json(bucket: str, key: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["aws", "s3", "cp", f"s3://{bucket}/{key}", "-"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    payload = result.stdout.strip()
    if not payload:
        return None
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def select_latest_screenshot_prefix(run_summaries: list[dict[str, object]]) -> str | None:
    if not run_summaries:
        return None

    def rank_key(item: dict[str, object]) -> tuple[int, float, str]:
        active_count = int(item.get("active_worker_count", 0) or 0)
        updated_at_epoch = float(item.get("updated_at_epoch", 0.0) or 0.0)
        prefix = str(item.get("prefix", "") or "")
        return (1 if active_count > 0 else 0, updated_at_epoch, prefix)

    winner = max(run_summaries, key=rank_key)
    return str(winner.get("prefix", "") or "") or None


def discover_latest_active_screenshot_prefix(
    bucket: str,
    *,
    results_root_prefix: str = "results/",
) -> str:
    run_summaries: list[dict[str, object]] = []
    for run_prefix in list_child_prefixes(bucket, results_root_prefix):
        normalized_run_prefix = _normalize_prefix(run_prefix)
        run_name = PurePosixPath(normalized_run_prefix).name
        if not run_name.startswith("screenshot-"):
            continue

        active_worker_count = 0
        updated_at_epoch = 0.0
        saw_summary = False
        for instance_prefix in list_child_prefixes(bucket, normalized_run_prefix):
            summary = fetch_s3_json(
                bucket,
                f"{_normalize_prefix(instance_prefix)}/run/runner_summary.json",
            )
            if summary is None:
                continue
            saw_summary = True
            active_worker_count += len(summary.get("active_workers") or [])
            updated_value = summary.get("updated_at_epoch")
            if isinstance(updated_value, (int, float)):
                updated_at_epoch = max(updated_at_epoch, float(updated_value))

        if saw_summary:
            run_summaries.append(
                {
                    "prefix": normalized_run_prefix,
                    "active_worker_count": active_worker_count,
                    "updated_at_epoch": updated_at_epoch,
                }
            )

    selected = select_latest_screenshot_prefix(run_summaries)
    if selected is None:
        raise RuntimeError(
            f"Could not find any screenshot run prefixes with runner summaries under s3://{bucket}/{_normalize_prefix(results_root_prefix)}/"
        )
    return selected


def resolve_watch_prefix(
    *,
    bucket: str | None,
    prefix: str | None,
    prefix_auto: str | None,
) -> str:
    if prefix_auto:
        if prefix_auto != PREFIX_AUTO_LATEST_ACTIVE_SCREENSHOT:
            raise RuntimeError(f"Unsupported prefix auto mode: {prefix_auto}")
        if not bucket:
            raise RuntimeError("--prefix-auto requires --bucket")
        return discover_latest_active_screenshot_prefix(bucket)
    if prefix:
        return _normalize_prefix(prefix)
    raise RuntimeError("Either --prefix or --prefix-auto is required")


def local_sync_root(output_dir: Path, prefix: str) -> Path:
    run_name = PurePosixPath(_normalize_prefix(prefix)).name
    return output_dir / "s3_results" / run_name
