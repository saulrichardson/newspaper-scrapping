# AWS Launch Runbook

This is the end-to-end launch procedure for a new Newspapers.com EC2 worker.
It covers the actual launch flow without publishing the current account's live
IDs or endpoints.

Before using this runbook, open the local private account reference:

- `docs/private/aws_account_reference.local.md`

That local-only file should provide the concrete values for the variables used
below.

## 1. Operator Prerequisites

Before launching anything, confirm:

- AWS CLI is authenticated to the correct account
- the target region is set
- the private SSH key exists locally
- the fleet bucket is reachable

Sanity check:

```bash
aws sts get-caller-identity
aws s3 ls "s3://$FLEET_BUCKET/"
test -f "$SSH_KEY"
```

## 2. Current Preferred Worker Shape

For new screenshot workers, the current preferred shape is:

- Ubuntu 24.04 amd64
- one EC2 instance per worker
- one headful Chrome profile per worker
- one screenshot worker only
- DCV enabled
- `64 GiB` `gp3` root disk
- no separate run volume by default

The canonical PNG archive now lives in S3, so large per-worker run disks are
optional rather than mandatory.

## 3. Set Run Variables

Export these from the local private account reference before you do anything
else:

```bash
export AWS_REGION=...
export FLEET_BUCKET=...
export PREVIEW_BUCKET=...
export SNS_TOPIC_ARN=...
export AMI_ID=...
export INSTANCE_TYPE=t3.large
export SUBNET_ID=...
export SECURITY_GROUP_ID=...
export INSTANCE_PROFILE=...
export KEY_NAME=...
export SSH_KEY=...
export COOKIE_SEED_KEY=...
```

Now set the run-specific values:

```bash
export RUN_NAME=screenshot-prod-YYYYMMDD
export BUNDLE_LOCAL="output/aws_launch_${RUN_NAME}/newscom_bundle_${RUN_NAME}.tgz"
export BUNDLE_KEY="bundles/newscom_bundle_${RUN_NAME}.tgz"
export PLAN_DIR="output/aws_launch_${RUN_NAME}/plan"
export PLAN_PREFIX="plans/${RUN_NAME}"
export OUTPUT_PREFIX="results/${RUN_NAME}"
export USER_DATA_PATH="output/aws_launch_${RUN_NAME}/user-data.sh"
export INSTANCE_NAME=newscom-shot-prod-01
export DCV_PASSWORD='set-a-temporary-password-here'
```

Use a fresh `RUN_NAME` every time.

## 4. Build and Upload the Worker Bundle

Build the deployable tarball:

```bash
python3 scripts/aws/build_worker_bundle.py \
  --output-path "${BUNDLE_LOCAL}"
```

Upload the bundle and refresh the bootstrap script in S3:

```bash
aws s3 cp "${BUNDLE_LOCAL}" "s3://${FLEET_BUCKET}/${BUNDLE_KEY}"
aws s3 cp scripts/aws/bootstrap_newscom_worker.sh \
  "s3://${FLEET_BUCKET}/bootstrap/bootstrap_newscom_worker.sh"
```

Why this matters:

- the bootstrap extracts with `--strip-components=1`
- the bundle must contain exactly one top-level directory
- [`scripts/aws/build_worker_bundle.py`](../scripts/aws/build_worker_bundle.py)
  creates that shape consistently

## 5. Stage the Worker Plan in S3

Screenshot workers need a plan prefix that contains:

- `worker_plan.csv`
- `workers/<worker_name>/input_manifest.csv`

Typical local staging shape:

```text
output/aws_launch_<run>/plan/
  worker_plan.csv
  workers/
    worker_01/
      input_manifest.csv
```

Upload it:

```bash
aws s3 sync "${PLAN_DIR}" "s3://${FLEET_BUCKET}/${PLAN_PREFIX}"
```

If you need to split a larger plan into per-instance subsets:

```bash
python3 scripts/aws/split_worker_plan.py \
  --plan-csv path/to/worker_plan.csv \
  --output-dir output/split_plan \
  --instance-count 2 \
  --copy-worker-assets
```

For screenshot plans, `--copy-worker-assets` is important.

## 6. Render User-Data

This is the complete screenshot-worker render flow:

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
  --sync-minutes 5 \
  --retry-cooldown-seconds 1800 \
  --poll-seconds 5 \
  --worker-stagger-seconds 120 \
  --max-worker-attempts 100 \
  --output-path "${USER_DATA_PATH}"
