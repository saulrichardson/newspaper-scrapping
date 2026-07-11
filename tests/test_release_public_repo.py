from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "release_public_repo.py"
SPEC = importlib.util.spec_from_file_location("release_public_repo", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")


def test_export_head_ignores_untracked_private_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / ".gitignore").write_text("docs/private/\n", encoding="utf-8")
    (repo / "README.md").write_text("public\n", encoding="utf-8")
    (repo / "docs" / "private").mkdir(parents=True)
    (repo / "docs" / "private" / "secret.local.md").write_text("secret\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "README.md")
    _git(repo, "commit", "-m", "init")

    output_dir = tmp_path / "public"
    output_dir.mkdir()
    MODULE.export_head(repo, output_dir)

    assert (output_dir / "README.md").read_text(encoding="utf-8") == "public\n"
    assert not (output_dir / "docs" / "private" / "secret.local.md").exists()


def test_scan_forbidden_patterns_reports_match(tmp_path: Path) -> None:
    root = tmp_path / "export"
    root.mkdir()
    (root / "README.md").write_text("account 123456789012\n", encoding="utf-8")

    matches = MODULE.scan_forbidden_patterns(root, [r"123456789012"])

    assert matches == ["README.md:1: 123456789012"]


def test_main_refuses_existing_output_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "README.md").write_text("public\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    output_dir = tmp_path / "public"
    output_dir.mkdir()

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release_public_repo.py",
            "--output-dir",
            str(output_dir),
            "--no-git-init",
        ],
    )

    with pytest.raises(SystemExit, match="Output directory already exists"):
        MODULE.main()


def test_refresh_preserves_git_history_and_commits_export(tmp_path: Path) -> None:
    target = tmp_path / "public"
    target.mkdir()
    _git(target, "init", "-b", "main")
    _git(target, "config", "user.name", "Test User")
    _git(target, "config", "user.email", "test@example.com")
    (target / "old.txt").write_text("old\n", encoding="utf-8")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "old snapshot")
    old_head = _git(target, "rev-parse", "HEAD")

    MODULE.clear_worktree_preserving_git(target)
    (target / "new.txt").write_text("new\n", encoding="utf-8")
    changed = MODULE.stage_and_commit(target, "new snapshot", initialize=False)

    assert changed is True
    assert (target / ".git").is_dir()
    assert not (target / "old.txt").exists()
    assert (target / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert _git(target, "rev-parse", "HEAD") != old_head
    assert _git(target, "log", "--format=%s", "-2").splitlines() == [
        "new snapshot",
        "old snapshot",
    ]


def test_stage_and_commit_is_noop_for_unchanged_tree(tmp_path: Path) -> None:
    target = tmp_path / "public"
    target.mkdir()
    _git(target, "init", "-b", "main")
    _git(target, "config", "user.name", "Test User")
    _git(target, "config", "user.email", "test@example.com")
    (target / "file.txt").write_text("same\n", encoding="utf-8")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "snapshot")

    changed = MODULE.stage_and_commit(target, "duplicate", initialize=False)

    assert changed is False
    assert _git(target, "rev-list", "--count", "HEAD") == "1"


def test_stage_export_includes_snapshot_files_ignored_by_target_rules(tmp_path: Path) -> None:
    target = tmp_path / "public"
    target.mkdir()
    _init_repo(target)
    (target / ".gitignore").write_text("*.fixture\n", encoding="utf-8")
    (target / "contract.fixture").write_text("tracked source fixture\n", encoding="utf-8")

    MODULE.stage_export(target, initialize=False)

    assert _git(target, "ls-files", "contract.fixture") == "contract.fixture"
