#!/usr/bin/env python3
"""Watch AWS screenshot-worker state and open DCV when human action is needed."""

from __future__ import annotations

import argparse
import html
import json
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from newspaper_scrapper.application.screenshot_operator import (
    extract_screenshot_operator_alerts,
    load_operator_alert_state,
    select_new_operator_alerts,
)
from newspaper_scrapper.application.watch_prefix import (
    PREFIX_AUTO_LATEST_ACTIVE_SCREENSHOT,
    local_sync_root,
    resolve_watch_prefix,
)


def sync_s3_metadata(bucket: str, prefix: str, output_dir: Path) -> Path:
    local_root = local_sync_root(output_dir, prefix)
    local_root.mkdir(parents=True, exist_ok=True)
    command = [
        "aws",
        "s3",
        "sync",
        f"s3://{bucket}/{prefix}",
        str(local_root),
        "--exact-timestamps",
        "--exclude",
        "*.png",
        "--exclude",
        "*.jpg",
        "--exclude",
        "*.jpeg",
    ]
    subprocess.run(command, check=True)
    return local_root


def notify_macos(*, title: str, subtitle: str, message: str) -> None:
    if platform.system() != "Darwin":
        return
    title = title.replace('"', '\\"')
    subtitle = subtitle.replace('"', '\\"')
    message = message.replace('"', '\\"')
    script = (
        f'display notification "{message}" '
        f'with title "{title}" subtitle "{subtitle}"'
    )
    subprocess.run(["osascript", "-e", script], check=False)


def open_url(url: str) -> None:
    if not url:
        return
    if platform.system() == "Darwin":
        subprocess.run(["open", url], check=False)
    else:
        subprocess.run(["xdg-open", url], check=False)


def publish_sns_email(*, topic_arn: str, subject: str, message: str) -> None:
    if not topic_arn:
        return
    subprocess.run(
        [
            "aws",
            "sns",
            "publish",
            "--topic-arn",
            topic_arn,
            "--subject",
            subject[:100],
            "--message",
            message,
        ],
        check=False,
    )


def _load_worker_plan_row(
    *,
    s3_root: Path,
    instance_id: str,
    worker_name: str,
) -> dict[str, str] | None:
    plan_path = s3_root / instance_id / "state" / "worker_plan.csv"
    if not plan_path.exists():
        return None
    import csv

    with plan_path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("worker_name", "") or "") == worker_name:
                return {str(k): str(v or "") for k, v in row.items()}
    return None


