#!/usr/bin/env python3
"""Send periodic global screenshot job digests with full remote desktop previews."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image
from newspaper_scrapper.application.watch_prefix import (
    PREFIX_AUTO_LATEST_ACTIVE_SCREENSHOT,
    local_sync_root,
    resolve_watch_prefix,
)


RUN_ROOT = Path("/opt/newscom/run")


def sync_s3_metadata(bucket: str, prefix: str, output_dir: Path) -> Path:
    local_root = local_sync_root(output_dir, prefix)
    local_root.mkdir(parents=True, exist_ok=True)
    command = [
        "aws",
        "s3",
        "sync",
        "--only-show-errors",
        "--no-progress",
        f"s3://{bucket}/{prefix}",
        str(local_root),
        "--exact-timestamps",
        "--exclude",
        "*.png",
        "--exclude",
        "*.jpg",
        "--exclude",
        "*.jpeg",
        "--exclude",
        "*.log",
    ]
    subprocess.run(command, check=True)
    return local_root


def publish_sns_email(*, topic_arn: str, subject: str, message: str) -> None:
    if not topic_arn:
        return
    subprocess.run(
        [
            "aws",
            "sns",
            "publish",
            "--topic-arn",
            topic_arn,
            "--subject",
            subject[:100],
            "--message",
            message,
        ],
        check=False,
    )


def build_public_s3_url(bucket: str, key: str, *, region: str = "us-west-2") -> str:
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def presign_s3_url(bucket: str, key: str, *, expires_in: int = 86400) -> str:
    result = subprocess.run(
        [
            "aws",
            "s3",
            "presign",
            f"s3://{bucket}/{key}",
            "--expires-in",
            str(expires_in),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _run_ssh(host: str, ssh_key: Path, ssh_user: str, remote_script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh",
            "-i",
            str(ssh_key),
            "-o",
            "StrictHostKeyChecking=no",
            f"{ssh_user}@{host}",
            remote_script,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _build_s3_key(prefix: str, instance_id: str, output_path: str) -> str:
    relative = Path(output_path).relative_to(RUN_ROOT).as_posix()
    return f"{prefix.rstrip('/')}/{instance_id}/run/{relative}"


def _parse_pass_index(name: str) -> int:
    try:
        return int(name.replace("pass_", ""))
    except Exception:
        return 0


def _summarize_csv(csv_path: Path, prefix: str, instance_id: str) -> dict[str, Any]:
    captured_count = 0
    failed_count = 0
    last_three: deque[dict[str, Any]] = deque(maxlen=3)
    total_rows = 0
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for total_rows, row in enumerate(reader, start=1):
            status = str(row.get("status", "") or "")
            output_path = str(row.get("output_path", "") or "")
            if status == "captured" and output_path:
                captured_count += 1
                last_three.append(
                    {
                        "issue_id": str(row.get("issue_id", "") or ""),
                        "issue_date": str(row.get("issue_date", "") or ""),
                        "page_num": str(row.get("page_num", "") or ""),
                        "image_id": str(row.get("preferred_image_id", "") or ""),
                        "image_page_url": str(row.get("preferred_image_page_url", "") or ""),
                        "output_path": output_path,
                        "full_s3_key": _build_s3_key(prefix, instance_id, output_path),
                        "elapsed_seconds": str(row.get("elapsed_seconds", "") or ""),
                        "selected_strategy": str(row.get("selected_strategy", "") or ""),
                        "row_order": total_rows,
                    }
                )
            elif status:
                failed_count += 1
    return {
        "captured_count": captured_count,
        "failed_count": failed_count,
        "total_rows": total_rows,
        "recent_captures": list(last_three),
    }


def _choose_worker_csvs(s3_root: Path) -> dict[tuple[str, str], Path]:
    grouped: dict[tuple[str, str], list[tuple[int, Path]]] = {}
    for csv_path in s3_root.glob("*/run/workers/*/final_results.csv"):
        instance_id = csv_path.parts[-5]
        worker_name = csv_path.parts[-2]
        grouped.setdefault((instance_id, worker_name), []).append((10_000, csv_path))
    for csv_path in s3_root.glob("*/run/workers/*/passes/pass_*/results.csv"):
        instance_id = csv_path.parts[-7]
        worker_name = csv_path.parts[-4]
        priority = _parse_pass_index(csv_path.parent.name)
        grouped.setdefault((instance_id, worker_name), []).append((priority, csv_path))
    chosen: dict[tuple[str, str], Path] = {}
    for key, options in grouped.items():
        chosen[key] = sorted(options, key=lambda item: (item[0], str(item[1])))[-1][1]
    return chosen


def build_global_report(s3_root: Path, prefix: str) -> dict[str, Any] | None:
    chosen_csvs = _choose_worker_csvs(s3_root)
    if not chosen_csvs:
        return None

    worker_reports: list[dict[str, Any]] = []
    latest_captures: list[dict[str, Any]] = []
    totals = {
        "workers_total": 0,
        "workers_active": 0,
        "workers_completed": 0,
        "workers_blocked": 0,
        "workers_pending": 0,
        "captured_total": 0,
        "attempt_failures_total": 0,
        "remaining_total": 0,
        "manifest_total": 0,
        "ppm_total": 0.0,
    }

    per_instance_runner: dict[str, dict[str, Any]] = {}
    per_instance_dcv: dict[str, dict[str, Any]] = {}

    for (instance_id, worker_name), csv_path in sorted(chosen_csvs.items()):
        runner_summary = per_instance_runner.setdefault(
            instance_id,
            _load_json(s3_root / instance_id / "run" / "runner_summary.json") or {},
        )
        dcv_connection = per_instance_dcv.setdefault(
            instance_id,
            _load_json(s3_root / instance_id / "state" / "dcv_connection.json") or {},
        )
        completed_map = runner_summary.get("completed_worker_summaries") or {}
        completed_summary = completed_map.get(worker_name) or {}

        active_workers = {
            str(item.get("worker_name", "") or "")
            for item in (runner_summary.get("active_workers") or [])
            if isinstance(item, dict)
        }
        failed_workers = {str(name or "") for name in (runner_summary.get("failed_workers") or [])}
        delayed_workers = {str(name or "") for name in (runner_summary.get("delayed_workers") or [])}
        pending_workers = {str(name or "") for name in (runner_summary.get("pending_workers") or [])}

        if worker_name in active_workers:
            state = "active"
            totals["workers_active"] += 1
        elif worker_name in failed_workers or worker_name in delayed_workers:
            state = "blocked"
            totals["workers_blocked"] += 1
        elif worker_name in pending_workers:
            state = "pending"
            totals["workers_pending"] += 1
        elif completed_summary:
            state = "completed"
            totals["workers_completed"] += 1
        else:
            state = "unknown"

        csv_stats = _summarize_csv(csv_path, prefix, instance_id)
        capture_count = int(completed_summary.get("captured_rows", 0) or csv_stats["captured_count"])
        manifest_total = int(
            completed_summary.get("subset_rows", 0)
            or (completed_summary.get("pass_summaries") or [{}])[-1].get("runner_summary", {}).get("total_manifest_rows", 0)
            or csv_stats["total_rows"]
        )
        attempt_failure_count = int(csv_stats["failed_count"])
        remaining_count = max(manifest_total - capture_count, 0)
        ppm = float(completed_summary.get("pages_per_minute_estimate", 0.0) or 0.0)

        totals["workers_total"] += 1
        totals["captured_total"] += capture_count
        totals["attempt_failures_total"] += attempt_failure_count
        totals["remaining_total"] += remaining_count
        totals["manifest_total"] += manifest_total
        totals["ppm_total"] += ppm

        report = {
            "instance_id": instance_id,
            "worker_name": worker_name,
            "state": state,
            "capture_count": capture_count,
            "attempt_failure_count": attempt_failure_count,
            "remaining_count": remaining_count,
            "manifest_total": manifest_total,
            "ppm": ppm,
            "public_ip": str(dcv_connection.get("public_ip", "") or ""),
            "web_url": str(dcv_connection.get("web_url", "") or ""),
            "session_id": str(dcv_connection.get("session_id", "") or ""),
            "source_csv": str(csv_path),
            "source_mtime": csv_path.stat().st_mtime,
            "recent_captures": csv_stats["recent_captures"],
        }
        worker_reports.append(report)
        for capture in csv_stats["recent_captures"]:
            latest_captures.append(
                {
                    **capture,
                    "instance_id": instance_id,
                    "worker_name": worker_name,
                    "state": state,
                    "source_mtime": report["source_mtime"],
                }
            )

    latest_captures = sorted(
        latest_captures,
        key=lambda item: (float(item["source_mtime"]), int(item["row_order"])),
    )[-3:]

    return {
        "totals": totals,
        "workers": worker_reports,
        "latest_captures": latest_captures,
    }


def create_thumbnail_for_s3_object(
    *,
    bucket: str,
    full_s3_key: str,
    thumb_s3_key: str,
) -> str:
    with tempfile.TemporaryDirectory(prefix="newscom-progress-preview-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        source_path = tmpdir_path / "source.png"
        thumb_path = tmpdir_path / "thumb.jpg"
        subprocess.run(
            ["aws", "s3", "cp", f"s3://{bucket}/{full_s3_key}", str(source_path)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        image = Image.open(source_path).convert("RGB")
        image.thumbnail((1400, 1400))
        image.save(
            thumb_path,
            format="JPEG",
            quality=60,
            optimize=True,
            progressive=True,
        )
        subprocess.run(
            [
                "aws",
                "s3",
                "cp",
                str(thumb_path),
                f"s3://{bucket}/{thumb_s3_key}",
                "--content-type",
                "image/jpeg",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    return presign_s3_url(bucket, thumb_s3_key)


def capture_remote_desktop_preview(
    *,
    bucket: str,
    prefix: str,
    instance_id: str,
    public_ip: str,
    worker_name: str,
    ssh_key: Path | None,
    ssh_user: str,
    public_preview_bucket: str | None = None,
) -> dict[str, str] | None:
    if not public_ip or ssh_key is None:
        return None
    timestamp = int(time.time())
    base_key = (
        f"{prefix.rstrip('/')}/{instance_id}/state/desktop_previews/"
        f"{worker_name}_{timestamp}"
    )
    full_s3_key = f"{base_key}.png"
    thumb_s3_key = f"{base_key}.jpg"
    public_preview_key = f"desktop/{instance_id}/{worker_name}_{timestamp}.jpg"
    with tempfile.TemporaryDirectory(prefix="newscom-desktop-preview-") as tmpdir:
        tmpdir_path = Path(tmpdir)
        local_png = tmpdir_path / "desktop.png"
        local_jpg = tmpdir_path / "desktop.jpg"
        remote_png = f"/tmp/{worker_name}_{int(time.time())}_desktop.png"
        remote_script = (
            "set -euo pipefail\n"
            "source /opt/newscom/state/dcv_session.env\n"
            f"DISPLAY=\"$NEWSCOM_DCV_DISPLAY\" XAUTHORITY=\"$NEWSCOM_DCV_XAUTHORITY\" scrot \"{remote_png}\"\n"
            "for _ in $(seq 1 50); do\n"
            f"  if [ -s \"{remote_png}\" ]; then\n"
            "    break\n"
            "  fi\n"
            "  sleep 0.1\n"
            "done\n"
            f"test -s \"{remote_png}\"\n"
            f"ls -lh \"{remote_png}\"\n"
        )
        result = _run_ssh(public_ip, ssh_key, ssh_user, remote_script)
        if result.returncode != 0:
            return None
        scp_result = subprocess.run(
            [
                "scp",
                "-i",
                str(ssh_key),
                "-o",
                "StrictHostKeyChecking=no",
                f"{ssh_user}@{public_ip}:{remote_png}",
                str(local_png),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        _run_ssh(public_ip, ssh_key, ssh_user, f'rm -f "{remote_png}"')
        if scp_result.returncode != 0 or not local_png.exists() or local_png.stat().st_size == 0:
            return None

        image = Image.open(local_png).convert("RGB")
        image.thumbnail((1600, 1600))
        image.save(
            local_jpg,
            format="JPEG",
            quality=55,
            optimize=True,
            progressive=True,
        )
        subprocess.run(
            ["aws", "s3", "cp", str(local_png), f"s3://{bucket}/{full_s3_key}", "--content-type", "image/png"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            ["aws", "s3", "cp", str(local_jpg), f"s3://{bucket}/{thumb_s3_key}", "--content-type", "image/jpeg"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if public_preview_bucket:
            subprocess.run(
                [
                    "aws",
                    "s3",
                    "cp",
                    str(local_jpg),
                    f"s3://{public_preview_bucket}/{public_preview_key}",
                    "--content-type",
                    "image/jpeg",
                    "--cache-control",
                    "public, max-age=300",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
    preview_url = (
        build_public_s3_url(public_preview_bucket, public_preview_key)
        if public_preview_bucket
        else presign_s3_url(bucket, thumb_s3_key)
    )
    return {
        "desktop_preview_url": preview_url,
        "desktop_preview_s3_key": public_preview_key if public_preview_bucket else thumb_s3_key,
        "desktop_full_preview_url": presign_s3_url(bucket, full_s3_key),
        "desktop_full_s3_key": full_s3_key,
    }


def fetch_live_worker_state(
    *,
    public_ip: str,
    worker_name: str,
    ssh_key: Path | None,
    ssh_user: str,
) -> dict[str, Any] | None:
    if not public_ip or ssh_key is None:
        return None
    remote_script = f"""python3 - <<'PY'
