# AWS Operations Runbook

This file covers day-2 operations for existing workers: discovery, access,
health checks, Cloudflare/auth recovery, watchers, updates, and safe worker
replacement.

For the exact live values on this machine, use:

- `docs/private/aws_account_reference.local.md`

The tracked runbook below assumes you export those values into environment
variables before running the commands.

## 1. Export The Current Values

Before using this runbook, export at least:

```bash
export AWS_REGION=...
export FLEET_BUCKET=...
export PREVIEW_BUCKET=...
export SNS_TOPIC_ARN=...
export WORKER_SECURITY_GROUP_ID=...
export SSH_KEY=...
export ACTIVE_WORKER_HOST=...
export RETIRED_WORKER_HOST=...
export RESULTS_PREFIX=...
```

## 2. Find The Current Fleet

Use this query to enumerate the current workers:

```bash
aws ec2 describe-instances \
  --region "${AWS_REGION}" \
  --filters \
    Name=instance-state-name,Values=pending,running,stopping,stopped \
    Name=key-name,Values="${KEY_NAME}" \
  --query 'Reservations[].Instances[].{
    InstanceId:InstanceId,
    Name:Tags[?Key==`Name`]|[0].Value,
    Role:Tags[?Key==`Role`]|[0].Value,
    Project:Tags[?Key==`Project`]|[0].Value,
    State:State.Name,
    PublicIp:PublicIpAddress,
    LaunchTime:LaunchTime
  }' \
  --output table
```

Important:

- user-data only tells you the original launch inputs
- after a manual reseat, the live runtime prefix may differ

## 3. Grant Yourself SSH And DCV Access

If SSH or DCV is failing, refresh your current public IP allowlist first:

```bash
python3 scripts/aws/ensure_worker_access.py \
  --group-id "${WORKER_SECURITY_GROUP_ID}" \
  --region "${AWS_REGION}" \
  --port 22 \
  --port 8443
```

This adds a new `/32`. It does not prune older rules.

## 4. Connect To A Worker

SSH:

```bash
ssh -i "${SSH_KEY}" -o StrictHostKeyChecking=no ubuntu@"${WORKER_HOST}"
```

DCV:

```text
https://${WORKER_HOST}:8443/#newscom-shot-01
```

The DCV password is chosen at launch time and should live only in the local
private reference or be recovered from EC2 user-data when needed.

## 5. On-Box Layout

Every worker host follows the same basic layout:

- `/opt/newscom/app`
- `/opt/newscom/venv`
- `/opt/newscom/data`
- `/opt/newscom/state`
- `/opt/newscom/run`
- `/opt/newscom/run_newscom_worker.sh`
- `/opt/newscom/sync_newscom_outputs.sh`
- `/etc/systemd/system/newscom-worker.service`

## 6. Reliable Health Checks

Do not trust a single summary JSON field in isolation.

Use:

```bash
systemctl is-active newscom-worker.service
sudo systemctl status newscom-worker.service --no-pager -l | sed -n '1,80p'
```

```bash
python3 - <<'PY'
from pathlib import Path
import csv

path = Path("/opt/newscom/run/workers/worker_01/passes/pass_01/results.csv")
print(path.exists())
if path.exists():
    rows = list(csv.DictReader(path.open()))
    print(f"rows={len(rows)}")
    if rows:
        row = rows[-1]
        print(
            row.get("status", ""),
            row.get("preferred_image_id", ""),
            row.get("issue_id", ""),
            row.get("page_num", ""),
            row.get("elapsed_seconds", ""),
        )
PY
```

Trust, in this order:

1. `systemctl is-active`
2. row growth in `results.csv`
3. last successful capture changing over time

`stopped_reason` may be stale on older hosts.

## 7. Map S3 Prefixes Back To Workers

Runtime sync layout in the fleet bucket looks like:

```text
s3://$FLEET_BUCKET/$RESULTS_PREFIX/<instance-id>/run/...
s3://$FLEET_BUCKET/$RESULTS_PREFIX/<instance-id>/state/...
```

That means:

- watcher `--prefix` must point at the run root, not the whole bucket
- the next segment is the EC2 `InstanceId`
- `state/worker_plan.csv` tells you which worker names exist on the instance

This is the reason stale digest emails happened previously: the watcher was
still pointed at an old prefix.

## 8. Recover From Cloudflare Or Auth Stops

### Preferred manual recovery

1. Stop the worker:

   ```bash
   sudo systemctl stop newscom-worker.service
   ```

2. Open the worker DCV session.
3. In the remote Chrome window:
   - clear the Cloudflare challenge, or
   - sign back into Newspapers.com, or both
