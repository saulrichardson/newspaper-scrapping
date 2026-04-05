"""Remote torch environment checks for HPC deployment planning."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any


def _run_ssh(host: str, remote_command: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["ssh", host, remote_command],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def torch_check(host: str = "torch") -> dict[str, Any]:
    checks = {
        "hostname": "hostname",
        "python": "python3 --version || python --version",
        "apptainer": "command -v apptainer || command -v singularity || echo missing",
        "xvfb_host": "command -v Xvfb || command -v xvfb-run || echo missing",
        "shared_browser_images": (
            "find /share/apps/images -maxdepth 1 "
            "\\( -iname '*chrome*' -o -iname '*browser*' -o -iname '*playwright*' \\) "
            "2>/dev/null | sed -n '1,40p'"
        ),
    }

    results: dict[str, Any] = {"host": host, "checks": {}}
    for name, remote_command in checks.items():
        code, stdout, stderr = _run_ssh(host, remote_command)
        results["checks"][name] = {
            "exit_code": code,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        }

    shared_images = str(results["checks"]["shared_browser_images"]["stdout"]).splitlines()
    results["summary"] = {
        "ssh_ok": results["checks"]["hostname"]["exit_code"] == 0,
        "apptainer_ok": "missing" not in str(results["checks"]["apptainer"]["stdout"]),
        "xvfb_on_host": "missing" not in str(results["checks"]["xvfb_host"]["stdout"]),
        "shared_browser_image_count": len([line for line in shared_images if line.strip()]),
    }
    return results