import csv
import json
import subprocess
from pathlib import Path

worker_name = {worker_name!r}
worker_root = Path('/opt/newscom/run/workers') / worker_name
passes_root = worker_root / 'passes'
pass_dirs = []
if passes_root.exists():
    for candidate in passes_root.glob('pass_*'):
        try:
            index = int(candidate.name.replace('pass_', ''))
        except Exception:
            continue
        if candidate.is_dir():
            pass_dirs.append((index, candidate))
pass_dir = sorted(pass_dirs)[-1][1] if pass_dirs else None
summary = {{}}
rows = captured = failed = 0
if pass_dir:
    summary_path = pass_dir / 'summary.json'
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
    results_path = pass_dir / 'results.csv'
    if results_path.exists():
        with results_path.open(newline='') as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                rows += 1
                status = str(row.get('status', '') or '')
                if status == 'captured':
                    captured += 1
                elif status:
                    failed += 1
service_state = subprocess.run(
    ['systemctl', 'is-active', 'newscom-worker.service'],
    text=True,
    capture_output=True,
    check=False,
).stdout.strip()
payload = {{
    'service_state': service_state,
    'rows': rows,
    'captured': captured,
    'failed': failed,
    'manifest_total': int(summary.get('total_manifest_rows', 0) or rows),
    'stopped_reason': str(summary.get('stopped_reason', '') or ''),
}}
print(json.dumps(payload))
PY"""
    result = _run_ssh(public_ip, ssh_key, ssh_user, remote_script)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None


def refresh_report_from_live_hosts(report: dict[str, Any], *, ssh_key: Path | None, ssh_user: str) -> dict[str, Any]:
    totals = {
        "workers_total": 0,
        "workers_active": 0,
        "workers_completed": 0,
        "workers_blocked": 0,
        "workers_pending": 0,
        "captured_total": 0,
        "attempt_failures_total": 0,
        "remaining_total": 0,
        "manifest_total": 0,
        "ppm_total": 0.0,
    }
    for worker in report["workers"]:
        live = fetch_live_worker_state(
            public_ip=str(worker.get("public_ip", "") or ""),
            worker_name=str(worker.get("worker_name", "") or ""),
            ssh_key=ssh_key,
            ssh_user=ssh_user,
        )
        if live:
            worker["capture_count"] = int(live.get("captured", worker["capture_count"]) or worker["capture_count"])
            worker["attempt_failure_count"] = int(
                live.get("failed", worker["attempt_failure_count"]) or worker["attempt_failure_count"]
            )
            worker["manifest_total"] = int(live.get("manifest_total", worker["manifest_total"]) or worker["manifest_total"])
            worker["remaining_count"] = max(int(worker["manifest_total"]) - int(worker["capture_count"]), 0)
            service_state = str(live.get("service_state", "") or "")
            stopped_reason = str(live.get("stopped_reason", "") or "")
            if service_state == "active":
                worker["state"] = "active"
            elif worker["capture_count"] >= worker["manifest_total"] > 0:
                worker["state"] = "completed"
            elif stopped_reason:
                worker["state"] = "blocked"
        totals["workers_total"] += 1
        totals["captured_total"] += int(worker["capture_count"])
        totals["attempt_failures_total"] += int(worker["attempt_failure_count"])
        totals["remaining_total"] += int(worker["remaining_count"])
        totals["manifest_total"] += int(worker["manifest_total"])
        totals["ppm_total"] += float(worker["ppm"])
        if worker["state"] == "active":
            totals["workers_active"] += 1
        elif worker["state"] == "completed":
            totals["workers_completed"] += 1
        elif worker["state"] == "blocked":
            totals["workers_blocked"] += 1
        elif worker["state"] == "pending":
            totals["workers_pending"] += 1
    report["totals"] = totals
    return report


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def should_send_report(
    *,
    report: dict[str, Any],
    state: dict[str, Any],
    now: float,
    interval_seconds: float,
    force: bool,
    current_prefix: str,
) -> bool:
    if force:
        return True
    if str(state.get("last_prefix", "") or "") != current_prefix:
        return True
    totals = report["totals"]
    last_captured = int(state.get("last_captured_total", -1) or -1)
    if int(totals["captured_total"]) != last_captured:
        return True
    last_sent_at = float(state.get("last_sent_at", 0.0) or 0.0)
    if now - last_sent_at < interval_seconds:
        return False
    return int(totals["workers_active"]) > 0


def build_message(report: dict[str, Any], desktop_previews: list[dict[str, Any]]) -> tuple[str, str]:
    totals = report["totals"]
    workers = sorted(report["workers"], key=lambda item: (item["state"], item["worker_name"]))
    subject = f"Newscom worker desktop digest: {totals['captured_total']} captured across {totals['workers_total']} worker(s)"

    lines = [
        "Newscom screenshot job digest.",
        "This report is about the remote worker computer state.",
        "All screenshot links below are static images and should not require DCV login.",
        "",
        f"Workers total: {totals['workers_total']}",
        f"Workers active: {totals['workers_active']}",
        f"Workers completed: {totals['workers_completed']}",
        f"Workers blocked: {totals['workers_blocked']}",
        f"Workers pending: {totals['workers_pending']}",
        f"Captured total: {totals['captured_total']}",
        f"Attempt failures total: {totals['attempt_failures_total']}",
        f"Remaining rows total: {totals['remaining_total']}",
        f"Manifest total: {totals['manifest_total']}",
        f"Aggregate pages/min estimate: {totals['ppm_total']:.2f}",
        "",
        "Worker states:",
    ]
    for worker in workers:
        percent = (
            100.0 * float(worker["capture_count"]) / float(worker["manifest_total"])
            if worker["manifest_total"]
            else 0.0
        )
        lines.append(
            f"- {worker['worker_name']} on {worker['instance_id']} [{worker['state']}]: "
            f"{worker['capture_count']}/{worker['manifest_total']} captured, "
            f"{worker['remaining_count']} remaining, "
            f"{worker['attempt_failure_count']} attempt failures so far, "
            f"{worker['ppm']:.2f} ppm, {percent:.1f}%"
        )

    if desktop_previews:
        lines.extend(["", "Worker computer screenshots (no login required):"])
        for preview in desktop_previews:
            lines.append(
                f"- {preview['worker_name']} on {preview['instance_id']} computer screenshot: {preview['desktop_preview_url']}"
            )

    return subject, "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix")
    parser.add_argument(
        "--prefix-auto",
        choices=(PREFIX_AUTO_LATEST_ACTIVE_SCREENSHOT,),
        default="",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sns-topic-arn", default="")
    parser.add_argument("--interval-seconds", type=float, default=3600.0)
    parser.add_argument("--ssh-key", type=Path, default=None)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--public-preview-bucket")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.prefix and not args.prefix_auto:
        parser.error("Either --prefix or --prefix-auto is required")

    state_path = args.output_dir / "progress_report_state.json"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    last_resolved_prefix = ""

    while True:
        try:
            resolved_prefix = resolve_watch_prefix(
                bucket=args.bucket,
                prefix=args.prefix,
                prefix_auto=args.prefix_auto,
            )
            if resolved_prefix != last_resolved_prefix:
                print(
                    json.dumps(
                        {
                            "event": "watch_prefix_resolved",
                            "prefix": resolved_prefix,
                        },
                        sort_keys=True,
                    )
                )
                last_resolved_prefix = resolved_prefix
            s3_root = sync_s3_metadata(args.bucket, resolved_prefix, args.output_dir)
            report = build_global_report(s3_root, resolved_prefix)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "watch_sync_error",
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                        "prefix": last_resolved_prefix or args.prefix or "",
                    },
                    sort_keys=True,
                )
            )
            if args.once:
                raise
            time.sleep(args.interval_seconds)
            continue
        if report:
            report = refresh_report_from_live_hosts(report, ssh_key=args.ssh_key, ssh_user=args.ssh_user)
            now = time.time()
            state = load_state(state_path)
            if should_send_report(
                report=report,
                state=state,
                now=now,
                interval_seconds=args.interval_seconds,
                force=args.force,
                current_prefix=resolved_prefix,
            ):
                desktop_previews: list[dict[str, Any]] = []
                preview_candidates = sorted(
                    report["workers"],
                    key=lambda worker: (
                        0 if worker["state"] == "active" else 1,
                        0 if worker["state"] == "completed" else 1,
                        worker["worker_name"],
                    ),
                )[:3]
                for worker in preview_candidates:
                    preview = capture_remote_desktop_preview(
                        bucket=args.bucket,
                        prefix=resolved_prefix,
                        instance_id=str(worker["instance_id"]),
                        public_ip=str(worker["public_ip"]),
                        worker_name=str(worker["worker_name"]),
                        ssh_key=args.ssh_key,
                        ssh_user=args.ssh_user,
                        public_preview_bucket=args.public_preview_bucket,
                    )
                    if preview:
                        desktop_previews.append({**worker, **preview})
                subject, message = build_message(report, desktop_previews)
                publish_sns_email(
                    topic_arn=args.sns_topic_arn,
                    subject=subject,
                    message=message,
                )
                state_path.write_text(
                    json.dumps(
                        {
                            "last_sent_at": now,
                            "last_captured_total": report["totals"]["captured_total"],
                            "last_workers_active": report["totals"]["workers_active"],
                            "last_prefix": resolved_prefix,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                args.force = False
        if args.once:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
