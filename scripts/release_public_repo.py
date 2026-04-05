from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_FORBIDDEN_PATTERNS_FILE = Path(
    "docs/private/public_export_forbidden_patterns.local.txt"
)


def run(
    args: list[str],
    *,
    cwd: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )


def repo_root(start: Path | None = None) -> Path:
    resolved_start = (start or Path.cwd()).resolve()
    result = run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=resolved_start,
        capture_output=True,
    )
    return Path(result.stdout.strip())


def remote_repo_name(repo_dir: Path, remote: str = "origin") -> str | None:
    try:
        result = run(
            ["git", "remote", "get-url", remote],
            cwd=repo_dir,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        return None

    remote_url = result.stdout.strip()
    if not remote_url:
        return None

    if remote_url.startswith("git@"):
        _, _, path = remote_url.partition(":")
        repo_name = path.rsplit("/", 1)[-1]
    else:
        parsed = urlparse(remote_url)
        repo_name = parsed.path.rsplit("/", 1)[-1]

    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    return repo_name or None


def export_head(repo_dir: Path, output_dir: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".tar") as archive_file:
        run(
            [
                "git",
                "archive",
                "--format=tar",
                f"--output={archive_file.name}",
                "HEAD",
            ],
            cwd=repo_dir,
        )
        with tarfile.open(archive_file.name, "r") as archive:
            archive.extractall(output_dir)


def init_git_repo(output_dir: Path, commit_message: str) -> None:
    run(["git", "init", "-b", "main"], cwd=output_dir)
    run(["git", "add", "."], cwd=output_dir)
    run(["git", "commit", "-m", commit_message], cwd=output_dir)


def load_patterns(patterns_file: Path | None) -> list[str]:
    if patterns_file is None or not patterns_file.exists():
        return []

    patterns: list[str] = []
    for raw_line in patterns_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def iter_text_files(root: Path) -> list[Path]:
    text_files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        text_files.append(path)
    return text_files


def scan_forbidden_patterns(root: Path, patterns: list[str]) -> list[str]:
    if not patterns:
        return []

    compiled = [re.compile(pattern) for pattern in patterns]
    matches: list[str] = []

    for file_path in iter_text_files(root):
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for line_number, line in enumerate(content.splitlines(), start=1):
            for pattern in compiled:
                if pattern.search(line):
                    relative_path = file_path.relative_to(root)
                    matches.append(f"{relative_path}:{line_number}: {pattern.pattern}")
    return matches


def default_output_dir(repo_dir: Path) -> Path:
    repo_name = remote_repo_name(repo_dir)
    if repo_name and repo_name.endswith("-ops"):
        return repo_dir.parent / repo_name[:-4]
    if repo_name:
        return repo_dir.parent / repo_name
    return repo_dir.parent / f"{repo_dir.name}-public"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export the current clean private repo HEAD into a fresh public-safe sibling repo."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Destination directory for the exported public repo. Defaults to ../<repo>-public.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Remove the output directory first if it already exists.",
    )
    parser.add_argument(
        "--no-git-init",
        action="store_true",
        help="Do not initialize a git repo and initial commit in the output directory.",
    )
    parser.add_argument(
        "--commit-message",
        default="Initial public release",
        help="Commit message used when initializing the exported git repo.",
    )
    parser.add_argument(
        "--patterns-file",
        type=Path,
        default=DEFAULT_FORBIDDEN_PATTERNS_FILE,
        help=(
            "Optional local-only file containing one forbidden regex pattern per line. "
            "Defaults to docs/private/public_export_forbidden_patterns.local.txt."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_dir = repo_root()
    output_dir = (args.output_dir or default_output_dir(repo_dir)).resolve()
    patterns_file = (
        args.patterns_file.resolve()
        if args.patterns_file is not None and not args.patterns_file.is_absolute()
        else args.patterns_file
    )

    if output_dir.exists():
        if not args.force:
            raise SystemExit(
                f"Output directory already exists: {output_dir}. Re-run with --force to replace it."
            )
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    export_head(repo_dir, output_dir)

    matches = scan_forbidden_patterns(output_dir, load_patterns(patterns_file))
    if matches:
        raise SystemExit(
            "Forbidden patterns found in exported repo:\n" + "\n".join(matches)
        )

    if not args.no_git_init:
        init_git_repo(output_dir, args.commit_message)

    print(f"Exported public repo to {output_dir}")
    if patterns_file is not None and patterns_file.exists():
        print(f"Verified against forbidden-pattern file: {patterns_file}")
    else:
        print("No forbidden-pattern file was used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
