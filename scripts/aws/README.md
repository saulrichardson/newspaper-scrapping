# `scripts/aws/` Reference

This directory contains the AWS-side operational helpers for the Newspapers.com
scraper.

Use this file as the script catalog. Use the runbooks in `docs/` for the
end-to-end procedures.

## First Scripts To Learn

If you only learn five scripts, learn these:

1. [`build_worker_bundle.py`](build_worker_bundle.py)
2. [`render_user_data.py`](render_user_data.py)
3. [`ensure_worker_access.py`](ensure_worker_access.py)
4. [`watch_screenshot_operator.py`](watch_screenshot_operator.py)
5. [`archive_viewer_pngs.py`](archive_viewer_pngs.py)

## Script Catalog

| Script | What it does | When to use it |
| --- | --- | --- |
| [`archive_viewer_pngs.py`](archive_viewer_pngs.py) | Builds the canonical S3 PNG archive and inventory from worker outputs and optional live hosts. | Before retiring workers, after big runs, whenever you want authoritative PNG counts. |
| [`bootstrap_newscom_worker.sh`](bootstrap_newscom_worker.sh) | First-boot bootstrap fetched by EC2 user-data. Installs dependencies, Chrome, optional DCV, rewrites worker paths, and installs systemd. | Uploaded to S3 and executed automatically by new workers. |
| [`build_worker_bundle.py`](build_worker_bundle.py) | Builds a clean tarball containing the repo logic needed by workers. | Every time you launch or update workers. |
| [`ensure_worker_access.py`](ensure_worker_access.py) | Adds the current public IP to the worker security group for selected ports. | When SSH or DCV is unreachable from a new network. |
| [`install_screenshot_progress_launchd.py`](install_screenshot_progress_launchd.py) | Installs the digest watcher under local macOS `launchd`. | When you want durable digest emails instead of a detached shell watcher. |
| [`pull_and_merge_results.py`](pull_and_merge_results.py) | Pulls a worker results prefix locally and merges the outputs. | When collecting search outputs or other synced worker state locally. |
| [`refresh_png_archive.py`](refresh_png_archive.py) | Builds a local symlink-based archive view under `output/png_archive/`. | Local browsing convenience only. |
| [`render_user_data.py`](render_user_data.py) | Renders the EC2 user-data script that points a worker at the bundle, plan, output prefix, and optional DCV/cookie settings. | Before launching any worker instance. |
| [`resume_screenshot_worker_after_auth.py`](resume_screenshot_worker_after_auth.py) | Polls a remote host until the browser is signed in again, then restarts the worker. | During `auth_required` recovery when you want a helper to resume the service automatically. |
| [`split_worker_plan.py`](split_worker_plan.py) | Splits a large `worker_plan.csv` into balanced per-instance subsets and optionally copies worker assets. | Before launching multiple workers from one larger plan. |
| [`watch_screenshot_operator.py`](watch_screenshot_operator.py) | Watches a screenshot prefix for operator-actionable stops and can publish SNS/SES notifications plus browser-stage previews. | For rapid Cloudflare/auth stop alerts. |
| [`watch_screenshot_progress.py`](watch_screenshot_progress.py) | Sends periodic global screenshot digests with static worker desktop previews. | For coarse-grained progress/status emails. |

## Common Recipes

### Build a worker bundle

```bash
python3 scripts/aws/build_worker_bundle.py \
  --output-path output/aws_launch_20260404/newscom_bundle_20260404.tgz
```

### Render screenshot-worker user-data

```bash
python3 scripts/aws/render_user_data.py \
  --bucket "${FLEET_BUCKET}" \
  --bundle-key "${BUNDLE_KEY}" \
  --plan-prefix "${PLAN_PREFIX}" \
  --cookies-key "${COOKIE_SEED_KEY}" \
  --output-prefix "${OUTPUT_PREFIX}" \
  --worker-mode screenshot \
  --enable-dcv \
  --dcv-session-id newscom-shot-01 \
  --dcv-session-owner ubuntu \
  --dcv-password "${DCV_PASSWORD}" \
  --output-path "${USER_DATA_PATH}"
```

### Allow the current operator IP

```bash
python3 scripts/aws/ensure_worker_access.py \
  --group-id "${WORKER_SECURITY_GROUP_ID}" \
  --region "${AWS_REGION}" \
  --port 22 \
  --port 8443
```

### Watch for operator-actionable screenshot stops

```bash
PYTHONPATH=src python3 scripts/aws/watch_screenshot_operator.py \
  --bucket "${FLEET_BUCKET}" \
  --prefix-auto latest-active-screenshot \
  --output-dir output/operator_watch_20260404 \
  --sns-topic-arn "${SNS_TOPIC_ARN}" \
  --ssh-key "${SSH_KEY}" \
  --capture-preview
```

### Install the digest watcher under `launchd`

```bash
python3 scripts/aws/install_screenshot_progress_launchd.py \
  --action install \
  --bucket "${FLEET_BUCKET}" \
  --prefix-auto latest-active-screenshot \
  --output-dir output/progress_watch_20260404 \
  --sns-topic-arn "${SNS_TOPIC_ARN}" \
  --ssh-key "${SSH_KEY}" \
  --public-preview-bucket "${PREVIEW_BUCKET}"
```

### Refresh the canonical PNG archive and inventory

```bash
python3 scripts/aws/archive_viewer_pngs.py \
  --bucket "${FLEET_BUCKET}" \
  --source-prefix results/ \
  --host "${ACTIVE_WORKER_HOST}" \
  --host "${RETIRED_WORKER_HOST}" \
  --ssh-key "${SSH_KEY}" \
  --output-dir output/aws_viewer_png_archive_20260404
```

## Which Runbook To Read

- Launching a new worker:
  - [docs/aws_launch_runbook.md](../../docs/aws_launch_runbook.md)
- Operating an existing worker:
  - [docs/aws_operations_runbook.md](../../docs/aws_operations_runbook.md)
- Understanding the account and current values:
  - [docs/aws_account_reference.md](../../docs/aws_account_reference.md)
- Understanding runtime vs canonical storage:
  - [docs/aws_storage_model.md](../../docs/aws_storage_model.md)
