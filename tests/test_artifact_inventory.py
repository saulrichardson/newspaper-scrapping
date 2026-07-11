from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from click.testing import CliRunner
from PIL import Image

from newspaper_scrapper.application.artifact_inventory import (
    INVENTORY_CONTRACT,
    list_s3_inventory_rows,
    reconcile_source_artifacts,
    validate_reconciliation_bundle,
)
from newspaper_scrapper.cli.main import cli


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_image(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 48), color).save(path, format="PNG")


def _source_row(
    *,
    page_id: str,
    image_path: Path,
    source_id: str,
    checksum: str,
) -> dict[str, object]:
    return {
        "page_id": page_id,
        "image_path": str(image_path),
        "issue_id": "issue-fixture",
        "page_number": 1,
        "checksum_sha256": checksum,
        "source": {
            "source_system": "fixture",
            "source_id": source_id,
            "source_url": f"https://example.test/image/{source_id}",
        },
        "metadata": {
            "contract_version": "source-artifact-v1",
            "artifact_kind": "page_image",
        },
    }


def _remote_row(
    *,
    uri: str,
    source_id: str = "",
    page_id: str = "",
    checksum: str = "",
    size_bytes: int = 100,
) -> dict[str, object]:
    return {
        "contract_version": INVENTORY_CONTRACT,
        "page_id": page_id,
        "source_id": source_id,
        "location_type": "s3",
        "uri": uri,
        "size_bytes": size_bytes,
        "checksum_sha256": checksum,
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_reconcile_source_artifacts_classifies_recovery_states(tmp_path: Path) -> None:
    ready_path = tmp_path / "images" / "ready.png"
    no_checksum_path = tmp_path / "images" / "no-checksum.png"
    corrupt_path = tmp_path / "images" / "corrupt.png"
    _write_image(ready_path, "white")
    _write_image(no_checksum_path, "gray")
    _write_image(corrupt_path, "black")
    expected_checksum = "0" * 64
    remote_checksum = "1" * 64
    rows = [
        _source_row(page_id="page-ready", image_path=ready_path, source_id="100", checksum=_sha256(ready_path)),
        _source_row(page_id="page-no-checksum", image_path=no_checksum_path, source_id="101", checksum=""),
        _source_row(page_id="page-corrupt", image_path=corrupt_path, source_id="102", checksum=expected_checksum),
        _source_row(page_id="page-remote", image_path=tmp_path / "missing-remote.png", source_id="103", checksum=remote_checksum),
        _source_row(page_id="page-missing", image_path=tmp_path / "missing.png", source_id="104", checksum=""),
        _source_row(page_id="page-duplicate", image_path=tmp_path / "missing-duplicate.png", source_id="105", checksum=remote_checksum),
        _source_row(page_id="page-conflict", image_path=tmp_path / "missing-conflict.png", source_id="106", checksum=expected_checksum),
    ]
    manifest = tmp_path / "source_artifacts.jsonl"
    _write_jsonl(manifest, rows)
    remote_inventory = tmp_path / "remote_inventory.jsonl"
    _write_jsonl(
        remote_inventory,
        [
            _remote_row(uri="s3://bucket/corrupt.png", source_id="102", checksum=expected_checksum),
            _remote_row(uri="s3://bucket/remote.png", source_id="103", checksum=remote_checksum),
            _remote_row(uri="s3://bucket/duplicate-a.png", source_id="105", checksum=remote_checksum),
            _remote_row(uri="s3://bucket/duplicate-b.png", source_id="105", checksum=remote_checksum),
            _remote_row(uri="s3://bucket/conflict.png", source_id="106", checksum="f" * 64),
            _remote_row(uri="s3://bucket/unmatched.png"),
        ],
    )

    summary = reconcile_source_artifacts(
        input_jsonl=manifest,
        output_dir=tmp_path / "reconciliation",
        remote_inventory_jsonl=[remote_inventory],
    )

    reconciliation = [
        json.loads(line)
        for line in (tmp_path / "reconciliation" / "artifact_reconciliation.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    by_page = {row["page_id"]: row for row in reconciliation}
    assert by_page["page-ready"]["classification"] == "ready_local_verified"
    assert by_page["page-no-checksum"]["recommended_action"] == "register_checksum"
    assert by_page["page-corrupt"]["classification"] == "corrupt_local_remote_recoverable"
    assert by_page["page-remote"]["recommended_action"] == "download_remote"
    assert by_page["page-missing"]["recommended_action"] == "reacquire"
    assert by_page["page-duplicate"]["classification"] == "remote_duplicate"
    assert by_page["page-conflict"]["classification"] == "remote_checksum_conflict"
    assert summary["counts"]["ready_artifacts"] == 1
    assert summary["counts"]["parser_ready_artifacts"] == 1
    assert summary["counts"]["recovery_actions"] == 6
    assert summary["counts"]["remote_download_actions"] == 2
    assert summary["counts"]["reacquire_actions"] == 1
    assert summary["counts"]["blocked_actions"] == 2
    assert summary["counts"]["unmatched_remote_rows"] == 1
    assert summary["ready_for_parsing"] is False
    parser_ready = json.loads(
        (tmp_path / "reconciliation" / "parser_ready_source_artifacts.jsonl").read_text(
            encoding="utf-8"
        )
    )
    assert parser_ready["page_id"] == "page-ready"
    with (tmp_path / "reconciliation" / "reacquire_manifest.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        reacquire_rows = list(csv.DictReader(handle))
    assert [row["page_id"] for row in reacquire_rows] == ["page-missing"]
    validation = validate_reconciliation_bundle(tmp_path / "reconciliation")
    assert validation["status"] == "ok"
    assert validation["issues"] == []


def test_list_s3_inventory_rows_paginates_and_extracts_source_ids() -> None:
    calls: list[list[str]] = []
    responses = [
        {
            "IsTruncated": True,
            "NextContinuationToken": "next-1",
            "Contents": [
                {
                    "Key": "archive/123_viewer.png",
                    "Size": 10,
                    "ETag": '"etag-a"',
                    "LastModified": "2026-01-01T00:00:00Z",
                }
            ],
        },
        {
            "IsTruncated": False,
            "Contents": [{"Key": "archive/456.jpg", "Size": 20, "ETag": '"etag-b"'}],
        },
    ]

    def fake_run(command: list[str]) -> dict[str, object]:
        calls.append(command)
        return responses.pop(0)

    rows = list_s3_inventory_rows(
        bucket="bucket-a",
        prefix="archive/",
        run_aws_json=fake_run,
    )

    assert [row["source_id"] for row in rows] == ["123", "456"]
    assert [row["uri"] for row in rows] == [
        "s3://bucket-a/archive/123_viewer.png",
        "s3://bucket-a/archive/456.jpg",
    ]
    assert "--continuation-token" not in calls[0]
    assert "--no-paginate" in calls[0]
    assert calls[1][-2:] == ["--continuation-token", "next-1"]


def test_reconciliation_cli_and_validator(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    _write_image(image_path, "white")
    manifest = tmp_path / "source_artifacts.jsonl"
    _write_jsonl(
        manifest,
        [_source_row(page_id="page-cli", image_path=image_path, source_id="200", checksum=_sha256(image_path))],
    )
    run_dir = tmp_path / "run"
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "reconcile-source-artifacts",
            "--input-jsonl",
            str(manifest),
            "--output-dir",
            str(run_dir),
        ],
    )
    validation_result = runner.invoke(
        cli,
        ["validate-artifact-reconciliation", "--run-dir", str(run_dir)],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ready_for_parsing"] is True
    assert validation_result.exit_code == 0, validation_result.output
    assert json.loads(validation_result.output)["status"] == "ok"


def test_reconciliation_refuses_nonempty_output_directory(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    _write_image(image_path, "white")
    manifest = tmp_path / "source_artifacts.jsonl"
    _write_jsonl(
        manifest,
        [_source_row(page_id="page-fresh", image_path=image_path, source_id="300", checksum=_sha256(image_path))],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "stale.txt").write_text("stale", encoding="utf-8")

    try:
        reconcile_source_artifacts(input_jsonl=manifest, output_dir=run_dir)
    except FileExistsError as exc:
        assert "must be empty or absent" in str(exc)
    else:
        raise AssertionError("expected nonempty output directory to fail")


def test_reconciliation_validator_detects_recovery_manifest_tampering(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.png"
    manifest = tmp_path / "source_artifacts.jsonl"
    _write_jsonl(
        manifest,
        [_source_row(page_id="page-tamper", image_path=missing_path, source_id="400", checksum="")],
    )
    run_dir = tmp_path / "run"
    reconcile_source_artifacts(input_jsonl=manifest, output_dir=run_dir)
    (run_dir / "recovery_manifest.jsonl").write_text("", encoding="utf-8")

    report = validate_reconciliation_bundle(run_dir)

    assert report["status"] == "error"
    assert any(issue["code"] == "recovery_manifest_mismatch" for issue in report["issues"])
