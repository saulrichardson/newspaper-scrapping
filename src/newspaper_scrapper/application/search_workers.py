"""Plan, run, and merge multi-worker keyword search seed harvests."""

from __future__ import annotations

import csv
import calendar
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

from newspaper_scrapper.application.search import (
    PAGE_MANIFEST_FIELDNAMES,
    RESULT_FIELDNAMES,
)


ISSUE_SEED_FIELDNAMES = [
    "issue_id",
    "issue_date",
    "publication_id",
    "publication_canonical_id",
    "newspaper_display_name",
    "publication_location",
    "query_keywords",
    "hit_page_count",
    "first_hit_page_num",
    "sample_image_id",
    "sample_image_page_url",
    "sample_viewer_url",
    "first_api_page_index",
    "first_search_record_index",
]


@dataclass(frozen=True)
class YearShard:
    worker_index: int
    start_year: int
    end_year: int

    @property
    def years(self) -> list[int]:
        return list(range(self.start_year, self.end_year + 1))


@dataclass(frozen=True)
class DateRangeSlice:
    worker_index: int
    label: str
    date_start: str
    date_end: str
    sleep_between_requests: float
    estimated_weight: float


def density_aware_year_settings(year: int) -> tuple[int, float]:
    if year <= 1849:
        return (25, 5.0)
    if year <= 1899:
        return (10, 7.5)
    if year <= 1937:
        return (5, 10.0)
    if year <= 1969:
        return (3, 12.5)
    if year <= 1989:
        return (2, 15.0)
    if year <= 2004:
        return (2, 18.0)
    return (1, 20.0)


def estimate_year_weight(year: int) -> float:
    if year <= 1849:
        return 1.0
    if year <= 1899:
        return 4.0
    if year <= 1937:
        return 8.0
    if year <= 1969:
        return 16.0
    if year <= 1989:
        return 28.0
    if year <= 2004:
        return 40.0
    return 55.0


def estimate_shard_weight(years: Iterable[int]) -> float:
    return sum(estimate_year_weight(year) for year in years)


def monthly_slice_settings(year: int) -> tuple[float, float]:
    if year <= 1937:
        return (12.0, 6.0)
    if year <= 1969:
        return (12.0, 8.0)
    if year <= 1989:
        return (12.0, 10.0)
    if year <= 2004:
        return (12.0, 12.0)
    return (12.0, 14.0)


def build_density_aware_date_slices(
    *,
    start_year: int,
    end_year: int,
    sleep_scale: float = 1.0,
) -> list[DateRangeSlice]:
    if end_year < start_year:
        raise ValueError("end_year must be >= start_year")

    slices: list[DateRangeSlice] = []
    index = 1
    for year in range(start_year, end_year + 1):
        if year <= 1914:
            base_sleep = density_aware_year_settings(year)[1]
            slices.append(
                DateRangeSlice(
                    worker_index=index,
                    label=str(year),
                    date_start=f"{year}-01-01",
                    date_end=f"{year}-12-31",
                    sleep_between_requests=round(base_sleep * sleep_scale, 3),
                    estimated_weight=estimate_year_weight(year),
                )
            )
            index += 1
            continue

        base_sleep, month_weight = monthly_slice_settings(year)
        for month in range(1, 13):
            last_day = calendar.monthrange(year, month)[1]
            slices.append(
                DateRangeSlice(
                    worker_index=index,
                    label=f"{year}-{month:02d}",
                    date_start=f"{year}-{month:02d}-01",
                    date_end=f"{year}-{month:02d}-{last_day:02d}",
                    sleep_between_requests=round(base_sleep * sleep_scale, 3),
                    estimated_weight=month_weight,
                )
            )
            index += 1
    return slices


def build_density_aware_year_shards(
    *,
    start_year: int,
    end_year: int,
) -> list[YearShard]:
    if end_year < start_year:
        raise ValueError("end_year must be >= start_year")

    shards: list[YearShard] = []
    cursor = start_year
    index = 1
    while cursor <= end_year:
        block_years, _sleep_between_requests = density_aware_year_settings(cursor)
        # Do not let a shard cross into a different pacing era.
        era_end = cursor + block_years - 1
        check_year = cursor
        while check_year <= min(end_year, era_end):
            next_block_years, _ = density_aware_year_settings(check_year)
            if next_block_years != block_years:
                era_end = check_year - 1
                break
            check_year += 1
        shard_end = min(end_year, era_end)
        shards.append(
            YearShard(
                worker_index=index,
                start_year=cursor,
                end_year=shard_end,
            )
        )
        cursor = shard_end + 1
        index += 1
    return shards


