# Artifact Inventory And Recovery

Acquisition is complete only when page artifacts can be proven present,
identified, and suitable for the parser. A successful browser or download row
is evidence about an attempt; the source artifact manifest and reconciliation
bundle are the durable handoff.

## Contracts

The workflow uses four explicit contracts:

1. `source-artifact-v1` describes each expected page image, stable page ID,
   local target path, source identity, checksum, and provenance.
2. `artifact-inventory-item-v1` describes one remote object location and the
   page or source identity used to match it.
3. `artifact-reconciliation-v1` records local and remote observations,
   classification, and recommended action for one expected page.
4. `artifact-recovery-action-v1` contains only pages that require work.

S3 ETags are retained as storage metadata but are never treated as SHA-256
checksums. Multipart ETags in particular are not content hashes. The
reconciler trusts `checksum_sha256` only when an inventory producer explicitly
provides a 64-character hexadecimal SHA-256 value.

## Snapshot S3

Create a paginated, normalized S3 inventory with the AWS CLI:

```bash
newspaper-scrapper snapshot-s3-artifact-inventory \
  --bucket "$FLEET_BUCKET" \
  --prefix archive/viewer_png/by_image_id/ \
  --output-jsonl /tmp/viewer_png.inventory.jsonl
```

By default, numeric source IDs are extracted from basenames such as
`165001395_viewer.png`. Use `--source-id-regex` for another key convention. The
regex may expose a named `source_id` group or use its first capture group.

## Reconcile

Compare the expected source manifest with local bytes and any number of remote
inventory snapshots:

```bash
newspaper-scrapper reconcile-source-artifacts \
  --input-jsonl /path/to/source_artifacts.jsonl \
  --remote-inventory-jsonl /tmp/viewer_png.inventory.jsonl \
  --output-dir /tmp/artifact_reconciliation

newspaper-scrapper validate-artifact-reconciliation \
  --run-dir /tmp/artifact_reconciliation \
  --output-json /tmp/artifact_reconciliation/validation.json
```

Local verification computes SHA-256 and asks Pillow to decode each image by
default. Disable either check only for diagnosis with
`--trust-local-checksums` or `--skip-image-decode`.

## Classifications

| Classification | Meaning | Default action |
| --- | --- | --- |
| `ready_local_verified` | Local image decodes and matches the manifest checksum | `none` |
| `ready_local_needs_checksum` | Local image exists, but the source manifest lacks a checksum | `register_checksum` |
| `ready_local_unverified` | Verification was explicitly disabled | `verify_local` |
| `corrupt_local_remote_recoverable` | Local image is invalid; one remote replacement exists | `download_remote` |
| `corrupt_local_remote_ambiguous` | Local image is invalid; several replacements exist | `review_remote_duplicates` |
| `corrupt_local` | Local image is invalid and no remote replacement exists | `reacquire` |
| `remote_recoverable` | Local image is absent; one remote copy exists | `download_remote` |
| `remote_duplicate` | Several remote locations have the same known signature | `review_remote_duplicates` |
| `remote_conflict` | Remote candidates disagree or cannot be proven identical | `review_remote_duplicates` |
| `remote_checksum_conflict` | Remote SHA-256 conflicts with the source manifest | `review_checksum_conflict` |
| `missing` | No local or remote artifact exists | `reacquire` |

One bad page does not prevent the system from materializing a strict
parser-ready subset for pages that are already verified.

## Bundle Layout

```text
RUN_DIR/
  summary.json
  source_manifest_validation.json
  artifact_reconciliation.jsonl
  recovery_manifest.jsonl
  parser_ready_source_artifacts.jsonl
  remote_download_manifest.jsonl
  reacquire_manifest.csv
  unmatched_remote_inventory.jsonl
  validation.json
```

- `parser_ready_source_artifacts.jsonl` is directly consumable by
  `newspaper-parsing`; every included row has a present image and verified
  checksum.
- `remote_download_manifest.jsonl` contains unambiguous remote restores.
- `reacquire_manifest.csv` uses the existing issue/page/image columns expected
  by acquisition workers.
- review actions remain in `recovery_manifest.jsonl` and are never resolved by
  guessing.

Output directories must be empty or absent. This prevents a new audit from
silently mixing with stale recovery artifacts.
