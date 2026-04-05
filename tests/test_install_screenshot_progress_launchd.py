from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_launchd_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "aws"
        / "install_screenshot_progress_launchd.py"
    )
    spec = importlib.util.spec_from_file_location("install_screenshot_progress_launchd", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_program_arguments_includes_required_flags() -> None:
    module = _load_launchd_module()
    repo_root = Path("/tmp/repo")
    output_dir = repo_root / "output" / "progress_watch"
    args = module.build_program_arguments(
        python_bin="/tmp/python3",
        repo_root=repo_root,
        bucket="bucket-a",
        prefix="results/prefix-a",
        prefix_auto="",
        output_dir=output_dir,
        sns_topic_arn="arn:aws:sns:us-west-2:123:topic-a",
        ssh_key=Path("/tmp/key.pem"),
        ssh_user="ubuntu",
        public_preview_bucket="preview-bucket-a",
        interval_seconds=900.0,
    )

    assert args[0] == "/tmp/python3"
    assert str(repo_root / "scripts" / "aws" / "watch_screenshot_progress.py") in args
    assert "--bucket" in args and "bucket-a" in args
    assert "--prefix" in args and "results/prefix-a" in args
    assert "--public-preview-bucket" in args and "preview-bucket-a" in args
    assert "--interval-seconds" in args and "900.0" in args


def test_build_program_arguments_omits_optional_flags_when_not_configured() -> None:
    module = _load_launchd_module()
    repo_root = Path("/tmp/repo")
    output_dir = repo_root / "output" / "progress_watch"
    args = module.build_program_arguments(
        python_bin="/tmp/python3",
        repo_root=repo_root,
        bucket="bucket-a",
        prefix="results/prefix-a",
        prefix_auto="",
        output_dir=output_dir,
        sns_topic_arn="",
        ssh_key=None,
        ssh_user="ubuntu",
        public_preview_bucket="",
        interval_seconds=900.0,
    )

    assert "--sns-topic-arn" not in args
    assert "--ssh-key" not in args
    assert "--public-preview-bucket" not in args


def test_build_program_arguments_supports_prefix_auto() -> None:
    module = _load_launchd_module()
    repo_root = Path("/tmp/repo")
    output_dir = repo_root / "output" / "progress_watch"
    args = module.build_program_arguments(
        python_bin="/tmp/python3",
        repo_root=repo_root,
        bucket="bucket-a",
        prefix="",
        prefix_auto="latest-active-screenshot",
        output_dir=output_dir,
        sns_topic_arn="arn:aws:sns:us-west-2:123:topic-a",
        ssh_key=None,
        ssh_user="ubuntu",
        public_preview_bucket="",
        interval_seconds=900.0,
    )

    assert "--prefix-auto" in args and "latest-active-screenshot" in args
    assert "--prefix" not in args


def test_build_launchd_payload_sets_keepalive_and_paths() -> None:
    module = _load_launchd_module()
    repo_root = Path("/tmp/repo")
    stdout_path = repo_root / "output" / "stdout.log"
    stderr_path = repo_root / "output" / "stderr.log"
    payload = module.build_launchd_payload(
        label="com.example.test",
        program_arguments=["/tmp/python3", "/tmp/watcher.py"],
        repo_root=repo_root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )

    assert payload["Label"] == "com.example.test"
    assert payload["ProgramArguments"] == ["/tmp/python3", "/tmp/watcher.py"]
    assert payload["WorkingDirectory"] == str(repo_root.resolve())
    assert payload["EnvironmentVariables"]["PYTHONPATH"] == str((repo_root / "src").resolve())
    assert payload["KeepAlive"] is True
    assert payload["RunAtLoad"] is True
    assert payload["StandardOutPath"] == str(stdout_path.resolve())
    assert payload["StandardErrorPath"] == str(stderr_path.resolve())


def test_validate_args_requires_explicit_install_configuration() -> None:
    module = _load_launchd_module()
    args = module.argparse.Namespace(
        action="install",
        bucket="",
        prefix="",
        prefix_auto="",
        sns_topic_arn="",
        public_preview_bucket="",
        ssh_key=None,
    )

    try:
        module.validate_args(args)
    except SystemExit as exc:
        assert "--bucket" in str(exc)
        assert "--prefix/--prefix-auto" in str(exc)
        assert "--sns-topic-arn" in str(exc)
    else:
        raise AssertionError("validate_args should reject missing install arguments")


def test_validate_args_rejects_preview_bucket_without_ssh_key() -> None:
    module = _load_launchd_module()
    args = module.argparse.Namespace(
        action="install",
        bucket="bucket-a",
        prefix="results/prefix-a",
        prefix_auto="",
        sns_topic_arn="arn:aws:sns:us-west-2:123:topic-a",
        public_preview_bucket="preview-bucket-a",
        ssh_key=None,
    )

    try:
        module.validate_args(args)
    except SystemExit as exc:
        assert "--public-preview-bucket requires --ssh-key" in str(exc)
    else:
        raise AssertionError("validate_args should require an SSH key for public previews")
