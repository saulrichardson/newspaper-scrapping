"""Plan, run, and merge multi-worker screenshot capture jobs."""

from __future__ import annotations

import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from newspaper_scrapper.application.download import read_manifest
from newspaper_scrapper.application import screenshot as screenshot_uc
from newspaper_scrapper.application.screenshot import (
    MANIFEST_FIELDNAMES,
    PRODUCTION_RESULT_FIELDNAMES,
)
from newspaper_scrapper.domain.models import ManifestRow


SCREENSHOT_PLAN_FIELDNAMES = [
    "worker_index",
    "worker_name",
    "manifest_csv",
    "output_dir",
    "chrome_profile_dir",
    "chrome_debug_port",
    "grouping_mode",
    "expected_page_count",
    "estimated_weight",
    "strategy",
    "page_load_seconds",
    "render_wait_seconds",
    "sleep_between_pages",
    "sleep_jitter_seconds",
    "adaptive_sleep",
    "min_sleep_between_pages",
    "max_sleep_between_pages",
    "sleep_step_seconds",
    "clean_streak_threshold",
    "slow_page_threshold_seconds",
    "post_render_settle_seconds",
    "recycle_browser_every_pages",
    "max_passes",
    "pass_page_load_increment",
    "pass_render_wait_increment",
    "stop_on_stall",
    "restart_browser_before_run",
    "restart_browser_each_pass",
]


BROWSER_UNHEALTHY_RETRY_COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True)
class ScreenshotGroup:
    key: str
    rows: tuple[ManifestRow, ...]

    @property
    def weight(self) -> int:
        return len(self.rows)


@dataclass
class WorkerRuntime:
    row: dict[str, str]
    process: subprocess.Popen[str]
    stdout_path: Path
    stderr_path: Path
    attempts: int
    started_at: float


def _write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return [{key: value or "" for key, value in row.items()} for row in csv.DictReader(handle)]


def _manifest_row_to_dict(row: ManifestRow) -> dict[str, str]:
    return {
        "issue_id": row.issue_id,
        "issue_date": row.issue_date,
        "page_num": row.page_num,
        "preferred_image_id": row.preferred_image_id,
        "preferred_image_page_url": row.preferred_image_page_url,
    }


