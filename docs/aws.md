# AWS worker fleet

This file is the AWS overview and index. The detailed handoff docs now live in:

- `docs/private/aws_account_reference.local.md` (gitignored local-only live-account reference)
- [docs/aws_account_reference.md](aws_account_reference.md)
- [docs/aws_launch_runbook.md](aws_launch_runbook.md)
- [docs/aws_operations_runbook.md](aws_operations_runbook.md)
- [docs/aws_storage_model.md](aws_storage_model.md)
- [scripts/aws/README.md](../scripts/aws/README.md)

If someone new needs to operate this setup locally, read those files in that
order. The exact live-account values intentionally live in the gitignored local
reference, not in the tracked docs.

This overview keeps the architectural rules and high-level operating model in
one place. The concrete AWS account values, exact EC2 launch commands, IAM
details, watcher commands, and replacement/recovery procedures are now broken
out into the dedicated runbooks above.

This repo supports a per-IP worker model for Newspapers.com search seed harvesting:

- one EC2 instance per public IP
- one headful Chrome session under Xvfb per instance
- one search worker per instance

Screenshot workers use a stricter operating mode:

- one authenticated screenshot worker per IP
- cookie bootstrap required before capture
- browser reuse required after cookie import
- issue-grouped manifests preferred over page-level sharding
- one active screenshot worker per IP
- Amazon DCV preferred over bare Xvfb when a human may need to solve a challenge

The intended deployment pattern is:

1. Build a local search worker plan with `plan-search-workers`.
2. Split that plan into per-instance CSVs.
3. Upload the application bundle and per-instance plan CSVs to S3.
4. Launch Ubuntu EC2 instances with a bootstrap script that:
   - installs AWS CLI first so it can fetch the real bootstrap from S3
   - installs Chrome, Xvfb, Python, and repo dependencies
   - installs this scraper package
   - downloads its assigned plan CSV
   - rewrites worker output/profile paths under `/opt/newscom` so sync stays self-contained
   - runs `run-search-workers` with `--max-concurrent-workers 1`
5. Periodically sync worker results back to S3.

Recommended screenshot-worker operating mode:

- provision separate EC2 instances from the search fleet
- install Amazon DCV and create one virtual session per instance
- expose TCP `8443` only to the operator's current IP
- run Chrome inside that DCV session with a persistent profile
- reuse the same browser after cookie import
- pause for human intervention if a Cloudflare challenge page is detected
- keep `--max-concurrent-workers 1` per instance
- current preferred shape is a `64 GiB` root-only worker because PNGs now flow
  into the canonical S3 archive; a dedicated run volume is optional, not the
  default

Current healthy operating mode for large keyword seed retrieval:

- do not reuse an authenticated Newspapers.com session for search-only workers
- navigate the real browser to the visual search results page first
- paginate the `/api/search/query` backend only after that page load
- use explicit `date-start` / `date-end` slices
- use yearly slices through `1914`
- use monthly slices from `1915` onward
- keep one active worker per IP
- keep shard retries conservative and resumable

Operational goals:

- keep pressure budgeted per IP
- use stable public IPs, ideally Elastic IPs
- recover cleanly from 429s and occasional Cloudflare challenges
- collect seed manifests centrally before any full-page retrieval

Helper scripts:

- [build_worker_bundle.py](../scripts/aws/build_worker_bundle.py)
- [archive_viewer_pngs.py](../scripts/aws/archive_viewer_pngs.py)
- [bootstrap_newscom_worker.sh](../scripts/aws/bootstrap_newscom_worker.sh)
- [ensure_worker_access.py](../scripts/aws/ensure_worker_access.py)
- [pull_and_merge_results.py](../scripts/aws/pull_and_merge_results.py)
- [render_user_data.py](../scripts/aws/render_user_data.py)
- [split_worker_plan.py](../scripts/aws/split_worker_plan.py)
- [watch_screenshot_operator.py](../scripts/aws/watch_screenshot_operator.py)

Bundle workflow:

1. Build a deployable repo tarball with a single top-level directory:

   `python3 scripts/aws/build_worker_bundle.py --output-path output/aws_launch_20260404/newscom_bundle_20260404.tgz`

2. Upload that tarball to S3 and point `render_user_data.py` at the resulting
   bundle key.

This avoids the common failure mode where `bootstrap_newscom_worker.sh` extracts
an ad hoc tarball with `--strip-components=1` and loses `pyproject.toml`.

Collection workflow:

