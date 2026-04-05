"""Helpers for conservative multi-session manifest sharding."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if not fieldnames:
            raise ValueError(f"{path} has no CSV header")
        rows = [{key: (value or "") for key, value in row.items()} for row in reader]
    return fieldnames, rows


def assign_round_robin(rows: list[dict[str, str]], num_shards: int) -> list[list[dict[str, str]]]:
    shards: list[list[dict[str, str]]] = [[] for _ in range(num_shards)]
    for index, row in enumerate(rows):
        shards[index % num_shards].append(row)
    return shards


def assign_by_issue(rows: list[dict[str, str]], num_shards: int) -> list[list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        issue_id = str(row.get("issue_id", "")).strip()
        if not issue_id:
            raise ValueError("by_issue sharding requires every row to have issue_id")
        grouped[issue_id].append(row)

    shards: list[list[dict[str, str]]] = [[] for _ in range(num_shards)]
    shard_sizes = [0 for _ in range(num_shards)]
    groups = sorted(grouped.values(), key=len, reverse=True)
    for group in groups:
        target_idx = min(range(num_shards), key=lambda idx: shard_sizes[idx])
        shards[target_idx].extend(group)
        shard_sizes[target_idx] += len(group)
    return shards


def write_rows(path: Path, fieldnames: list[str], rows: Iterable[dict[str, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def shard_manifest(
    *,
    manifest_csv: Path,
    output_dir: Path,
    num_shards: int,
    strategy: str,
) -> dict[str, object]:
    if num_shards < 1:
        raise ValueError("num_shards must be at least 1")

    fieldnames, rows = read_rows(manifest_csv)
    output_dir.mkdir(parents=True, exist_ok=True)

    if strategy == "round_robin":
        shards = assign_round_robin(rows, num_shards)
    elif strategy == "by_issue":
        shards = assign_by_issue(rows, num_shards)
    else:
        raise ValueError(f"Unsupported sharding strategy: {strategy}")

    shard_files: list[dict[str, object]] = []
    for index, shard_rows in enumerate(shards, start=1):
        shard_path = output_dir / f"shard_{index:03d}.csv"
        write_rows(shard_path, fieldnames, shard_rows)
        shard_files.append(
            {
                "shard_index": index,
                "row_count": len(shard_rows),
                "path": str(shard_path),
            }
        )

    summary = {
        "manifest_csv": str(manifest_csv),
        "output_dir": str(output_dir),
        "num_shards": num_shards,
        "strategy": strategy,
        "total_rows": len(rows),
        "shards": shard_files,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary
