#!/bin/bash
# CPU Slurm smoke for parser-ready source artifact manifests.

#SBATCH -A torch_pr_609_general
#SBATCH -p cs
#SBATCH --cpus-per-task=2
#SBATCH --mem=2G
#SBATCH --time=00:10:00
#SBATCH -J scrapping_source_manifest
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
"$SCRAPPER_PY" -m pip install -e "$PROJECT_ROOT" >/dev/null

IMAGE_DIR="$RUN_DIR/images/torch-issue"
IMAGE_PATH="$IMAGE_DIR/0001__torch-img-001.jpg"
INPUT_CSV="$RUN_DIR/results.csv"
OUTPUT_JSONL="$RUN_DIR/source_artifacts.jsonl"
mkdir -p "$IMAGE_DIR"
printf "fake acquired page image\n" > "$IMAGE_PATH"
cat > "$INPUT_CSV" <<CSV
issue_id,issue_date,page_num,preferred_image_id,preferred_image_page_url,status,output_path
torch-issue,1942-04-18,1,torch-img-001,https://www.newspapers.com/image/torch-img-001/,downloaded,$IMAGE_PATH
CSV

cd "$PROJECT_ROOT"
"$SCRAPPER_PY" -m newspaper_scrapper.cli.main build-source-artifact-manifest \
  --input-csv "$INPUT_CSV" \
  --output-jsonl "$OUTPUT_JSONL" \
  --include-status downloaded \
  --require-files \
  > "$RUN_DIR/summary.json"

"$SCRAPPER_PY" - <<PY
from __future__ import annotations

import json
from pathlib import Path

run_dir = Path("$RUN_DIR")
summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
rows = [
    json.loads(line)
    for line in (run_dir / "source_artifacts.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
status = {
    "status": "ok" if summary.get("rows_written") == 1 and rows and rows[0].get("checksum_sha256") else "error",
    "run_dir": str(run_dir),
    "summary": summary,
    "source_artifacts_jsonl": str(run_dir / "source_artifacts.jsonl"),
    "page_id": rows[0].get("page_id") if rows else "",
}
(run_dir / "slurm_status.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
print(json.dumps(status, indent=2, sort_keys=True))
raise SystemExit(0 if status["status"] == "ok" else 1)
PY
