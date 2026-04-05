#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

: "${NEWSCOM_BUCKET:?missing NEWSCOM_BUCKET}"
: "${NEWSCOM_BUNDLE_KEY:?missing NEWSCOM_BUNDLE_KEY}"
: "${NEWSCOM_OUTPUT_PREFIX:?missing NEWSCOM_OUTPUT_PREFIX}"

APP_USER="${APP_USER:-ubuntu}"
APP_GROUP="${APP_GROUP:-ubuntu}"
APP_HOME="/home/${APP_USER}"
APP_ROOT="${APP_ROOT:-/opt/newscom}"
APP_SRC_DIR="${APP_ROOT}/app"
APP_VENV_DIR="${APP_ROOT}/venv"
APP_DATA_DIR="${APP_ROOT}/data"
APP_STATE_DIR="${APP_ROOT}/state"
APP_RUN_DIR="${APP_ROOT}/run"
APP_PLAN_CSV="${APP_STATE_DIR}/worker_plan.csv"
APP_COOKIES_JSON="${APP_STATE_DIR}/cookies.json"
APP_BUNDLE_TGZ="${APP_ROOT}/bundle.tar.gz"
CHROME_BIN="${CHROME_BIN:-/usr/bin/google-chrome}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
CDP_PORT="${CDP_PORT:-9222}"
SYNC_MINUTES="${SYNC_MINUTES:-10}"
WORKER_STAGGER_SECONDS="${WORKER_STAGGER_SECONDS:-0}"
RETRY_COOLDOWN_SECONDS="${RETRY_COOLDOWN_SECONDS:-1800}"
MAX_WORKER_ATTEMPTS="${MAX_WORKER_ATTEMPTS:-20}"
POLL_SECONDS="${POLL_SECONDS:-5}"
NEWSCOM_WORKER_MODE="${NEWSCOM_WORKER_MODE:-search}"
NEWSCOM_ENABLE_DCV="${NEWSCOM_ENABLE_DCV:-false}"
NEWSCOM_SKIP_COOKIE_BOOTSTRAP="${NEWSCOM_SKIP_COOKIE_BOOTSTRAP:-false}"
NEWSCOM_DCV_SESSION_ID="${NEWSCOM_DCV_SESSION_ID:-newscom}"
NEWSCOM_DCV_SESSION_OWNER="${NEWSCOM_DCV_SESSION_OWNER:-${APP_USER}}"
NEWSCOM_DCV_PASSWORD="${NEWSCOM_DCV_PASSWORD:-}"
NEWSCOM_DCV_PORT="${NEWSCOM_DCV_PORT:-8443}"
NEWSCOM_DCV_BUNDLE_URL="${NEWSCOM_DCV_BUNDLE_URL:-https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-ubuntu2404-x86_64.tgz}"
NEWSCOM_RUN_VOLUME_ID="${NEWSCOM_RUN_VOLUME_ID:-}"
NEWSCOM_RUN_VOLUME_DEVICE="${NEWSCOM_RUN_VOLUME_DEVICE:-}"
NEWSCOM_RUN_VOLUME_LABEL="${NEWSCOM_RUN_VOLUME_LABEL:-NEWSCOM_RUN}"
NEWSCOM_RUN_VOLUME_FSTYPE="${NEWSCOM_RUN_VOLUME_FSTYPE:-ext4}"
NEWSCOM_RUN_VOLUME_WAIT_SECONDS="${NEWSCOM_RUN_VOLUME_WAIT_SECONDS:-120}"

export DEBIAN_FRONTEND=noninteractive

if [[ -z "${NEWSCOM_PLAN_KEY:-}" && -z "${NEWSCOM_PLAN_PREFIX:-}" ]]; then
  echo "either NEWSCOM_PLAN_KEY or NEWSCOM_PLAN_PREFIX must be set" >&2
  exit 1
fi

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  dbus-x11 \
  gpg \
  jq \
  openbox \
  python3 \
  python3-venv \
  python3-pip \
  cloud-guest-utils \
  unzip \
  xauth \
  xterm \
  xvfb

if ! command -v aws >/dev/null 2>&1; then
  tmp_dir="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "${tmp_dir}/awscliv2.zip"
  unzip -q "${tmp_dir}/awscliv2.zip" -d "${tmp_dir}"
  "${tmp_dir}/aws/install" --update
  rm -rf "${tmp_dir}"
fi

install -d -m 0755 /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/google-linux.gpg ]]; then
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | gpg --dearmor -o /etc/apt/keyrings/google-linux.gpg
fi
cat >/etc/apt/sources.list.d/google-chrome.list <<'EOF'
deb [arch=amd64 signed-by=/etc/apt/keyrings/google-linux.gpg] https://dl.google.com/linux/chrome/deb/ stable main
EOF

apt-get update
apt-get install -y google-chrome-stable

if [[ "${NEWSCOM_ENABLE_DCV}" == "true" ]]; then
  dcv_tmp_dir="$(mktemp -d)"
  curl -fsSL "${NEWSCOM_DCV_BUNDLE_URL}" -o "${dcv_tmp_dir}/nice-dcv-ubuntu2404-x86_64.tgz"
  tar -xzf "${dcv_tmp_dir}/nice-dcv-ubuntu2404-x86_64.tgz" -C "${dcv_tmp_dir}"
  dcv_bundle_dir="$(find "${dcv_tmp_dir}" -maxdepth 1 -type d -name 'nice-dcv-*ubuntu2404*' | head -n 1)"
  if [[ -z "${dcv_bundle_dir}" ]]; then
    echo "could not locate extracted DCV bundle under ${dcv_tmp_dir}" >&2
    exit 1
  fi
  dcv_server_deb="$(find "${dcv_bundle_dir}" -maxdepth 1 -type f -name 'nice-dcv-server_*_amd64.ubuntu2404.deb' | head -n 1)"
  dcv_web_deb="$(find "${dcv_bundle_dir}" -maxdepth 1 -type f -name 'nice-dcv-web-viewer_*_amd64.ubuntu2404.deb' | head -n 1)"
  dcv_xdcv_deb="$(find "${dcv_bundle_dir}" -maxdepth 1 -type f -name 'nice-xdcv_*_amd64.ubuntu2404.deb' | head -n 1)"
  if [[ -z "${dcv_server_deb}" || -z "${dcv_web_deb}" || -z "${dcv_xdcv_deb}" ]]; then
    echo "could not locate expected DCV packages under ${dcv_bundle_dir}" >&2
    find "${dcv_bundle_dir}" -maxdepth 1 -type f | sort >&2 || true
    exit 1
  fi
  apt-get install -y \
    "${dcv_server_deb}" \
    "${dcv_web_deb}" \
    "${dcv_xdcv_deb}"
  if id dcv >/dev/null 2>&1; then
    usermod -aG video dcv || true
  fi
  rm -rf "${dcv_tmp_dir}"
fi

resolve_run_volume_device() {
  local direct_device="${NEWSCOM_RUN_VOLUME_DEVICE}"
  local volume_id="${NEWSCOM_RUN_VOLUME_ID}"
  local normalized_id=""
  local waited=0
  local candidate=""

  if [[ -n "${direct_device}" ]]; then
    while [[ "${waited}" -lt "${NEWSCOM_RUN_VOLUME_WAIT_SECONDS}" ]]; do
      if [[ -b "${direct_device}" ]]; then
        readlink -f "${direct_device}"
        return 0
      fi
      sleep 2
      waited=$((waited + 2))
    done
    echo "run volume device ${direct_device} did not appear within ${NEWSCOM_RUN_VOLUME_WAIT_SECONDS}s" >&2
    return 1
  fi

  if [[ -z "${volume_id}" ]]; then
    return 1
  fi

  normalized_id="${volume_id//-/}"
  waited=0
  while [[ "${waited}" -lt "${NEWSCOM_RUN_VOLUME_WAIT_SECONDS}" ]]; do
    for candidate in \
      "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${normalized_id}" \
      "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${normalized_id}_1"; do
      if [[ -e "${candidate}" ]]; then
        readlink -f "${candidate}"
        return 0
      fi
    done
    sleep 2
    waited=$((waited + 2))
  done

  echo "run volume ${volume_id} did not appear under /dev/disk/by-id within ${NEWSCOM_RUN_VOLUME_WAIT_SECONDS}s" >&2
  return 1
}

