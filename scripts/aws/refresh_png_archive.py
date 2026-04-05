#!/usr/bin/env python3
"""Build a canonical local archive view for preserved viewer PNGs.

The archive is intentionally symlink-based. Worker-local runtime directories and
older ad hoc output folders remain where they are, while this script creates a
stable central browse point plus an inventory.
"""

from __future__ import annotations

import json
from pathlib import Path


def rel_symlink_target(link_parent: Path, target: Path) -> str:
    return str(target.resolve().relative_to(link_parent.resolve()).joinpath())  # pragma: no cover


def make_relative_symlink(link_path: Path, target: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    relative = Path(
        Path(
            __import__("os").path.relpath(
                target.resolve(),
                start=link_path.parent.resolve(),
            )
        )
    )
    link_path.symlink_to(relative)


def count_viewer_pngs(path: Path) -> int:
    return sum(1 for _ in path.rglob("*_viewer.png"))


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    output_root = repo_root / "output"
    archive_root = output_root / "png_archive"
    sources_root = archive_root / "sources"
    inventory_tsv = archive_root / "inventory.tsv"
    inventory_json = archive_root / "inventory.json"

    sources_root.mkdir(parents=True, exist_ok=True)

    source_dirs: list[tuple[str, int, Path]] = []
    for candidate in sorted(output_root.iterdir()):
        if not candidate.is_dir() or candidate.name == "png_archive":
            continue
        count = count_viewer_pngs(candidate)
        if count <= 0:
            continue
        source_dirs.append((candidate.name, count, candidate))

    # Rebuild central symlink view from current output directories.
    existing_links = {p.name for p in sources_root.iterdir() if p.is_symlink()}
    current_names = {name for name, _, _ in source_dirs}
    for stale in sorted(existing_links - current_names):
        (sources_root / stale).unlink()

    for name, _, source in source_dirs:
        make_relative_symlink(sources_root / name, source)

    total_pngs = sum(count for _, count, _ in source_dirs)
    inventory_rows = [
        {
            "source": name,
            "viewer_png_count": count,
            "path": str(path),
            "archive_link": str(sources_root / name),
        }
        for name, count, path in sorted(source_dirs, key=lambda item: (-item[1], item[0]))
    ]

    inventory_tsv.write_text(
        "source\tviewer_png_count\tpath\tarchive_link\n"
        + "\n".join(
            f"{row['source']}\t{row['viewer_png_count']}\t{row['path']}\t{row['archive_link']}"
            for row in inventory_rows
        )
        + "\n"
    )
    inventory_json.write_text(
        json.dumps(
            {
                "total_viewer_pngs": total_pngs,
                "source_count": len(inventory_rows),
                "sources": inventory_rows,
            },
            indent=2,
        )
        + "\n"
    )

    print(f"Archive root: {archive_root}")
    print(f"Sources linked: {len(inventory_rows)}")
    print(f"Total viewer PNGs: {total_pngs}")


if __name__ == "__main__":
    main()
