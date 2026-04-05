from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "release_public_repo.py"
)
SPEC = importlib.util.spec_from_file_location("release_public_repo", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_repo(root: Path) -> None:
    git(["init", "-b", "main"], cwd=root)
    git(["config", "user.name", "Test User"], cwd=root)
    git(["config", "user.email", "test@example.com"], cwd=root)


def test_export_head_ignores_untracked_private_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / ".gitignore").write_text("docs/private/\n", encoding="utf-8")
    (repo / "README.md").write_text("public\n", encoding="utf-8")
    (repo / "docs" / "private").mkdir(parents=True)
    (repo / "docs" / "private" / "secret.local.md").write_text(
        "secret\n",
        encoding="utf-8",
    )
    git(["add", ".gitignore", "README.md"], cwd=repo)
    git(["commit", "-m", "init"], cwd=repo)

    output_dir = tmp_path / "public"
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
    init_repo(repo)
    (repo / "README.md").write_text("public\n", encoding="utf-8")
    git(["add", "README.md"], cwd=repo)
    git(["commit", "-m", "init"], cwd=repo)

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