prepare_run_volume() {
  local run_device=""
  local root_source=""
  local current_fstype=""
  local current_uuid=""
  local fstab_line=""

  if [[ -z "${NEWSCOM_RUN_VOLUME_ID}" && -z "${NEWSCOM_RUN_VOLUME_DEVICE}" ]]; then
    mkdir -p "${APP_RUN_DIR}"
    return 0
  fi

  run_device="$(resolve_run_volume_device)"
  root_source="$(findmnt -n -o SOURCE /)"

  if [[ -z "${run_device}" || ! -b "${run_device}" ]]; then
    echo "resolved run volume device is invalid: ${run_device}" >&2
    exit 1
  fi
  if [[ "${run_device}" == "${root_source}" || "${run_device}" == "${root_source%p*}" ]]; then
    echo "refusing to use root device ${run_device} as run volume" >&2
    exit 1
  fi

  current_fstype="$(blkid -o value -s TYPE "${run_device}" 2>/dev/null || true)"
  if [[ -z "${current_fstype}" ]]; then
    if [[ "${NEWSCOM_RUN_VOLUME_FSTYPE}" == "ext4" ]]; then
      mkfs.ext4 -F -L "${NEWSCOM_RUN_VOLUME_LABEL}" "${run_device}"
    else
      mkfs -t "${NEWSCOM_RUN_VOLUME_FSTYPE}" "${run_device}"
    fi
    current_fstype="${NEWSCOM_RUN_VOLUME_FSTYPE}"
  fi

  current_uuid="$(blkid -o value -s UUID "${run_device}" 2>/dev/null || true)"
  if [[ -z "${current_uuid}" ]]; then
    echo "could not determine UUID for run volume ${run_device}" >&2
    exit 1
  fi

  mkdir -p "${APP_RUN_DIR}"
  fstab_line="UUID=${current_uuid} ${APP_RUN_DIR} ${current_fstype} defaults,nofail 0 2"
  if ! grep -Fq "${fstab_line}" /etc/fstab; then
    grep -v "[[:space:]]${APP_RUN_DIR}[[:space:]]" /etc/fstab >/etc/fstab.newscom.tmp || true
    printf '%s\n' "${fstab_line}" >>/etc/fstab.newscom.tmp
    mv /etc/fstab.newscom.tmp /etc/fstab
  fi

  if findmnt -n "${APP_RUN_DIR}" >/dev/null 2>&1; then
    umount "${APP_RUN_DIR}" || true
  fi
  mount "${APP_RUN_DIR}"
}

clear_directory_contents() {
  local target_dir="$1"
  mkdir -p "${target_dir}"
  find "${target_dir}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
}

mkdir -p "${APP_ROOT}" "${APP_DATA_DIR}" "${APP_STATE_DIR}"
prepare_run_volume
clear_directory_contents "${APP_RUN_DIR}"
rm -rf "${APP_STATE_DIR}" "${APP_DATA_DIR}/chrome_profiles" "${APP_DATA_DIR}/chrome_profile"
mkdir -p "${APP_STATE_DIR}"
chown -R "${APP_USER}:${APP_GROUP}" "${APP_ROOT}"

sudo -u "${APP_USER}" aws s3 cp "s3://${NEWSCOM_BUCKET}/${NEWSCOM_BUNDLE_KEY}" "${APP_BUNDLE_TGZ}"
rm -rf "${APP_SRC_DIR}"
mkdir -p "${APP_SRC_DIR}"
tar -xzf "${APP_BUNDLE_TGZ}" -C "${APP_SRC_DIR}" --strip-components=1
chown -R "${APP_USER}:${APP_GROUP}" "${APP_SRC_DIR}"
if [[ ! -f "${APP_SRC_DIR}/pyproject.toml" ]]; then
  echo "bundle extraction failed: ${APP_SRC_DIR}/pyproject.toml not found" >&2
  find "${APP_SRC_DIR}" -maxdepth 2 -type f | sort >&2 || true
  exit 1
fi

