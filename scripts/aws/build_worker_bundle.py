#!/usr/bin/env python3
"""Build a deployable repo bundle for EC2 workers.

The AWS bootstrap extracts bundles with ``--strip-components=1``, so the tarball
must contain a single top-level directory. This helper creates that shape
consistently and keeps large local state such as ``output/`` or browser
profiles out of the bundle.
"""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path


DEFAULT_INCLUDE_PATHS = [
    "README.md",
    "pyproject.toml",
    "poetry.lock",
    "docs",
    "scripts",
    "src",
    "tests",
]


def build_bundle(
    *,
    repo_root: Path,
    output_path: Path,
    top_level_dir: str,
    include_paths: list[str],
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, "w:gz") as archive:
        for relative_path in include_paths:
            source_path = (repo_root / relative_path).resolve()
            if not source_path.exists():
                raise FileNotFoundError(f"bundle path does not exist: {source_path}")
            archive_name = str(Path(top_level_dir) / relative_path)
            archive.add(source_path, arcname=archive_name)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to package. Defaults to the current working directory.",
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        required=True,
        help="Destination tar.gz path.",
    )
    parser.add_argument(
        "--top-level-dir",
        default="newscom-worker-bundle",
        help="Top-level directory name inside the tarball.",
    )
    parser.add_argument(
        "--include-path",
        action="append",
        dest="include_paths",
        help="Path relative to repo root to include. Repeat to override the defaults.",
    )
    args = parser.parse_args()

    include_paths = args.include_paths or list(DEFAULT_INCLUDE_PATHS)
    build_bundle(
        repo_root=args.repo_root.resolve(),
        output_path=args.output_path.resolve(),
        top_level_dir=args.top_level_dir,
        include_paths=include_paths,
    )
    print(args.output_path)


if __name__ == "__main__":
    main()