@dataclass
class WorkerRuntime:
    row: dict[str, str]
    process: subprocess.Popen[str]
    stdout_path: Path
    stderr_path: Path
    attempts: int
    started_at: float


def build_year_shards(
    *,
    start_year: int,
    end_year: int,
    worker_count: int,
) -> list[YearShard]:
    if worker_count < 1:
        raise ValueError("worker_count must be at least 1")
    if end_year < start_year:
        raise ValueError("end_year must be >= start_year")

    total_years = end_year - start_year + 1
    base = total_years // worker_count
    extra = total_years % worker_count

    shards: list[YearShard] = []
    cursor = start_year
    for index in range(1, worker_count + 1):
        shard_size = base + (1 if index <= extra else 0)
        if shard_size <= 0:
            shard_start = cursor
            shard_end = cursor - 1
        else:
            shard_start = cursor
            shard_end = cursor + shard_size - 1
            cursor = shard_end + 1
        shards.append(
            YearShard(
                worker_index=index,
                start_year=shard_start,
                end_year=shard_end,
            )
        )
    return shards


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


def _parse_years_csv(row: dict[str, str]) -> list[int]:
    years_csv = str(row.get("years_csv", "")).strip()
    if years_csv:
        return [int(part) for part in years_csv.split(",") if part.strip()]
    start_year = int(row["start_year"])
    end_year = int(row["end_year"])
    return list(range(start_year, end_year + 1))