def _write_manifest(path: Path, rows: Iterable[ManifestRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(_manifest_row_to_dict(row))


def _build_groups(rows: list[ManifestRow], grouping_mode: str) -> list[ScreenshotGroup]:
    if grouping_mode == "page":
        return [
            ScreenshotGroup(
                key=f"{row.issue_id}::{row.page_num}",
                rows=(row,),
            )
            for row in rows
        ]
    if grouping_mode != "issue":
        raise ValueError("grouping_mode must be 'issue' or 'page'")

    groups: dict[str, list[ManifestRow]] = {}
    for row in rows:
        groups.setdefault(row.issue_id, []).append(row)
    return [
        ScreenshotGroup(key=issue_id, rows=tuple(issue_rows))
        for issue_id, issue_rows in groups.items()
    ]


def _bucket_groups(
    groups: list[ScreenshotGroup],
    worker_count: int,
) -> tuple[list[list[ScreenshotGroup]], list[int]]:
    if worker_count < 1:
        raise ValueError("worker_count must be at least 1")
    buckets: list[list[ScreenshotGroup]] = [[] for _ in range(worker_count)]
    weights = [0 for _ in range(worker_count)]
    for group in sorted(groups, key=lambda item: item.weight, reverse=True):
        target = min(range(worker_count), key=lambda idx: weights[idx])
        buckets[target].append(group)
        weights[target] += group.weight
    return buckets, weights


def build_worker_shell_script(
    row: dict[str, str],
    *,
    repo_root: Path,
    cookies_json: Path | None,
) -> str:
    worker_name = row["worker_name"]
    manifest_csv = Path(row["manifest_csv"]).resolve()
    output_dir = Path(row["output_dir"]).resolve()
    strategy = row["strategy"]
    page_load_seconds = row["page_load_seconds"]
    render_wait_seconds = row["render_wait_seconds"]
    sleep_between_pages = row["sleep_between_pages"]
    sleep_jitter_seconds = row["sleep_jitter_seconds"]
    adaptive_sleep = row.get("adaptive_sleep", "false")
    min_sleep_between_pages = row.get("min_sleep_between_pages", "0.0")
    max_sleep_between_pages = row.get("max_sleep_between_pages", sleep_between_pages)
    sleep_step_seconds = row.get("sleep_step_seconds", "0.25")
    clean_streak_threshold = row.get("clean_streak_threshold", "3")
    slow_page_threshold_seconds = row.get("slow_page_threshold_seconds", "12.0")
    post_render_settle_seconds = row.get(
        "post_render_settle_seconds",
        str(screenshot_uc.POST_RENDER_SETTLE_SECONDS),
    )
    recycle_browser_every_pages = row.get("recycle_browser_every_pages", "0")
    max_passes = row["max_passes"]
    pass_page_load_increment = row["pass_page_load_increment"]
    pass_render_wait_increment = row["pass_render_wait_increment"]
    stop_on_stall = row["stop_on_stall"]
    restart_browser_before_run = row["restart_browser_before_run"]
    restart_browser_each_pass = row["restart_browser_each_pass"]

    lines = [
        "set -euo pipefail",
        f"cd {shlex.quote(str(repo_root))}",
        f"mkdir -p {shlex.quote(str(output_dir))}",
        f"echo 'Starting {worker_name} ({row['expected_page_count']} pages)'",
    ]
    python_executable = shlex.quote(sys.executable)
    if cookies_json is not None:
        lines.extend(
            [
                f"echo 'Bootstrapping {worker_name} auth profile'",
                (
                    f"{python_executable} -m newspaper_scrapper.cli.main "
                    f"auth-import-cookies --cookies-json {shlex.quote(str(cookies_json))} "
                    "--navigate-url https://www.newspapers.com/account/ "
                    "--force-new-instance >/dev/null"
                ),
            ]
        )

    command = [
        sys.executable,
        "-m",
        "newspaper_scrapper.cli.main",
        "screenshot-pages-production",
        "--manifest-csv",
        str(manifest_csv),
        "--output-dir",
        str(output_dir),
        "--strategy",
        strategy,
        "--page-load-seconds",
        str(page_load_seconds),
        "--render-wait-seconds",
        str(render_wait_seconds),
        "--sleep-between-pages",
        str(sleep_between_pages),
        "--sleep-jitter-seconds",
        str(sleep_jitter_seconds),
        "--adaptive-sleep" if adaptive_sleep == "true" else "--fixed-sleep",
        "--min-sleep-between-pages",
        str(min_sleep_between_pages),
        "--max-sleep-between-pages",
        str(max_sleep_between_pages),
        "--sleep-step-seconds",
        str(sleep_step_seconds),
        "--clean-streak-threshold",
        str(clean_streak_threshold),
        "--slow-page-threshold-seconds",
        str(slow_page_threshold_seconds),
        "--post-render-settle-seconds",
        str(post_render_settle_seconds),
        "--recycle-browser-every-pages",
        str(recycle_browser_every_pages),
        "--max-passes",
        str(max_passes),
        "--pass-page-load-increment",
        str(pass_page_load_increment),
        "--pass-render-wait-increment",
        str(pass_render_wait_increment),
    ]
    command.append("--stop-on-stall" if stop_on_stall == "true" else "--allow-stall")
    reuse_bootstrapped_browser = cookies_json is not None
    command.append(
        "--reuse-browser-before-run"
        if reuse_bootstrapped_browser or restart_browser_before_run != "true"
        else "--restart-browser-before-run"
    )
    command.append(
        "--reuse-browser-each-pass"
        if reuse_bootstrapped_browser or restart_browser_each_pass != "true"
        else "--restart-browser-each-pass"
    )

    lines.append(shlex.join(command))
    return "\n".join(lines) + "\n"


def plan_screenshot_workers(
    *,
    manifest_csv: Path,
    output_dir: Path,
    worker_count: int,
    grouping_mode: str,
    base_debug_port: int,
    profile_root: Path | None,
    cookies_json: Path | None,
    strategy: str,
    page_load_seconds: float,
    render_wait_seconds: float,
    sleep_between_pages: float,
    sleep_jitter_seconds: float,
    adaptive_sleep: bool,
    min_sleep_between_pages: float,
    max_sleep_between_pages: float | None,
    sleep_step_seconds: float,
    clean_streak_threshold: int,
    slow_page_threshold_seconds: float,
    post_render_settle_seconds: float,
    recycle_browser_every_pages: int,
    max_passes: int,
    pass_page_load_increment: float,
    pass_render_wait_increment: float,
    stop_on_stall: bool,
    restart_browser_before_run: bool,
    restart_browser_each_pass: bool,
) -> dict[str, object]:
    rows = read_manifest(manifest_csv)
    output_dir.mkdir(parents=True, exist_ok=True)
    workers_dir = output_dir / "workers"
    profiles_dir = profile_root or (output_dir / "worker_profiles")
    plan_csv = output_dir / "worker_plan.csv"
    launch_script = output_dir / "launch_workers.sh"
    repo_root = Path.cwd().resolve()

    groups = _build_groups(rows, grouping_mode)
    buckets, weights = _bucket_groups(groups, worker_count)

    plan_rows: list[dict[str, str]] = []
    launch_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"cd {json.dumps(str(repo_root))}",
        "",
    ]

    for index, bucket in enumerate(buckets, start=1):
        worker_name = f"worker_{index:02d}"
        worker_root = workers_dir / worker_name
        worker_root.mkdir(parents=True, exist_ok=True)
        profile_dir = (profiles_dir / worker_name).resolve()
        manifest_path = (worker_root / "input_manifest.csv").resolve()
        worker_output_dir = (worker_root / "run").resolve()
        bucket_rows = [row for group in bucket for row in group.rows]
        _write_manifest(manifest_path, bucket_rows)
        debug_port = base_debug_port + index - 1

        row = {
            "worker_index": str(index),
            "worker_name": worker_name,
            "manifest_csv": str(manifest_path),
            "output_dir": str(worker_output_dir),
            "chrome_profile_dir": str(profile_dir),
            "chrome_debug_port": str(debug_port),
            "grouping_mode": grouping_mode,
            "expected_page_count": str(len(bucket_rows)),
            "estimated_weight": str(weights[index - 1]),
            "strategy": strategy,
            "page_load_seconds": str(page_load_seconds),
            "render_wait_seconds": str(render_wait_seconds),
            "sleep_between_pages": str(sleep_between_pages),
            "sleep_jitter_seconds": str(sleep_jitter_seconds),
            "adaptive_sleep": "true" if adaptive_sleep else "false",
            "min_sleep_between_pages": str(min_sleep_between_pages),
            "max_sleep_between_pages": str(
                sleep_between_pages if max_sleep_between_pages is None else max_sleep_between_pages
            ),
            "sleep_step_seconds": str(sleep_step_seconds),
            "clean_streak_threshold": str(clean_streak_threshold),
            "slow_page_threshold_seconds": str(slow_page_threshold_seconds),
            "post_render_settle_seconds": str(post_render_settle_seconds),
            "recycle_browser_every_pages": str(recycle_browser_every_pages),
            "max_passes": str(max_passes),
            "pass_page_load_increment": str(pass_page_load_increment),
            "pass_render_wait_increment": str(pass_render_wait_increment),
            "stop_on_stall": "true" if stop_on_stall else "false",
            "restart_browser_before_run": "true" if restart_browser_before_run else "false",
            "restart_browser_each_pass": "true" if restart_browser_each_pass else "false",
        }
        plan_rows.append(row)

        worker_script = build_worker_shell_script(
            row,
            repo_root=repo_root,
            cookies_json=cookies_json,
        )
        launch_lines.extend(
            [
                f"mkdir -p {json.dumps(str(worker_output_dir))} {json.dumps(str(profile_dir))}",
                f"echo 'Launching {worker_name} for {len(bucket_rows)} pages'",
                "(",
                f"  export NEWSCOM_CHROME_DEBUG_PORT={row['chrome_debug_port']}",
                f"  export NEWSCOM_CHROME_PROFILE_DIR={json.dumps(str(profile_dir))}",
                "  export PYTHONPATH=src",
            ]
        )
        for line in worker_script.strip().splitlines():
            launch_lines.append(f"  {line}")
        launch_lines.extend(
            [
                ") > "
                + json.dumps(str(worker_root / "stdout.log"))
                + " 2> "
                + json.dumps(str(worker_root / "stderr.log"))
                + " &",
                f"echo $! > {json.dumps(str(worker_root / 'pid.txt'))}",
                "",
            ]
        )

    _write_csv(plan_csv, SCREENSHOT_PLAN_FIELDNAMES, plan_rows)
    launch_script.write_text("\n".join(launch_lines) + "\n")
    launch_script.chmod(0o755)

    summary = {
        "manifest_csv": str(manifest_csv),
        "output_dir": str(output_dir),
        "worker_count": worker_count,
        "grouping_mode": grouping_mode,
        "total_pages": len(rows),
        "total_groups": len(groups),
        "base_debug_port": base_debug_port,
        "cookies_json": str(cookies_json) if cookies_json is not None else "",
        "strategy": strategy,
        "page_load_seconds": page_load_seconds,
        "render_wait_seconds": render_wait_seconds,
        "sleep_between_pages": sleep_between_pages,
        "sleep_jitter_seconds": sleep_jitter_seconds,
        "adaptive_sleep": adaptive_sleep,
        "min_sleep_between_pages": min_sleep_between_pages,
        "max_sleep_between_pages": (
            sleep_between_pages if max_sleep_between_pages is None else max_sleep_between_pages
        ),
        "sleep_step_seconds": sleep_step_seconds,
        "clean_streak_threshold": clean_streak_threshold,
        "slow_page_threshold_seconds": slow_page_threshold_seconds,
        "post_render_settle_seconds": post_render_settle_seconds,
        "recycle_browser_every_pages": recycle_browser_every_pages,
        "max_passes": max_passes,
        "pass_page_load_increment": pass_page_load_increment,
        "pass_render_wait_increment": pass_render_wait_increment,
        "stop_on_stall": stop_on_stall,
        "restart_browser_before_run": restart_browser_before_run,
        "restart_browser_each_pass": restart_browser_each_pass,
        "worker_weights": weights,
        "worker_plan_csv": str(plan_csv),
        "launch_script": str(launch_script),
        "workers_dir": str(workers_dir),
        "profile_root": str(profiles_dir),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def _load_worker_plan(plan_csv: Path) -> list[dict[str, str]]:
    rows = _read_csv(plan_csv)
    if not rows:
        raise ValueError(f"No worker rows found in {plan_csv}")
    return rows


def _write_runner_summary(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _load_worker_completion_summary(output_dir: Path) -> dict[str, object]:
    summary_path = output_dir / "summary.json"
    if not summary_path.exists():
        return {}
    return json.loads(summary_path.read_text())


def _load_worker_production_rows(run_root: Path) -> list[dict[str, str]]:
    final_results_path = run_root / "final_results.csv"
    if final_results_path.exists():
        return _read_csv(final_results_path)

    passes_root = run_root / "passes"
    if not passes_root.exists():
        return []
    candidate_results = sorted(passes_root.glob("pass_*/results.csv"))
    if not candidate_results:
        return []

    latest_results = candidate_results[-1]
    pass_name = latest_results.parent.name
    try:
        pass_index = int(pass_name.split("_")[-1])
    except Exception:
        pass_index = 0

    rows = _read_csv(latest_results)
    normalized: list[dict[str, str]] = []
    for row in rows:
        enriched = dict(row)
        enriched.setdefault("pass_index", str(pass_index))
        enriched.setdefault("pass_name", pass_name)
        normalized.append(enriched)
    return normalized


def _load_worker_summary_fallback(run_root: Path) -> dict[str, object]:
    summary_path = run_root / "summary.json"
    if summary_path.exists():
        return json.loads(summary_path.read_text())
    passes_root = run_root / "passes"
    if not passes_root.exists():
        return {}
    candidate_summaries = sorted(passes_root.glob("pass_*/summary.json"))
    if not candidate_summaries:
        return {}
    return json.loads(candidate_summaries[-1].read_text())


def run_screenshot_workers(
    *,
    plan_csv: Path,
    output_dir: Path,
    max_concurrent_workers: int,
    worker_stagger_seconds: float,
    retry_cooldown_seconds: float,
    max_worker_attempts: int,
    cookies_json: Path | None,
    poll_seconds: float,
    retry_on_cloudflare_challenge: bool = False,
) -> dict[str, object]:
    if max_concurrent_workers < 1:
        raise ValueError("max_concurrent_workers must be at least 1")

    repo_root = Path.cwd().resolve()
    rows = _load_worker_plan(plan_csv)
    output_dir.mkdir(parents=True, exist_ok=True)
    runner_summary_json = output_dir / "runner_summary.json"

    pending: list[dict[str, str]] = list(rows)
    delayed: list[tuple[float, dict[str, str], int]] = []
    active: dict[str, WorkerRuntime] = {}
    completed: list[str] = []
    failed: list[str] = []
    attempt_counts: dict[str, int] = {}
    completed_summaries: dict[str, dict[str, object]] = {}
    launch_sequence = 0
    started_at = time.time()

    def write_summary() -> None:
        payload = {
            "plan_csv": str(plan_csv),
            "output_dir": str(output_dir),
            "repo_root": str(repo_root),
            "workers_total": len(rows),
            "max_concurrent_workers": max_concurrent_workers,
            "worker_stagger_seconds": worker_stagger_seconds,
            "retry_cooldown_seconds": retry_cooldown_seconds,
            "max_worker_attempts": max_worker_attempts,
            "retry_on_cloudflare_challenge": retry_on_cloudflare_challenge,
            "cookies_json": str(cookies_json) if cookies_json is not None else "",
            "started_at_epoch": started_at,
            "updated_at_epoch": time.time(),
            "pending_workers": [row["worker_name"] for row in pending],
            "delayed_workers": [
                {
                    "worker_name": row["worker_name"],
                    "eligible_at_epoch": eligible_at,
                    "attempts": attempts,
                }
                for eligible_at, row, attempts in delayed
            ],
            "active_workers": [
                {
                    "worker_name": runtime.row["worker_name"],
                    "pid": runtime.process.pid,
                    "attempts": runtime.attempts,
                    "started_at_epoch": runtime.started_at,
                    "stdout_log": str(runtime.stdout_path),
                    "stderr_log": str(runtime.stderr_path),
                }
                for runtime in active.values()
            ],
            "completed_workers": completed,
            "failed_workers": failed,
            "attempt_counts": attempt_counts,
            "completed_worker_summaries": completed_summaries,
        }
        _write_runner_summary(runner_summary_json, payload)

    def launch_worker(row: dict[str, str], attempts: int) -> WorkerRuntime:
        nonlocal launch_sequence
        worker_name = row["worker_name"]
        worker_root = Path(row["manifest_csv"]).resolve().parent
        stdout_path = worker_root / "stdout.log"
        stderr_path = worker_root / "stderr.log"
        env = os.environ.copy()
        env["PYTHONPATH"] = "src"
        env["NEWSCOM_CHROME_DEBUG_PORT"] = row["chrome_debug_port"]
        env["NEWSCOM_CHROME_PROFILE_DIR"] = row["chrome_profile_dir"]
        shell_script = build_worker_shell_script(
            row,
            repo_root=repo_root,
            cookies_json=cookies_json,
        )
        launch_sequence += 1
        stdout_handle = stdout_path.open("a")
        stderr_handle = stderr_path.open("a")
        stdout_handle.write(
            f"\n===== worker launch {launch_sequence} attempt {attempts} at {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
        )
        stderr_handle.write(
            f"\n===== worker launch {launch_sequence} attempt {attempts} at {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
        )
        stdout_handle.flush()
        stderr_handle.flush()
        shell_path = shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
        process = subprocess.Popen(
            [shell_path, "-lc", shell_script],
            cwd=repo_root,
            env=env,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        stdout_handle.close()
        stderr_handle.close()
        return WorkerRuntime(
            row=row,
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            attempts=attempts,
            started_at=time.time(),
        )

    while pending or delayed or active:
        now = time.time()
        still_delayed: list[tuple[float, dict[str, str], int]] = []
        for eligible_at, row, attempts in delayed:
            if eligible_at <= now:
                pending.append(row)
                attempt_counts[row["worker_name"]] = attempts
            else:
                still_delayed.append((eligible_at, row, attempts))
        delayed = still_delayed

        while pending and len(active) < max_concurrent_workers:
            row = pending.pop(0)
            worker_name = row["worker_name"]
            attempts = attempt_counts.get(worker_name, 0) + 1
            attempt_counts[worker_name] = attempts
            active[worker_name] = launch_worker(row, attempts)
            write_summary()
            if worker_stagger_seconds > 0 and len(active) < max_concurrent_workers:
                time.sleep(worker_stagger_seconds)

        finished_worker_names: list[str] = []
        for worker_name, runtime in active.items():
            return_code = runtime.process.poll()
            if return_code is None:
                continue
            finished_worker_names.append(worker_name)
            worker_summary = _load_worker_completion_summary(
                Path(runtime.row["output_dir"]).resolve()
            )
            completed_summaries[worker_name] = worker_summary
            stop_reason = str(worker_summary.get("stopped_reason", "") or "")

            if stop_reason == screenshot_uc.STOP_REASON_CLOUDFLARE_CHALLENGE:
                if not retry_on_cloudflare_challenge or runtime.attempts >= max_worker_attempts:
                    failed.append(worker_name)
                else:
                    delayed.append(
                        (
                            time.time() + retry_cooldown_seconds,
                            runtime.row,
                            runtime.attempts,
                        )
                    )
            elif stop_reason == screenshot_uc.STOP_REASON_AUTH_REQUIRED:
                failed.append(worker_name)
            elif stop_reason == screenshot_uc.STOP_REASON_BROWSER_UNHEALTHY:
                if runtime.attempts >= max_worker_attempts:
                    failed.append(worker_name)
                else:
                    delayed.append(
                        (
                            time.time() + BROWSER_UNHEALTHY_RETRY_COOLDOWN_SECONDS,
                            runtime.row,
                            runtime.attempts,
                        )
                    )
            elif return_code == 0:
                completed.append(worker_name)
            elif runtime.attempts >= max_worker_attempts:
                failed.append(worker_name)
            else:
                delayed.append(
                    (
                        time.time() + retry_cooldown_seconds,
                        runtime.row,
                        runtime.attempts,
                    )
                )

        for worker_name in finished_worker_names:
            active.pop(worker_name, None)

        write_summary()

        if pending or delayed or active:
            time.sleep(poll_seconds)

    final_summary = {
        "plan_csv": str(plan_csv),
        "output_dir": str(output_dir),
        "workers_total": len(rows),
        "completed_worker_count": len(completed),
        "failed_worker_count": len(failed),
        "completed_workers": completed,
        "failed_workers": failed,
        "attempt_counts": attempt_counts,
        "completed_worker_summaries": completed_summaries,
        "elapsed_seconds": round(time.time() - started_at, 3),
        "runner_summary_json": str(runner_summary_json),
    }
    _write_runner_summary(output_dir / "summary.json", final_summary)
    write_summary()
    return final_summary


def merge_screenshot_workers(
    *,
    workers_root: Path,
    output_dir: Path,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    worker_dirs = sorted(path for path in workers_root.iterdir() if path.is_dir())

    merged_results: list[dict[str, str]] = []
    best_by_key: dict[tuple[str, str], dict[str, str]] = {}
    worker_summaries: dict[str, dict[str, object]] = {}

    for worker_dir in worker_dirs:
        local_run_root = worker_dir / "run"
        flattened_run_root = worker_dir
        run_root = local_run_root if local_run_root.exists() else flattened_run_root

        rows = _load_worker_production_rows(run_root)
        if rows:
            merged_results.extend(rows)
            for row in rows:
                key = (row.get("issue_id", "").strip(), row.get("page_num", "").strip())
                if not key[0] or not key[1]:
                    continue
                existing = best_by_key.get(key)
                if existing is None:
                    best_by_key[key] = row
                    continue
                if existing.get("status") != "captured" and row.get("status") == "captured":
                    best_by_key[key] = row
                    continue
                if (
                    existing.get("status") == row.get("status") == "captured"
                    and int(row.get("pass_index", "0") or 0)
                    >= int(existing.get("pass_index", "0") or 0)
                ):
                    best_by_key[key] = row
                elif existing.get("status") != "captured":
                    best_by_key[key] = row

        worker_summary = _load_worker_summary_fallback(run_root)
        if worker_summary:
            worker_summaries[worker_dir.name] = worker_summary

    final_results_csv = output_dir / "final_results_merged.csv"
    captured_manifest_csv = output_dir / "captured_manifest_merged.csv"
    remaining_failures_csv = output_dir / "remaining_failures_manifest_merged.csv"

    _write_csv(final_results_csv, PRODUCTION_RESULT_FIELDNAMES, merged_results)

    merged_best_rows = sorted(
        best_by_key.values(),
        key=lambda row: (
            row.get("issue_date", ""),
            row.get("issue_id", ""),
            row.get("page_num", ""),
        ),
    )
    captured_manifest_rows: list[dict[str, str]] = []
    failed_manifest_rows: list[dict[str, str]] = []
    for row in merged_best_rows:
        manifest_row = {
            "issue_id": row.get("issue_id", ""),
            "issue_date": row.get("issue_date", ""),
            "page_num": row.get("page_num", ""),
            "preferred_image_id": row.get("preferred_image_id", ""),
            "preferred_image_page_url": row.get("preferred_image_page_url", ""),
        }
        if row.get("status") == "captured":
            captured_manifest_rows.append(manifest_row)
        else:
            failed_manifest_rows.append(manifest_row)
    _write_csv(captured_manifest_csv, MANIFEST_FIELDNAMES, captured_manifest_rows)
    _write_csv(remaining_failures_csv, MANIFEST_FIELDNAMES, failed_manifest_rows)

    elapsed_seconds = [
        float(row["elapsed_seconds"])
        for row in merged_best_rows
        if row.get("status") == "captured" and row.get("elapsed_seconds")
    ]
    pages_per_minute_estimate = None
    if elapsed_seconds:
        mean_elapsed = sum(elapsed_seconds) / len(elapsed_seconds)
        pages_per_minute_estimate = 60.0 / mean_elapsed if mean_elapsed > 0 else None

    summary = {
        "workers_root": str(workers_root),
        "worker_count_detected": len(worker_dirs),
        "raw_result_rows": len(merged_results),
        "unique_pages": len(merged_best_rows),
        "captured_rows": len(captured_manifest_rows),
        "failed_rows": len(failed_manifest_rows),
        "final_results_merged_csv": str(final_results_csv),
        "captured_manifest_merged_csv": str(captured_manifest_csv),
        "remaining_failures_manifest_merged_csv": str(remaining_failures_csv),
        "pages_per_minute_estimate": pages_per_minute_estimate,
        "worker_summaries": worker_summaries,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary
