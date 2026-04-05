#!/usr/bin/env bash
set -euo pipefail

IMAGE_URI="${IMAGE_URI:-docker://mcr.microsoft.com/playwright/python:v1.55.0-jammy}"
IMAGE_PATH="${1:-${IMAGE_PATH:-$HOME/newscom-runtime/images/playwright-python-v1.55.0-jammy.sif}}"

if ! command -v apptainer >/dev/null 2>&1; then
  echo "apptainer is required on the remote host" >&2
  exit 1
fi

mkdir -p "$(dirname "$IMAGE_PATH")"

if [[ -f "$IMAGE_PATH" ]]; then
  echo "image already present: $IMAGE_PATH"
  exit 0
fi

echo "pulling $IMAGE_URI -> $IMAGE_PATH"
apptainer pull "$IMAGE_PATH" "$IMAGE_URI"
echo "done: $IMAGE_PATH"