def build_worker_shell_script(
    row: dict[str, str],
    *,
    repo_root: Path,
    cookies_json: Path | None,
    max_api_retries: int,
    api_backoff_seconds: float,
) -> str:
    keyword = row["keyword"]
    worker_name = row["worker_name"]
    worker_dir = Path(row["output_dir"]).resolve()
    location = row.get("location", "").strip()
    entity_types = row["entity_types"]
    max_pages = int(row["max_pages"])
    count_per_request = int(row["count_per_request"])
    sleep_between_requests = float(row["sleep_between_requests"])
    date_start = str(row.get("date_start", "")).strip()
    date_end = str(row.get("date_end", "")).strip()
    slice_label = str(row.get("slice_label", "")).strip()
    years = _parse_years_csv(row) if not (date_start and date_end) else []

    lines = [
        "set -euo pipefail",
        f"cd {shlex.quote(str(repo_root))}",
        f"mkdir -p {shlex.quote(str(worker_dir))}",
        f"echo 'Starting {worker_name} ({slice_label or (f'{years[0]}-{years[-1]}' if years else f'{date_start}..{date_end}')})'",
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

    if date_start and date_end:
        slice_output_dir = worker_dir / "slices" / (slice_label or f"{date_start}_to_{date_end}")
        command = [
            sys.executable,
            "-m",
            "newspaper_scrapper.cli.main",
            "search-content",
            "--keyword",
            keyword,
            "--output-dir",
            str(slice_output_dir),
            "--date-start",
            date_start,
            "--date-end",
            date_end,
            "--entity-types",
            entity_types,
            "--max-pages",
            str(max_pages),
            "--count-per-request",
            str(count_per_request),
            "--navigate-search-results",
            "--sleep-between-requests",
            str(sleep_between_requests),
            "--max-api-retries",
            str(max_api_retries),
            "--api-backoff-seconds",
            str(api_backoff_seconds),
        ]
        if location:
            command.extend(["--location", location])
        lines.extend(
            [
                f"mkdir -p {shlex.quote(str(slice_output_dir))}",
                f"echo '  {worker_name}: {slice_label or date_start}'",
                shlex.join(command),
            ]
        )
    else:
        for year in years:
            year_output_dir = worker_dir / "years" / str(year)
            command = [
                sys.executable,
                "-m",
                "newspaper_scrapper.cli.main",
                "search-content",
                "--keyword",
                keyword,
                "--output-dir",
                str(year_output_dir),
                "--date",
                str(year),
                "--entity-types",
                entity_types,
                "--max-pages",
                str(max_pages),
                "--count-per-request",
                str(count_per_request),
                "--navigate-search-results",
                "--sleep-between-requests",
                str(sleep_between_requests),
                "--max-api-retries",
                str(max_api_retries),
                "--api-backoff-seconds",
                str(api_backoff_seconds),
            ]
            if location:
                command.extend(["--location", location])
            lines.extend(
                [
                    f"mkdir -p {shlex.quote(str(year_output_dir))}",
                    f"echo '  {worker_name}: {year}'",
                    shlex.join(command),
                ]
            )
    return "\n".join(lines) + "\n"


def plan_search_workers(
    *,
    keyword: str,
    output_dir: Path,
    worker_count: int,
    start_year: int,
    end_year: int,
    max_pages: int,
    count_per_request: int,
    sleep_between_requests: float,
    max_api_retries: int,
    api_backoff_seconds: float,
    entity_types: str,
    location: str | None,
    base_debug_port: int,
    profile_root: Path | None,
    cookies_json: Path | None,
    shard_preset: str = "uniform",
    sleep_scale: float = 1.0,
) -> dict[str, object]:
    date_slices: list[DateRangeSlice] = []
    shards: list[YearShard] = []
    if shard_preset == "density-aware":
        date_slices = build_density_aware_date_slices(
            start_year=start_year,
            end_year=end_year,
            sleep_scale=sleep_scale,
        )
    else:
        shards = build_year_shards(
            start_year=start_year,
            end_year=end_year,
            worker_count=worker_count,
        )
    repo_root = Path.cwd().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    workers_dir = output_dir / "workers"
    profiles_dir = profile_root or (output_dir / "worker_profiles")
    plan_csv = output_dir / "worker_plan.csv"
    launch_script = output_dir / "launch_workers.sh"

    plan_rows: list[dict[str, str]] = []
    launch_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f"cd {json.dumps(str(repo_root))}",
        "",
    ]

    slice_iterable: list[dict[str, str]] = []
    for shard in shards:
        worker_name = f"worker_{shard.worker_index:02d}"
        worker_dir = workers_dir / worker_name
        profile_dir = profiles_dir / worker_name
        worker_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
        debug_port = base_debug_port + shard.worker_index - 1
        years = shard.years
        years_csv = ",".join(str(year) for year in years)
        shard_sleep_between_requests = sleep_between_requests
        estimated_weight = estimate_shard_weight(years)
        plan_row = {
            "worker_index": str(shard.worker_index),
            "worker_name": worker_name,
            "keyword": keyword,
            "slice_label": "",
            "date_start": "",
            "date_end": "",
            "start_year": str(shard.start_year),
            "end_year": str(shard.end_year),
            "year_count": str(len(years)),
            "years_csv": years_csv,
            "output_dir": str(worker_dir),
            "chrome_profile_dir": str(profile_dir),
            "chrome_debug_port": str(debug_port),
            "max_pages": str(max_pages),
            "count_per_request": str(count_per_request),
            "sleep_between_requests": str(shard_sleep_between_requests),
            "max_api_retries": str(max_api_retries),
            "api_backoff_seconds": str(api_backoff_seconds),
            "entity_types": entity_types,
            "location": location or "",
            "estimated_weight": f"{estimated_weight:.2f}",
        }
        slice_iterable.append(plan_row)

    for slice_spec in date_slices:
        worker_name = f"worker_{slice_spec.worker_index:02d}"
        worker_dir = workers_dir / worker_name
        profile_dir = profiles_dir / worker_name
        worker_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.mkdir(parents=True, exist_ok=True)
        debug_port = base_debug_port + slice_spec.worker_index - 1
        slice_iterable.append(
            {
                "worker_index": str(slice_spec.worker_index),
                "worker_name": worker_name,
                "keyword": keyword,
                "slice_label": slice_spec.label,
                "date_start": slice_spec.date_start,
                "date_end": slice_spec.date_end,
                "start_year": "",
                "end_year": "",
                "year_count": "0",
                "years_csv": "",
                "output_dir": str(worker_dir),
                "chrome_profile_dir": str(profile_dir),
                "chrome_debug_port": str(debug_port),
                "max_pages": str(max_pages),
                "count_per_request": str(count_per_request),
                "sleep_between_requests": str(slice_spec.sleep_between_requests),
                "max_api_retries": str(max_api_retries),
                "api_backoff_seconds": str(api_backoff_seconds),
                "entity_types": entity_types,
                "location": location or "",
                "estimated_weight": f"{slice_spec.estimated_weight:.2f}",
            }
        )

    for plan_row in slice_iterable:
        worker_name = plan_row["worker_name"]
        worker_dir = Path(plan_row["output_dir"]).resolve()
        profile_dir = Path(plan_row["chrome_profile_dir"]).resolve()
        if plan_row["date_start"] and plan_row["date_end"]:
            worker_label = plan_row["slice_label"]
        else:
            years = _parse_years_csv(plan_row)
            worker_label = f"years {years[0]}-{years[-1]}"

        worker_script = build_worker_shell_script(
            plan_row,
            repo_root=repo_root,
            cookies_json=cookies_json,
            max_api_retries=max_api_retries,
            api_backoff_seconds=api_backoff_seconds,
        )
        launch_lines.extend(
            [
                f"mkdir -p {json.dumps(str(worker_dir))} {json.dumps(str(profile_dir))}",
                f"echo 'Launching {worker_name} for {worker_label}'",
                "(",
                f"  export NEWSCOM_CHROME_DEBUG_PORT={plan_row['chrome_debug_port']}",
                f"  export NEWSCOM_CHROME_PROFILE_DIR={json.dumps(str(plan_row['chrome_profile_dir']))}",
                "  export PYTHONPATH=src",
            ]
        )
        for line in worker_script.strip().splitlines():
            launch_lines.append(f"  {line}")
        launch_lines.extend(
            [
                ") > "
                + json.dumps(str(worker_dir / "stdout.log"))
                + " 2> "
                + json.dumps(str(worker_dir / "stderr.log"))
                + " &",
                f"echo $! > {json.dumps(str(worker_dir / 'pid.txt'))}",
                "",
            ]
        )
        plan_rows.append(plan_row)

    _write_csv(
        plan_csv,
        [
            "worker_index",
            "worker_name",
            "keyword",
            "slice_label",
            "date_start",
            "date_end",
            "start_year",
            "end_year",
            "year_count",
            "years_csv",
            "output_dir",
            "chrome_profile_dir",
            "chrome_debug_port",
            "max_pages",
            "count_per_request",
            "sleep_between_requests",
            "max_api_retries",
            "api_backoff_seconds",
            "entity_types",
            "location",
            "estimated_weight",
        ],
        plan_rows,
    )
    launch_script.write_text("\n".join(launch_lines) + "\n")
    launch_script.chmod(0o755)

    summary = {
        "keyword": keyword,
        "worker_count": worker_count,
        "worker_count_planned": len(plan_rows),
        "start_year": start_year,
        "end_year": end_year,
        "max_pages": max_pages,
        "count_per_request": count_per_request,
        "sleep_between_requests": sleep_between_requests,
        "max_api_retries": max_api_retries,
        "api_backoff_seconds": api_backoff_seconds,
        "entity_types": entity_types,
        "location": location or "",
        "base_debug_port": base_debug_port,
        "cookies_json": str(cookies_json) if cookies_json is not None else "",
        "shard_preset": shard_preset,
        "sleep_scale": sleep_scale,
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


def run_search_workers(
    *,
    plan_csv: Path,
    output_dir: Path,
    max_concurrent_workers: int,
    worker_stagger_seconds: float,
    retry_cooldown_seconds: float,
    max_worker_attempts: int,
    cookies_json: Path | None,
    poll_seconds: float,
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
        }
        _write_runner_summary(runner_summary_json, payload)

    def launch_worker(row: dict[str, str], attempts: int) -> WorkerRuntime:
        nonlocal launch_sequence
        worker_name = row["worker_name"]
        worker_dir = Path(row["output_dir"]).resolve()
        worker_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = worker_dir / "stdout.log"
        stderr_path = worker_dir / "stderr.log"
        env = os.environ.copy()
        env["PYTHONPATH"] = "src"
        env["NEWSCOM_CHROME_DEBUG_PORT"] = row["chrome_debug_port"]
        env["NEWSCOM_CHROME_PROFILE_DIR"] = row["chrome_profile_dir"]
        shell_script = build_worker_shell_script(
            row,
            repo_root=repo_root,
            cookies_json=cookies_json,
            max_api_retries=int(row.get("max_api_retries", "0") or 0),
            api_backoff_seconds=float(row.get("api_backoff_seconds", "0") or 0),
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
            if return_code == 0:
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
        "elapsed_seconds": round(time.time() - started_at, 3),
        "runner_summary_json": str(runner_summary_json),
    }
    _write_runner_summary(output_dir / "summary.json", final_summary)
    write_summary()
    return final_summary


def merge_search_workers(
    *,
    workers_root: Path,
    output_dir: Path,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    worker_dirs = sorted(path for path in workers_root.iterdir() if path.is_dir())

    merged_results: list[dict[str, str]] = []
    deduped_pages_by_image: dict[str, dict[str, str]] = {}
    issue_rows: dict[str, dict[str, str]] = {}

    for worker_dir in worker_dirs:
        results_rows: list[dict[str, str]] = []
        page_rows: list[dict[str, str]] = []
        for path in sorted(worker_dir.rglob("results.csv")):
            results_rows.extend(_read_csv(path))
        for path in sorted(worker_dir.rglob("page_manifest.csv")):
            page_rows.extend(_read_csv(path))
        merged_results.extend(results_rows)

        for row in page_rows:
            key = row.get("preferred_image_id", "").strip() or row.get(
                "preferred_image_page_url", ""
            ).strip()
            if key and key not in deduped_pages_by_image:
                deduped_pages_by_image[key] = row

            issue_id = row.get("issue_id", "").strip()
            if not issue_id:
                continue
            issue = issue_rows.get(issue_id)
            if issue is None:
                issue = {
                    "issue_id": issue_id,
                    "issue_date": row.get("issue_date", "").strip(),
                    "publication_id": row.get("publication_id", "").strip(),
                    "publication_canonical_id": row.get("publication_canonical_id", "").strip(),
                    "newspaper_display_name": row.get("newspaper_display_name", "").strip(),
                    "publication_location": row.get("publication_location", "").strip(),
                    "query_keywords": row.get("query_keyword", "").strip(),
                    "hit_page_count": "1",
                    "first_hit_page_num": row.get("page_num", "").strip(),
                    "sample_image_id": row.get("preferred_image_id", "").strip(),
                    "sample_image_page_url": row.get("preferred_image_page_url", "").strip(),
                    "sample_viewer_url": row.get("viewer_url", "").strip(),
                    "first_api_page_index": row.get("first_api_page_index", "").strip(),
                    "first_search_record_index": row.get("first_search_record_index", "").strip(),
                }
                issue_rows[issue_id] = issue
            else:
                issue["hit_page_count"] = str(int(issue["hit_page_count"]) + 1)
                keywords = {
                    kw.strip()
                    for kw in issue.get("query_keywords", "").split("|")
                    if kw.strip()
                }
                new_kw = row.get("query_keyword", "").strip()
                if new_kw:
                    keywords.add(new_kw)
                issue["query_keywords"] = "|".join(sorted(keywords))

    merged_results_csv = output_dir / "results_merged.csv"
    merged_page_manifest_csv = output_dir / "page_manifest_merged.csv"
    merged_issue_manifest_csv = output_dir / "issue_manifest_merged.csv"

    _write_csv(merged_results_csv, RESULT_FIELDNAMES, merged_results)
    merged_page_rows = sorted(
        deduped_pages_by_image.values(),
        key=lambda row: (
            row.get("issue_date", ""),
            row.get("issue_id", ""),
            row.get("page_num", ""),
        ),
    )
    _write_csv(merged_page_manifest_csv, PAGE_MANIFEST_FIELDNAMES, merged_page_rows)
    merged_issue_rows = sorted(
        issue_rows.values(),
        key=lambda row: (
            row.get("issue_date", ""),
            row.get("issue_id", ""),
        ),
    )
    _write_csv(merged_issue_manifest_csv, ISSUE_SEED_FIELDNAMES, merged_issue_rows)

    summary = {
        "workers_root": str(workers_root),
        "worker_count_detected": len(worker_dirs),
        "raw_result_rows": len(merged_results),
        "unique_page_hits": len(merged_page_rows),
        "unique_issue_hits": len(merged_issue_rows),
        "results_merged_csv": str(merged_results_csv),
        "page_manifest_merged_csv": str(merged_page_manifest_csv),
        "issue_manifest_merged_csv": str(merged_issue_manifest_csv),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary
