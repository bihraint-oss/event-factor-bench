from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_evaluation import (
    COLLECTOR_SHA,
    EVIDENCE_SHA,
    FROZEN_FIELDS,
    MANIFEST_SHA,
    PROTOCOL_SHA,
    UNCOMPRESSED_SHA,
    _evaluate,
    _row_dict,
    _seal_manifest,
    collector_manifest,
    evidence_rows,
    manifest,
    protocol,
)

import event_factor_bench.evaluation as evaluation


def _set_path(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    target = value
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = replacement


def _write_csv(path: Path, fields: list[str], row: dict[str, object]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(row)


def test_hash_and_json_helpers_bind_exact_bytes(tmp_path: Path) -> None:
    payload = b"canonical evidence\n"
    plain = tmp_path / "evidence.csv"
    plain.write_bytes(payload)
    compressed = tmp_path / "evidence.csv.gz"
    with gzip.open(compressed, "wb") as handle:
        handle.write(payload)

    assert evaluation.sha256_file(plain) == hashlib.sha256(payload).hexdigest()
    assert evaluation.sha256_gzip_content(compressed) == hashlib.sha256(payload).hexdigest()
    assert evaluation.sha256_file(compressed) != evaluation.sha256_gzip_content(compressed)

    object_path = tmp_path / "object.json"
    object_path.write_text('{"frozen": true}', encoding="utf-8")
    assert evaluation.load_json(object_path) == {"frozen": True}

    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        evaluation.load_json(array_path)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ([field for field in FROZEN_FIELDS if field != "event_id"], "missing columns"),
        ([*FROZEN_FIELDS, "undeclared"], "unexpected columns"),
    ],
)
def test_read_evidence_rejects_schema_drift(
    tmp_path: Path, fields: list[str], expected: str
) -> None:
    path = tmp_path / "tampered.csv.gz"
    row = _row_dict(evidence_rows()[0])
    row["undeclared"] = "surprise"
    _write_csv(path, fields, row)
    with pytest.raises(ValueError, match=expected):
        evaluation.read_evidence(path)


@pytest.mark.parametrize(
    ("field", "replacement", "expected"),
    [
        ("payout_vector", "[]", "invalid evidence row"),
        ("scheduled_time", "not-a-time", "invalid evidence row"),
        ("threshold", "nan", "non-finite threshold"),
        ("label", "0.5", "non-binary label"),
        ("reference_probability", "1.01", "probability outside"),
        ("horizon_seconds", "0", "non-positive horizon"),
        ("staleness_seconds", "-1", "negative staleness"),
        ("event_horizon_contract_count", "0", "non-positive event contract count"),
        ("resolution_block_number", "-1", "negative chain coordinate"),
        ("event_id", "", "empty identifier"),
        ("yes_token", "0x123", "non-decimal yes_token"),
        ("source_event_sha256", "A" * 64, "lowercase SHA-256"),
        ("condition_id", "0x1234", "invalid condition_id"),
        ("payout_vector", "[0,1]", "label/payout disagreement"),
        ("gamma_candidate_label", "No", "Gamma/canonical-label disagreement"),
    ],
)
def test_row_parser_rejects_semantic_tampering(field: str, replacement: str, expected: str) -> None:
    row = _row_dict(evidence_rows()[0])
    row[field] = replacement
    with pytest.raises(ValueError, match=expected):
        evaluation._parse_row(row, 7)


def test_row_parser_requires_explicit_utc() -> None:
    row = _row_dict(evidence_rows()[0])
    row["scheduled_time"] = "2026-06-01T01:00:00"
    with pytest.raises(ValueError, match="invalid evidence row"):
        evaluation._parse_row(row, 7)


def test_evaluate_rejects_empty_evidence() -> None:
    with pytest.raises(ValueError, match="empty"):
        evaluation.evaluate_frozen(
            [],
            protocol(),
            {},
            {},
            evidence_sha256=EVIDENCE_SHA,
            evidence_uncompressed_sha256=UNCOMPRESSED_SHA,
            protocol_sha256=PROTOCOL_SHA,
            manifest_sha256=MANIFEST_SHA,
            collector_manifest_sha256=COLLECTOR_SHA,
        )


