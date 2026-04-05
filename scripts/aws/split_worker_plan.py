#!/usr/bin/env python3
"""Split a worker plan CSV into balanced per-instance subsets."""

from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--instance-count", type=int, required=True)
    parser.add_argument(
        "--copy-worker-assets",
        action="store_true",
        help=(
            "Copy per-worker asset directories, such as workers/<name>/input_manifest.csv, "
            "into instance-scoped subdirectories."
        ),
    )
    args = parser.parse_args()

    with args.plan_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames

    if not fieldnames:
        raise SystemExit("missing fieldnames")
    if args.instance_count < 1:
        raise SystemExit("instance-count must be >= 1")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    buckets: list[list[dict[str, str]]] = [[] for _ in range(args.instance_count)]
    bucket_weights = [0.0 for _ in range(args.instance_count)]

    def row_weight(row: dict[str, str]) -> float:
        value = str(row.get("estimated_weight", "")).strip()
        if value:
            try:
                return float(value)
            except ValueError:
                pass
        years = str(row.get("year_count", "")).strip()
        try:
            return float(years)
        except ValueError:
            return 1.0

    for row in sorted(rows, key=row_weight, reverse=True):
        target_index = min(range(args.instance_count), key=lambda idx: bucket_weights[idx])
        buckets[target_index].append(row)
        bucket_weights[target_index] += row_weight(row)

    workers_root = args.plan_csv.parent / "workers"

    for index, bucket in enumerate(buckets, start=1):
        if args.copy_worker_assets:
            instance_root = args.output_dir / f"instance_{index:02d}"
            path = instance_root / "worker_plan.csv"
            instance_root.mkdir(parents=True, exist_ok=True)
        else:
            path = args.output_dir / f"instance_{index:02d}_worker_plan.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(bucket)
        if args.copy_worker_assets and workers_root.exists():
            assets_root = path.parent / "workers"
            assets_root.mkdir(parents=True, exist_ok=True)
            for row in bucket:
                worker_name = str(row.get("worker_name", "")).strip()
                if not worker_name:
                    continue
                source_dir = workers_root / worker_name
                if not source_dir.exists():
                    continue
                target_dir = assets_root / worker_name
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                shutil.copytree(source_dir, target_dir)
        print(f"{path} rows={len(bucket)} weight={bucket_weights[index - 1]:.2f}")


if __name__ == "__main__":
    main()
