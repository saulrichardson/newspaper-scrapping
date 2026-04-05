#!/usr/bin/env bash
set -euo pipefail

IMAGE_PATH="${IMAGE_PATH:-$HOME/newscom-runtime/images/playwright-python-v1.55.0-jammy.sif}"
SESSION_ROOT="${SESSION_ROOT:-$HOME/newscom-runtime/session}"
PROFILE_DIR="${PROFILE_DIR:-$SESSION_ROOT/profile}"
RUNTIME_DIR="${RUNTIME_DIR:-$SESSION_ROOT/runtime}"
LOG_DIR="${LOG_DIR:-$SESSION_ROOT/logs}"
START_URL="${START_URL:-https://www.newspapers.com/}"
DEBUG_PORT="${DEBUG_PORT:-9222}"
WINDOW_WIDTH="${WINDOW_WIDTH:-1280}"
WINDOW_HEIGHT="${WINDOW_HEIGHT:-720}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"

if ! command -v apptainer >/dev/null 2>&1; then
  echo "apptainer is required" >&2
  exit 1
fi

if [[ ! -f "$IMAGE_PATH" ]]; then
  echo "missing browser image: $IMAGE_PATH" >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR" "$RUNTIME_DIR/tmp" "$RUNTIME_DIR/home" "$LOG_DIR"

export WINDOW_WIDTH WINDOW_HEIGHT DEBUG_PORT DISPLAY_NUM START_URL

CONTAINER_SCRIPT=$(cat <<'SH'
set -eu
CHROME_BIN=""
for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
  if command -v "$candidate" >/dev/null 2>&1; then
    CHROME_BIN="$candidate"
    break
  fi
done
if [ -z "$CHROME_BIN" ] && [ -d /ms-playwright ]; then
  CHROME_BIN="$(find /ms-playwright -path '*/chrome-linux/chrome' | head -n 1 || true)"
fi
if [ -z "$CHROME_BIN" ]; then
  echo "no chrome/chromium binary found inside image" >&2
  exit 1
fi

export HOME=/runtime/home
export TMPDIR=/runtime/tmp
mkdir -p "$HOME" "$TMPDIR" /logs

CHROME_ARGS="
  --no-sandbox
  --disable-dev-shm-usage
  --disable-gpu
  --use-gl=swiftshader
  --disable-breakpad
  --disable-background-networking
  --disable-component-update
  --disable-default-apps
  --disable-extensions
  --disable-sync
  --no-first-run
  --no-default-browser-check
  --window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT}
  --remote-debugging-address=127.0.0.1
  --remote-debugging-port=${DEBUG_PORT}
  --user-data-dir=/profile
  ${START_URL}
"

if command -v xvfb-run >/dev/null 2>&1; then
  exec xvfb-run -a -s "-screen 0 ${WINDOW_WIDTH}x${WINDOW_HEIGHT}x24 -nolisten tcp -ac" \
    "$CHROME_BIN" $CHROME_ARGS
fi

if command -v Xvfb >/dev/null 2>&1; then
  export DISPLAY=":${DISPLAY_NUM}"
  Xvfb "$DISPLAY" -screen 0 "${WINDOW_WIDTH}x${WINDOW_HEIGHT}x24" -nolisten tcp -ac >/logs/xvfb.log 2>&1 &
  XVFB_PID=$!
  trap 'kill "$XVFB_PID"' EXIT
  exec "$CHROME_BIN" $CHROME_ARGS
fi

echo "neither xvfb-run nor Xvfb is available inside image" >&2
exit 1
SH
)

apptainer exec \
  --bind "$PROFILE_DIR:/profile" \
  --bind "$RUNTIME_DIR:/runtime" \
  --bind "$LOG_DIR:/logs" \
  "$IMAGE_PATH" \
  /bin/sh -lc "$CONTAINER_SCRIPT"