@pytest.mark.parametrize(
    ("digest_name", "expected"),
    [
        ("evidence_sha256", "evidence SHA-256"),
        ("evidence_uncompressed_sha256", "uncompressed evidence SHA-256"),
        ("protocol_sha256", "protocol SHA-256"),
        ("manifest_sha256", "manifest file SHA-256"),
        ("collector_manifest_sha256", "collector manifest file SHA-256"),
    ],
)
def test_evaluate_rejects_noncanonical_binding_digests(digest_name: str, expected: str) -> None:
    rows = evidence_rows()
    digests = {
        "evidence_sha256": EVIDENCE_SHA,
        "evidence_uncompressed_sha256": UNCOMPRESSED_SHA,
        "protocol_sha256": PROTOCOL_SHA,
        "manifest_sha256": MANIFEST_SHA,
        "collector_manifest_sha256": COLLECTOR_SHA,
    }
    digests[digest_name] = "A" * 64
    with pytest.raises(ValueError, match=expected):
        frozen_manifest = manifest(rows)
        evaluation.evaluate_frozen(
            rows,
            protocol(),
            frozen_manifest,
            collector_manifest(frozen_manifest),
            **digests,
        )


@pytest.mark.parametrize(
    ("path", "replacement", "expected"),
    [
        (("schema_version",), "v2", "manifest schema"),
        (("protocol", "sha256"), "a" * 64, "protocol SHA-256"),
        (("files", "frozen_v0.1.csv.gz", "sha256"), "b" * 64, "evidence SHA-256"),
        (
            ("files", "frozen_v0.1.csv.gz", "uncompressed_sha256"),
            "c" * 64,
            "decompressed evidence SHA-256",
        ),
        (("files", "frozen_v0.1.csv.gz", "rows"), 95, "row count"),
        (
            ("files", "collector_comparison_v0.1.1.json", "new_collector_manifest_sha256"),
            "f" * 64,
            "comparison is not bound",
        ),
        (("run_source_commit",), "9" * 39, "run_source_commit"),
        (("chain", "chain_id"), 1, "Polygon chain 137"),
        (("chain", "conditional_tokens_address"), "0xdead", "address differs"),
        (("chain", "condition_resolution_topic"), "0xdead", "topic differs"),
        (("chain", "resolution_grace_seconds"), 7201, "grace differs"),
    ],
)
def test_manifest_binding_tampering_fails_closed(
    path: tuple[str, ...], replacement: Any, expected: str
) -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)
    _set_path(frozen_manifest, path, replacement)
    _seal_manifest(frozen_manifest)
    with pytest.raises(ValueError, match=expected):
        _evaluate(rows, frozen_manifest)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("schema", "schema_version"),
        ("unknown_source", "unknown source"),
        ("unsafe_path", "unsafe source path"),
        ("event_hash", "absent from Gamma provenance"),
        ("history_hash", "absent from CLOB provenance"),
        ("history_token", "absent from its CLOB history request"),
        ("body_hash", "body_sha256 does not match"),
    ],
)
def test_collector_provenance_tampering_fails_closed(mutation: str, expected: str) -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)
    collector = collector_manifest(frozen_manifest)
    gamma = collector["raw_responses"][0]
    clob_records = collector["raw_responses"][1:]
    if mutation == "schema":
        collector["schema_version"] = "v2"
    elif mutation == "unknown_source":
        gamma["source"] = "unknown"
    elif mutation == "unsafe_path":
        gamma["path"] = "../gamma.json.gz"
    elif mutation == "event_hash":
        rows = [replace(row, source_event_sha256="d" * 64) for row in rows]
    elif mutation == "history_hash":
        rows = [replace(row, source_history_sha256="e" * 64) for row in rows]
    elif mutation == "history_token":
        target = rows[0].yes_token
        record = next(item for item in clob_records if target in item["request"]["body"]["markets"])
        markets = record["request"]["body"]["markets"]
        markets[markets.index(target)] = "999999999999"
        body = json.dumps(
            record["request"]["body"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        record["request"]["body_sha256"] = hashlib.sha256(body).hexdigest()
    elif mutation == "body_hash":
        clob_records[0]["request"]["body_sha256"] = "0" * 64

    with pytest.raises(ValueError, match=expected):
        evaluation.evaluate_frozen(
            rows,
            protocol(),
            frozen_manifest,
            collector,
            evidence_sha256=EVIDENCE_SHA,
            evidence_uncompressed_sha256=UNCOMPRESSED_SHA,
            protocol_sha256=PROTOCOL_SHA,
            manifest_sha256=MANIFEST_SHA,
            collector_manifest_sha256=COLLECTOR_SHA,
        )


def test_protocol_rejects_undeclared_assets() -> None:
    rows = evidence_rows()
    frozen_protocol = protocol()
    frozen_protocol["universe"]["assets"] = []
    with pytest.raises(ValueError, match="undeclared assets"):
        evaluation.evaluate_frozen(
            rows,
            frozen_protocol,
            manifest(rows),
            collector_manifest(manifest(rows)),
            evidence_sha256=EVIDENCE_SHA,
            evidence_uncompressed_sha256=UNCOMPRESSED_SHA,
            protocol_sha256=PROTOCOL_SHA,
            manifest_sha256=MANIFEST_SHA,
            collector_manifest_sha256=COLLECTOR_SHA,
        )


@pytest.mark.parametrize(
    ("field", "replacement", "all_matching", "expected"),
    [
        (
            "scheduled_time",
            evidence_rows()[0].scheduled_time.replace(month=5, day=31),
            True,
            "outside split",
        ),
        ("utc_day", "2099-01-01", True, "UTC day differs"),
        (
            "cutoff_time",
            evidence_rows()[0].cutoff_time.replace(second=1),
            False,
            "cutoff time differs",
        ),
        (
            "reference_timestamp",
            evidence_rows()[0].cutoff_time.replace(second=1),
            False,
            "future reference point",
        ),
        ("staleness_seconds", 31, False, "invalid reference staleness"),
        ("event_horizon_contract_count", 3, False, "smaller than"),
        ("event_horizon_contract_count", 5, False, "contract count is inconsistent"),
    ],
)
def test_protocol_row_timing_and_ladder_tampering_fails_closed(
    field: str, replacement: Any, all_matching: bool, expected: str
) -> None:
    rows = evidence_rows()
    first = rows[0]
    for index, row in enumerate(rows):
        match = row.event_id == first.event_id if all_matching else index == 0
        if match:
            rows[index] = replace(row, **{field: replacement})
    with pytest.raises(ValueError, match=expected):
        _evaluate(rows, manifest(evidence_rows()))


def test_resolution_after_protocol_grace_fails_closed() -> None:
    rows = evidence_rows()
    condition_id = rows[0].condition_id
    invalid_time = rows[0].scheduled_time.replace(hour=4)
    rows = [
        replace(row, resolution_block_timestamp=invalid_time)
        if row.condition_id == condition_id
        else row
        for row in rows
    ]
    with pytest.raises(ValueError, match="resolution timestamp outside grace"):
        _evaluate(rows, manifest(evidence_rows()))


@pytest.mark.parametrize(
    ("path", "replacement", "expected"),
    [
        (("summary", "expected_conditions"), 49, "condition status counts"),
        (("summary", "expected_rows"), 97, "candidate-row status counts"),
        (("summary", "verified_conditions"), 47, "verified condition count"),
        (("summary", "directly_verified_rows"), 95, "directly verified row count"),
        (("summary", "retained_rows_after_complete_event_gate"), 95, "retained evidence count"),
        (("gamma_candidate_mismatch_count",), 1, "mismatch scalar"),
        (("onchain_verified",), False, "onchain_verified"),
        (("resolution_counts", "gamma_agreement_conditions"), 47, "Gamma agreement count"),
        (("resolution_counts", "missing_conditions"), 1, "missing condition count"),
        (("resolution_counts", "ambiguous_conditions"), 1, "ambiguous condition count"),
        (
            ("resolution_counts", "gamma_disagreement_conditions"),
            1,
            "Gamma disagreement count",
        ),
    ],
)
def test_status_algebra_tampering_fails_closed(
    path: tuple[str, ...], replacement: Any, expected: str
) -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)
    _set_path(frozen_manifest, path, replacement)
    _seal_manifest(frozen_manifest)
    with pytest.raises(ValueError, match=expected):
        _evaluate(rows, frozen_manifest)


def test_status_vocabulary_and_types_are_frozen() -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)
    frozen_manifest["resolution_counts"]["conditions"]["pending"] = 0
    _seal_manifest(frozen_manifest)
    with pytest.raises(ValueError, match="frozen status vocabulary"):
        _evaluate(rows, frozen_manifest)

    frozen_manifest = manifest(rows)
    frozen_manifest["summary"]["expected_conditions"] = True
    _seal_manifest(frozen_manifest)
    with pytest.raises(ValueError, match="non-negative integer"):
        _evaluate(rows, frozen_manifest)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing_cell", "coverage cells differ"),
        ("event_nesting", "event coverage counts are not nested"),
        ("row_nesting", "row coverage counts are not nested"),
        ("wrong_count", "coverage count expected_events"),
        ("boolean_ratio", "must be numeric"),
        ("nonnumeric_ratio", "must be numeric"),
        ("nonfinite_ratio", "Out of range float values"),
        ("prechain_ratio", "does not match its counts"),
        ("summary_total", "pre-chain candidate rows"),
        ("flat_absent", "flat coverage audit"),
        ("flat_undeclared", "undeclared cell"),
        ("flat_disagrees", "flat and nested"),
    ],
)
def test_coverage_audit_tampering_fails_closed(mutation: str, expected: str) -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)
    final = frozen_manifest["coverage"]["development"]["1800"]
    history = frozen_manifest["coverage_pre_chain"]["development"]["1800"]
    if mutation == "missing_cell":
        del frozen_manifest["coverage"]["development"]["1800"]
    elif mutation == "event_nesting":
        history["history_retained_events"] = 3
    elif mutation == "row_nesting":
        history["history_retained_rows"] = 15
    elif mutation == "wrong_count":
        final["expected_events"] = 5
    elif mutation == "boolean_ratio":
        final["event_coverage"] = True
    elif mutation == "nonnumeric_ratio":
        final["event_coverage"] = []
    elif mutation == "nonfinite_ratio":
        final["event_coverage"] = float("nan")
    elif mutation == "prechain_ratio":
        history["event_history_coverage"] = 0.5
    elif mutation == "summary_total":
        frozen_manifest["summary"]["expected_rows"] = 97
        frozen_manifest["summary"]["directly_verified_rows"] = 97
        frozen_manifest["resolution_counts"]["candidate_rows"]["agreed"] = 97
    elif mutation == "flat_absent":
        frozen_manifest["coverage_by_split_horizon"] = None
    elif mutation == "flat_undeclared":
        frozen_manifest["coverage_by_split_horizon"][0]["split"] = "undeclared"
    elif mutation == "flat_disagrees":
        frozen_manifest["coverage_by_split_horizon"][0]["event_coverage"] = 0.5
    else:  # pragma: no cover - parameter list is frozen above
        raise AssertionError(mutation)
    _seal_manifest(frozen_manifest)
    with pytest.raises(ValueError, match=expected):
        _evaluate(rows, frozen_manifest)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("no_raw", "no raw chain response"),
        ("request_sequence", "contiguous from one"),
        ("request_id", "contiguous from one"),
        ("request_params", "request SHA-256 does not match"),
        ("request_sha", "request SHA-256 does not match"),
        ("unknown_method", "unknown JSON-RPC method"),
        ("reversed_methods", "order log evidence before block evidence"),
        ("no_bundles", "no chain source bundles"),
        ("short_bundle", "must contain log and block"),
        ("absent_raw", "absent raw response"),
        ("invalid_bundle_hash", "invalid or duplicate"),
        ("duplicate_bundle", "invalid or duplicate"),
    ],
)
def test_chain_source_bundle_tampering_fails_closed(mutation: str, expected: str) -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)
    if mutation == "no_raw":
        frozen_manifest["raw_responses"] = []
    elif mutation == "request_sequence":
        frozen_manifest["raw_responses"][0]["sequence"] = 2
    elif mutation == "request_id":
        frozen_manifest["raw_responses"][0]["request_id"] = 2
    elif mutation == "request_params":
        frozen_manifest["raw_responses"][0]["params"][0]["toBlock"] = "0x3"
    elif mutation == "request_sha":
        frozen_manifest["raw_responses"][0]["request_sha256"] = "f" * 64
    elif mutation in {"unknown_method", "reversed_methods"}:
        records = frozen_manifest["raw_responses"]
        if mutation == "unknown_method":
            records[0]["method"] = "eth_fakeMethod"
        else:
            records[0]["method"], records[1]["method"] = (
                records[1]["method"],
                records[0]["method"],
            )
        for record in records:
            request = {
                "jsonrpc": "2.0",
                "id": record["request_id"],
                "method": record["method"],
                "params": record["params"],
            }
            record["request_sha256"] = hashlib.sha256(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
    elif mutation == "no_bundles":
        frozen_manifest["chain_source_bundles"] = []
    elif mutation == "short_bundle":
        frozen_manifest["chain_source_bundles"][0]["raw_response_sha256s"] = ["1" * 64]
    elif mutation == "absent_raw":
        frozen_manifest["chain_source_bundles"][0]["raw_response_sha256s"][1] = "3" * 64
    elif mutation == "invalid_bundle_hash":
        frozen_manifest["chain_source_bundles"][0]["chain_source_sha256"] = "4" * 64
    elif mutation == "duplicate_bundle":
        frozen_manifest["chain_source_bundles"].append(
            copy.deepcopy(frozen_manifest["chain_source_bundles"][0])
        )
    else:  # pragma: no cover - parameter list is frozen above
        raise AssertionError(mutation)
    _seal_manifest(frozen_manifest)
    with pytest.raises(ValueError, match=expected):
        _evaluate(rows, frozen_manifest)


def test_evidence_cannot_cite_an_unmanifested_chain_source() -> None:
    rows = [replace(row, chain_source_sha256="4" * 64) for row in evidence_rows()]
    with pytest.raises(ValueError, match="absent from manifest"):
        _evaluate(rows, manifest(evidence_rows()))


def test_evidence_identity_and_ladder_invariants_fail_closed() -> None:
    base = evidence_rows()

    with pytest.raises(ValueError, match="duplicate evidence row"):
        evaluation._validate_evidence([*base, base[0]])

    rows = base.copy()
    rows[4] = replace(rows[4], market_id=rows[0].market_id)
    with pytest.raises(ValueError, match="market appears in multiple events"):
        evaluation._validate_evidence(rows)

    rows = base.copy()
    peer = next(
        index
        for index, row in enumerate(rows)
        if row.market_id == rows[0].market_id and row.horizon_seconds != rows[0].horizon_seconds
    )
    rows[peer] = replace(rows[peer], source_history_sha256="4" * 64)
    with pytest.raises(ValueError, match="market properties disagree"):
        evaluation._validate_evidence(rows)

    rows = base.copy()
    rows[peer] = replace(rows[peer], resolution_tx_hash="0x" + "4" * 64)
    with pytest.raises(ValueError, match="chain evidence disagrees"):
        evaluation._validate_evidence(rows)

    rows = base.copy()
    first_market = rows[0].market_id
    for index, row in enumerate(rows):
        if row.market_id == first_market:
            rows[index] = replace(row, threshold=rows[1].threshold)
    with pytest.raises(ValueError, match="duplicate threshold"):
        evaluation._validate_evidence(rows)

    rows = [row for index, row in enumerate(base) if index != 0]
    with pytest.raises(ValueError, match="ladder differs across horizons"):
        evaluation._validate_evidence(rows)


def test_projection_and_manifest_coverage_helpers_fail_closed() -> None:
    rows = evidence_rows()
    with pytest.raises(ValueError, match="vector length"):
        evaluation._project_all(rows, np.zeros(len(rows) - 1))

    with pytest.raises(ValueError, match="lacks coverage"):
        evaluation._manifest_coverage({}, "holdout", 1800)

    frozen_manifest = manifest(rows)
    frozen_manifest["coverage"]["holdout"]["1800"]["event_coverage"] = 1.01
    with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
        evaluation._manifest_coverage(frozen_manifest, "holdout", 1800)