sudo -u "${APP_USER}" python3 -m venv "${APP_VENV_DIR}"
sudo -u "${APP_USER}" "${APP_VENV_DIR}/bin/pip" install --upgrade pip
sudo -u "${APP_USER}" "${APP_VENV_DIR}/bin/pip" install "${APP_SRC_DIR}"

if [[ -n "${NEWSCOM_PLAN_PREFIX:-}" ]]; then
  sudo -u "${APP_USER}" aws s3 sync "s3://${NEWSCOM_BUCKET}/${NEWSCOM_PLAN_PREFIX}" "${APP_STATE_DIR}"
else
  sudo -u "${APP_USER}" aws s3 cp "s3://${NEWSCOM_BUCKET}/${NEWSCOM_PLAN_KEY}" "${APP_PLAN_CSV}"
fi
if [[ -n "${NEWSCOM_COOKIES_KEY:-}" ]]; then
  sudo -u "${APP_USER}" aws s3 cp "s3://${NEWSCOM_BUCKET}/${NEWSCOM_COOKIES_KEY}" "${APP_COOKIES_JSON}"
fi

if [[ -n "${NEWSCOM_DCV_PASSWORD}" ]]; then
  echo "${NEWSCOM_DCV_SESSION_OWNER}:${NEWSCOM_DCV_PASSWORD}" | chpasswd
fi

if [[ "${NEWSCOM_ENABLE_DCV}" == "true" ]]; then
  systemctl enable dcvserver >/dev/null 2>&1 || true
  systemctl start dcvserver
fi

# Rewrite worker paths so every instance keeps its outputs and browser profiles
# under the managed app root, which is also what the sync timer uploads.
APP_PLAN_CSV="${APP_PLAN_CSV}" APP_RUN_DIR="${APP_RUN_DIR}" APP_DATA_DIR="${APP_DATA_DIR}" APP_STATE_DIR="${APP_STATE_DIR}" \
python3 - <<'PY'
import csv
import os
from pathlib import Path

plan_csv = Path(os.environ["APP_PLAN_CSV"])
run_dir = Path(os.environ["APP_RUN_DIR"])
data_dir = Path(os.environ["APP_DATA_DIR"])
state_dir = Path(os.environ["APP_STATE_DIR"])

with plan_csv.open(newline="") as handle:
    reader = csv.DictReader(handle)
    fieldnames = list(reader.fieldnames or [])
    rows = list(reader)

if not fieldnames:
    raise SystemExit(f"no header found in {plan_csv}")

for row in rows:
    worker_name = str(row.get("worker_name", "")).strip()
    if not worker_name:
        raise SystemExit(f"worker_name missing in {plan_csv}")
    row["output_dir"] = str(run_dir / "workers" / worker_name)
    row["chrome_profile_dir"] = str(data_dir / "chrome_profiles" / worker_name)
    if "manifest_csv" in row:
        row["manifest_csv"] = str(state_dir / "workers" / worker_name / "input_manifest.csv")

tmp_csv = plan_csv.with_suffix(".tmp")
with tmp_csv.open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
tmp_csv.replace(plan_csv)
PY
chown "${APP_USER}:${APP_GROUP}" "${APP_PLAN_CSV}"

cat >"${APP_ROOT}/dcv-session-init.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail

export HOME="${APP_HOME}"
export USER="${NEWSCOM_DCV_SESSION_OWNER}"

if command -v dbus-launch >/dev/null 2>&1; then
  eval "\$(dbus-launch --sh-syntax)"
fi

if command -v xsetroot >/dev/null 2>&1; then
  xsetroot -solid "#1f1f1f"
fi

if command -v openbox-session >/dev/null 2>&1; then
  openbox-session &
fi

if command -v xterm >/dev/null 2>&1; then
  xterm -geometry 100x20+20+20 -title "newscom-worker" &
fi

wait
EOF
chmod 0755 "${APP_ROOT}/dcv-session-init.sh"
chown "${APP_USER}:${APP_GROUP}" "${APP_ROOT}/dcv-session-init.sh"

cat >"${APP_ROOT}/ensure_dcv_session.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail

