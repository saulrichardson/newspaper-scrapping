#!/bin/bash
# CPU Slurm smoke for parser-ready source artifact manifests.

#SBATCH -A torch_pr_609_general
#SBATCH -p cpu_short
#SBATCH --cpus-per-task=2
#SBATCH --mem=2G
#SBATCH --time=00:10:00
#SBATCH -J scrapping_artifact_canary
#SBATCH -o /scratch/%u/codex_hpc/newspaper_scrapping_ops/logs/%x-%j.out
#SBATCH -e /scratch/%u/codex_hpc/newspaper_scrapping_ops/logs/%x-%j.err

set -euo pipefail

BASE="${BASE:-/scratch/$USER/codex_hpc/newspaper_scrapping_ops}"
PROJECT_ROOT="${PROJECT_ROOT:-$BASE/newspaper-scrapping-ops}"
RUN_DIR="${RUN_DIR:-$BASE/runs/source_manifest_${SLURM_JOB_ID:-manual}}"
VENV_DIR="${VENV_DIR:-$BASE/envs/scrapper_py312}"
BOOTSTRAP_PY="${BOOTSTRAP_PY:-}"
SCRAPPER_PY="${SCRAPPER_PY:-$VENV_DIR/bin/python}"

mkdir -p "$BASE/logs" "$BASE/runs" "$RUN_DIR"

echo "[source-manifest] host=$(hostname)"
echo "[source-manifest] date=$(date -Is)"
echo "[source-manifest] project_root=$PROJECT_ROOT"
echo "[source-manifest] run_dir=$RUN_DIR"

if [ ! -d "$PROJECT_ROOT/src/newspaper_scrapper" ]; then
  echo "[source-manifest] ERROR: PROJECT_ROOT does not look like newspaper-scrapping-ops: $PROJECT_ROOT" >&2
  exit 2
fi

select_bootstrap_python() {
  if [ -n "$BOOTSTRAP_PY" ]; then
    printf "%s\n" "$BOOTSTRAP_PY"
    return 0
  fi
  for candidate in python3.12 python3.11 python3 /scratch/$USER/paddleocr_vl15/envs/mineru25_py310/bin/python; do
    if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
      then
        command -v "$candidate" 2>/dev/null || printf "%s\n" "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

if [ ! -x "$SCRAPPER_PY" ]; then
  BOOTSTRAP_PY="$(select_bootstrap_python)" || {
    echo "[source-manifest] ERROR: could not find Python >=3.11 on this node" >&2
    exit 2
  }
  echo "[source-manifest] bootstrap_python=$BOOTSTRAP_PY"
  "$BOOTSTRAP_PY" -m venv "$VENV_DIR"
fi

"$SCRAPPER_PY" -m pip install --upgrade pip >/dev/null
"$SCRAPPER_PY" -m pip install -e "$PROJECT_ROOT" pytest >/dev/null

IMAGE_DIR="$RUN_DIR/images/torch-issue"
IMAGE_PATH="$IMAGE_DIR/0001__torch-img-001.jpg"
INPUT_CSV="$RUN_DIR/results.csv"
OUTPUT_JSONL="$RUN_DIR/source_artifacts.jsonl"
mkdir -p "$IMAGE_DIR"
"$SCRAPPER_PY" - <<PY
from pathlib import Path
from PIL import Image, ImageDraw

path = Path("$IMAGE_PATH")
image = Image.new("RGB", (240, 360), "white")
draw = ImageDraw.Draw(image)
for y in range(40, 320, 24):
    draw.rectangle([24, y, 216, y + 7], fill="black")
image.save(path, format="JPEG", quality=90)
PY
cat > "$INPUT_CSV" <<CSV
issue_id,issue_date,page_num,preferred_image_id,preferred_image_page_url,status,output_path
torch-issue,1942-04-18,1,torch-img-001,https://www.newspapers.com/image/torch-img-001/,downloaded,$IMAGE_PATH
CSV

cd "$PROJECT_ROOT"
"$SCRAPPER_PY" -m pytest -q tests/test_source_manifest.py tests/test_artifact_inventory.py

"$SCRAPPER_PY" -m newspaper_scrapper.cli.main build-source-artifact-manifest \
  --input-csv "$INPUT_CSV" \
  --output-jsonl "$OUTPUT_JSONL" \
  --include-status downloaded \
  --require-files \
  > "$RUN_DIR/summary.json"

"$SCRAPPER_PY" -m newspaper_scrapper.cli.main validate-source-artifact-manifest \
  --input-jsonl "$OUTPUT_JSONL" \
  --require-files \
  --require-checksums \
  --verify-checksums \
  --output-json "$RUN_DIR/validation.json" \
  > "$RUN_DIR/validation.stdout.json"

"$SCRAPPER_PY" - <<PY
from __future__ import annotations

import json
from pathlib import Path

run_dir = Path("$RUN_DIR")
manifest = run_dir / "source_artifacts.jsonl"
rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
rows.append(
    {
        "page_id": "torch-issue__p0002__torch-img-002",
        "image_path": str(run_dir / "images" / "torch-issue" / "0002__torch-img-002.jpg"),
        "issue_id": "torch-issue",
        "page_number": 2,
        "checksum_sha256": "1" * 64,
        "source": {
            "source_system": "torch_fixture",
            "source_id": "torch-img-002",
            "source_url": "https://www.newspapers.com/image/torch-img-002/",
        },
        "metadata": {
            "contract_version": "source-artifact-v1",
            "artifact_kind": "page_image",
            "issue_date": "1942-04-18",
        },
    }
)
manifest.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
inventory = run_dir / "remote_inventory.jsonl"
inventory.write_text(
    json.dumps(
        {
            "contract_version": "artifact-inventory-item-v1",
            "page_id": "",
            "source_id": "torch-img-002",
            "location_type": "s3",
            "uri": "s3://fixture-bucket/archive/torch-img-002.jpg",
            "size_bytes": 123,
            "checksum_sha256": "1" * 64,
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

"$SCRAPPER_PY" -m newspaper_scrapper.cli.main reconcile-source-artifacts \
  --input-jsonl "$OUTPUT_JSONL" \
  --remote-inventory-jsonl "$RUN_DIR/remote_inventory.jsonl" \
  --output-dir "$RUN_DIR/reconciliation" \
  > "$RUN_DIR/reconciliation.stdout.json"

"$SCRAPPER_PY" -m newspaper_scrapper.cli.main validate-artifact-reconciliation \
  --run-dir "$RUN_DIR/reconciliation" \
  --output-json "$RUN_DIR/reconciliation/validation.json" \
  > "$RUN_DIR/reconciliation.validation.stdout.json"

"$SCRAPPER_PY" - <<PY
from __future__ import annotations

import json
from pathlib import Path

run_dir = Path("$RUN_DIR")
summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
validation = json.loads((run_dir / "validation.json").read_text(encoding="utf-8"))
reconciliation = json.loads((run_dir / "reconciliation" / "summary.json").read_text(encoding="utf-8"))
reconciliation_validation = json.loads((run_dir / "reconciliation" / "validation.json").read_text(encoding="utf-8"))
rows = [
    json.loads(line)
    for line in (run_dir / "source_artifacts.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
status_ok = all(
    (
        summary.get("rows_written") == 1,
        len(rows) == 2,
        rows[0].get("checksum_sha256"),
        validation.get("status") == "ok",
        reconciliation.get("counts", {}).get("expected_artifacts") == 2,
        reconciliation.get("counts", {}).get("parser_ready_artifacts") == 1,
        reconciliation.get("counts", {}).get("remote_download_actions") == 1,
        reconciliation_validation.get("status") == "ok",
    )
)
status = {
    "status": "ok" if status_ok else "error",
    "run_dir": str(run_dir),
    "summary": summary,
    "validation": validation,
    "reconciliation_summary": reconciliation,
    "reconciliation_validation": reconciliation_validation,
    "reconciliation_dir": str(run_dir / "reconciliation"),
    "validation_json": str(run_dir / "validation.json"),
    "source_artifacts_jsonl": str(run_dir / "source_artifacts.jsonl"),
    "page_id": rows[0].get("page_id") if rows else "",
}
(run_dir / "slurm_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
print(json.dumps(status, indent=2, sort_keys=True))
raise SystemExit(0 if status["status"] == "ok" else 1)
PY
