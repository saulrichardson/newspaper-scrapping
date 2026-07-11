from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
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


def git_status(repo_dir: Path) -> str:
    result = run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True)
    return result.stdout.strip()


def clear_worktree_preserving_git(repo_dir: Path) -> None:
    for child in repo_dir.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def stage_export(repo_dir: Path, *, initialize: bool) -> None:
    if initialize:
        run(["git", "init", "-b", "main"], cwd=repo_dir)
    run(["git", "add", "-A", "--force"], cwd=repo_dir)


def commit_staged(repo_dir: Path, commit_message: str) -> bool:
    changed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_dir,
        check=False,
    ).returncode != 0
    if changed:
        run(["git", "commit", "-m", commit_message], cwd=repo_dir)
    return changed


def stage_and_commit(repo_dir: Path, commit_message: str, *, initialize: bool) -> bool:
    stage_export(repo_dir, initialize=initialize)
    return commit_staged(repo_dir, commit_message)


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
        help="Do not initialize, stage, or commit the exported output.",
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
        if (output_dir / ".git").is_dir():
            dirty = git_status(output_dir)
            if dirty:
                raise SystemExit(
                    f"Public target has uncommitted changes and will not be replaced: {output_dir}\n{dirty}"
                )
            source_name = remote_repo_name(repo_dir) or repo_dir.name
            expected_name = source_name[:-4] if source_name.endswith("-ops") else source_name
            target_name = remote_repo_name(output_dir)
            if target_name and target_name != expected_name:
                raise SystemExit(
                    f"Refusing to replace unexpected public remote {target_name!r}; expected {expected_name!r}"
                )
            clear_worktree_preserving_git(output_dir)
            existing_git = True
        else:
            shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            existing_git = False
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        existing_git = False

    export_head(repo_dir, output_dir)

    matches = scan_forbidden_patterns(output_dir, load_patterns(patterns_file))
    if matches:
        raise SystemExit(
            "Forbidden patterns found in exported repo:\n" + "\n".join(matches)
        )

    if not args.no_git_init:
        stage_export(output_dir, initialize=not existing_git)

    safety_script = output_dir / "scripts" / "check_public_safety.py"
    if not safety_script.is_file():
        raise SystemExit(f"Export is missing the required public-safety scanner: {safety_script}")
    run([sys.executable, str(safety_script)], cwd=output_dir)

    if not args.no_git_init:
        changed = commit_staged(output_dir, args.commit_message)
    else:
        changed = False

    print(f"Exported public repo to {output_dir}")
    if patterns_file is not None and patterns_file.exists():
        print(f"Verified against forbidden-pattern file: {patterns_file}")
    else:
        print("No forbidden-pattern file was used.")
    if not args.no_git_init:
        print("Committed exported changes." if changed else "Public export already matched the current HEAD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
