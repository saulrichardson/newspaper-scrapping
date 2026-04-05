#!/usr/bin/env python3
"""Render EC2 user-data for a Newspapers.com worker instance."""

from __future__ import annotations

import argparse
from pathlib import Path


def build_user_data(
    *,
    bucket: str,
    bundle_key: str,
    plan_key: str,
    plan_prefix: str,
    cookies_key: str,
    output_prefix: str,
    bootstrap_key: str,
    sync_minutes: int,
    retry_cooldown_seconds: int,
    poll_seconds: int,
    worker_mode: str,
    worker_stagger_seconds: float,
    max_worker_attempts: int,
    enable_dcv: bool,
    dcv_session_id: str,
    dcv_session_owner: str,
    dcv_password: str,
    dcv_port: int,
    dcv_bundle_url: str,
    run_volume_id: str,
    run_volume_device: str,
    run_volume_label: str,
    run_volume_fstype: str,
    run_volume_wait_seconds: int,
) -> str:
    return f"""#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl unzip
tmp_dir="$(mktemp -d)"
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "${{tmp_dir}}/awscliv2.zip"
unzip -q "${{tmp_dir}}/awscliv2.zip" -d "${{tmp_dir}}"
"${{tmp_dir}}/aws/install" --update
rm -rf "${{tmp_dir}}"
export NEWSCOM_BUCKET='{bucket}'
export NEWSCOM_BUNDLE_KEY='{bundle_key}'
export NEWSCOM_PLAN_KEY='{plan_key}'
export NEWSCOM_PLAN_PREFIX='{plan_prefix}'
export NEWSCOM_COOKIES_KEY='{cookies_key}'
export NEWSCOM_OUTPUT_PREFIX='{output_prefix}'
export SYNC_MINUTES='{sync_minutes}'
export RETRY_COOLDOWN_SECONDS='{retry_cooldown_seconds}'
export POLL_SECONDS='{poll_seconds}'
export NEWSCOM_WORKER_MODE='{worker_mode}'
export WORKER_STAGGER_SECONDS='{worker_stagger_seconds}'
export MAX_WORKER_ATTEMPTS='{max_worker_attempts}'
export NEWSCOM_ENABLE_DCV='{"true" if enable_dcv else "false"}'
export NEWSCOM_DCV_SESSION_ID='{dcv_session_id}'
export NEWSCOM_DCV_SESSION_OWNER='{dcv_session_owner}'
export NEWSCOM_DCV_PASSWORD='{dcv_password}'
export NEWSCOM_DCV_PORT='{dcv_port}'
export NEWSCOM_DCV_BUNDLE_URL='{dcv_bundle_url}'
export NEWSCOM_RUN_VOLUME_ID='{run_volume_id}'
export NEWSCOM_RUN_VOLUME_DEVICE='{run_volume_device}'
export NEWSCOM_RUN_VOLUME_LABEL='{run_volume_label}'
export NEWSCOM_RUN_VOLUME_FSTYPE='{run_volume_fstype}'
export NEWSCOM_RUN_VOLUME_WAIT_SECONDS='{run_volume_wait_seconds}'
aws s3 cp s3://{bucket}/{bootstrap_key} /root/bootstrap_newscom_worker.sh
chmod +x /root/bootstrap_newscom_worker.sh
/root/bootstrap_newscom_worker.sh
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--bundle-key", required=True)
    parser.add_argument("--plan-key", default="")
    parser.add_argument("--plan-prefix", default="")
    parser.add_argument("--cookies-key", default="")
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument(
        "--bootstrap-key",
        default="bootstrap/bootstrap_newscom_worker.sh",
        help="S3 key for the bootstrap shell script.",
    )
    parser.add_argument("--sync-minutes", type=int, default=5)
    parser.add_argument("--retry-cooldown-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--worker-stagger-seconds", type=float, default=120.0)
    parser.add_argument("--max-worker-attempts", type=int, default=100)
    parser.add_argument(
        "--worker-mode",
        default="search",
        choices=["search", "screenshot"],
    )
    parser.add_argument("--enable-dcv", action="store_true")
    parser.add_argument("--dcv-session-id", default="newscom")
    parser.add_argument("--dcv-session-owner", default="ubuntu")
    parser.add_argument("--dcv-password", default="")
    parser.add_argument("--dcv-port", type=int, default=8443)
    parser.add_argument(
        "--dcv-bundle-url",
        default="https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-ubuntu2404-x86_64.tgz",
    )
    parser.add_argument("--run-volume-id", default="")
    parser.add_argument("--run-volume-device", default="")
    parser.add_argument("--run-volume-label", default="NEWSCOM_RUN")
    parser.add_argument("--run-volume-fstype", default="ext4")
    parser.add_argument("--run-volume-wait-seconds", type=int, default=120)
    parser.add_argument("--output-path", required=True, type=Path)
    args = parser.parse_args()
    if not args.plan_key and not args.plan_prefix:
        raise SystemExit("one of --plan-key or --plan-prefix is required")

    user_data = build_user_data(
        bucket=args.bucket,
        bundle_key=args.bundle_key,
        plan_key=args.plan_key,
        plan_prefix=args.plan_prefix,
        cookies_key=args.cookies_key,
        output_prefix=args.output_prefix,
        bootstrap_key=args.bootstrap_key,
        sync_minutes=args.sync_minutes,
        retry_cooldown_seconds=args.retry_cooldown_seconds,
        poll_seconds=args.poll_seconds,
        worker_mode=args.worker_mode,
        worker_stagger_seconds=args.worker_stagger_seconds,
        max_worker_attempts=args.max_worker_attempts,
        enable_dcv=args.enable_dcv,
        dcv_session_id=args.dcv_session_id,
        dcv_session_owner=args.dcv_session_owner,
        dcv_password=args.dcv_password,
        dcv_port=args.dcv_port,
        dcv_bundle_url=args.dcv_bundle_url,
        run_volume_id=args.run_volume_id,
        run_volume_device=args.run_volume_device,
        run_volume_label=args.run_volume_label,
        run_volume_fstype=args.run_volume_fstype,
        run_volume_wait_seconds=args.run_volume_wait_seconds,
    )
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    args.output_path.write_text(user_data)
    args.output_path.chmod(0o755)


if __name__ == "__main__":
    main()
