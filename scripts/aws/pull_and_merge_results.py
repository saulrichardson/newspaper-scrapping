#!/usr/bin/env python3
"""Pull AWS worker results from S3 and merge them into one local output set."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def sync_s3_prefix(
    bucket: str,
    prefix: str,
    output_dir: Path,
    *,
    metadata_only: bool,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    local_root = output_dir / "s3_results"
    command = [
        "aws",
        "s3",
        "sync",
        f"s3://{bucket}/{prefix}",
        str(local_root),
        "--exact-timestamps",
    ]
    if metadata_only:
        command.extend(["--exclude", "*.png", "--exclude", "*.jpg", "--exclude", "*.jpeg"])
    subprocess.run(command, check=True)
    return local_root


def flatten_workers(s3_root: Path, output_dir: Path) -> Path:
    flat_root = output_dir / "workers_flat"
    if flat_root.exists():
        shutil.rmtree(flat_root)
    flat_root.mkdir(parents=True, exist_ok=True)

    for instance_dir in sorted(path for path in s3_root.iterdir() if path.is_dir()):
        workers_root = instance_dir / "run" / "workers"
        if not workers_root.exists():
            continue
        for worker_dir in sorted(path for path in workers_root.iterdir() if path.is_dir()):
            target = flat_root / f"{instance_dir.name}__{worker_dir.name}"
            shutil.copytree(worker_dir, target)
    return flat_root


def merge_workers(
    repo_root: Path,
    workers_root: Path,
    output_dir: Path,
    *,
    mode: str,
) -> Path:
    merged_dir = output_dir / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)
    merge_command = (
        "merge-search-workers" if mode == "search" else "merge-screenshot-workers"
    )
    subprocess.run(
        [
            "python3",
            "-m",
            "newspaper_scrapper.cli.main",
            merge_command,
            "--workers-root",
            str(workers_root),
            "--output-dir",
            str(merged_dir),
        ],
        cwd=repo_root,
        check=True,
        env={
            **dict(os.environ),
            "PYTHONPATH": "src",
        },
    )
    return merged_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--mode",
        default="search",
        choices=["search", "screenshot"],
        help="Which worker merger to run after pulling the S3 prefix.",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Skip pulling PNG/JPEG payloads when you only need logs, summaries, and CSVs.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    args = parser.parse_args()

    s3_root = sync_s3_prefix(
        args.bucket,
        args.prefix,
        args.output_dir,
        metadata_only=args.metadata_only,
    )
    flat_root = flatten_workers(s3_root, args.output_dir)
    merged_dir = merge_workers(
        args.repo_root,
        flat_root,
        args.output_dir,
        mode=args.mode,
    )

    summary = {
        "bucket": args.bucket,
        "prefix": args.prefix,
        "mode": args.mode,
        "metadata_only": args.metadata_only,
        "s3_root": str(s3_root),
        "workers_flat_root": str(flat_root),
        "merged_dir": str(merged_dir),
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
