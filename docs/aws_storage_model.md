# AWS Storage and Archive Model

This file documents the clean storage model for screenshot PNGs across worker
replacement and long-running operations.

## Why This Exists

Worker disks are ephemeral runtime storage. Workers will eventually:

- stop on Cloudflare
- stop on `auth_required`
- be replaced with a new IP
- be terminated

If screenshots only live on a worker disk, they are operationally fragile. The
archive model fixes that by separating:

- worker runtime output
- canonical preserved PNG archive
- authoritative inventory/provenance

## Canonical Rule

Workers are ephemeral. The archive is canonical. The inventory is the source of
truth.

## S3 Layout

The canonical AWS-side layout is:

```text
s3://$FLEET_BUCKET/
  results/
    <run-prefix>/
      <instance-id>/
        run/
        state/
  archive/
    viewer_png/
      by_image_id/
        <fanout>/
          <image_id>_viewer.png
    inventory/
      viewer_png_inventory.tsv
      viewer_png_provenance.tsv
      summary.json
    snapshots/
      <source-group>.tsv
```

Meaning:

- `results/...`
  - worker-oriented runtime sync
  - not canonical
- `archive/viewer_png/by_image_id/...`
  - canonical deduped screenshot archive
- `archive/inventory/...`
  - source of truth for what is preserved
- `archive/snapshots/...`
  - provenance linking workers/runs to archived image IDs

## Runtime vs Canonical Storage

### Runtime storage

Lives on the worker and syncs to:

- `results/<output-prefix>/<instance-id>/run/...`
- `results/<output-prefix>/<instance-id>/state/...`

This is useful for:

- ongoing worker execution
- pass summaries
- logs
- current worker debugging

This is not the authoritative long-term archive.

### Canonical storage

Lives in:

- `archive/viewer_png/by_image_id/...`

This is what survives worker retirement and replacement.

## Current Import Helper

The script that builds and refreshes the canonical archive is:

- [`scripts/aws/archive_viewer_pngs.py`](../scripts/aws/archive_viewer_pngs.py)

It does four things:

1. scans `results/...` in S3 for `*_viewer.png`
2. copies them into `archive/viewer_png/by_image_id/...`
3. optionally scans live worker hosts for host-only PNGs that have not hit S3 yet
4. writes and uploads fresh inventory/provenance manifests

Generic command:

```bash
python3 scripts/aws/archive_viewer_pngs.py \
  --bucket "${FLEET_BUCKET}" \
  --source-prefix results/ \
  --host "${ACTIVE_WORKER_HOST}" \
  --host "${RETIRED_WORKER_HOST}" \
  --ssh-key "${SSH_KEY}" \
  --output-dir output/aws_viewer_png_archive_<date>
```

Use `--host` only for hosts that still contain PNGs you care about.

## Inventory Files

The inventory files are:

- `archive/inventory/viewer_png_inventory.tsv`
- `archive/inventory/viewer_png_provenance.tsv`
- `archive/inventory/summary.json`

What they mean:

- `viewer_png_inventory.tsv`
  - one row per canonical archived PNG
- `viewer_png_provenance.tsv`
  - one row per occurrence/source of each PNG
- `summary.json`
  - top-level counts and summary metadata

If someone asks, “How many PNGs do we have on AWS?”, the right answer comes
from the archive inventory, not from old email digests and not from a raw
worker disk count.

## How To Answer “How Many PNGs Do We Have?”

If the active worker may be ahead of the archive:

1. run `archive_viewer_pngs.py`
2. read `archive/inventory/summary.json`

Do not answer from:

- a stale email digest
- just one worker’s `results.csv`
- a local ad hoc output folder

## Retiring A Worker Without Losing PNGs

Before stopping or terminating a worker:

1. run the archive import
2. confirm the worker’s host-only PNGs are reflected in the archive inventory
3. only then stop or terminate the worker

This is the clean retirement rule going forward.

## Local Convenience View

There is also a local helper:

- [`scripts/aws/refresh_png_archive.py`](../scripts/aws/refresh_png_archive.py)

That script builds a symlink-based browse view under `output/png_archive/`. It
is a convenience view only. The AWS-side source of truth remains the canonical
S3 archive plus inventory.

## Why Worker-By-Worker Folders Are Not Canonical

Organizing long-term preserved screenshots by worker is the wrong abstraction.
Workers will come and go. The screenshot identity is the image/page, not the
EC2 instance that happened to capture it.

That is why the archive key is based on:

- `image_id`

and not on:

- instance ID
- worker name
- old run prefix

## Related Documents

- [docs/aws.md](aws.md)
- [docs/aws_account_reference.md](aws_account_reference.md)
- [docs/aws_operations_runbook.md](aws_operations_runbook.md)
- [scripts/aws/README.md](../scripts/aws/README.md)