```

What `render_user_data.py` actually requires:

- `--bucket`
- `--bundle-key`
- `--output-prefix`
- `--output-path`
- and one of:
  - `--plan-key`
  - `--plan-prefix`

Screenshot workers usually also need:

- `--worker-mode screenshot`
- `--enable-dcv`
- `--dcv-session-id`
- `--dcv-session-owner`
- `--dcv-password`
- `--cookies-key`

## 7. Launch the EC2 Instance

The current preferred screenshot-worker launch command is:

```bash
aws ec2 run-instances \
  --region "${AWS_REGION}" \
  --image-id "${AMI_ID}" \
  --instance-type "${INSTANCE_TYPE}" \
  --subnet-id "${SUBNET_ID}" \
  --security-group-ids "${SECURITY_GROUP_ID}" \
  --iam-instance-profile Name="${INSTANCE_PROFILE}" \
  --key-name "${KEY_NAME}" \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":64,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}},{Key=Project,Value=newspaper-scrapper},{Key=Role,Value=screenshot-primary}]" \
  --user-data "file://${USER_DATA_PATH}"
```

Save the returned:

- `InstanceId`
- `PublicIpAddress`

## 8. Optional Dedicated Run Volume

Current preferred workers do not need a dedicated run volume.

If you still want a separate EBS run volume mounted at `/opt/newscom/run`:

1. add a second EBS mapping to `run-instances`
2. render user-data with `--run-volume-device /dev/sdf`

Example:

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
  --run-volume-device /dev/sdf \
  --output-path "${USER_DATA_PATH}"
```

Then launch with:

```bash
aws ec2 run-instances \
  --region "${AWS_REGION}" \
  --image-id "${AMI_ID}" \
  --instance-type "${INSTANCE_TYPE}" \
  --subnet-id "${SUBNET_ID}" \
  --security-group-ids "${SECURITY_GROUP_ID}" \
  --iam-instance-profile Name="${INSTANCE_PROFILE}" \
  --key-name "${KEY_NAME}" \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":64,"VolumeType":"gp3","DeleteOnTermination":true}},{"DeviceName":"/dev/sdf","Ebs":{"VolumeSize":1024,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${INSTANCE_NAME}},{Key=Project,Value=newspaper-scrapper},{Key=Role,Value=screenshot-primary}]" \
  --user-data "file://${USER_DATA_PATH}"
```

## 9. Open Access For The Current Operator

Authorize the current public IP for SSH and DCV:

```bash
python3 scripts/aws/ensure_worker_access.py \
  --group-id "${SECURITY_GROUP_ID}" \
  --region "${AWS_REGION}" \
  --port 22 \
  --port 8443
```

## 10. Verify Bootstrap

Once the instance is `running`:

```bash
ssh -i "${SSH_KEY}" -o StrictHostKeyChecking=no ubuntu@"${PUBLIC_IP}"
```

Then verify:

```bash
sudo systemctl status newscom-worker.service --no-pager -l
ls -la /opt/newscom
ls -la /opt/newscom/run
ls -la /opt/newscom/state
sed -n '1,20p' /opt/newscom/state/worker_plan.csv
```

## 11. Sign In And Clear First Challenge

Open the worker desktop:

```text
https://${PUBLIC_IP}:8443/#newscom-shot-01
```

Use:

- user: `ubuntu`
- password: the `DCV_PASSWORD` used at launch time

Then inside remote Chrome:

1. sign into Newspapers.com
2. clear any Cloudflare/Turnstile gate
3. leave the worker-profile browser open

## 12. Verify The First Capture

The worker is only healthy once a new row lands in the current pass results:

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
        print(rows[-1]["status"], rows[-1].get("preferred_image_id", ""))
PY
```

Good signs:

- `systemctl is-active newscom-worker.service` returns `active`
- `results.csv` row count increases over time
- last row status is `captured`

## 13. Search Worker Differences

Search workers use the same bundle/bootstrap/EC2 pattern with these changes:

- `--worker-mode search`
- no `--enable-dcv`
- no `--cookies-key`
- plan prefix only needs the search worker plan

## Related Documents

- [docs/aws_account_reference.md](aws_account_reference.md)
- [docs/aws_operations_runbook.md](aws_operations_runbook.md)
- [docs/aws_storage_model.md](aws_storage_model.md)
- [scripts/aws/README.md](../scripts/aws/README.md)
