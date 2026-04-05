from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "aws" / "archive_viewer_pngs.py"
)
SPEC = importlib.util.spec_from_file_location("archive_viewer_pngs", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_parse_image_id() -> None:
    assert MODULE.parse_image_id("165001395_viewer.png") == "165001395"


def test_canonical_key_for_filename() -> None:
    assert (
        MODULE.canonical_key_for_filename(
            "archive/viewer_png/by_image_id",
            "165001395_viewer.png",
        )
        == "archive/viewer_png/by_image_id/165/165001395_viewer.png"
    )


def test_source_group_for_s3_key() -> None:
    key = "results/screenshot-canary-adaptive-20260402/i-0example1234567890/workers/worker_01/passes/pass_01/1045682142_viewer.png"
    assert (
        MODULE.source_group_for_s3_key(key)
        == "results/screenshot-canary-adaptive-20260402/i-0example1234567890"
    )


def test_snapshot_name_for_group() -> None:
    assert (
        MODULE.snapshot_name_for_group(
            "results/screenshot-canary-adaptive-20260402/i-0example1234567890"
        )
        == "results__screenshot-canary-adaptive-20260402__i-0example1234567890"
    )