4. Once the page is visibly usable again, restart:

   ```bash
   sudo systemctl start newscom-worker.service
   ```

5. Confirm new rows land in `results.csv`.

### Auto-resume helper

If you want to wait for the signed-in state and then restart automatically:

```bash
python3 scripts/aws/resume_screenshot_worker_after_auth.py \
  --host "${WORKER_HOST}" \
  --ssh-key "${SSH_KEY}"
```

## 9. Watchers And Alerts

### Operator watcher

```bash
PYTHONPATH=src python3 scripts/aws/watch_screenshot_operator.py \
  --bucket "${FLEET_BUCKET}" \
  --prefix-auto latest-active-screenshot \
  --output-dir output/operator_watch_<date> \
  --sns-topic-arn "${SNS_TOPIC_ARN}" \
  --ssh-key "${SSH_KEY}" \
  --capture-preview
```

### Progress/digest watcher

```bash
PYTHONPATH=src python3 scripts/aws/watch_screenshot_progress.py \
  --bucket "${FLEET_BUCKET}" \
  --prefix-auto latest-active-screenshot \
  --output-dir output/progress_watch_<date> \
  --sns-topic-arn "${SNS_TOPIC_ARN}" \
  --ssh-key "${SSH_KEY}" \
  --public-preview-bucket "${PREVIEW_BUCKET}"
```

### launchd wrapper

```bash
python3 scripts/aws/install_screenshot_progress_launchd.py \
  --action install \
  --bucket "${FLEET_BUCKET}" \
  --prefix-auto latest-active-screenshot \
  --output-dir output/progress_watch_<date> \
  --sns-topic-arn "${SNS_TOPIC_ARN}" \
  --ssh-key "${SSH_KEY}" \
  --public-preview-bucket "${PREVIEW_BUCKET}"
```

Check status:

```bash
python3 scripts/aws/install_screenshot_progress_launchd.py --action status
```

Uninstall:

```bash
python3 scripts/aws/install_screenshot_progress_launchd.py --action uninstall
```

Watcher rule that must not be violated:

- use `--prefix-auto latest-active-screenshot` unless you are intentionally pinning a watcher to one historical run

## 10. Safe Update Procedure For An Existing Worker

There is still no single blessed deploy command in this repo.

### Small Python-only change, in place

Use only at a natural stop or when you can afford interruption.

1. Build and upload a fresh bundle.
2. Stop the worker:

   ```bash
   sudo systemctl stop newscom-worker.service
   ```

3. Sync current outputs:

   ```bash
   sudo /opt/newscom/sync_newscom_outputs.sh
   ```

4. Pull the new bundle and reinstall:

   ```bash
   sudo -u ubuntu aws s3 cp \
     "s3://${FLEET_BUCKET}/${NEW_BUNDLE_KEY}" \
     /opt/newscom/bundle.tar.gz
   sudo rm -rf /opt/newscom/app
   sudo mkdir -p /opt/newscom/app
   sudo tar -xzf /opt/newscom/bundle.tar.gz -C /opt/newscom/app --strip-components=1
   sudo /opt/newscom/venv/bin/pip install --upgrade /opt/newscom/app
   ```

5. Restart and verify:

   ```bash
   sudo systemctl start newscom-worker.service
   systemctl is-active newscom-worker.service
   ```

### Bootstrap/systemd/DCV change

If the change affects bootstrap, DCV, disk mounting, or the service wrapper,
the cleaner path is:

1. archive current PNGs
2. launch a fresh worker with the new bundle/user-data
3. sign in once
4. cut over to the new worker
5. retire the old one

## 11. Replace Or Retire A Worker

Preferred replacement flow:

1. archive current PNGs into the canonical archive
2. launch a fresh worker on a new IP
3. sign in and verify first captures
4. stop the old worker
5. final archive sync
6. terminate the old worker

## 12. Common Failure Modes

### SSH or DCV stopped working

Usually the current public IP is not allowed on the security group. Refresh the
security-group ingress rules first.

### Email digest says obviously wrong counts

Usually the watcher is pointed at an old `results/...` prefix.

### Worker says `active` but is not doing useful work

Check `results.csv` row growth, not just `systemctl is-active`.

### `stopped_reason` says something old

Treat it as advisory only. Trust:

- service state
- current pass row growth
- last successful capture changing

### User-data disagrees with the live run

User-data only reflects the original launch configuration. Manual reseats can
change the live runtime prefix after launch.

## Related Documents

- [docs/aws_account_reference.md](aws_account_reference.md)
- [docs/aws_launch_runbook.md](aws_launch_runbook.md)
- [docs/aws_storage_model.md](aws_storage_model.md)
- [scripts/aws/README.md](../scripts/aws/README.md)