1. Let the fleet write and sync per-instance results to S3.
2. Pull the S3 prefix locally:

   `python3 scripts/aws/pull_and_merge_results.py --bucket "$FLEET_BUCKET" --prefix "$RESULTS_PREFIX" --output-dir output/pull_results_<date>`

3. Read merged outputs under `<local_dir>/merged`.

Notes:

- Search seed harvest is not the same as full-page extraction. The cloud fleet
  is optimized for collecting page-hit and issue-hit seeds first.
- EC2 workers need an instance profile or other AWS credentials on-box. The
  bootstrap fetches the bundle, plan, and sync target from S3, so bare user-data
  without AWS credentials will fail before the scraper starts.
- Screenshot workers should keep `--max-concurrent-workers 1` per instance and
  should not restart the browser after importing cookies. The validated smoke
  path is one authenticated Chrome worker using `synthetic_tiles`. For
  production, prefer a DCV virtual session over raw Xvfb so the same browser
  can be taken over manually when a challenge appears.
- Future workers can mount a dedicated run volume automatically at bootstrap by
  passing either `--run-volume-id <vol-...>` or `--run-volume-device /dev/sdf`
  to [render_user_data.py](../scripts/aws/render_user_data.py). The bootstrap will resolve the Nitro NVMe device via
  `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_<volumeid>`, format it on
  first boot if needed, persist it in `/etc/fstab`, and mount it at
  `/opt/newscom/run`.
- In live AWS validation on `zoning`, monthly no-auth shards from the modern
  period were materially healthier than large authenticated year buckets.
- If a shard family starts accumulating `429`s again, first reduce date-slice
  density before adding more concurrent IP workers.
- To authorize operator access to SSH and DCV from the current public IP:

  `python3 scripts/aws/ensure_worker_access.py --group-id "$WORKER_SECURITY_GROUP_ID" --region "$AWS_REGION" --port 22 --port 8443`

- To render DCV-enabled screenshot worker user-data:

  See [docs/aws_launch_runbook.md](aws_launch_runbook.md) for the complete runnable command with the current bucket, bundle key, plan prefix, cookie seed, and output prefix.

- To render user-data that mounts a dedicated 1 TiB run volume at `/opt/newscom/run`:

  See [docs/aws_launch_runbook.md](aws_launch_runbook.md) for the current `--run-volume-device /dev/sdf` launch-time example.

- To watch screenshot workers locally, auto-notify on macOS, and open the DCV session when a worker stops on `cloudflare_challenge` or `auth_required`:

  `PYTHONPATH=src python3 scripts/aws/watch_screenshot_operator.py --bucket "$FLEET_BUCKET" --prefix-auto latest-active-screenshot --output-dir output/operator_watch_<date>`

- To also publish real-time email alerts through AWS SNS, add a topic ARN:

  `PYTHONPATH=src python3 scripts/aws/watch_screenshot_operator.py --bucket "$FLEET_BUCKET" --prefix-auto latest-active-screenshot --output-dir output/operator_watch_<date> --sns-topic-arn "$SNS_TOPIC_ARN"`

- To capture the current remote browser stage when an alert fires, upload that
  preview to S3, and include the preview URL in the alert email:

  `PYTHONPATH=src python3 scripts/aws/watch_screenshot_operator.py --bucket "$FLEET_BUCKET" --prefix-auto latest-active-screenshot --output-dir output/operator_watch_<date> --sns-topic-arn "$SNS_TOPIC_ARN" --capture-preview --ssh-key "$SSH_KEY"`

- To run the screenshot progress digest durably on macOS under `launchd`
  instead of a fragile detached shell:

  `python3 scripts/aws/install_screenshot_progress_launchd.py --action install --bucket "$FLEET_BUCKET" --prefix-auto latest-active-screenshot --output-dir output/progress_watch_<date> --sns-topic-arn "$SNS_TOPIC_ARN" --ssh-key "$SSH_KEY" --public-preview-bucket "$PREVIEW_BUCKET"`

  This installs a `launchd` agent that keeps one watcher process alive under
  `launchd` supervision. The watcher itself sleeps between hourly sends, which
  has been more reliable than relying on detached shells.

  The installer does not bake in account-specific defaults. Pass your own S3
  bucket, SNS topic ARN, and optional SSH key explicitly. Use
  `--prefix-auto latest-active-screenshot` when you want the watcher to follow
  the newest live screenshot run automatically.

- The installer writes a plist under `~/Library/LaunchAgents/` and boots it
  into the current `gui/<uid>` domain. Use these to inspect or remove it:

  `python3 scripts/aws/install_screenshot_progress_launchd.py --action status`

  `python3 scripts/aws/install_screenshot_progress_launchd.py --action uninstall`