SESSION_ID="${NEWSCOM_DCV_SESSION_ID}"
SESSION_OWNER="${NEWSCOM_DCV_SESSION_OWNER}"
SESSION_JSON="${APP_STATE_DIR}/dcv_session.json"
SESSION_ENV="${APP_STATE_DIR}/dcv_session.env"
CONNECTION_JSON="${APP_STATE_DIR}/dcv_connection.json"

if ! dcv describe-session "\${SESSION_ID}" -j >/dev/null 2>&1; then
  dcv create-session \\
    --type virtual \\
    --name "newscom screenshot worker" \\
    --max-concurrent-clients 2 \\
    --init "${APP_ROOT}/dcv-session-init.sh" \\
    "\${SESSION_ID}"
fi

session_ready="false"
for _ in \$(seq 1 30); do
  dcv describe-session "\${SESSION_ID}" -j >"\${SESSION_JSON}"
  session_status="\$(jq -r '.status // empty' "\${SESSION_JSON}")"
  session_display="\$(jq -r '."x11-display" // empty' "\${SESSION_JSON}")"
  session_authority="\$(jq -r '."x11-authority" // empty' "\${SESSION_JSON}")"
  if [[ "\${session_status}" == "running" && -n "\${session_display}" && -n "\${session_authority}" ]]; then
    session_ready="true"
    break
  fi
  sleep 2
done

if [[ "\${session_ready}" != "true" ]]; then
  echo "DCV session \${SESSION_ID} did not become ready" >&2
  cat "\${SESSION_JSON}" >&2 || true
  exit 1
fi

python3 - "\${SESSION_JSON}" "\${SESSION_ENV}" "\${CONNECTION_JSON}" "${NEWSCOM_DCV_PORT}" "\${SESSION_ID}" <<'PY'
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

session_json = Path(sys.argv[1])
session_env = Path(sys.argv[2])
connection_json = Path(sys.argv[3])
dcv_port = sys.argv[4]
session_id = sys.argv[5]

payload = json.loads(session_json.read_text())
x_display = str(payload.get("x11-display", "")).strip()
x_authority = str(payload.get("x11-authority", "")).strip()
if not x_display or not x_authority:
    raise SystemExit(f"missing DCV display metadata in {session_json}")

