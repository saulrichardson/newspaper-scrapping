#!/usr/bin/env python3
"""Build a canonical S3 archive and inventory for viewer PNG artifacts.

The runtime worker prefixes under ``results/`` remain ephemeral. This script
creates a stable archive keyed by image ID plus inventory/provenance manifests
that can survive worker retirement and replacement.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VIEWER_SUFFIX = "_viewer.png"
IMAGE_ID_RE = re.compile(r"^(?P<image_id>\d+)_viewer\.png$")


@dataclass(frozen=True)
class PngOccurrence:
    filename: str
    image_id: str
    source_type: str
    source_group: str
    source_ref: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Fleet S3 bucket name.")
    parser.add_argument(
        "--source-prefix",
        action="append",
        default=["results/"],
        help="S3 source prefix to scan. May be passed multiple times.",
    )
    parser.add_argument(
        "--archive-prefix",
        default="archive/viewer_png/by_image_id",
        help="Canonical archive prefix inside the bucket.",
    )
    parser.add_argument(
        "--inventory-prefix",
        default="archive/inventory",
        help="Inventory prefix inside the bucket.",
    )
    parser.add_argument(
        "--snapshots-prefix",
        default="archive/snapshots",
        help="Snapshot-manifest prefix inside the bucket.",
    )
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="Optional live worker host to scan for host-only PNGs. May be repeated.",
    )
    parser.add_argument(
        "--ssh-key",
        default=None,
        help="SSH private key for host scanning/import.",
    )
    parser.add_argument(
        "--ssh-user",
        default="ubuntu",
        help="SSH username for live worker hosts.",
    )
    parser.add_argument(
        "--host-scan-root",
        default="/opt/newscom",
        help="Root path to scan on each live worker host.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Local directory for generated inventory artifacts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not copy or upload objects; only compute inventories.",
    )
    parser.add_argument(
        "--copy-workers",
        type=int,
        default=16,
        help="Parallel copy/upload worker count for canonical archive population.",
    )
    return parser.parse_args()


def run_command(args: list[str], *, stdin=None) -> str:
    completed = subprocess.run(
        args,
        check=True,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout


def run_command_bytes(args: list[str], *, stdin=None) -> None:
    subprocess.run(
        args,
        check=True,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def parse_image_id(filename: str) -> str:
    match = IMAGE_ID_RE.match(filename)
    if not match:
        raise ValueError(f"Unsupported viewer PNG filename: {filename}")
    return match.group("image_id")


def canonical_key_for_filename(archive_prefix: str, filename: str) -> str:
    image_id = parse_image_id(filename)
    fanout = image_id[:3]
    return f"{archive_prefix.rstrip('/')}/{fanout}/{filename}"


def source_group_for_s3_key(key: str) -> str:
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "results":
        return "/".join(parts[:3])
    if len(parts) > 1:
        return "/".join(parts[:-1])
    return key


def snapshot_name_for_group(source_group: str) -> str:
    return source_group.replace("/", "__").replace(":", "_")


def list_s3_viewer_pngs(bucket: str, prefixes: Iterable[str]) -> list[tuple[str, str]]:
    output = run_command(["aws", "s3", "ls", f"s3://{bucket}/", "--recursive"])
    allowed = tuple(prefix.rstrip("/") + "/" for prefix in prefixes)
    results: list[tuple[str, str]] = []
    for line in output.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) != 4:
            continue
        key = parts[3]
        if not key.endswith(VIEWER_SUFFIX):
            continue
        if allowed and not key.startswith(allowed):
            continue
        filename = key.rsplit("/", 1)[-1]
        results.append((key, filename))
    return results


def list_remote_host_viewer_pngs(
    host: str,
    *,
    ssh_user: str,
    ssh_key: str,
    scan_root: str,
) -> list[tuple[str, str]]:
    find_cmd = (
        f"find {shlex.quote(scan_root)} -type f -name '*{VIEWER_SUFFIX}' "
        "-printf '%f\\t%p\\n' 2>/dev/null | sort -u"
    )
    output = run_command(
        [
            "ssh",
            "-i",
            ssh_key,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            f"{ssh_user}@{host}",
            "bash",
            "-lc",
            find_cmd,
        ]
    )
    rows: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        if "\t" not in line:
            continue
        filename, remote_path = line.split("\t", 1)
        rows.append((filename, remote_path))
    return rows


def copy_s3_object(bucket: str, source_key: str, dest_key: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    run_command_bytes(
        [
            "aws",
            "s3",
            "cp",
            f"s3://{bucket}/{source_key}",
            f"s3://{bucket}/{dest_key}",
            "--only-show-errors",
        ]
    )


def copy_remote_file_to_s3(
    *,
    host: str,
    ssh_user: str,
    ssh_key: str,
    remote_path: str,
    bucket: str,
    dest_key: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    ssh_proc = subprocess.Popen(
        [
            "ssh",
            "-i",
            ssh_key,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            f"{ssh_user}@{host}",
            "cat",
            remote_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert ssh_proc.stdout is not None
    try:
        run_command_bytes(
            [
                "aws",
                "s3",
                "cp",
                "-",
                f"s3://{bucket}/{dest_key}",
                "--only-show-errors",
            ],
            stdin=ssh_proc.stdout,
        )
    finally:
        ssh_proc.stdout.close()
    _, stderr = ssh_proc.communicate()
    if ssh_proc.returncode != 0:
        raise subprocess.CalledProcessError(
            ssh_proc.returncode,
            ssh_proc.args,
            stderr=stderr,
        )


def write_tsv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def upload_file_to_s3(path: Path, bucket: str, key: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    run_command_bytes(
        [
            "aws",
            "s3",
            "cp",
            str(path),
            f"s3://{bucket}/{key}",
            "--only-show-errors",
        ]
    )


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    canonical_objects = list_s3_viewer_pngs(args.bucket, [args.archive_prefix])
    canonical_filenames = {filename for _, filename in canonical_objects}

    source_objects = list_s3_viewer_pngs(args.bucket, args.source_prefix)
    provenance: list[PngOccurrence] = []
    unique_sources: dict[str, PngOccurrence] = {}
    source_groups: dict[str, list[dict[str, str]]] = defaultdict(list)

    copied_from_s3 = 0
    copied_from_hosts = 0
    s3_copy_tasks: list[tuple[str, str]] = []
    host_copy_tasks: list[tuple[str, str, str]] = []

    for key, filename in source_objects:
        occurrence = PngOccurrence(
            filename=filename,
            image_id=parse_image_id(filename),
            source_type="s3",
            source_group=source_group_for_s3_key(key),
            source_ref=f"s3://{args.bucket}/{key}",
        )
        provenance.append(occurrence)
        source_groups[occurrence.source_group].append(
            {
                "filename": occurrence.filename,
                "image_id": occurrence.image_id,
                "source_ref": occurrence.source_ref,
                "source_type": occurrence.source_type,
            }
        )
        unique_sources.setdefault(filename, occurrence)
        if filename in canonical_filenames:
            continue
        dest_key = canonical_key_for_filename(args.archive_prefix, filename)
        canonical_filenames.add(filename)
        s3_copy_tasks.append((key, dest_key))

    if args.host and not args.ssh_key:
        raise SystemExit("--ssh-key is required when --host is used")

    for host in args.host:
        for filename, remote_path in list_remote_host_viewer_pngs(
            host,
            ssh_user=args.ssh_user,
            ssh_key=args.ssh_key,
            scan_root=args.host_scan_root,
        ):
            occurrence = PngOccurrence(
                filename=filename,
                image_id=parse_image_id(filename),
                source_type="host",
                source_group=f"hosts/{host}",
                source_ref=f"{host}:{remote_path}",
            )
            provenance.append(occurrence)
            source_groups[occurrence.source_group].append(
                {
                    "filename": occurrence.filename,
                    "image_id": occurrence.image_id,
                    "source_ref": occurrence.source_ref,
                    "source_type": occurrence.source_type,
                }
            )
            if filename not in unique_sources:
                unique_sources[filename] = occurrence
            if filename in canonical_filenames:
                continue
            dest_key = canonical_key_for_filename(args.archive_prefix, filename)
            canonical_filenames.add(filename)
            host_copy_tasks.append((host, remote_path, dest_key))

    if not args.dry_run:
        with ThreadPoolExecutor(max_workers=max(1, args.copy_workers)) as executor:
            futures = [
                executor.submit(copy_s3_object, args.bucket, source_key, dest_key, dry_run=False)
                for source_key, dest_key in s3_copy_tasks
            ]
            for future in futures:
                future.result()
        copied_from_s3 = len(s3_copy_tasks)

        with ThreadPoolExecutor(max_workers=max(1, args.copy_workers)) as executor:
            futures = [
                executor.submit(
                    copy_remote_file_to_s3,
                    host=host,
                    ssh_user=args.ssh_user,
                    ssh_key=args.ssh_key,
                    remote_path=remote_path,
                    bucket=args.bucket,
                    dest_key=dest_key,
                    dry_run=False,
                )
                for host, remote_path, dest_key in host_copy_tasks
            ]
            for future in futures:
                future.result()
        copied_from_hosts = len(host_copy_tasks)
    else:
        copied_from_s3 = len(s3_copy_tasks)
        copied_from_hosts = len(host_copy_tasks)

    inventory_rows = []
    for filename, first_source in sorted(unique_sources.items()):
        inventory_rows.append(
            {
                "image_id": first_source.image_id,
                "filename": filename,
                "canonical_s3_uri": f"s3://{args.bucket}/{canonical_key_for_filename(args.archive_prefix, filename)}",
                "first_source_type": first_source.source_type,
                "first_source_group": first_source.source_group,
                "first_source_ref": first_source.source_ref,
                "source_occurrences": sum(1 for item in provenance if item.filename == filename),
            }
        )

    provenance_rows = [
        {
            "image_id": item.image_id,
            "filename": item.filename,
            "source_type": item.source_type,
            "source_group": item.source_group,
            "source_ref": item.source_ref,
            "canonical_s3_uri": f"s3://{args.bucket}/{canonical_key_for_filename(args.archive_prefix, item.filename)}",
        }
        for item in sorted(
            provenance,
            key=lambda row: (row.source_group, row.filename, row.source_ref),
        )
    ]

    inventory_tsv = output_dir / "viewer_png_inventory.tsv"
    provenance_tsv = output_dir / "viewer_png_provenance.tsv"
    write_tsv(
        inventory_tsv,
        [
            "image_id",
            "filename",
            "canonical_s3_uri",
            "first_source_type",
            "first_source_group",
            "first_source_ref",
            "source_occurrences",
        ],
        inventory_rows,
    )
    write_tsv(
        provenance_tsv,
        [
            "image_id",
            "filename",
            "source_type",
            "source_group",
            "source_ref",
            "canonical_s3_uri",
        ],
        provenance_rows,
    )

    snapshots_dir = output_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    snapshot_files: list[tuple[Path, str]] = []
    for source_group, rows in sorted(source_groups.items()):
        snapshot_path = snapshots_dir / f"{snapshot_name_for_group(source_group)}.tsv"
        write_tsv(
            snapshot_path,
            ["image_id", "filename", "source_type", "source_ref"],
            sorted(rows, key=lambda row: (row["filename"], row["source_ref"])),
        )
        snapshot_files.append((snapshot_path, source_group))

    summary = {
        "bucket": args.bucket,
        "source_prefixes": args.source_prefix,
        "archive_prefix": args.archive_prefix,
        "inventory_prefix": args.inventory_prefix,
        "snapshots_prefix": args.snapshots_prefix,
        "unique_png_count": len(inventory_rows),
        "provenance_row_count": len(provenance_rows),
        "snapshot_count": len(snapshot_files),
        "canonical_png_count": len(canonical_filenames),
        "copied_from_s3": copied_from_s3,
        "copied_from_hosts": copied_from_hosts,
        "hosts_scanned": args.host,
        "dry_run": args.dry_run,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    upload_file_to_s3(
        inventory_tsv,
        args.bucket,
        f"{args.inventory_prefix.rstrip('/')}/viewer_png_inventory.tsv",
        dry_run=args.dry_run,
    )
    upload_file_to_s3(
        provenance_tsv,
        args.bucket,
        f"{args.inventory_prefix.rstrip('/')}/viewer_png_provenance.tsv",
        dry_run=args.dry_run,
    )
    upload_file_to_s3(
        summary_path,
        args.bucket,
        f"{args.inventory_prefix.rstrip('/')}/summary.json",
        dry_run=args.dry_run,
    )
    for snapshot_path, source_group in snapshot_files:
        upload_file_to_s3(
            snapshot_path,
            args.bucket,
            f"{args.snapshots_prefix.rstrip('/')}/{snapshot_name_for_group(source_group)}.tsv",
            dry_run=args.dry_run,
        )

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