- SNS email is plain text only. To render the preview image inline in the email,
  configure SES for a verified recipient/sender identity and pass:

  `--ses-from-email <verified-email> --ses-to-email <verified-email>`

## Source of truth and deployment model

The intended operating model is:

1. Edit scraper logic locally in this repository.
2. Commit that logic to git.
3. Push `main` to GitHub.
4. Deploy that committed code to AWS workers.
5. Treat EC2 hosts as runtime environments, not as the primary home of source code.

What should live in git:

- application logic under `src/`
- CLI surface under `src/newspaper_scrapper/cli/`
- AWS bootstrap and watcher scripts under `scripts/aws/`
- tests under `tests/`
- runbooks and operating notes under `docs/`

What should not be treated as source-of-truth code:

- files under `/opt/newscom/run`
- per-host Chrome profiles
- generated DCV session metadata such as `dcv_session.json`
- generated systemd state and logs
- per-run manifests, screenshots, and merged outputs

Emergency rule:

- If a host-side script is patched directly on an EC2 worker to recover a live run,
  backport the same change into this repo as soon as possible and commit it.
- Do not leave operational fixes living only on-host.

Deployment note:

- This repo does not yet define a single blessed deploy command for updating an
  already-running screenshot host.
- Until that is formalized, the correct sequence is still:
  - change code locally
  - commit locally
  - push to GitHub
  - then update the worker from the committed repo version at a controlled restart point
- The current blessed manual update procedure is documented in
  [docs/aws_operations_runbook.md](aws_operations_runbook.md).

## Current screenshot production pattern

Current healthy screenshot behavior is based on:

- one active screenshot browser per IP
- one worker process per host
- a persistent Chrome profile per worker
- Amazon DCV for human intervention when needed
- conservative stop-on-challenge / stop-on-auth semantics

The current scaled screenshot pattern supports two workers safely if:

- each worker has its own public IP
- each worker has its own Chrome profile
- each worker has its own disjoint manifest
- workers do not share page ranges

Do not increase throughput on one IP by running multiple active screenshot
browser sessions simultaneously. Scale horizontally by worker/IP instead.

## Reporting semantics

Screenshot digests now distinguish between:

- `Attempt failures total`: rows that were actually attempted and failed in the
  current pass/results view
- `Remaining rows total`: backlog still left to capture from the assigned manifest

Do not interpret `remaining rows` as hard failures.

Worker pass summaries may contain stale `stopped_reason` values on hosts that
have not yet been restarted onto the latest code. The reliable live-state checks
are:

- `systemctl is-active newscom-worker.service`
- fresh rows appearing in the current pass `results.csv`
- current page timing / last-capture movement on-host

## Canonical screenshot storage model

Screenshot-worker disks are ephemeral runtime storage. The canonical archive
should live in S3 and should be organized by page identity, not by worker
lifecycle.

Recommended layout in the fleet bucket:

- `archive/viewer_png/by_image_id/<fanout>/<image_id>_viewer.png`
- `archive/inventory/viewer_png_inventory.tsv`
- `archive/inventory/viewer_png_provenance.tsv`
- `archive/inventory/summary.json`
- `archive/snapshots/<source-group>.tsv`

Where:

- `results/...` remains worker/run-oriented runtime output
- `archive/viewer_png/...` is the stable deduped archive
- `archive/inventory/...` is the source-of-truth manifest of what exists
- `archive/snapshots/...` keeps worker/run provenance without making workers the
  permanent archive layout

To backfill the current bucket into that model and optionally absorb host-only
PNG tails from live workers without restarting them:

```bash
python3 scripts/aws/archive_viewer_pngs.py \
  --bucket "$FLEET_BUCKET" \
  --source-prefix results/ \
  --host "$ACTIVE_WORKER_HOST" \
  --host "$RETIRED_WORKER_HOST" \
  --ssh-key "$SSH_KEY" \
  --output-dir output/aws_viewer_png_archive_<date>
```

This script:

- scans the existing S3 worker outputs for `*_viewer.png`
- copies them into the canonical archive path keyed by image ID
- uploads any host-only viewer PNGs directly from the live workers into the same
  canonical path
- writes authoritative inventory and provenance manifests locally and uploads
  them back to S3

The intended worker lifecycle is:

1. worker writes runtime outputs under `results/...`
2. archive job imports viewer PNGs into `archive/viewer_png/...`
3. inventory job updates `archive/inventory/...`
4. worker can then be rotated or terminated without becoming the source of truth
