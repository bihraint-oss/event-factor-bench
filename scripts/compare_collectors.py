#!/usr/bin/env python3
"""Compare two collector snapshots without loading frozen labels or computing scores."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "event-factor-bench-collector-comparison-v1"
COLLECTOR_SCHEMA_VERSION = "event-factor-bench-collector-v1"
REPORT_FILE_NAME = "collector_comparison_v0.1.1.json"
REQUIRED_ARTIFACTS = (
    "protocol_v0.1.json",
    "candidate_rows_v0.1.csv.gz",
    "selection_audit.json",
)
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class ComparisonError(ValueError):
    """Raised when a collector comparison cannot be trusted."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _access_policy() -> dict[str, Any]:
    return {
        "candidate_csv_fields_used": ["event_id", "horizon_seconds"],
        "selection_audit_access": "sha256_only",
        "frozen_evidence_accessed": False,
        "result_files_accessed": False,
        "scoring_invoked_by_tool": False,
    }


def canonical_json(value: Any) -> bytes:
    """Encode strict canonical JSON used by report self-hashes."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ComparisonError(f"cannot load strict JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ComparisonError(f"{path} must contain a JSON object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ComparisonError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def sha256_gzip_content(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with gzip.open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ComparisonError(f"cannot hash decompressed content of {path}: {error}") from error
    return digest.hexdigest()


def _require_digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise ComparisonError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_commit(value: Any, name: str) -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise ComparisonError(f"{name} must be a full lowercase commit")
    return value


def _require_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ComparisonError(f"{name} must be a non-negative integer")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ComparisonError(f"{name} must be an object")
    return value


def _safe_child(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ComparisonError("collector artifact path must be non-empty text")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or len(pure.parts) != 1:
        raise ComparisonError(f"unsafe collector artifact path {relative!r}")
    return root / relative


def _artifact_records(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, list):
        raise ComparisonError("collector manifest lacks artifact records")
    records: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ComparisonError("collector artifact record must be an object")
        relative = item.get("path")
        if not isinstance(relative, str) or relative in records:
            raise ComparisonError("collector artifact paths must be unique strings")
        records[relative] = item
    if set(records) != set(REQUIRED_ARTIFACTS):
        raise ComparisonError("collector artifact set differs from the frozen v0.1 contract")
    return records


def _candidate_counts(path: Path) -> tuple[int, int, int]:
    rows = 0
    event_ids: set[str] = set()
    curves: set[tuple[str, str]] = set()
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = set(reader.fieldnames or ())
            if not {"event_id", "horizon_seconds"}.issubset(fields):
                raise ComparisonError(
                    "candidate CSV must expose event_id and horizon_seconds for count comparison"
                )
            for row in reader:
                event_id = row.get("event_id", "")
                horizon = row.get("horizon_seconds", "")
                if not event_id or not horizon:
                    raise ComparisonError("candidate CSV has an empty comparison key")
                rows += 1
                event_ids.add(event_id)
                curves.add((event_id, horizon))
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError, csv.Error) as error:
        raise ComparisonError(f"cannot inspect candidate CSV {path}: {error}") from error
    return rows, len(event_ids), len(curves)


def _coverage_totals(coverage: Mapping[str, Any]) -> tuple[int, int, int]:
    row_total = 0
    curve_total = 0
    selected_event_total = 0
    for split, raw_horizons in coverage.items():
        horizons = _mapping(raw_horizons, f"coverage {split}")
        if not horizons:
            raise ComparisonError(f"coverage {split} has no horizons")
        split_expected_events: set[int] = set()
        for horizon, raw_metrics in horizons.items():
            metrics = _mapping(raw_metrics, f"coverage {split}/{horizon}")
            split_expected_events.add(
                _require_nonnegative_int(
                    metrics.get("expected_events"),
                    f"coverage {split}/{horizon} expected_events",
                )
            )
            curve_total += _require_nonnegative_int(
                metrics.get("history_retained_events"),
                f"coverage {split}/{horizon} history_retained_events",
            )
            row_total += _require_nonnegative_int(
                metrics.get("history_retained_rows"),
                f"coverage {split}/{horizon} history_retained_rows",
            )
        if len(split_expected_events) != 1:
            raise ComparisonError(f"coverage {split} disagrees on expected events across horizons")
        selected_event_total += split_expected_events.pop()
    return row_total, curve_total, selected_event_total


def inspect_snapshot(directory: Path) -> dict[str, Any]:
    """Inspect only collector provenance and non-label candidate identifiers."""

    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    manifest = load_object(manifest_path)
    if manifest.get("schema_version") != COLLECTOR_SCHEMA_VERSION:
        raise ComparisonError(f"unsupported collector schema in {manifest_path}")
    source_commit = _require_commit(manifest.get("run_source_commit"), "collector source commit")
    protocol_sha = _require_digest(manifest.get("protocol_sha256"), "collector protocol SHA-256")
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        raise ComparisonError("collector generated_at must be non-empty text")
    raw_responses = manifest.get("raw_responses")
    if not isinstance(raw_responses, list):
        raise ComparisonError("collector manifest lacks raw-response records")
    coverage = _mapping(manifest.get("coverage_pre_chain"), "collector coverage_pre_chain")
    records = _artifact_records(manifest)

    artifact_hashes: dict[str, str] = {}
    for relative in REQUIRED_ARTIFACTS:
        path = _safe_child(directory, relative)
        record = records[relative]
        digest = sha256_file(path)
        if _require_digest(record.get("sha256"), f"artifact {relative} SHA-256") != digest:
            raise ComparisonError(f"artifact {relative} differs from its collector manifest")
        declared_bytes = _require_nonnegative_int(record.get("bytes"), f"artifact {relative} bytes")
        if declared_bytes != path.stat().st_size:
            raise ComparisonError(f"artifact {relative} byte count differs from its manifest")
        artifact_hashes[relative] = digest
    if artifact_hashes["protocol_v0.1.json"] != protocol_sha:
        raise ComparisonError("collector protocol artifact differs from protocol_sha256")

    candidate_path = directory / "candidate_rows_v0.1.csv.gz"
    row_count, event_count, curve_count = _candidate_counts(candidate_path)
    coverage_rows, coverage_curves, selected_events = _coverage_totals(coverage)
    if coverage_rows != row_count:
        raise ComparisonError("collector coverage row totals differ from candidate CSV")
    if coverage_curves != curve_count:
        raise ComparisonError("collector coverage event totals differ from candidate CSV")

    return {
        "local_directory_name": directory.name,
        "collector_manifest_sha256": sha256_file(manifest_path),
        "collector_generated_at": generated_at,
        "run_source_commit": source_commit,
        "benchmark": manifest.get("benchmark"),
        "protocol_version": manifest.get("protocol_version"),
        "protocol_sha256": protocol_sha,
        "label_caveat": manifest.get("label_caveat"),
        "artifact_sha256": artifact_hashes,
        "candidate_rows_gzip_sha256": artifact_hashes["candidate_rows_v0.1.csv.gz"],
        "candidate_rows_content_sha256": sha256_gzip_content(candidate_path),
        "selection_audit_sha256": artifact_hashes["selection_audit.json"],
        "candidate_rows": row_count,
        "candidate_event_ids_any_horizon": event_count,
        "candidate_event_horizon_curves": curve_count,
        "events_selected_before_history": selected_events,
        "archived_api_responses": len(raw_responses),
        "coverage_pre_chain": json.loads(canonical_json(coverage)),
    }


def _validate_old_against_failure_audit(
    snapshot: Mapping[str, Any], failure_audit: Mapping[str, Any]
) -> None:
    statistical = _mapping(
        failure_audit.get("statistical_protocol"), "failure statistical protocol"
    )
    failed = _mapping(failure_audit.get("failed_source_freeze"), "failed source freeze")
    expected = {
        "collector_manifest_sha256": failed.get("collector_manifest_sha256"),
        "collector_generated_at": failed.get("collector_generated_at_utc"),
        "run_source_commit": failed.get("source_commit"),
        "protocol_version": statistical.get("config_version"),
        "protocol_sha256": statistical.get("config_sha256"),
        "candidate_rows_gzip_sha256": failed.get("candidate_rows_gzip_sha256"),
        "candidate_rows_content_sha256": failed.get("candidate_rows_content_sha256"),
        "selection_audit_sha256": failed.get("selection_audit_sha256"),
        "candidate_rows": failed.get("candidate_rows"),
        "candidate_event_ids_any_horizon": failed.get("candidate_event_ids_any_horizon"),
        "candidate_event_horizon_curves": failed.get("candidate_event_horizon_curves"),
        "events_selected_before_history": failed.get("events_selected_before_history"),
        "archived_api_responses": failed.get("archived_api_responses"),
    }
    for key, value in expected.items():
        if snapshot.get(key) != value:
            raise ComparisonError(f"old collector {key} differs from the failure audit")
    holdout = _mapping(
        failed.get("holdout_pre_chain_primary_coverage"),
        "failure holdout primary coverage",
    )
    horizon = str(holdout.get("horizon_seconds"))
    old_holdout = _mapping(
        _mapping(snapshot.get("coverage_pre_chain"), "old coverage").get("holdout"),
        "old holdout coverage",
    )
    old_primary = _mapping(old_holdout.get(horizon), f"old holdout/{horizon} coverage")
    for key, value in holdout.items():
        if key != "horizon_seconds" and old_primary.get(key) != value:
            raise ComparisonError(f"old holdout {key} differs from the failure audit")
    _require_digest(statistical.get("claim_gate_sha256"), "failure claim-gate SHA-256")
    _require_digest(
        statistical.get("development_audit_sha256"), "failure development-audit SHA-256"
    )
    _require_commit(failed.get("source_tag_object"), "failed source tag object")


def _flatten_coverage(value: Mapping[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for split in sorted(value):
        horizons = _mapping(value[split], f"coverage {split}")
        for horizon in sorted(horizons, key=str):
            metrics = _mapping(horizons[horizon], f"coverage {split}/{horizon}")
            for metric in sorted(metrics):
                flattened[f"coverage.{split}.{horizon}.{metric}"] = metrics[metric]
    return flattened


def _comparison_values(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    values = {
        "benchmark": snapshot.get("benchmark"),
        "protocol_version": snapshot.get("protocol_version"),
        "protocol_sha256": snapshot.get("protocol_sha256"),
        "protocol_artifact_sha256": _mapping(
            snapshot.get("artifact_sha256"), "artifact hashes"
        ).get("protocol_v0.1.json"),
        "candidate_rows_gzip_sha256": snapshot.get("candidate_rows_gzip_sha256"),
        "candidate_rows_content_sha256": snapshot.get("candidate_rows_content_sha256"),
        "selection_audit_sha256": snapshot.get("selection_audit_sha256"),
        "candidate_rows": snapshot.get("candidate_rows"),
        "candidate_event_ids_any_horizon": snapshot.get("candidate_event_ids_any_horizon"),
        "candidate_event_horizon_curves": snapshot.get("candidate_event_horizon_curves"),
        "events_selected_before_history": snapshot.get("events_selected_before_history"),
        "archived_api_responses": snapshot.get("archived_api_responses"),
        "label_caveat": snapshot.get("label_caveat"),
    }
    values.update(
        _flatten_coverage(_mapping(snapshot.get("coverage_pre_chain"), "coverage_pre_chain"))
    )
    return values


def load_explanations(value: str | None) -> dict[str, str]:
    if value is None:
        return {}
    candidate = Path(value)
    try:
        raw: Any
        if candidate.is_file():
            raw = json.loads(
                candidate.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        else:
            raw = json.loads(
                value,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ComparisonError(f"cannot parse explanations JSON: {error}") from error
    if not isinstance(raw, dict):
        raise ComparisonError("explanations must be a JSON object")
    result: dict[str, str] = {}
    for key, explanation in raw.items():
        if not isinstance(key, str) or not isinstance(explanation, str) or not explanation.strip():
            raise ComparisonError("explanation keys and values must be non-empty strings")
        result[key] = explanation.strip()
    return result


def _attach_self_hash(report: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(report)
    result["report_payload_sha256"] = hashlib.sha256(canonical_json(result)).hexdigest()
    return result


def validate_report_self_hash(report: Mapping[str, Any]) -> None:
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ComparisonError("unsupported collector-comparison schema")
    claimed = _require_digest(report.get("report_payload_sha256"), "comparison self-hash")
    payload = dict(report)
    del payload["report_payload_sha256"]
    if hashlib.sha256(canonical_json(payload)).hexdigest() != claimed:
        raise ComparisonError("collector-comparison self-hash does not match")


def validate_report_semantics(report: Mapping[str, Any]) -> None:
    """Recompute comparison rows and the explanation gate from report snapshots."""

    if report.get("comparison_scope") != (
        "collector provenance and non-label identifiers only; no scoring"
    ):
        raise ComparisonError("collector-comparison scope is not canonical")
    if report.get("access_policy") != _access_policy():
        raise ComparisonError("collector-comparison access policy is not canonical")
    old = _mapping(report.get("old"), "comparison old snapshot")
    new = _mapping(report.get("new"), "comparison new snapshot")
    old_values = _comparison_values(old)
    new_values = _comparison_values(new)
    if set(old_values) != set(new_values):
        raise ComparisonError("collector-comparison snapshot fields differ")
    raw_explanations = _mapping(report.get("explanations"), "comparison explanations")
    explanations: dict[str, str] = {}
    for key, value in raw_explanations.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            raise ComparisonError("comparison explanations must be non-empty strings")
        if value != value.strip():
            raise ComparisonError("comparison explanations must be stored in normalized form")
        explanations[key] = value
    changed = {key for key in old_values if old_values[key] != new_values[key]}
    if not set(explanations).issubset(changed):
        raise ComparisonError("comparison explanations cite unchanged or unknown fields")
    expected_comparisons = [
        {
            "field": key,
            "old": old_values[key],
            "new": new_values[key],
            "changed": key in changed,
            "explanation": explanations.get(key),
        }
        for key in sorted(old_values)
    ]
    if report.get("substantive_comparisons") != expected_comparisons:
        raise ComparisonError("substantive comparison rows do not match snapshot differences")
    missing = sorted(changed.difference(explanations))
    if report.get("missing_explanations") != missing:
        raise ComparisonError("comparison missing-explanation list is inconsistent")
    if report.get("gate_passed") is not (not missing):
        raise ComparisonError("collector-comparison gate is inconsistent with explanations")
    expected_metadata = [
        {
            "field": key,
            "old": old[key],
            "new": new[key],
            "changed": old[key] != new[key],
            "substantive": False,
        }
        for key in (
            "local_directory_name",
            "collector_manifest_sha256",
            "collector_generated_at",
            "run_source_commit",
        )
    ]
    if report.get("expected_metadata_comparisons") != expected_metadata:
        raise ComparisonError("metadata comparison rows do not match snapshot metadata")


def generate_report(
    old_dir: Path,
    new_dir: Path,
    failure_audit_path: Path,
    *,
    explanations: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    failure_audit = load_object(failure_audit_path)
    old = inspect_snapshot(old_dir)
    new = inspect_snapshot(new_dir)
    _validate_old_against_failure_audit(old, failure_audit)
    if old["run_source_commit"] == new["run_source_commit"]:
        raise ComparisonError("successor collector must bind a new source commit")

    old_values = _comparison_values(old)
    new_values = _comparison_values(new)
    if set(old_values) != set(new_values):  # pragma: no cover - built by one function
        raise AssertionError("collector comparison field sets differ")
    normalized_explanations = dict(explanations or {})
    changed_keys = {key for key in old_values if old_values[key] != new_values[key]}
    unknown = sorted(set(normalized_explanations).difference(changed_keys))
    if unknown:
        raise ComparisonError(f"explanations cite unchanged or unknown fields: {unknown!r}")
    for key, explanation in normalized_explanations.items():
        if not isinstance(key, str) or not isinstance(explanation, str) or not explanation.strip():
            raise ComparisonError("explanation keys and values must be non-empty strings")
        normalized_explanations[key] = explanation.strip()

    comparisons = [
        {
            "field": key,
            "old": old_values[key],
            "new": new_values[key],
            "changed": key in changed_keys,
            "explanation": normalized_explanations.get(key),
        }
        for key in sorted(old_values)
    ]
    missing = sorted(changed_keys.difference(normalized_explanations))
    metadata = [
        {
            "field": key,
            "old": old[key],
            "new": new[key],
            "changed": old[key] != new[key],
            "substantive": False,
        }
        for key in (
            "local_directory_name",
            "collector_manifest_sha256",
            "collector_generated_at",
            "run_source_commit",
        )
    ]
    report = {
        "schema_version": SCHEMA_VERSION,
        "comparison_scope": "collector provenance and non-label identifiers only; no scoring",
        "access_policy": _access_policy(),
        "failure_audit": {
            "path": failure_audit_path.name,
            "sha256": sha256_file(failure_audit_path),
        },
        "old": old,
        "new": new,
        "expected_metadata_comparisons": metadata,
        "substantive_comparisons": comparisons,
        "explanations": dict(sorted(normalized_explanations.items())),
        "missing_explanations": missing,
        "gate_passed": not missing,
    }
    return _attach_self_hash(report)


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(canonical_json(report) + b"\n")
    temporary.replace(path)


def _public_collector_view(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") != COLLECTOR_SCHEMA_VERSION:
        raise ComparisonError("unsupported public collector manifest schema")
    artifacts = _artifact_records(manifest)
    coverage = _mapping(manifest.get("coverage_pre_chain"), "public collector coverage")
    raw = manifest.get("raw_responses")
    if not isinstance(raw, list):
        raise ComparisonError("public collector manifest lacks raw-response records")
    candidate_rows, candidate_curves, selected_events = _coverage_totals(coverage)
    return {
        "run_source_commit": _require_commit(
            manifest.get("run_source_commit"), "public collector source commit"
        ),
        "benchmark": manifest.get("benchmark"),
        "protocol_version": manifest.get("protocol_version"),
        "protocol_sha256": _require_digest(
            manifest.get("protocol_sha256"), "public collector protocol SHA-256"
        ),
        "label_caveat": manifest.get("label_caveat"),
        "candidate_rows_gzip_sha256": _require_digest(
            artifacts["candidate_rows_v0.1.csv.gz"].get("sha256"),
            "public collector candidate SHA-256",
        ),
        "selection_audit_sha256": _require_digest(
            artifacts["selection_audit.json"].get("sha256"),
            "public collector selection-audit SHA-256",
        ),
        "protocol_artifact_sha256": _require_digest(
            artifacts["protocol_v0.1.json"].get("sha256"),
            "public collector protocol-artifact SHA-256",
        ),
        "candidate_rows": candidate_rows,
        "candidate_event_horizon_curves": candidate_curves,
        "events_selected_before_history": selected_events,
        "archived_api_responses": len(raw),
        "coverage_pre_chain": json.loads(canonical_json(coverage)),
    }


def _validate_report_old_view(report: Mapping[str, Any], failure_audit: Mapping[str, Any]) -> None:
    old = _mapping(report.get("old"), "comparison old snapshot")
    _validate_old_against_failure_audit(old, failure_audit)


def verify_public(
    report_path: Path,
    failure_audit_path: Path,
    collector_manifest_path: Path,
    chain_manifest_path: Path,
) -> dict[str, Any]:
    """Verify public comparison bindings without opening evidence rows or labels."""

    report = load_object(report_path)
    validate_report_self_hash(report)
    validate_report_semantics(report)
    if report.get("gate_passed") is not True or report.get("missing_explanations") != []:
        raise ComparisonError("collector-comparison gate did not pass")
    failure_audit = load_object(failure_audit_path)
    failure_record = _mapping(report.get("failure_audit"), "comparison failure-audit record")
    failure_sha = _require_digest(failure_record.get("sha256"), "comparison failure-audit SHA-256")
    if failure_sha != sha256_file(failure_audit_path):
        raise ComparisonError("comparison is not bound to this failure audit")
    _validate_report_old_view(report, failure_audit)

    collector = load_object(collector_manifest_path)
    collector_sha = sha256_file(collector_manifest_path)
    new = _mapping(report.get("new"), "comparison new snapshot")
    if new.get("collector_manifest_sha256") != collector_sha:
        raise ComparisonError("comparison is not bound to the public collector manifest")
    public = _public_collector_view(collector)
    direct_fields = (
        "run_source_commit",
        "benchmark",
        "protocol_version",
        "protocol_sha256",
        "label_caveat",
        "candidate_rows_gzip_sha256",
        "selection_audit_sha256",
        "candidate_rows",
        "candidate_event_horizon_curves",
        "events_selected_before_history",
        "archived_api_responses",
        "coverage_pre_chain",
    )
    for key in direct_fields:
        if new.get(key) != public[key]:
            raise ComparisonError(f"comparison new {key} differs from public collector")
    artifact_hashes = _mapping(new.get("artifact_sha256"), "comparison artifact hashes")
    if artifact_hashes.get("protocol_v0.1.json") != public["protocol_artifact_sha256"]:
        raise ComparisonError("comparison protocol artifact differs from public collector")

    chain = load_object(chain_manifest_path)
    if chain.get("run_source_commit") != new.get("run_source_commit"):
        raise ComparisonError("chain manifest source commit differs from comparison")
    protocol_record = _mapping(chain.get("protocol"), "chain protocol record")
    if protocol_record.get("sha256") != new.get("protocol_sha256"):
        raise ComparisonError("chain protocol differs from comparison")
    collector_record = _mapping(chain.get("collector_manifest"), "chain collector record")
    if collector_record.get("sha256") != collector_sha or collector_record.get(
        "run_source_commit"
    ) != new.get("run_source_commit"):
        raise ComparisonError("chain collector binding differs from comparison")
    if chain.get("coverage_pre_chain") != new.get("coverage_pre_chain"):
        raise ComparisonError("chain coverage differs from comparison")
    candidate_record = _mapping(chain.get("candidate_manifest"), "chain candidate record")
    if candidate_record.get("file_sha256") != new.get("candidate_rows_gzip_sha256"):
        raise ComparisonError("chain candidate gzip hash differs from comparison")
    if candidate_record.get("content_sha256") != new.get("candidate_rows_content_sha256"):
        raise ComparisonError("chain candidate content hash differs from comparison")
    count_bindings = {
        "rows": "candidate_rows",
        "event_ids_any_horizon": "candidate_event_ids_any_horizon",
        "event_horizon_curves": "candidate_event_horizon_curves",
    }
    for chain_key, report_key in count_bindings.items():
        if candidate_record.get(chain_key) != new.get(report_key):
            raise ComparisonError(f"chain candidate {chain_key} differs from collector comparison")

    files = _mapping(chain.get("files"), "chain files")
    collector_file = _mapping(
        files.get("collector_manifest_v0.1.json"), "chain public collector file"
    )
    if collector_file.get("sha256") != collector_sha:
        raise ComparisonError("chain public collector file hash differs from comparison")
    comparison_file = _mapping(files.get(REPORT_FILE_NAME), "chain comparison file")
    report_file_sha = sha256_file(report_path)
    if comparison_file.get("sha256") != report_file_sha:
        raise ComparisonError("chain comparison file SHA-256 differs from the public report")
    if comparison_file.get("report_payload_sha256") != report.get("report_payload_sha256"):
        raise ComparisonError("chain comparison self-hash binding differs from the report")
    if comparison_file.get("new_collector_manifest_sha256") != collector_sha:
        raise ComparisonError("chain comparison record is not bound to the new collector")
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--old-dir", type=Path, required=True)
    generate.add_argument("--new-dir", type=Path, required=True)
    generate.add_argument("--failure-audit", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--explanations")
    verify = commands.add_parser("verify-public")
    verify.add_argument("--report", type=Path, required=True)
    verify.add_argument("--failure-audit", type=Path, required=True)
    verify.add_argument("--collector-manifest", type=Path, required=True)
    verify.add_argument("--chain-manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            report = generate_report(
                args.old_dir,
                args.new_dir,
                args.failure_audit,
                explanations=load_explanations(args.explanations),
            )
            write_report(args.output, report)
            if report["gate_passed"]:
                print(f"collector comparison gate passed: {args.output}")
                return 0
            print(
                "collector comparison gate failed; missing explanations: "
                + ", ".join(report["missing_explanations"]),
                file=sys.stderr,
            )
            return 2
        verify_public(
            args.report,
            args.failure_audit,
            args.collector_manifest,
            args.chain_manifest,
        )
        print(f"public collector comparison verified: {args.report}")
        return 0
    except ComparisonError as error:
        print(f"collector comparison failed closed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
