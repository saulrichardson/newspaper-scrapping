"""AppleScript helpers for real Google Chrome control on macOS."""

from __future__ import annotations

import json
import subprocess


def run_osascript(lines: list[str]) -> str:
    args: list[str] = ["osascript"]
    for line in lines:
        args.extend(["-e", line])
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(stderr or "osascript failed")
    return result.stdout.strip()


def activate_app(app_name: str) -> None:
    run_osascript([f'tell application "{app_name}" to activate'])


def navigate_front_tab(app_name: str, target_url: str) -> None:
    run_osascript(
        [
            f'tell application "{app_name}" to activate',
            f'''
tell application "{app_name}"
  if (count of windows) = 0 then
    make new window
  end if
  set URL of active tab of front window to "{target_url}"
end tell
''',
        ]
    )


def execute_front_tab_javascript(app_name: str, expression: str) -> str:
    return run_osascript(
        [
            f'''
tell application "{app_name}"
  return execute active tab of front window javascript {json.dumps(expression)}
end tell
'''
        ]
    )