def _run_ssh(host: str, ssh_key: Path, ssh_user: str, remote_script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh",
            "-i",
            str(ssh_key),
            "-o",
            "StrictHostKeyChecking=no",
            f"{ssh_user}@{host}",
            remote_script,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _presign_s3_url(bucket: str, key: str, *, expires_in: int = 86400) -> str:
    presign = subprocess.run(
        [
            "aws",
            "s3",
            "presign",
            f"s3://{bucket}/{key}",
            "--expires-in",
            str(expires_in),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    return presign.stdout.strip() if presign.returncode == 0 else ""


def capture_remote_stage_preview(
    *,
    alert: dict[str, Any],
    s3_root: Path,
    bucket: str,
    prefix: str,
    ssh_key: Path,
    ssh_user: str,
) -> dict[str, str] | None:
    worker_name = str(alert.get("worker_name", "") or "")
    instance_id = str(alert.get("instance_id", "") or "")
    public_ip = str(alert.get("public_ip", "") or "")
    if not worker_name or not instance_id or not public_ip:
        return None

    plan_row = _load_worker_plan_row(s3_root=s3_root, instance_id=instance_id, worker_name=worker_name)
    if not plan_row:
        return None

    debug_port = plan_row.get("chrome_debug_port", "").strip() or "9223"
    base_key = (
        f"{prefix.rstrip('/')}/{instance_id}/state/operator_previews/"
        f"{worker_name}_{int(time.time())}"
    )
    full_s3_key = f"{base_key}.png"
    thumb_s3_key = f"{base_key}.jpg"

    remote_script = f"""
set -euo pipefail
export HOME=/home/{ssh_user}
export PYTHONPATH=/opt/newscom/app/src
tmp_png="$(mktemp /tmp/newscom-operator-preview-XXXXXX.png)"
tmp_jpg="$(mktemp /tmp/newscom-operator-preview-XXXXXX.jpg)"
/opt/newscom/venv/bin/python - <<'PY'
from newspaper_scrapper.adapters.chrome import cdp
import json
import os
import sys
from PIL import Image

debug_base = "http://127.0.0.1:{debug_port}"
pages = [
    page for page in cdp.list_page_tabs(debug_base)
    if "newspapers.com" in str(page.get("url", ""))
]
if not pages:
    raise SystemExit("no newspapers.com tabs found")

def score(page):
    url = str(page.get("url", ""))
    title = str(page.get("title", ""))
    value = 0
    if "/signin" in url or "/account/" in url:
        value -= 5
    if "cloudflare" in title.lower():
        value += 20
    if "image/" in url:
        value += 10
    return value

page = sorted(pages, key=score, reverse=True)[0]
ws_url = str(page["webSocketDebuggerUrl"])
result = cdp.capture_screenshot(ws_url, output_path=os.environ["TMP_PREVIEW_PNG"])
image = Image.open(os.environ["TMP_PREVIEW_PNG"]).convert("RGB")
image.thumbnail((1400, 1400))
image.save(
    os.environ["TMP_PREVIEW_JPG"],
    format="JPEG",
    quality=60,
    optimize=True,
    progressive=True,
)
payload = {{
    "page_url": str(page.get("url", "")),
    "title": str(page.get("title", "")),
    "byte_count": int(result.get("byte_count", 0) or 0),
    "thumbnail_bytes": int(os.path.getsize(os.environ["TMP_PREVIEW_JPG"])),
}}
print(json.dumps(payload))
PY
aws s3 cp "$tmp_png" "s3://{bucket}/{full_s3_key}" --content-type image/png >/dev/null
aws s3 cp "$tmp_jpg" "s3://{bucket}/{thumb_s3_key}" --content-type image/jpeg >/dev/null
rm -f "$tmp_png" "$tmp_jpg"
""".strip()

    env_prefix = "TMP_PREVIEW_PNG=$tmp_png TMP_PREVIEW_JPG=$tmp_jpg "
    remote_script = remote_script.replace(
        "/opt/newscom/venv/bin/python - <<'PY'",
        f"{env_prefix}/opt/newscom/venv/bin/python - <<'PY'",
        1,
    )
    result = _run_ssh(public_ip, ssh_key, ssh_user, remote_script)
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout.strip().splitlines()[0])
    except Exception:
        return None

    preview_url = _presign_s3_url(bucket, thumb_s3_key)
    preview_full_url = _presign_s3_url(bucket, full_s3_key)
    return {
        "preview_s3_key": thumb_s3_key,
        "preview_full_s3_key": full_s3_key,
        "preview_url": preview_url,
        "preview_full_url": preview_full_url,
        "preview_page_url": str(payload.get("page_url", "") or ""),
        "preview_title": str(payload.get("title", "") or ""),
        "preview_thumbnail_bytes": str(payload.get("thumbnail_bytes", "") or ""),
    }


def send_ses_html_email(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> None:
    if not from_email or not to_email:
        return
    payload = {
        "FromEmailAddress": from_email,
        "Destination": {"ToAddresses": [to_email]},
        "Content": {
            "Simple": {
                "Subject": {"Data": subject[:100]},
                "Body": {
                    "Text": {"Data": text_body},
                    "Html": {"Data": html_body},
                },
            }
        },
    }
    subprocess.run(
        [
            "aws",
            "sesv2",
            "send-email",
            "--cli-input-json",
            json.dumps(payload),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--local-s3-root", type=Path)
    source.add_argument("--bucket")
    parser.add_argument("--prefix", help="S3 prefix to watch when using --bucket")
    parser.add_argument(
        "--prefix-auto",
        choices=(PREFIX_AUTO_LATEST_ACTIVE_SCREENSHOT,),
        default="",
        help="Resolve the current screenshot run prefix automatically when using --bucket.",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--sns-topic-arn", default="")
    parser.add_argument("--ses-from-email", default="")
    parser.add_argument("--ses-to-email", default="")
    parser.add_argument("--ssh-key", type=Path, default=None)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--capture-preview", action="store_true")
    args = parser.parse_args()

    if args.bucket and not args.prefix and not args.prefix_auto:
        parser.error("Either --prefix or --prefix-auto is required with --bucket")

    state_path = args.output_dir / "operator_alert_state.json"
    current_alerts_path = args.output_dir / "current_operator_alerts.json"
    last_resolved_prefix = ""

    while True:
        try:
            if args.local_s3_root is not None:
                s3_root = args.local_s3_root
                resolved_prefix = args.prefix or ""
            else:
                resolved_prefix = resolve_watch_prefix(
                    bucket=args.bucket,
                    prefix=args.prefix,
                    prefix_auto=args.prefix_auto,
                )
                if resolved_prefix != last_resolved_prefix:
                    print(
                        json.dumps(
                            {
                                "event": "watch_prefix_resolved",
                                "prefix": resolved_prefix,
                            },
                            sort_keys=True,
                        )
                    )
                    last_resolved_prefix = resolved_prefix
                s3_root = sync_s3_metadata(args.bucket, resolved_prefix, args.output_dir)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "watch_sync_error",
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                        "prefix": last_resolved_prefix or args.prefix or "",
                    },
                    sort_keys=True,
                )
            )
            if args.once:
                raise
            time.sleep(args.interval_seconds)
            continue

        alerts = extract_screenshot_operator_alerts(s3_root)
        current_alerts_path.parent.mkdir(parents=True, exist_ok=True)
        current_alerts_path.write_text(json.dumps(alerts, indent=2, sort_keys=True))

        state = load_operator_alert_state(state_path)
        new_alerts, updated_state = select_new_operator_alerts(alerts, state)
        state_path.write_text(json.dumps(updated_state, indent=2, sort_keys=True))

        for alert in new_alerts:
            reason = str(alert["stop_reason"])
            public_ip = str(alert["public_ip"])
            worker_name = str(alert["worker_name"])
            session_id = str(alert["session_id"])
            web_url = str(alert["web_url"])
            message = str(alert["stop_message"] or "Operator action required on screenshot worker.")
            preview = None
            if (
                args.capture_preview
                and args.bucket
                and args.ssh_key is not None
            ):
                preview = capture_remote_stage_preview(
                    alert=alert,
                    s3_root=s3_root,
                    bucket=args.bucket,
                    prefix=resolved_prefix,
                    ssh_key=args.ssh_key,
                    ssh_user=args.ssh_user,
                )
                if preview:
                    alert.update(preview)
            subtitle = f"{reason} on {public_ip or worker_name}"
            email_lines = [
                "Newscom screenshot worker needs human action.",
                f"Reason: {reason}",
                f"Worker: {worker_name}",
                f"Public IP: {public_ip}",
                f"Session: {session_id}",
                f"DCV URL: {web_url}",
                f"Message: {message}",
            ]
            if preview:
                email_lines.extend(
                    [
                        f"Preview URL: {preview.get('preview_url', '')}",
                        f"Full preview URL: {preview.get('preview_full_url', '')}",
                        f"Preview page: {preview.get('preview_page_url', '')}",
                        f"Preview title: {preview.get('preview_title', '')}",
                    ]
                )
            email_message = "\n".join(
                email_lines
            )
            html_parts = [
                "<p><strong>Newscom screenshot worker needs human action.</strong></p>",
                "<ul>",
                f"<li><strong>Reason:</strong> {html.escape(reason)}</li>",
                f"<li><strong>Worker:</strong> {html.escape(worker_name)}</li>",
                f"<li><strong>Public IP:</strong> {html.escape(public_ip)}</li>",
                f"<li><strong>Session:</strong> {html.escape(session_id)}</li>",
                f'<li><strong>DCV URL:</strong> <a href="{html.escape(web_url, quote=True)}">{html.escape(web_url)}</a></li>',
                f"<li><strong>Message:</strong> {html.escape(message)}</li>",
                "</ul>",
            ]
            if preview and preview.get("preview_url"):
                preview_url_html = html.escape(str(preview["preview_url"]), quote=True)
                preview_full_url_html = html.escape(str(preview.get("preview_full_url", "") or ""), quote=True)
                preview_page_url = html.escape(str(preview.get("preview_page_url", "") or ""), quote=True)
                preview_page_label = html.escape(str(preview.get("preview_page_url", "") or ""))
                html_parts.extend(
                    [
                        "<p><strong>Current stage preview</strong></p>",
                        f'<p><a href="{preview_url_html}">Open fast preview</a></p>',
                        (
                            f'<p><a href="{preview_full_url_html}">Open full-resolution preview</a></p>'
                            if preview_full_url_html
                            else ""
                        ),
                        (
                            f'<p><strong>Preview page:</strong> '
                            f'<a href="{preview_page_url}">{preview_page_label}</a></p>'
                            if preview_page_url
                            else ""
                        ),
                        f'<img src="{preview_url_html}" alt="Current worker stage preview" style="max-width: 100%; height: auto; border: 1px solid #ccc;" />',
                    ]
                )
            html_message = "".join(html_parts)
            if not args.no_notify:
                notify_macos(
                    title="Newscom Screenshot Worker Needs You",
                    subtitle=subtitle,
                    message=message[:240],
                )
            if args.sns_topic_arn:
                publish_sns_email(
                    topic_arn=args.sns_topic_arn,
                    subject=f"Newscom screenshot alert: {subtitle}",
                    message=email_message,
                )
            if args.ses_from_email and args.ses_to_email:
                send_ses_html_email(
                    from_email=args.ses_from_email,
                    to_email=args.ses_to_email,
                    subject=f"Newscom screenshot alert: {subtitle}",
                    text_body=email_message,
                    html_body=html_message,
                )
            if not args.no_open and web_url:
                open_url(web_url)
            print(
                json.dumps(
                    {
                        "alert_key": alert["alert_key"],
                        "worker_name": worker_name,
                        "public_ip": public_ip,
                        "session_id": session_id,
                        "stop_reason": reason,
                        "web_url": web_url,
                    },
                    sort_keys=True,
                )
            )

        if args.once:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