public_ip = ""
try:
    token_request = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
    )
    with urllib.request.urlopen(token_request, timeout=2) as response:
        token = response.read().decode("utf-8")

    ip_request = urllib.request.Request(
        "http://169.254.169.254/latest/meta-data/public-ipv4",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with urllib.request.urlopen(ip_request, timeout=2) as response:
        public_ip = response.read().decode("utf-8").strip()
except (OSError, urllib.error.URLError):
    public_ip = ""

session_env.write_text(
    "\n".join(
        [
            f'export NEWSCOM_DCV_DISPLAY="{x_display}"',
            f'export NEWSCOM_DCV_XAUTHORITY="{x_authority}"',
            "",
        ]
    )
)
connection_json.write_text(
    json.dumps(
        {
            "session_id": session_id,
            "public_ip": public_ip,
            "web_url": f"https://{public_ip}:{dcv_port}/#{session_id}" if public_ip else "",
            "x11_display": x_display,
            "x11_authority": x_authority,
        },
        indent=2,
        sort_keys=True,
    )
)
PY
EOF
chmod 0755 "${APP_ROOT}/ensure_dcv_session.sh"

cat >"${APP_ROOT}/run_newscom_worker.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail

export HOME="${APP_HOME}"
export NEWSCOM_CHROME_APP_NAME="Google Chrome"
export NEWSCOM_CHROME_APP_PATH="${CHROME_BIN}"
export NEWSCOM_CHROME_DEBUG_PORT="${CDP_PORT}"
export NEWSCOM_CHROME_PROFILE_DIR="${APP_DATA_DIR}/chrome_profile"
export NEWSCOM_DATA_DIR="${APP_DATA_DIR}"
export PYTHONPATH="${APP_SRC_DIR}/src"
export NEWSCOM_BROWSER_START_TIMEOUT_SECONDS="60"
export NEWSCOM_DCV_SESSION_ID="${NEWSCOM_DCV_SESSION_ID}"
export NEWSCOM_DCV_SESSION_OWNER="${NEWSCOM_DCV_SESSION_OWNER}"
export NEWSCOM_DCV_PORT="${NEWSCOM_DCV_PORT}"

mkdir -p "${APP_DATA_DIR}" "${APP_RUN_DIR}"

if [[ "${NEWSCOM_ENABLE_DCV}" == "true" ]]; then
  "${APP_ROOT}/ensure_dcv_session.sh"
  # shellcheck disable=SC1091
  source "${APP_STATE_DIR}/dcv_session.env"
  export DISPLAY="\${NEWSCOM_DCV_DISPLAY}"
  export XAUTHORITY="\${NEWSCOM_DCV_XAUTHORITY}"
else
  export DISPLAY=":${DISPLAY_NUM}"
  pkill -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1 || true
  Xvfb ":${DISPLAY_NUM}" -screen 0 1600x1200x24 -nolisten tcp -ac >"${APP_ROOT}/newscom-xvfb.log" 2>&1 &
  sleep 3
fi

command=(
  "${APP_VENV_DIR}/bin/python"
  -m
  newspaper_scrapper.cli.main
)
if [[ "${NEWSCOM_WORKER_MODE}" == "screenshot" ]]; then
  command+=(run-screenshot-workers)
else
  command+=(run-search-workers)
fi
command+=(
  --plan-csv "${APP_PLAN_CSV}"
  --output-dir "${APP_RUN_DIR}"
  --max-concurrent-workers 1
  --worker-stagger-seconds "${WORKER_STAGGER_SECONDS}"
  --retry-cooldown-seconds "${RETRY_COOLDOWN_SECONDS}"
  --max-worker-attempts "${MAX_WORKER_ATTEMPTS}"
  --poll-seconds "${POLL_SECONDS}"
)
if [[ "${NEWSCOM_WORKER_MODE}" == "screenshot" ]]; then
  command+=(--stop-on-cloudflare-challenge)
fi
if [[ -f "${APP_COOKIES_JSON}" ]]; then
  if [[ "${NEWSCOM_SKIP_COOKIE_BOOTSTRAP}" != "true" ]]; then
    command+=(--cookies-json "${APP_COOKIES_JSON}")
  fi
fi

exec "\${command[@]}"
EOF
chmod 0755 "${APP_ROOT}/run_newscom_worker.sh"

cat >"${APP_ROOT}/sync_newscom_outputs.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail

TOKEN=\$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=\$(curl -sS -H "X-aws-ec2-metadata-token: \${TOKEN}" http://169.254.169.254/latest/meta-data/instance-id)

aws s3 sync "${APP_RUN_DIR}" "s3://${NEWSCOM_BUCKET}/${NEWSCOM_OUTPUT_PREFIX}/\${INSTANCE_ID}/run" --exact-timestamps --delete
aws s3 sync "${APP_STATE_DIR}" "s3://${NEWSCOM_BUCKET}/${NEWSCOM_OUTPUT_PREFIX}/\${INSTANCE_ID}/state" --exact-timestamps --delete
if [[ -f "${APP_COOKIES_JSON}" ]]; then
  aws s3 cp "${APP_COOKIES_JSON}" "s3://${NEWSCOM_BUCKET}/${NEWSCOM_OUTPUT_PREFIX}/\${INSTANCE_ID}/state/cookies.json"
fi
EOF
chmod 0755 "${APP_ROOT}/sync_newscom_outputs.sh"

cat >/etc/systemd/system/newscom-worker.service <<EOF
[Unit]
Description=Newspapers.com worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_SRC_DIR}
ExecStart=${APP_ROOT}/run_newscom_worker.sh
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/newscom-sync.service <<EOF
[Unit]
Description=Sync Newspapers.com worker outputs to S3
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${APP_USER}
Group=${APP_GROUP}
ExecStart=${APP_ROOT}/sync_newscom_outputs.sh
EOF

cat >/etc/systemd/system/newscom-sync.timer <<EOF
[Unit]
Description=Periodic sync of Newspapers.com worker outputs to S3

[Timer]
OnBootSec=5min
OnUnitActiveSec=${SYNC_MINUTES}min
Unit=newscom-sync.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable newscom-worker.service
systemctl enable newscom-sync.timer
systemctl start newscom-worker.service
systemctl start newscom-sync.timer
