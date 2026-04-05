#!/usr/bin/env python3
"""Poll a remote screenshot worker host and resume the worker after auth succeeds."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def run_ssh(host: str, key_path: Path, remote_script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh",
            "-i",
            str(key_path),
            "-o",
            "StrictHostKeyChecking=no",
            f"ubuntu@{host}",
            remote_script,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def remote_auth_status_script(debug_port: int, profile_dir: str) -> str:
    return f"""
set -e
export HOME=/home/ubuntu
export PYTHONPATH=/opt/newscom/app/src
export NEWSCOM_CHROME_APP_NAME="Google Chrome"
export NEWSCOM_CHROME_APP_PATH=/usr/bin/google-chrome
export NEWSCOM_CHROME_DEBUG_PORT={debug_port}
export NEWSCOM_CHROME_PROFILE_DIR={json.dumps(profile_dir)}
export NEWSCOM_DATA_DIR=/opt/newscom/data
export NEWSCOM_BROWSER_START_TIMEOUT_SECONDS=60
export DISPLAY=:0
export XAUTHORITY=/run/user/1000/dcv/newscom-shot-01.xauth
cd /opt/newscom/app
/opt/newscom/venv/bin/python -m newspaper_scrapper.cli.main auth-status
""".strip()


def remote_restart_script() -> str:
    return "sudo systemctl restart newscom-worker.service && sudo systemctl status newscom-worker.service --no-pager -l | sed -n '1,40p'"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--ssh-key", type=Path, required=True)
    parser.add_argument("--debug-port", type=int, default=9701)
    parser.add_argument(
        "--profile-dir",
        default="/opt/newscom/data/chrome_profiles/worker_01",
    )
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--log-path", type=Path, default=None)
    args = parser.parse_args()

    deadline = time.time() + args.timeout_seconds
    log_lines: list[str] = []

    while time.time() < deadline:
        result = run_ssh(
            args.host,
            args.ssh_key,
            remote_auth_status_script(args.debug_port, args.profile_dir),
        )
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload = {}
            signed_in = payload.get("signed_in") == "true"
            url = payload.get("url", "")
            title = payload.get("title", "")
            log_lines.append(
                json.dumps(
                    {
                        "ts": time.time(),
                        "signed_in": signed_in,
                        "url": url,
                        "title": title,
                    }
                )
            )
            if signed_in:
                restart = run_ssh(args.host, args.ssh_key, remote_restart_script())
                log_lines.append(
                    json.dumps(
                        {
                            "ts": time.time(),
                            "action": "restart_worker",
                            "returncode": restart.returncode,
                            "stdout": restart.stdout[-2000:],
                            "stderr": restart.stderr[-2000:],
                        }
                    )
                )
                if args.log_path:
                    args.log_path.parent.mkdir(parents=True, exist_ok=True)
                    args.log_path.write_text("\n".join(log_lines) + "\n")
                print("signed_in")
                print(restart.stdout.strip())
                return 0 if restart.returncode == 0 else 1
        else:
            log_lines.append(
                json.dumps(
                    {
                        "ts": time.time(),
                        "action": "auth_status_error",
                        "returncode": result.returncode,
                        "stderr": result.stderr[-2000:],
                    }
                )
            )
        time.sleep(args.poll_seconds)

    if args.log_path:
        args.log_path.parent.mkdir(parents=True, exist_ok=True)
        args.log_path.write_text("\n".join(log_lines) + "\n")
    print("timeout_waiting_for_signed_in", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
