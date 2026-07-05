from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from click.testing import CliRunner

from newspaper_scrapper.application.source_manifest import (
    build_page_id,
    write_source_artifact_manifest,
)
from newspaper_scrapper.cli.main import cli


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "issue_id",
        "issue_date",
        "page_num",
        "preferred_image_id",
        "preferred_image_page_url",
        "status",
        "output_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_source_artifact_manifest_is_parser_compatible(tmp_path: Path) -> None:
    image_path = tmp_path / "images" / "issue-a" / "0001__img-001.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"fake-image-bytes")
    input_csv = tmp_path / "results.csv"
    _write_csv(
        input_csv,
        [
            {
                "issue_id": "issue-a",
                "issue_date": "1942-04-18",
                "page_num": "1",
                "preferred_image_id": "img-001",
                "preferred_image_page_url": "https://www.newspapers.com/image/img-001/",
                "status": "downloaded",
                "output_path": str(image_path),
            }
        ],
    )
    output_jsonl = tmp_path / "source_artifacts.jsonl"

    summary = write_source_artifact_manifest(
        input_csv=input_csv,
        output_jsonl=output_jsonl,
        include_statuses={"downloaded"},
        require_files=True,
    )

    row = json.loads(output_jsonl.read_text(encoding="utf-8"))
    assert summary["rows_written"] == 1
    assert row["page_id"] == "issue-a__p0001__img-001"
    assert row["image_path"] == str(image_path)
    assert row["page_number"] == 1
    assert row["checksum_sha256"] == hashlib.sha256(b"fake-image-bytes").hexdigest()
    assert row["source"]["source_system"] == "newspapers.com"
    assert row["source"]["source_url"] == "https://www.newspapers.com/image/img-001/"
    assert row["metadata"]["contract_version"] == "source-artifact-v1"


def test_source_artifact_manifest_cli(tmp_path: Path) -> None:
    image_path = tmp_path / "issue-b" / "0002__22.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"x")
    input_csv = tmp_path / "results.csv"
    _write_csv(
        input_csv,
        [
            {
                "issue_id": "issue-b",
                "issue_date": "1950-01-01",
                "page_num": "2",
                "preferred_image_id": "22",
                "preferred_image_page_url": "https://www.newspapers.com/image/22/",
                "status": "downloaded",
                "output_path": str(image_path),
            }
        ],
    )
    output_jsonl = tmp_path / "manifest.jsonl"
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "build-source-artifact-manifest",
            "--input-csv",
            str(input_csv),
            "--output-jsonl",
            str(output_jsonl),
            "--include-status",
            "downloaded",
            "--require-files",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["rows_written"] == 1
    assert output_jsonl.exists()


def test_build_page_id_is_stable_for_non_numeric_page() -> None:
    assert (
        build_page_id(
            {
                "issue_id": "Cambridge Sentinel / 1942",
                "page_num": "A-1",
                "preferred_image_id": "image 12",
            }
        )
        == "Cambridge-Sentinel-1942__A-1__image-12"
    )
