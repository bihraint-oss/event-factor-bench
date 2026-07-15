from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "compare_collectors.py"
SPEC = importlib.util.spec_from_file_location("compare_collectors_for_tests", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
compare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compare
SPEC.loader.exec_module(compare)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(compare.canonical_json(value) + b"\n")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _artifact_record(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {"path": path.name, "bytes": len(payload), "sha256": _sha256(payload)}


def _snapshot(
    root: Path,
    name: str,
    *,
    source_commit: str,
    generated_at: str,
    rows: list[tuple[str, str, str]] | None = None,
    raw_response_count: int = 3,
    selected_events: int = 2,
) -> Path:
    directory = root / name
    directory.mkdir()
    protocol = directory / "protocol_v0.1.json"
    protocol.write_bytes(b'{"benchmark":"EventFactorBench","version":"0.1.0"}\n')
    candidate_rows = rows or [
        ("event-1", "1800", "never-inspected-label-a"),
        ("event-1", "1800", "never-inspected-label-b"),
        ("event-2", "1800", "never-inspected-label-c"),
    ]
    candidate_text = "event_id,horizon_seconds,label\n" + "".join(
        f"{event_id},{horizon},{label}\n" for event_id, horizon, label in candidate_rows
    )
    candidate = directory / "candidate_rows_v0.1.csv.gz"
    candidate.write_bytes(gzip.compress(candidate_text.encode(), mtime=0))
    curves = len({(event_id, horizon) for event_id, horizon, _ in candidate_rows})
    selection = directory / "selection_audit.json"
    _write_json(
        selection,
        {
            "counts": {
                "candidate_rows": len(candidate_rows),
                "curves_selected": curves,
                "events_selected": selected_events,
            }
        },
    )
    event_count = len({event_id for event_id, _, _ in candidate_rows})
    coverage = {
        "holdout": {
            "1800": {
                "expected_events": selected_events,
                "history_retained_events": event_count,
                "expected_rows": len(candidate_rows),
                "history_retained_rows": len(candidate_rows),
                "event_history_coverage": event_count / selected_events,
                "row_history_coverage": 1.0,
            }
        }
    }
    manifest = {
        "schema_version": "event-factor-bench-collector-v1",
        "benchmark": "EventFactorBench",
        "generated_at": generated_at,
        "label_caveat": "candidate values are not canonical labels",
        "protocol_sha256": _sha256(protocol.read_bytes()),
        "protocol_version": "0.1.0",
        "run_source_commit": source_commit,
        "coverage_pre_chain": coverage,
        "raw_responses": [{"record": index} for index in range(raw_response_count)],
        "artifacts": [
            _artifact_record(protocol),
            _artifact_record(candidate),
            _artifact_record(selection),
        ],
    }
    _write_json(directory / "manifest.json", manifest)
    return directory


def _failure_audit(path: Path, old_dir: Path) -> Path:
    old = compare.inspect_snapshot(old_dir)
    primary = old["coverage_pre_chain"]["holdout"]["1800"]
    audit = {
        "schema_version": "event-factor-bench-formal-freeze-failures-v1",
        "statistical_protocol": {
            "config_version": old["protocol_version"],
            "config_sha256": old["protocol_sha256"],
            "claim_gate_sha256": "d" * 64,
            "development_audit_sha256": "e" * 64,
        },
        "failed_source_freeze": {
            "source_tag_object": "c" * 40,
            "source_commit": old["run_source_commit"],
            "collector_generated_at_utc": old["collector_generated_at"],
            "collector_manifest_sha256": old["collector_manifest_sha256"],
            "candidate_rows_gzip_sha256": old["candidate_rows_gzip_sha256"],
            "candidate_rows_content_sha256": old["candidate_rows_content_sha256"],
            "selection_audit_sha256": old["selection_audit_sha256"],
            "candidate_rows": old["candidate_rows"],
            "candidate_event_ids_any_horizon": old["candidate_event_ids_any_horizon"],
            "candidate_event_horizon_curves": old["candidate_event_horizon_curves"],
            "events_selected_before_history": old["events_selected_before_history"],
            "archived_api_responses": old["archived_api_responses"],
            "holdout_pre_chain_primary_coverage": {
                "horizon_seconds": 1800,
                **primary,
            },
        },
    }
    _write_json(path, audit)
    return path


def _pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    old = _snapshot(
        tmp_path,
        "collection-v0.1",
        source_commit="a" * 40,
        generated_at="2026-07-14T20:12:00Z",
    )
    new = _snapshot(
        tmp_path,
        "collection-v0.1.1",
        source_commit="b" * 40,
        generated_at="2026-07-15T05:00:00Z",
    )
    audit = _failure_audit(tmp_path / "formal_freeze_failures_v0.1.json", old)
    return old, new, audit


def _write_public_bundle(
    tmp_path: Path,
    new_dir: Path,
    report_path: Path,
    report: dict[str, Any],
) -> tuple[Path, Path]:
    collector = tmp_path / "data" / "collector_manifest_v0.1.json"
    collector.parent.mkdir(exist_ok=True)
    collector.write_bytes((new_dir / "manifest.json").read_bytes())
    collector_sha = compare.sha256_file(collector)
    new = report["new"]
    chain = {
        "run_source_commit": new["run_source_commit"],
        "protocol": {"sha256": new["protocol_sha256"]},
        "collector_manifest": {
            "sha256": collector_sha,
            "run_source_commit": new["run_source_commit"],
        },
        "candidate_manifest": {
            "file_sha256": new["candidate_rows_gzip_sha256"],
            "content_sha256": new["candidate_rows_content_sha256"],
            "rows": new["candidate_rows"],
            "event_ids_any_horizon": new["candidate_event_ids_any_horizon"],
            "event_horizon_curves": new["candidate_event_horizon_curves"],
        },
        "coverage_pre_chain": new["coverage_pre_chain"],
        "files": {
            "collector_manifest_v0.1.json": {"sha256": collector_sha},
            compare.REPORT_FILE_NAME: {
                "sha256": compare.sha256_file(report_path),
                "report_payload_sha256": report["report_payload_sha256"],
                "new_collector_manifest_sha256": collector_sha,
            },
        },
    }
    chain_path = tmp_path / "data" / "manifest_v0.1.json"
    _write_json(chain_path, chain)
    return collector, chain_path


def test_generate_passes_when_only_expected_metadata_changes(tmp_path: Path) -> None:
    old, new, audit = _pair(tmp_path)

    report = compare.generate_report(old, new, audit)

    compare.validate_report_self_hash(report)
    assert report["gate_passed"] is True
    assert report["missing_explanations"] == []
    assert not any(item["changed"] for item in report["substantive_comparisons"])
    changed_metadata = {
        item["field"] for item in report["expected_metadata_comparisons"] if item["changed"]
    }
    assert changed_metadata == {
        "local_directory_name",
        "collector_manifest_sha256",
        "collector_generated_at",
        "run_source_commit",
    }


def test_substantive_deltas_require_one_nonempty_explanation_each(tmp_path: Path) -> None:
    old, _, audit = _pair(tmp_path)
    changed = _snapshot(
        tmp_path,
        "changed-v0.1.1",
        source_commit="b" * 40,
        generated_at="2026-07-15T05:00:00Z",
        rows=[
            ("event-1", "1800", "changed-a"),
            ("event-3", "1800", "changed-b"),
        ],
        raw_response_count=4,
        selected_events=3,
    )

    failed = compare.generate_report(old, changed, audit)

    assert failed["gate_passed"] is False
    assert "candidate_rows_content_sha256" in failed["missing_explanations"]
    explanations = {key: f"documented reason for {key}" for key in failed["missing_explanations"]}
    passed = compare.generate_report(old, changed, audit, explanations=explanations)
    assert passed["gate_passed"] is True
    assert passed["missing_explanations"] == []


def test_explanations_cannot_cite_unchanged_or_unknown_fields(tmp_path: Path) -> None:
    old, new, audit = _pair(tmp_path)

    with pytest.raises(compare.ComparisonError, match="unchanged or unknown"):
        compare.generate_report(old, new, audit, explanations={"candidate_rows": "not changed"})


def test_old_snapshot_must_match_failure_audit(tmp_path: Path) -> None:
    old, new, audit = _pair(tmp_path)
    payload = _load_json(audit)
    payload["failed_source_freeze"]["candidate_rows"] += 1
    _write_json(audit, payload)

    with pytest.raises(compare.ComparisonError, match="differs from the failure audit"):
        compare.generate_report(old, new, audit)


def test_selection_audit_is_hashed_without_parsing_label_records(tmp_path: Path) -> None:
    directory = _snapshot(
        tmp_path,
        "selection-hash-only",
        source_commit="a" * 40,
        generated_at="2026-07-14T20:12:00Z",
    )
    selection = directory / "selection_audit.json"
    selection.write_bytes(b"opaque selection bytes with no parsed label records\n")
    manifest_path = directory / "manifest.json"
    manifest = _load_json(manifest_path)
    record = next(item for item in manifest["artifacts"] if item["path"] == selection.name)
    record.update(_artifact_record(selection))
    _write_json(manifest_path, manifest)

    snapshot = compare.inspect_snapshot(directory)

    assert snapshot["selection_audit_sha256"] == _sha256(selection.read_bytes())


def test_generate_cli_writes_failed_report_before_returning_two(tmp_path: Path) -> None:
    old, _, audit = _pair(tmp_path)
    changed = _snapshot(
        tmp_path,
        "cli-v0.1.1",
        source_commit="b" * 40,
        generated_at="2026-07-15T05:00:00Z",
        rows=[("event-1", "1800", "changed")],
        selected_events=1,
    )
    output = changed / compare.REPORT_FILE_NAME

    status = compare.main(
        [
            "generate",
            "--old-dir",
            str(old),
            "--new-dir",
            str(changed),
            "--failure-audit",
            str(audit),
            "--output",
            str(output),
        ]
    )

    assert status == 2
    assert output.exists()
    assert _load_json(output)["gate_passed"] is False


def test_verify_public_accepts_all_hash_bindings(tmp_path: Path) -> None:
    old, new, audit = _pair(tmp_path)
    report = compare.generate_report(old, new, audit)
    report_path = tmp_path / "data" / compare.REPORT_FILE_NAME
    compare.write_report(report_path, report)
    collector, chain = _write_public_bundle(tmp_path, new, report_path, report)

    verified = compare.verify_public(report_path, audit, collector, chain)

    assert verified["report_payload_sha256"] == report["report_payload_sha256"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("gate", "gate is inconsistent"),
        ("access_policy", "access policy is not canonical"),
        ("comparison_rows", "comparison rows do not match"),
        ("self_hash", "self-hash does not match"),
        ("report_file", "comparison file SHA-256 differs"),
        ("new_collector", "not bound to the new collector"),
        ("candidate_content", "candidate content hash differs"),
        ("candidate_event_count", "event_ids_any_horizon differs"),
    ],
)
def test_verify_public_rejects_gate_or_binding_tampering(
    tmp_path: Path,
    mutation: str,
    expected: str,
) -> None:
    old, new, audit = _pair(tmp_path)
    report = compare.generate_report(old, new, audit)
    report_path = tmp_path / "data" / compare.REPORT_FILE_NAME
    compare.write_report(report_path, report)
    collector, chain_path = _write_public_bundle(tmp_path, new, report_path, report)
    if mutation in {"gate", "access_policy", "comparison_rows", "self_hash"}:
        tampered = _load_json(report_path)
        if mutation == "gate":
            tampered["gate_passed"] = False
            tampered = compare._attach_self_hash(
                {key: value for key, value in tampered.items() if key != "report_payload_sha256"}
            )
        elif mutation == "access_policy":
            tampered["access_policy"]["frozen_evidence_accessed"] = True
            tampered = compare._attach_self_hash(
                {key: value for key, value in tampered.items() if key != "report_payload_sha256"}
            )
        elif mutation == "comparison_rows":
            tampered["substantive_comparisons"].pop()
            tampered = compare._attach_self_hash(
                {key: value for key, value in tampered.items() if key != "report_payload_sha256"}
            )
        else:
            tampered["new"]["candidate_rows"] += 1
        _write_json(report_path, tampered)
    else:
        chain = _load_json(chain_path)
        comparison_file = chain["files"][compare.REPORT_FILE_NAME]
        if mutation == "report_file":
            comparison_file["sha256"] = "f" * 64
        elif mutation == "new_collector":
            comparison_file["new_collector_manifest_sha256"] = "f" * 64
        elif mutation == "candidate_content":
            chain["candidate_manifest"]["content_sha256"] = "f" * 64
        else:
            chain["candidate_manifest"]["event_ids_any_horizon"] = 999
        _write_json(chain_path, chain)

    with pytest.raises(compare.ComparisonError, match=expected):
        compare.verify_public(report_path, audit, collector, chain_path)


def test_verify_public_requires_a_legitimately_passing_gate(tmp_path: Path) -> None:
    old, _, audit = _pair(tmp_path)
    changed = _snapshot(
        tmp_path,
        "failed-gate-v0.1.1",
        source_commit="b" * 40,
        generated_at="2026-07-15T05:00:00Z",
        rows=[("event-1", "1800", "changed")],
        selected_events=1,
    )
    report = compare.generate_report(old, changed, audit)
    assert report["gate_passed"] is False
    report_path = tmp_path / "data" / compare.REPORT_FILE_NAME
    compare.write_report(report_path, report)
    collector, chain = _write_public_bundle(tmp_path, changed, report_path, report)

    with pytest.raises(compare.ComparisonError, match="gate did not pass"):
        compare.verify_public(report_path, audit, collector, chain)


def test_load_explanations_accepts_inline_or_file_and_rejects_empty(tmp_path: Path) -> None:
    path = tmp_path / "explanations.json"
    _write_json(path, {"candidate_rows": "documented source drift"})

    assert compare.load_explanations(str(path)) == {"candidate_rows": "documented source drift"}
    assert compare.load_explanations('{"candidate_rows":"inline reason"}') == {
        "candidate_rows": "inline reason"
    }
    with pytest.raises(compare.ComparisonError, match="non-empty strings"):
        compare.load_explanations('{"candidate_rows":"  "}')
    with pytest.raises(compare.ComparisonError, match="duplicate JSON object key"):
        compare.load_explanations('{"candidate_rows":"first","candidate_rows":"second"}')
