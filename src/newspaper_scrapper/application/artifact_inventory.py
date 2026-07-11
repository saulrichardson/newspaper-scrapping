"""Artifact inventory snapshots, reconciliation, and recovery planning."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

from PIL import Image

from newspaper_scrapper.application.source_manifest import validate_source_artifact_manifest


INVENTORY_CONTRACT = "artifact-inventory-item-v1"
RECONCILIATION_CONTRACT = "artifact-reconciliation-v1"
RECOVERY_CONTRACT = "artifact-recovery-action-v1"
RECONCILIATION_SUMMARY_CONTRACT = "artifact-reconciliation-summary-v1"
RECONCILIATION_VALIDATION_CONTRACT = "artifact-reconciliation-validation-v1"
DEFAULT_SOURCE_ID_PATTERN = r"(?P<source_id>[0-9]+)(?:_|\.|$)"
ALLOWED_ACTIONS = {
    "none",
    "download_remote",
    "reacquire",
    "register_checksum",
    "verify_local",
    "review_checksum_conflict",
    "review_remote_duplicates",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            count += 1
    return count


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(payload)
    return rows


def _require_fresh_directory(path: Path) -> Path:
    root = path.expanduser().resolve()
    if root.exists():
        if not root.is_dir():
            raise NotADirectoryError(f"output path is not a directory: {root}")
        if any(root.iterdir()):
            raise FileExistsError(f"output directory must be empty or absent: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_aws_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"AWS CLI failed with exit {completed.returncode}: {detail[-2000:]}")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AWS CLI returned invalid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AWS CLI response must be a JSON object")
    return payload


def _extract_source_id(key: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(Path(key).name)
    if match is None:
        return ""
    if "source_id" in pattern.groupindex:
        return str(match.group("source_id") or "")
    if match.groups():
        return str(match.group(1) or "")
    return str(match.group(0) or "")


def list_s3_inventory_rows(
    *,
    bucket: str,
    prefix: str = "",
    source_id_pattern: str = DEFAULT_SOURCE_ID_PATTERN,
    aws_cli: str = "aws",
    run_aws_json: Callable[[list[str]], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not bucket.strip():
        raise ValueError("bucket is required")
    try:
        compiled_pattern = re.compile(source_id_pattern)
    except re.error as exc:
        raise ValueError(f"invalid source ID regex: {exc}") from exc
    runner = run_aws_json or _run_aws_json
    rows: list[dict[str, Any]] = []
    continuation_token = ""
    seen_tokens: set[str] = set()

    while True:
        command = [
            aws_cli,
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--no-paginate",
            "--output",
            "json",
        ]
        if continuation_token:
            command.extend(["--continuation-token", continuation_token])
        payload = runner(command)
        contents = payload.get("Contents") or []
        if not isinstance(contents, list):
            raise RuntimeError("AWS list-objects-v2 Contents must be a list")
        for item in contents:
            if not isinstance(item, dict):
                continue
            key = str(item.get("Key") or "")
            if not key:
                continue
            checksum_algorithms = item.get("ChecksumAlgorithm") or []
            rows.append(
                {
                    "contract_version": INVENTORY_CONTRACT,
                    "page_id": "",
                    "source_id": _extract_source_id(key, compiled_pattern),
                    "location_type": "s3",
                    "uri": f"s3://{bucket}/{key}",
                    "bucket": bucket,
                    "storage_key": key,
                    "size_bytes": int(item.get("Size") or 0),
                    "etag": str(item.get("ETag") or "").strip('"'),
                    "checksum_sha256": "",
                    "last_modified": str(item.get("LastModified") or ""),
                    "metadata": {
                        "storage_class": str(item.get("StorageClass") or ""),
                        "checksum_algorithms": (
                            [str(value) for value in checksum_algorithms]
                            if isinstance(checksum_algorithms, list)
                            else []
                        ),
                        "s3_checksum_sha256_base64": str(item.get("ChecksumSHA256") or ""),
                    },
                }
            )

        if not bool(payload.get("IsTruncated")):
            break
        next_token = str(payload.get("NextContinuationToken") or "")
        if not next_token:
            raise RuntimeError("AWS inventory response was truncated without a continuation token")
        if next_token in seen_tokens:
            raise RuntimeError("AWS inventory pagination repeated a continuation token")
        seen_tokens.add(next_token)
        continuation_token = next_token

    return rows


def write_s3_inventory_snapshot(
    *,
    bucket: str,
    prefix: str,
    output_jsonl: Path,
    source_id_pattern: str = DEFAULT_SOURCE_ID_PATTERN,
    aws_cli: str = "aws",
) -> dict[str, Any]:
    destination = output_jsonl.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"inventory output already exists: {destination}")
    rows = list_s3_inventory_rows(
        bucket=bucket,
        prefix=prefix,
        source_id_pattern=source_id_pattern,
        aws_cli=aws_cli,
    )
    _write_jsonl(destination, rows)
    summary = {
        "contract_version": "artifact-inventory-summary-v1",
        "location_type": "s3",
        "bucket": bucket,
        "prefix": prefix,
        "rows": len(rows),
        "rows_with_source_id": sum(1 for row in rows if row["source_id"]),
        "bytes_total": sum(int(row["size_bytes"]) for row in rows),
        "output_jsonl": str(destination),
    }
    _write_json(destination.with_suffix(destination.suffix + ".summary.json"), summary)
    return summary


def _resolve_local_path(manifest_path: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _normalize_remote_entry(row: dict[str, Any], *, source_path: Path) -> dict[str, Any]:
    if row.get("contract_version") != INVENTORY_CONTRACT:
        raise ValueError(f"{source_path}: remote inventory row has an invalid contract_version")
    page_id = str(row.get("page_id") or "").strip()
    source_id = str(row.get("source_id") or "").strip()
    uri = str(row.get("uri") or "").strip()
    if not uri:
        raise ValueError(f"{source_path}: remote inventory row is missing uri")
    checksum = str(row.get("checksum_sha256") or "").strip().lower()
    if checksum and not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError(f"{source_path}: remote inventory checksum_sha256 is invalid")
    try:
        size_bytes = int(row.get("size_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{source_path}: remote inventory size_bytes is invalid") from exc
    if size_bytes < 0:
        raise ValueError(f"{source_path}: remote inventory size_bytes cannot be negative")
    return {
        "contract_version": INVENTORY_CONTRACT,
        "page_id": page_id,
        "source_id": source_id,
        "location_type": str(row.get("location_type") or "remote"),
        "uri": uri,
        "bucket": str(row.get("bucket") or ""),
        "storage_key": str(row.get("storage_key") or ""),
        "size_bytes": size_bytes,
        "etag": str(row.get("etag") or ""),
        "checksum_sha256": checksum,
        "last_modified": str(row.get("last_modified") or ""),
        "metadata": dict(row.get("metadata") or {}),
    }


def _classify_artifact(
    *,
    local_exists: bool,
    expected_checksum: str,
    actual_checksum: str,
    local_image_valid: bool | None,
    remote_candidates: list[dict[str, Any]],
    verify_local_checksums: bool,
) -> tuple[str, str, str]:
    remote_checksums = {
        str(candidate.get("checksum_sha256") or "").lower()
        for candidate in remote_candidates
        if candidate.get("checksum_sha256")
    }
    remote_checksum_conflict = bool(
        expected_checksum
        and remote_checksums
        and any(checksum != expected_checksum for checksum in remote_checksums)
    )

    if local_exists:
        if local_image_valid is False:
            if remote_checksum_conflict:
                return (
                    "checksum_conflict",
                    "review_checksum_conflict",
                    "local image is invalid and remote checksums conflict with the manifest",
                )
            if len(remote_candidates) == 1:
                return (
                    "corrupt_local_remote_recoverable",
                    "download_remote",
                    "local image cannot be decoded and one remote replacement is available",
                )
            if len(remote_candidates) > 1:
                return (
                    "corrupt_local_remote_ambiguous",
                    "review_remote_duplicates",
                    "local image cannot be decoded and multiple remote replacements are available",
                )
            return "corrupt_local", "reacquire", "local image cannot be decoded"
        if not expected_checksum:
            if not verify_local_checksums:
                return (
                    "ready_local_unverified",
                    "verify_local",
                    "local artifact exists but checksum verification was disabled",
                )
            return (
                "ready_local_needs_checksum",
                "register_checksum",
                "local artifact exists but the source manifest has no checksum",
            )
        if not verify_local_checksums:
            return (
                "ready_local_unverified",
                "verify_local",
                "local artifact exists but checksum verification was disabled",
            )
        if actual_checksum == expected_checksum:
            return "ready_local_verified", "none", "local artifact checksum matches the manifest"
        if remote_checksum_conflict:
            return (
                "checksum_conflict",
                "review_checksum_conflict",
                "local and remote checksums do not support the expected checksum",
            )
        if len(remote_candidates) == 1:
            return (
                "corrupt_local_remote_recoverable",
                "download_remote",
                "local checksum is wrong and one remote replacement is available",
            )
        if len(remote_candidates) > 1:
            return (
                "corrupt_local_remote_ambiguous",
                "review_remote_duplicates",
                "local checksum is wrong and multiple remote replacements are available",
            )
        return "corrupt_local", "reacquire", "local checksum is wrong and no remote replacement exists"

    if not remote_candidates:
        return "missing", "reacquire", "artifact is absent locally and from remote inventory"
    if remote_checksum_conflict:
        return (
            "remote_checksum_conflict",
            "review_checksum_conflict",
            "remote artifact checksums conflict with the source manifest",
        )
    if len(remote_candidates) == 1:
        return (
            "remote_recoverable",
            "download_remote",
            "artifact is absent locally and one remote copy is available",
        )

    signatures = {
        (str(candidate.get("checksum_sha256") or ""), int(candidate.get("size_bytes") or 0))
        for candidate in remote_candidates
    }
    if len(signatures) == 1 and next(iter(signatures))[0]:
        classification = "remote_duplicate"
        reason = "multiple remote locations contain the same apparent artifact"
    else:
        classification = "remote_conflict"
        reason = "multiple remote locations disagree or cannot be proven identical"
    return classification, "review_remote_duplicates", reason


def reconcile_source_artifacts(
    *,
    input_jsonl: Path,
    output_dir: Path,
    remote_inventory_jsonl: Iterable[Path] = (),
    verify_local_checksums: bool = True,
    verify_image_decode: bool = True,
) -> dict[str, Any]:
    manifest = input_jsonl.expanduser().resolve()
    root = _require_fresh_directory(output_dir)
    structural_validation = validate_source_artifact_manifest(input_jsonl=manifest)
    _write_json(root / "source_manifest_validation.json", structural_validation)
    if structural_validation["status"] == "error":
        raise ValueError(f"source artifact manifest is structurally invalid: {manifest}")

    expected_rows = _read_jsonl(manifest)
    inventory_paths = [path.expanduser().resolve() for path in remote_inventory_jsonl]
    remote_by_uri: dict[str, dict[str, Any]] = {}
    duplicate_inventory_rows = 0
    for inventory_path in inventory_paths:
        for raw in _read_jsonl(inventory_path):
            normalized = _normalize_remote_entry(raw, source_path=inventory_path)
            if normalized["uri"] in remote_by_uri:
                duplicate_inventory_rows += 1
            remote_by_uri[normalized["uri"]] = normalized

    remote_by_page: dict[str, list[dict[str, Any]]] = defaultdict(list)
    remote_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for remote in remote_by_uri.values():
        if remote["page_id"]:
            remote_by_page[remote["page_id"]].append(remote)
        if remote["source_id"]:
            remote_by_source[remote["source_id"]].append(remote)

    reconciliation_rows: list[dict[str, Any]] = []
    recovery_rows: list[dict[str, Any]] = []
    parser_ready_rows: list[dict[str, Any]] = []
    matched_remote_uris: set[str] = set()
    priority_by_action = {
        "review_checksum_conflict": 10,
        "review_remote_duplicates": 20,
        "download_remote": 30,
        "reacquire": 40,
        "register_checksum": 50,
        "verify_local": 60,
    }

    for expected in expected_rows:
        page_id = str(expected.get("page_id") or "")
        source = expected.get("source") if isinstance(expected.get("source"), dict) else {}
        source_id = str(source.get("source_id") or "")
        source_url = str(source.get("source_url") or "")
        expected_metadata = (
            expected.get("metadata") if isinstance(expected.get("metadata"), dict) else {}
        )
        expected_checksum = str(expected.get("checksum_sha256") or "").lower()
        local_path = _resolve_local_path(manifest, str(expected.get("image_path") or ""))
        local_exists = local_path.is_file()
        actual_checksum = _sha256_file(local_path) if local_exists and verify_local_checksums else ""
        local_size = local_path.stat().st_size if local_exists else 0
        local_image_valid: bool | None = None
        local_image_error = ""
        image_format = ""
        image_width = 0
        image_height = 0
        if local_exists and verify_image_decode:
            try:
                with Image.open(local_path) as image:
                    image_format = str(image.format or "")
                    image_width, image_height = image.size
                    image.verify()
                local_image_valid = True
            except Exception as exc:
                local_image_valid = False
                local_image_error = f"{type(exc).__name__}: {exc}"

        candidates_by_uri: dict[str, dict[str, Any]] = {}
        for candidate in remote_by_page.get(page_id, []):
            candidates_by_uri[candidate["uri"]] = candidate
        for candidate in remote_by_source.get(source_id, []) if source_id else []:
            candidates_by_uri[candidate["uri"]] = candidate
        remote_candidates = sorted(candidates_by_uri.values(), key=lambda row: row["uri"])
        matched_remote_uris.update(candidate["uri"] for candidate in remote_candidates)

        classification, action, reason = _classify_artifact(
            local_exists=local_exists,
            expected_checksum=expected_checksum,
            actual_checksum=actual_checksum,
            local_image_valid=local_image_valid,
            remote_candidates=remote_candidates,
            verify_local_checksums=verify_local_checksums,
        )
        reconciliation_rows.append(
            {
                "contract_version": RECONCILIATION_CONTRACT,
                "page_id": page_id,
                "issue_id": str(expected.get("issue_id") or ""),
                "page_number": expected.get("page_number"),
                "source_id": source_id,
                "source_url": source_url,
                "classification": classification,
                "recommended_action": action,
                "reason": reason,
                "expected": {
                    "image_path": str(expected.get("image_path") or ""),
                    "checksum_sha256": expected_checksum,
                },
                "local": {
                    "path": str(local_path),
                    "exists": local_exists,
                    "size_bytes": local_size,
                    "checksum_sha256": actual_checksum,
                    "checksum_matches": (
                        actual_checksum == expected_checksum
                        if actual_checksum and expected_checksum
                        else None
                    ),
                    "image_valid": local_image_valid,
                    "image_format": image_format,
                    "image_width": image_width,
                    "image_height": image_height,
                    "image_error": local_image_error,
                },
                "remote": {
                    "match_count": len(remote_candidates),
                    "candidates": remote_candidates,
                },
            }
        )
        if action == "none":
            parser_ready = dict(expected)
            parser_ready["image_path"] = str(local_path)
            parser_ready["checksum_sha256"] = actual_checksum or expected_checksum
            parser_ready_metadata = dict(expected_metadata)
            parser_ready_metadata["reconciliation_classification"] = classification
            parser_ready_metadata["reconciliation_contract_version"] = RECONCILIATION_CONTRACT
            parser_ready["metadata"] = parser_ready_metadata
            parser_ready_rows.append(parser_ready)
        if action != "none":
            remote_uri = remote_candidates[0]["uri"] if len(remote_candidates) == 1 else ""
            recovery_rows.append(
                {
                    "contract_version": RECOVERY_CONTRACT,
                    "page_id": page_id,
                    "source_id": source_id,
                    "source_url": source_url,
                    "issue_id": str(expected.get("issue_id") or ""),
                    "issue_date": str(expected_metadata.get("issue_date") or ""),
                    "page_num": str(expected.get("page_number") or ""),
                    "preferred_image_id": source_id,
                    "preferred_image_page_url": source_url,
                    "action": action,
                    "priority": priority_by_action[action],
                    "classification": classification,
                    "reason": reason,
                    "target_path": str(local_path),
                    "remote_uri": remote_uri,
                    "expected_checksum_sha256": expected_checksum,
                    "actual_checksum_sha256": actual_checksum,
                }
            )

    recovery_rows.sort(key=lambda row: (int(row["priority"]), str(row["page_id"])))
    unmatched_remote_rows = sorted(
        (
            remote
            for uri, remote in remote_by_uri.items()
            if uri not in matched_remote_uris
        ),
        key=lambda row: row["uri"],
    )
    _write_jsonl(root / "artifact_reconciliation.jsonl", reconciliation_rows)
    _write_jsonl(root / "recovery_manifest.jsonl", recovery_rows)
    _write_jsonl(root / "parser_ready_source_artifacts.jsonl", parser_ready_rows)
    remote_download_rows = [row for row in recovery_rows if row["action"] == "download_remote"]
    reacquire_rows = [row for row in recovery_rows if row["action"] == "reacquire"]
    _write_jsonl(root / "remote_download_manifest.jsonl", remote_download_rows)
    _write_csv(
        root / "reacquire_manifest.csv",
        reacquire_rows,
        [
            "page_id",
            "issue_id",
            "issue_date",
            "page_num",
            "preferred_image_id",
            "preferred_image_page_url",
            "target_path",
            "classification",
            "reason",
        ],
    )
    _write_jsonl(root / "unmatched_remote_inventory.jsonl", unmatched_remote_rows)

    classification_counts = Counter(row["classification"] for row in reconciliation_rows)
    action_counts = Counter(row["recommended_action"] for row in reconciliation_rows)
    ready_count = action_counts.get("none", 0)
    blocked_actions = {"review_checksum_conflict", "review_remote_duplicates"}
    summary = {
        "contract_version": RECONCILIATION_SUMMARY_CONTRACT,
        "status": "ok",
        "ready_for_parsing": len(recovery_rows) == 0,
        "input_manifest": str(manifest),
        "remote_inventory_inputs": [str(path) for path in inventory_paths],
        "verify_local_checksums": verify_local_checksums,
        "verify_image_decode": verify_image_decode,
        "counts": {
            "expected_artifacts": len(expected_rows),
            "ready_artifacts": ready_count,
            "parser_ready_artifacts": len(parser_ready_rows),
            "recovery_actions": len(recovery_rows),
            "remote_download_actions": len(remote_download_rows),
            "reacquire_actions": len(reacquire_rows),
            "blocked_actions": sum(action_counts.get(action, 0) for action in blocked_actions),
            "remote_inventory_rows": len(remote_by_uri),
            "duplicate_inventory_rows": duplicate_inventory_rows,
            "matched_remote_rows": len(matched_remote_uris),
            "unmatched_remote_rows": len(unmatched_remote_rows),
        },
        "readiness_ratio": round(ready_count / len(expected_rows), 6) if expected_rows else 0.0,
        "classification_counts": dict(sorted(classification_counts.items())),
        "action_counts": dict(sorted(action_counts.items())),
        "paths": {
            "artifact_reconciliation": "artifact_reconciliation.jsonl",
            "recovery_manifest": "recovery_manifest.jsonl",
            "parser_ready_source_artifacts": "parser_ready_source_artifacts.jsonl",
            "remote_download_manifest": "remote_download_manifest.jsonl",
            "reacquire_manifest": "reacquire_manifest.csv",
            "unmatched_remote_inventory": "unmatched_remote_inventory.jsonl",
            "source_manifest_validation": "source_manifest_validation.json",
        },
    }
    _write_json(root / "summary.json", summary)
    return summary


def validate_reconciliation_bundle(run_dir: Path) -> dict[str, Any]:
    root = run_dir.expanduser().resolve()
    issues: list[dict[str, Any]] = []

    def load_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            issues.append({"level": "error", "code": "invalid_json", "path": str(path), "message": str(exc)})
            return {}
        if not isinstance(payload, dict):
            issues.append({"level": "error", "code": "invalid_json", "path": str(path), "message": "expected a JSON object"})
            return {}
        return payload

    def load_jsonl(path: Path) -> list[dict[str, Any]]:
        try:
            return _read_jsonl(path)
        except (FileNotFoundError, ValueError) as exc:
            issues.append({"level": "error", "code": "invalid_jsonl", "path": str(path), "message": str(exc)})
            return []

    def load_csv(path: Path) -> list[dict[str, str]]:
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except OSError as exc:
            issues.append({"level": "error", "code": "invalid_csv", "path": str(path), "message": str(exc)})
            return []

    summary = load_json(root / "summary.json")
    reconciliation_rows = load_jsonl(root / "artifact_reconciliation.jsonl")
    recovery_rows = load_jsonl(root / "recovery_manifest.jsonl")
    parser_ready_rows = load_jsonl(root / "parser_ready_source_artifacts.jsonl")
    remote_download_rows = load_jsonl(root / "remote_download_manifest.jsonl")
    reacquire_rows = load_csv(root / "reacquire_manifest.csv")
    unmatched_rows = load_jsonl(root / "unmatched_remote_inventory.jsonl")
    source_validation = load_json(root / "source_manifest_validation.json")

    if summary.get("contract_version") != RECONCILIATION_SUMMARY_CONTRACT:
        issues.append({"level": "error", "code": "invalid_summary_contract", "message": "invalid reconciliation summary contract"})
    if source_validation.get("status") not in {"ok", "warning"}:
        issues.append({"level": "error", "code": "source_manifest_invalid", "message": "source manifest validation did not pass"})

    actions_by_page: dict[str, str] = {}
    for row in reconciliation_rows:
        page_id = str(row.get("page_id") or "")
        action = str(row.get("recommended_action") or "")
        if row.get("contract_version") != RECONCILIATION_CONTRACT or not page_id:
            issues.append({"level": "error", "code": "invalid_reconciliation_row", "page_id": page_id, "message": "invalid reconciliation row contract or page_id"})
        if page_id in actions_by_page:
            issues.append({"level": "error", "code": "duplicate_reconciliation_page", "page_id": page_id, "message": "page appears more than once"})
        if action not in ALLOWED_ACTIONS:
            issues.append({"level": "error", "code": "invalid_recovery_action", "page_id": page_id, "message": f"unsupported action {action!r}"})
        actions_by_page[page_id] = action

    recovery_by_page: dict[str, str] = {}
    for row in recovery_rows:
        page_id = str(row.get("page_id") or "")
        action = str(row.get("action") or "")
        if row.get("contract_version") != RECOVERY_CONTRACT or action == "none":
            issues.append({"level": "error", "code": "invalid_recovery_row", "page_id": page_id, "message": "invalid recovery row contract or action"})
        if page_id in recovery_by_page:
            issues.append({"level": "error", "code": "duplicate_recovery_page", "page_id": page_id, "message": "page appears more than once in recovery manifest"})
        recovery_by_page[page_id] = action

    expected_recovery = {
        page_id: action for page_id, action in actions_by_page.items() if action != "none"
    }
    if recovery_by_page != expected_recovery:
        issues.append({"level": "error", "code": "recovery_manifest_mismatch", "message": "recovery manifest does not match reconciliation actions"})

    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    expected_counts = {
        "expected_artifacts": len(reconciliation_rows),
        "ready_artifacts": sum(action == "none" for action in actions_by_page.values()),
        "parser_ready_artifacts": len(parser_ready_rows),
        "recovery_actions": len(recovery_rows),
        "remote_download_actions": len(remote_download_rows),
        "reacquire_actions": len(reacquire_rows),
        "unmatched_remote_rows": len(unmatched_rows),
    }
    for key, expected in expected_counts.items():
        if counts.get(key) != expected:
            issues.append({"level": "error", "code": "summary_count_mismatch", "message": f"summary count {key} does not match artifacts"})
    classification_counts = Counter(
        str(row.get("classification") or "") for row in reconciliation_rows
    )
    action_counts = Counter(actions_by_page.values())
    if summary.get("classification_counts") != dict(sorted(classification_counts.items())):
        issues.append({"level": "error", "code": "classification_count_mismatch", "message": "classification counts do not match reconciliation rows"})
    if summary.get("action_counts") != dict(sorted(action_counts.items())):
        issues.append({"level": "error", "code": "action_count_mismatch", "message": "action counts do not match reconciliation rows"})
    if bool(summary.get("ready_for_parsing")) != (len(recovery_rows) == 0):
        issues.append({"level": "error", "code": "readiness_mismatch", "message": "ready_for_parsing does not match recovery manifest"})
    expected_ready_page_ids = {
        page_id for page_id, action in actions_by_page.items() if action == "none"
    }
    actual_ready_page_ids = {str(row.get("page_id") or "") for row in parser_ready_rows}
    if actual_ready_page_ids != expected_ready_page_ids:
        issues.append({"level": "error", "code": "parser_ready_manifest_mismatch", "message": "parser-ready manifest does not match ready reconciliation rows"})
    expected_download_page_ids = {
        page_id for page_id, action in recovery_by_page.items() if action == "download_remote"
    }
    actual_download_page_ids = {str(row.get("page_id") or "") for row in remote_download_rows}
    if actual_download_page_ids != expected_download_page_ids:
        issues.append({"level": "error", "code": "remote_download_manifest_mismatch", "message": "remote download manifest does not match recovery actions"})
    expected_reacquire_page_ids = {
        page_id for page_id, action in recovery_by_page.items() if action == "reacquire"
    }
    actual_reacquire_page_ids = {str(row.get("page_id") or "") for row in reacquire_rows}
    if actual_reacquire_page_ids != expected_reacquire_page_ids:
        issues.append({"level": "error", "code": "reacquire_manifest_mismatch", "message": "reacquire CSV does not match recovery actions"})
    if parser_ready_rows:
        parser_ready_validation = validate_source_artifact_manifest(
            input_jsonl=root / "parser_ready_source_artifacts.jsonl",
            require_files=True,
            require_checksums=True,
            verify_checksums=True,
        )
        if parser_ready_validation["status"] != "ok":
            issues.append({"level": "error", "code": "parser_ready_manifest_invalid", "message": "parser-ready source artifact manifest failed strict validation"})

    error_count = sum(issue.get("level") == "error" for issue in issues)
    return {
        "contract_version": RECONCILIATION_VALIDATION_CONTRACT,
        "status": "error" if error_count else "ok",
        "run_dir": str(root),
        "counts": {
            "reconciliation_rows": len(reconciliation_rows),
            "recovery_rows": len(recovery_rows),
            "parser_ready_rows": len(parser_ready_rows),
            "remote_download_rows": len(remote_download_rows),
            "reacquire_rows": len(reacquire_rows),
            "unmatched_remote_rows": len(unmatched_rows),
            "errors": error_count,
        },
        "issues": issues,
    }
