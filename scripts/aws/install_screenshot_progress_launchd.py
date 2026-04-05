#!/usr/bin/env python3
"""Install or manage the local launchd job for screenshot progress digests."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_LABEL = "com.saulrichardson.newscom.screenshot-progress"


def build_program_arguments(
    *,
    python_bin: str,
    repo_root: Path,
    bucket: str,
    prefix: str,
    prefix_auto: str,
    output_dir: Path,
    sns_topic_arn: str,
    ssh_key: Path | None,
    ssh_user: str,
    public_preview_bucket: str,
    interval_seconds: float,
) -> list[str]:
    output_dir = output_dir.resolve()
    args = [
        python_bin,
        str(repo_root / "scripts" / "aws" / "watch_screenshot_progress.py"),
        "--bucket",
        bucket,
        "--output-dir",
        str(output_dir),
        "--interval-seconds",
        str(interval_seconds),
    ]
    if prefix_auto:
        args.extend(["--prefix-auto", prefix_auto])
    else:
        args.extend(["--prefix", prefix])
    if sns_topic_arn:
        args.extend(["--sns-topic-arn", sns_topic_arn])
    if ssh_key is not None:
        args.extend(["--ssh-key", str(ssh_key), "--ssh-user", ssh_user])
        if public_preview_bucket:
            args.extend(["--public-preview-bucket", public_preview_bucket])
    return args


def build_launchd_payload(
    *,
    label: str,
    program_arguments: list[str],
    repo_root: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    stdout_path = stdout_path.resolve()
    stderr_path = stderr_path.resolve()
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {
            "PYTHONPATH": str(repo_root / "src"),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "ProcessType": "Background",
    }


def plist_path_for_label(label: str, home_dir: Path) -> Path:
    return home_dir / "Library" / "LaunchAgents" / f"{label}.plist"


def launchctl_domain(uid: int) -> str:
    return f"gui/{uid}"


def run_launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def install_job(*, plist_path: Path, payload: dict[str, Any], uid: int) -> None:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = Path(payload["StandardOutPath"])
    stderr_path = Path(payload["StandardErrorPath"])
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(payload))

    domain = launchctl_domain(uid)
    run_launchctl("bootout", domain, str(plist_path), check=False)
    run_launchctl("bootstrap", domain, str(plist_path), check=True)
    run_launchctl("kickstart", "-k", f"{domain}/{payload['Label']}", check=False)


def uninstall_job(*, plist_path: Path, label: str, uid: int, remove_file: bool) -> None:
    domain = launchctl_domain(uid)
    run_launchctl("bootout", domain, str(plist_path), check=False)
    run_launchctl("remove", f"{domain}/{label}", check=False)
    if remove_file and plist_path.exists():
        plist_path.unlink()


def status_job(*, label: str, uid: int) -> subprocess.CompletedProcess[str]:
    domain = launchctl_domain(uid)
    return run_launchctl("print", f"{domain}/{label}", check=False)


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    default_output = repo_root / "output" / "progress_watch"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("install", "uninstall", "status", "print-plist"), default="install")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--uid", type=int, default=os.getuid())
    parser.add_argument("--python-bin", default=sys.executable or "python3")
    parser.add_argument("--bucket", default="")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--prefix-auto", default="")
    parser.add_argument("--output-dir", type=Path, default=default_output)
    parser.add_argument("--sns-topic-arn", default="")
    parser.add_argument("--ssh-key", type=Path, default=None)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--public-preview-bucket", default="")
    parser.add_argument("--interval-seconds", type=float, default=3600.0)
    parser.add_argument("--stdout-path", type=Path)
    parser.add_argument("--stderr-path", type=Path)
    parser.add_argument("--home-dir", type=Path, default=Path.home())
    parser.add_argument("--keep-plist", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.action not in {"install", "print-plist"}:
        return
    missing: list[str] = []
    if not args.bucket:
        missing.append("--bucket")
    if not args.prefix and not args.prefix_auto:
        missing.append("--prefix/--prefix-auto")
    if not args.sns_topic_arn:
        missing.append("--sns-topic-arn")
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"missing required arguments for {args.action}: {joined}")
    if args.public_preview_bucket and args.ssh_key is None:
        raise SystemExit("--public-preview-bucket requires --ssh-key")


def main() -> None:
    args = parse_args()
    validate_args(args)
    repo_root = Path(__file__).resolve().parents[2]
    stdout_path = args.stdout_path or (args.output_dir / "launchd.stdout.log")
    stderr_path = args.stderr_path or (args.output_dir / "launchd.stderr.log")
    program_arguments = build_program_arguments(
        python_bin=args.python_bin,
        repo_root=repo_root,
        bucket=args.bucket,
        prefix=args.prefix,
        prefix_auto=args.prefix_auto,
        output_dir=args.output_dir,
        sns_topic_arn=args.sns_topic_arn,
        ssh_key=args.ssh_key,
        ssh_user=args.ssh_user,
        public_preview_bucket=args.public_preview_bucket,
        interval_seconds=args.interval_seconds,
    )
    payload = build_launchd_payload(
        label=args.label,
        program_arguments=program_arguments,
        repo_root=repo_root,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )
    plist_path = plist_path_for_label(args.label, args.home_dir)

    if args.action == "print-plist":
        print(plistlib.dumps(payload).decode())
        return
    if args.action == "install":
        install_job(plist_path=plist_path, payload=payload, uid=args.uid)
        print(plist_path)
        return
    if args.action == "uninstall":
        uninstall_job(
            plist_path=plist_path,
            label=args.label,
            uid=args.uid,
            remove_file=not args.keep_plist,
        )
        print(plist_path)
        return
    status = status_job(label=args.label, uid=args.uid)
    if status.stdout:
        print(status.stdout)
    if status.stderr:
        print(status.stderr)
    raise SystemExit(status.returncode)


if __name__ == "__main__":
    main()
