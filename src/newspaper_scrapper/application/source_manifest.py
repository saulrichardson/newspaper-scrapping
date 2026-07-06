"""Source artifact manifests for downstream parsing."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


PAGE_ID_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no CSV header")
        return [
            {key: (value or "").strip() for key, value in row.items()}
            for row in reader
        ]


def _stable_token(value: str, *, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    token = PAGE_ID_SAFE_RE.sub("-", raw).strip("-._")
    return token or fallback


def build_page_id(row: dict[str, str]) -> str:
    issue = _stable_token(row.get("issue_id", ""), fallback="issue")
    image_id = _stable_token(
        row.get("preferred_image_id", "") or row.get("image_id", ""),
        fallback="image",
    )
    page_raw = row.get("page_num", "") or row.get("page_number", "")
    try:
        page = f"p{int(page_raw):04d}"
    except ValueError:
        page = _stable_token(page_raw, fallback="p0000")
    return f"{issue}__{page}__{image_id}"


def _page_number(row: dict[str, str]) -> int | None:
    raw = row.get("page_num", "") or row.get("page_number", "")
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_image_path(
    row: dict[str, str],
    *,
    image_root: Path | None,
    image_path_field: str,
) -> Path:
    raw_path = str(row.get(image_path_field) or "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path
        if image_root is not None:
            return image_root / path
        return path

    if image_root is None:
        raise ValueError(
            f"row {build_page_id(row)} has no {image_path_field!r}; pass --image-root "
            "or provide an image path column"
        )
    issue_id = row.get("issue_id", "").strip()
    page_num = row.get("page_num", "").strip()
    image_id = row.get("preferred_image_id", "").strip() or row.get("image_id", "").strip()
    if not issue_id or not page_num or not image_id:
        raise ValueError(
            f"row {build_page_id(row)} cannot infer image path without issue_id/page_num/image_id"
        )
    return image_root / issue_id / f"{page_num.zfill(4)}__{image_id}.jpg"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _issue(
    issues: list[dict[str, Any]],
    *,
    level: str,
    code: str,
    message: str,
    line: int | None = None,
    page_id: str = "",
    path: Path | str | None = None,
) -> None:
    row: dict[str, Any] = {"level": level, "code": code, "message": message}
    if line is not None:
        row["line"] = line
    if page_id:
        row["page_id"] = page_id
    if path is not None:
        row["path"] = str(path)
    issues.append(row)


def _validation_status(issues: list[dict[str, Any]], *, warnings_are_errors: bool) -> str:
    errors = sum(1 for issue in issues if issue.get("level") == "error")
    warnings = sum(1 for issue in issues if issue.get("level") == "warning")
    if errors or (warnings_are_errors and warnings):
        return "error"
    if warnings:
        return "warning"
    return "ok"


def _row_source_metadata(row: dict[str, str]) -> dict[str, str]:
    skip = {
        "output_path",
        "image_path",
        "path",
        "preferred_image_page_url",
        "image_page_url",
        "viewer_url",
    }
    return {key: value for key, value in row.items() if value and key not in skip}


def build_source_artifact_rows(
    *,
    input_csv: Path,
    image_root: Path | None = None,
    image_path_field: str = "output_path",
    source_system: str = "newspapers.com",
    require_files: bool = False,
    include_statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows = _read_csv_rows(input_csv)
    image_root = image_root.expanduser().resolve() if image_root is not None else None
    include_statuses = include_statuses or set()
    artifacts: list[dict[str, Any]] = []

    for csv_index, row in enumerate(rows, start=1):
        status = str(row.get("status") or "").strip()
        if include_statuses and status not in include_statuses:
            continue
        image_path = _resolve_image_path(
            row,
            image_root=image_root,
            image_path_field=image_path_field,
        ).expanduser()
        exists = image_path.is_file()
        if require_files and not exists:
            raise FileNotFoundError(f"missing image file for row {csv_index}: {image_path}")

        image_id = row.get("preferred_image_id", "").strip() or row.get("image_id", "").strip()
        source_url = (
            row.get("preferred_image_page_url", "").strip()
            or row.get("image_page_url", "").strip()
            or row.get("viewer_url", "").strip()
        )
        metadata = _row_source_metadata(row)
        metadata.update(
            {
                "contract_version": "source-artifact-v1",
                "artifact_kind": "page_image",
                "input_csv": str(input_csv),
                "input_csv_row": csv_index,
                "image_exists": exists,
            }
        )
        artifacts.append(
            {
                "page_id": build_page_id(row),
                "image_path": str(image_path.resolve() if exists else image_path),
                "issue_id": row.get("issue_id", ""),
                "page_number": _page_number(row),
                "checksum_sha256": _sha256_file(image_path) if exists else "",
                "source": {
                    "source_system": source_system,
                    "source_id": image_id,
                    "source_url": source_url,
                    "metadata": metadata,
                },
                "metadata": metadata,
            }
        )
    return artifacts


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            count += 1
    return count


def write_source_artifact_manifest(
    *,
    input_csv: Path,
    output_jsonl: Path,
    image_root: Path | None = None,
    image_path_field: str = "output_path",
    source_system: str = "newspapers.com",
    require_files: bool = False,
    include_statuses: set[str] | None = None,
) -> dict[str, Any]:
    rows = build_source_artifact_rows(
        input_csv=input_csv,
        image_root=image_root,
        image_path_field=image_path_field,
        source_system=source_system,
        require_files=require_files,
        include_statuses=include_statuses,
    )
    written = write_jsonl(output_jsonl, rows)
    summary = {
        "contract_version": "source-artifact-v1",
        "input_csv": str(input_csv),
        "output_jsonl": str(output_jsonl),
        "rows_written": written,
        "rows_with_files": sum(1 for row in rows if row["metadata"]["image_exists"]),
        "require_files": require_files,
        "image_path_field": image_path_field,
        "source_system": source_system,
    }
    summary_path = output_jsonl.with_suffix(output_jsonl.suffix + ".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["summary_json"] = str(summary_path)
    return summary


def iter_jsonl_rows(path: Path, issues: list[dict[str, Any]]) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        _issue(issues, level="error", code="missing_manifest", message="manifest JSONL is missing", path=path)
        return
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            _issue(
                issues,
                level="error",
                code="invalid_jsonl",
                message=f"invalid JSONL row: {exc.msg}",
                line=line_number,
                path=path,
            )
            continue
        if not isinstance(payload, dict):
            _issue(
                issues,
                level="error",
                code="invalid_row",
                message="manifest row must be a JSON object",
                line=line_number,
                path=path,
            )
            continue
        yield line_number, payload


def validate_source_artifact_manifest(
    *,
    input_jsonl: Path,
    require_files: bool = False,
    require_checksums: bool = False,
    verify_checksums: bool = False,
    warnings_are_errors: bool = False,
) -> dict[str, Any]:
    """Validate the parser-ready source artifact JSONL contract."""

    manifest = input_jsonl.expanduser().resolve()
    issues: list[dict[str, Any]] = []
    seen_page_ids: set[str] = set()
    rows = 0
    rows_with_files = 0
    rows_with_checksums = 0
    source_systems: set[str] = set()

    for line_number, row in iter_jsonl_rows(manifest, issues):
        rows += 1
        page_id = str(row.get("page_id") or "").strip()
        if not page_id:
            _issue(
                issues,
                level="error",
                code="missing_page_id",
                message="row is missing page_id",
                line=line_number,
                path=manifest,
            )
        elif page_id in seen_page_ids:
            _issue(
                issues,
                level="error",
                code="duplicate_page_id",
                message="page_id appears more than once",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )
        else:
            seen_page_ids.add(page_id)

        image_path_raw = str(row.get("image_path") or "").strip()
        if not image_path_raw:
            _issue(
                issues,
                level="error",
                code="missing_image_path",
                message="row is missing image_path",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )
            image_path = None
        else:
            image_path = Path(image_path_raw).expanduser()
            if not image_path.is_absolute():
                image_path = (manifest.parent / image_path).resolve()
            if image_path.is_file():
                rows_with_files += 1
            else:
                level = "error" if require_files else "warning"
                _issue(
                    issues,
                    level=level,
                    code="missing_image_file",
                    message="image_path does not point to an existing file",
                    line=line_number,
                    page_id=page_id,
                    path=image_path,
                )

        page_number = row.get("page_number")
        if page_number not in (None, ""):
            try:
                int(page_number)
            except (TypeError, ValueError):
                _issue(
                    issues,
                    level="error",
                    code="invalid_page_number",
                    message="page_number must be an integer or null",
                    line=line_number,
                    page_id=page_id,
                    path=manifest,
                )

        checksum = str(row.get("checksum_sha256") or "").strip()
        if checksum:
            rows_with_checksums += 1
            if not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
                _issue(
                    issues,
                    level="error",
                    code="invalid_checksum",
                    message="checksum_sha256 must be a 64-character hex digest",
                    line=line_number,
                    page_id=page_id,
                    path=manifest,
                )
            elif verify_checksums and image_path is not None and image_path.is_file():
                actual = _sha256_file(image_path)
                if actual.lower() != checksum.lower():
                    _issue(
                        issues,
                        level="error",
                        code="checksum_mismatch",
                        message="checksum_sha256 does not match image file bytes",
                        line=line_number,
                        page_id=page_id,
                        path=image_path,
                    )
        elif require_checksums or (require_files and image_path is not None and image_path.is_file()):
            _issue(
                issues,
                level="error",
                code="missing_checksum",
                message="row is missing checksum_sha256",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )

        source = row.get("source")
        if not isinstance(source, dict):
            _issue(
                issues,
                level="error",
                code="invalid_source",
                message="source must be a JSON object",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )
            source = {}
        source_system = str(source.get("source_system") or "").strip()
        source_id = str(source.get("source_id") or "").strip()
        if source_system:
            source_systems.add(source_system)
        else:
            _issue(
                issues,
                level="error",
                code="missing_source_system",
                message="source.source_system is required",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )
        if not source_id:
            _issue(
                issues,
                level="warning",
                code="missing_source_id",
                message="source.source_id is empty",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )

        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            _issue(
                issues,
                level="error",
                code="invalid_metadata",
                message="metadata must be a JSON object",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )
            metadata = {}
        if metadata.get("contract_version") != "source-artifact-v1":
            _issue(
                issues,
                level="error",
                code="invalid_contract_version",
                message="metadata.contract_version must be source-artifact-v1",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )
        if metadata.get("artifact_kind") != "page_image":
            _issue(
                issues,
                level="error",
                code="invalid_artifact_kind",
                message="metadata.artifact_kind must be page_image",
                line=line_number,
                page_id=page_id,
                path=manifest,
            )

    if rows == 0:
        _issue(
            issues,
            level="error",
            code="empty_manifest",
            message="manifest contains no data rows",
            path=manifest,
        )

    errors = sum(1 for issue in issues if issue.get("level") == "error")
    warnings = sum(1 for issue in issues if issue.get("level") == "warning")
    return {
        "contract_version": "source-artifact-validation-v1",
        "status": _validation_status(issues, warnings_are_errors=warnings_are_errors),
        "input_jsonl": str(manifest),
        "counts": {
            "rows": rows,
            "unique_page_ids": len(seen_page_ids),
            "rows_with_files": rows_with_files,
            "rows_with_checksums": rows_with_checksums,
            "source_systems": len(source_systems),
            "errors": errors,
            "warnings": warnings,
        },
        "source_systems": sorted(source_systems),
        "require_files": require_files,
        "require_checksums": require_checksums,
        "verify_checksums": verify_checksums,
        "issues": issues,
    }
