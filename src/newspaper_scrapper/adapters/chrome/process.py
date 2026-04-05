"""Launch and reuse a real Google Chrome process with a CDP port."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from newspaper_scrapper.config import Settings


def chrome_binary_path(chrome_app_path: Path) -> Path:
    if chrome_app_path.is_file():
        return chrome_app_path
    candidate = chrome_app_path / "Contents" / "MacOS" / "Google Chrome"
    if candidate.exists():
        return candidate
    raise RuntimeError(f"Could not locate Google Chrome binary under {chrome_app_path}")


def debug_port_ready(debug_base: str) -> bool:
    try:
        with urlopen(f"{debug_base.rstrip('/')}/json/version", timeout=2) as response:
            json.load(response)
        return True
    except (OSError, URLError, ValueError):
        return False


def wait_for_debug_port(debug_base: str, timeout_seconds: float) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if debug_port_ready(debug_base):
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Chrome debug port did not come up at {debug_base} within {timeout_seconds:.1f}s"
    )


def _worker_process_markers(settings: Settings) -> tuple[str, str, str]:
    return (
        f"--remote-debugging-port={settings.chrome_debug_port}",
        f"--user-data-dir={settings.chrome_profile_dir}",
        f"--user-data-dir={settings.chrome_profile_dir.resolve()}",
    )


def list_worker_pids(settings: Settings) -> list[int]:
    completed = subprocess.run(
        ["ps", "ax", "-o", "pid=,command="],
        check=True,
        capture_output=True,
        text=True,
    )
    markers = _worker_process_markers(settings)
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if not pid_text.isdigit():
            continue
        if not any(marker in command for marker in markers):
            continue
        pids.append(int(pid_text))
    return sorted(set(pids))


def terminate_real_chrome(
    settings: Settings,
    *,
    grace_seconds: float = 5.0,
) -> list[int]:
    pids = list_worker_pids(settings)
    if not pids:
        return []

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        remaining = list_worker_pids(settings)
        if not remaining:
            return pids
        time.sleep(0.25)

    for pid in list_worker_pids(settings):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    return pids


def launch_real_chrome(
    settings: Settings,
    *,
    start_url: str | None = None,
    force_new_instance: bool = False,
) -> None:
    settings.chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    if debug_port_ready(settings.chrome_debug_base) and not force_new_instance:
        return
    if force_new_instance:
        terminate_real_chrome(settings)

    chrome_binary = str(chrome_binary_path(settings.chrome_app_path))
    target_url = start_url or settings.home_url
    command = [
        chrome_binary,
        f"--remote-debugging-port={settings.chrome_debug_port}",
        f"--user-data-dir={settings.chrome_profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        target_url,
    ]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    wait_for_debug_port(settings.chrome_debug_base, settings.browser_start_timeout_seconds)
