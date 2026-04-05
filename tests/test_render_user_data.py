from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_render_user_data_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "aws" / "render_user_data.py"
    )
    spec = importlib.util.spec_from_file_location("render_user_data", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_user_data_includes_dcv_flags() -> None:
    module = _load_render_user_data_module()

    user_data = module.build_user_data(
        bucket="bucket-a",
        bundle_key="bundles/app.tar.gz",
        plan_key="plans/worker.csv",
        plan_prefix="",
        cookies_key="state/cookies.json",
        output_prefix="results/test",
        bootstrap_key="bootstrap/bootstrap_newscom_worker.sh",
        sync_minutes=7,
        retry_cooldown_seconds=900,
        poll_seconds=11,
        worker_mode="screenshot",
        worker_stagger_seconds=45.0,
        max_worker_attempts=17,
        enable_dcv=True,
        dcv_session_id="newscom-shot-01",
        dcv_session_owner="ubuntu",
        dcv_password="secret-pass",
        dcv_port=8443,
        dcv_bundle_url="https://example.com/dcv.tgz",
        run_volume_id="vol-0123456789abcdef0",
        run_volume_device="",
        run_volume_label="NEWSCOM_RUN",
        run_volume_fstype="ext4",
        run_volume_wait_seconds=180,
    )

    assert "export NEWSCOM_WORKER_MODE='screenshot'" in user_data
    assert "export WORKER_STAGGER_SECONDS='45.0'" in user_data
    assert "export MAX_WORKER_ATTEMPTS='17'" in user_data
    assert "export NEWSCOM_ENABLE_DCV='true'" in user_data
    assert "export NEWSCOM_DCV_SESSION_ID='newscom-shot-01'" in user_data
    assert "export NEWSCOM_DCV_SESSION_OWNER='ubuntu'" in user_data
    assert "export NEWSCOM_DCV_PASSWORD='secret-pass'" in user_data
    assert "export NEWSCOM_DCV_PORT='8443'" in user_data
    assert "export NEWSCOM_DCV_BUNDLE_URL='https://example.com/dcv.tgz'" in user_data
    assert "export NEWSCOM_RUN_VOLUME_ID='vol-0123456789abcdef0'" in user_data
    assert "export NEWSCOM_RUN_VOLUME_DEVICE=''" in user_data
    assert "export NEWSCOM_RUN_VOLUME_LABEL='NEWSCOM_RUN'" in user_data
    assert "export NEWSCOM_RUN_VOLUME_FSTYPE='ext4'" in user_data
    assert "export NEWSCOM_RUN_VOLUME_WAIT_SECONDS='180'" in user_data
