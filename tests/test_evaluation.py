from __future__ import annotations

import csv
import gzip
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import event_factor_bench.evaluation as evaluation_module
from event_factor_bench.evaluation import (
    EvidenceRow,
    evaluate_frozen,
    read_evidence,
    validate_frozen_inputs,
)

EVIDENCE_SHA = "a" * 64
PROTOCOL_SHA = "b" * 64
MANIFEST_SHA = "c" * 64
UNCOMPRESSED_SHA = "d" * 64
COLLECTOR_SHA = "0" * 64
RAW_LOG_SHA = "1" * 64
RAW_BLOCK_SHA = "2" * 64
CHAIN_SOURCE_SHA = hashlib.sha256(
    b"event-factor-bench-chain-source-v1\x00"
    + bytes.fromhex(RAW_LOG_SHA)
    + bytes.fromhex(RAW_BLOCK_SHA)
).hexdigest()


def _rpc_record(sequence: int, method: str, params: list, response_sha256: str) -> dict:
    request = {"jsonrpc": "2.0", "id": sequence, "method": method, "params": params}
    request_bytes = json.dumps(
        request,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "sequence": sequence,
        "request_id": sequence,
        "method": method,
        "params": params,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "response_sha256": response_sha256,
    }


FROZEN_FIELDS = [
    "event_id",
    "market_id",
    "condition_id",
    "asset",
    "scheduled_time",
    "utc_day",
    "split",
    "horizon_seconds",
    "cutoff_time",
    "threshold",
    "gamma_candidate_label",
    "yes_token",
    "reference_probability",
    "reference_timestamp",
    "staleness_seconds",
    "source_event_sha256",
    "source_history_sha256",
    "event_horizon_contract_count",
    "label",
    "payout_vector",
    "resolution_block_number",
    "resolution_block_hash",
    "resolution_block_timestamp",
    "resolution_tx_hash",
    "resolution_log_index",
    "chain_source_sha256",
]


def protocol() -> dict:
    return {
        "benchmark": "EventFactorBench",
        "version": "0.1.0",
        "retrieval": {
            "gamma_endpoint": "https://gamma.invalid/events/keyset",
            "clob_endpoint": "https://clob.invalid/batch-prices-history",
            "gamma_title_search": "above",
            "history_fidelity_minutes": 1,
            "history_window_seconds": 2700,
        },
        "universe": {
            "assets": ["Bitcoin", "Ethereum"],
            "minimum_thresholds_per_event": 4,
            "conditional_tokens_address": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
            "condition_resolution_topic": (
                "0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894"
            ),
            "resolution_grace_seconds": 7200,
        },
        "splits": {
            "development": {
                "start_inclusive": "2026-06-01T00:00:00Z",
                "end_exclusive": "2026-06-20T00:00:00Z",
            },
            "validation": {
                "start_inclusive": "2026-06-20T00:00:00Z",
                "end_exclusive": "2026-06-30T00:00:00Z",
            },
            "holdout": {
                "start_inclusive": "2026-06-30T00:00:00Z",
                "end_exclusive": "2026-07-14T00:00:00Z",
            },
        },
        "forecast": {
            "primary_horizon_seconds": 1800,
            "secondary_horizons_seconds": [900],
            "max_staleness_seconds": 120,
            "calibration_training_split": "development",
            "calibration_epsilon": 1e-6,
            "calibration_l2": 1e-3,
            "calibration_tolerance": 1e-9,
            "calibration_max_iterations": 100,
            "metric_log_loss_epsilon": 1e-6,
        },
        "inference": {"resamples": 100, "confidence": 0.95, "seed": 7},
        "claim_gate": {
            "minimum_holdout_events": 4,
            "minimum_holdout_utc_days": 2,
            "minimum_event_coverage": 0.95,
            "minimum_contract_coverage": 0.95,
            "minimum_relative_brier_improvement": 0.001,
            "maximum_event_macro_log_loss_regression": 1.0,
        },
    }


def evidence_rows() -> list[EvidenceRow]:
    rows: list[EvidenceRow] = []
    probabilities = [0.9, 0.65, 0.7, 0.1]
    starts = {
        "development": datetime(2026, 6, 1, tzinfo=UTC),
        "validation": datetime(2026, 6, 20, tzinfo=UTC),
        "holdout": datetime(2026, 6, 30, tzinfo=UTC),
    }
    for split_index, split in enumerate(("development", "validation", "holdout")):
        for horizon in (1800, 900):
            for event_number in range(4):
                scheduled = starts[split] + timedelta(days=event_number % 2, hours=event_number + 1)
                boundary = 1 + (event_number % 3)
                event_id = f"{split}-{event_number}"
                for threshold_index, probability in enumerate(probabilities):
                    label = float(threshold_index <= boundary)
                    condition_number = split_index * 100 + event_number * 10 + threshold_index + 1
                    cutoff = scheduled - timedelta(seconds=horizon)
                    rows.append(
                        EvidenceRow(
                            event_id=event_id,
                            market_id=f"{event_id}-{threshold_index}",
                            condition_id=f"0x{condition_number:064x}",
                            asset="Bitcoin" if event_number % 2 == 0 else "Ethereum",
                            scheduled_time=scheduled,
                            split=split,
                            utc_day=scheduled.date().isoformat(),
                            horizon_seconds=horizon,
                            cutoff_time=cutoff,
                            threshold=float(threshold_index),
                            gamma_candidate_label="Yes" if label else "No",
                            yes_token=str(10_000 + condition_number),
                            label=label,
                            reference_probability=probability,
                            reference_timestamp=cutoff - timedelta(seconds=30),
                            staleness_seconds=30,
                            source_event_sha256="5" * 64,
                            source_history_sha256="6" * 64,
                            event_horizon_contract_count=4,
                            payout_vector=(1, 0) if label else (0, 1),
                            resolution_block_number=70_000_000,
                            resolution_block_hash="0x" + "7" * 64,
                            resolution_block_timestamp=scheduled + timedelta(seconds=60),
                            resolution_tx_hash="0x" + "8" * 64,
                            resolution_log_index=threshold_index,
                            chain_source_sha256=CHAIN_SOURCE_SHA,
                        )
                    )
    return rows


def _coverage_cell() -> dict:
    return {
        "expected_events": 4,
        "retained_events": 4,
        "expected_rows": 16,
        "retained_rows": 16,
        "history_retained_events": 4,
        "history_retained_rows": 16,
        "event_coverage": 1.0,
        "row_coverage": 1.0,
        "event_history_coverage": 1.0,
        "row_history_coverage": 1.0,
        "event_chain_given_history_coverage": 1.0,
        "row_chain_given_history_coverage": 1.0,
        "history_coverage": {"event": 1.0, "row": 1.0},
        "chain_given_history_coverage": {"event": 1.0, "row": 1.0},
    }


def _seal_manifest(value: dict) -> dict:
    value.pop("manifest_payload_sha256", None)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    value["manifest_payload_sha256"] = hashlib.sha256(payload).hexdigest()
    return value


def manifest(rows: list[EvidenceRow]) -> dict:
    coverage = {
        split: {str(horizon): _coverage_cell() for horizon in (1800, 900)}
        for split in ("development", "validation", "holdout")
    }
    pre_chain = {
        split: {
            str(horizon): {
                "expected_events": 4,
                "history_retained_events": 4,
                "expected_rows": 16,
                "history_retained_rows": 16,
                "event_history_coverage": 1.0,
                "row_history_coverage": 1.0,
            }
            for horizon in (1800, 900)
        }
        for split in ("development", "validation", "holdout")
    }
    flat = [
        {"split": split, "horizon": horizon, **coverage[split][str(horizon)]}
        for split in ("development", "validation", "holdout")
        for horizon in (1800, 900)
    ]
    condition_count = len({row.condition_id for row in rows})
    value = {
        "schema_version": "event-factor-bench-chain-freeze-v2",
        "run_source_commit": "9" * 40,
        "onchain_verified": True,
        "gamma_candidate_mismatch_count": 0,
        "protocol": {"path": "configs/protocol_v0.1.json", "sha256": PROTOCOL_SHA},
        "files": {
            "collector_manifest_v0.1.json": {
                "sha256": COLLECTOR_SHA,
                "bytes": 1234,
            },
            "collector_comparison_v0.1.1.json": {
                "sha256": "7" * 64,
                "report_payload_sha256": "8" * 64,
                "new_collector_manifest_sha256": COLLECTOR_SHA,
            },
            "frozen_v0.1.csv.gz": {
                "sha256": EVIDENCE_SHA,
                "uncompressed_sha256": UNCOMPRESSED_SHA,
                "rows": len(rows),
            },
        },
        "chain": {
            "chain_id": 137,
            "conditional_tokens_address": protocol()["universe"]["conditional_tokens_address"],
            "condition_resolution_topic": protocol()["universe"]["condition_resolution_topic"],
            "resolution_grace_seconds": 7200,
        },
        "candidate_manifest": {
            "path": "candidate_rows_v0.1.csv.gz",
            "file_sha256": "e" * 64,
            "content_sha256": "f" * 64,
        },
        "collector_manifest": {
            "path": "manifest.json",
            "sha256": COLLECTOR_SHA,
            "run_source_commit": "9" * 40,
        },
        "coverage_pre_chain": pre_chain,
        "coverage": coverage,
        "coverage_by_split_horizon": flat,
        "resolution_counts": {
            "conditions": {
                "agreed": condition_count,
                "missing": 0,
                "ambiguous": 0,
                "gamma_disagreement": 0,
            },
            "candidate_rows": {
                "agreed": len(rows),
                "missing": 0,
                "ambiguous": 0,
                "gamma_disagreement": 0,
            },
            "gamma_agreement_conditions": condition_count,
            "missing_conditions": 0,
            "ambiguous_conditions": 0,
            "gamma_disagreement_conditions": 0,
        },
        "summary": {
            "expected_conditions": condition_count,
            "verified_conditions": condition_count,
            "expected_rows": len(rows),
            "directly_verified_rows": len(rows),
            "retained_rows_after_complete_event_gate": len(rows),
        },
        "raw_responses": [
            _rpc_record(
                1,
                "eth_getLogs",
                [{"fromBlock": "0x1", "toBlock": "0x2", "topics": []}],
                RAW_LOG_SHA,
            ),
            _rpc_record(2, "eth_getBlockByNumber", ["0x2", False], RAW_BLOCK_SHA),
        ],
        "chain_source_bundles": [
            {
                "chain_source_sha256": CHAIN_SOURCE_SHA,
                "raw_response_sha256s": [RAW_LOG_SHA, RAW_BLOCK_SHA],
            }
        ],
    }
    return _seal_manifest(value)


def collector_manifest(frozen_manifest: dict) -> dict:
    yes_tokens = sorted({row.yes_token for row in evidence_rows()}, key=int)
    history_records = []
    for batch_index in range(0, len(yes_tokens), 20):
        body = {
            "end_ts": 1_780_272_000,
            "fidelity": 1,
            "markets": yes_tokens[batch_index : batch_index + 20],
            "start_ts": 1_780_269_300,
        }
        body_bytes = json.dumps(
            body, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        history_records.append(
            {
                "path": f"raw/clob/event_fixture/batch_{batch_index // 20:04d}.json.gz",
                "source": "clob.batch_prices_history",
                "request": {
                    "method": "POST",
                    "url": "https://clob.invalid/batch-prices-history",
                    "body": body,
                    "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
                },
                "archived_at_utc": "2026-07-14T18:01:00Z",
                "content_bytes": 100,
                "content_sha256": "6" * 64,
                "gzip_bytes": 80,
                "gzip_sha256": "2" * 64,
            }
        )
    return {
        "schema_version": "event-factor-bench-collector-v1",
        "protocol_sha256": PROTOCOL_SHA,
        "run_source_commit": "9" * 40,
        "coverage_pre_chain": frozen_manifest["coverage_pre_chain"],
        "artifacts": [
            {
                "path": "candidate_rows_v0.1.csv.gz",
                "sha256": "e" * 64,
            }
        ],
        "raw_responses": [
            {
                "path": "raw/gamma/window_0000/page_0000.json.gz",
                "source": "gamma.events_keyset",
                "request": {
                    "method": "GET",
                    "url": (
                        "https://gamma.invalid/events/keyset?closed=true&"
                        "end_date_max=2026-06-02T00%3A00%3A00Z&"
                        "end_date_min=2026-06-01T00%3A00%3A00Z&limit=100&"
                        "title_search=above"
                    ),
                },
                "archived_at_utc": "2026-07-14T18:00:00Z",
                "content_bytes": 100,
                "content_sha256": "5" * 64,
                "gzip_bytes": 80,
                "gzip_sha256": "1" * 64,
            },
            *history_records,
        ],
    }


def _row_dict(row: EvidenceRow) -> dict[str, object]:
    result = {field: getattr(row, field) for field in FROZEN_FIELDS}
    for field in (
        "scheduled_time",
        "cutoff_time",
        "reference_timestamp",
        "resolution_block_timestamp",
    ):
        result[field] = getattr(row, field).isoformat().replace("+00:00", "Z")
    result["payout_vector"] = json.dumps(row.payout_vector, separators=(",", ":"))
    return result


def _evaluate(rows: list[EvidenceRow], frozen_manifest: dict) -> dict:
    return evaluate_frozen(
        rows,
        protocol(),
        frozen_manifest,
        collector_manifest(frozen_manifest),
        evidence_sha256=EVIDENCE_SHA,
        evidence_uncompressed_sha256=UNCOMPRESSED_SHA,
        protocol_sha256=PROTOCOL_SHA,
        manifest_sha256=MANIFEST_SHA,
        collector_manifest_sha256=COLLECTOR_SHA,
    )


def test_validate_frozen_inputs_does_not_fit_or_score(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)

    def forbidden_fit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("validate-only mode must not fit a calibrator")

    monkeypatch.setattr(evaluation_module, "fit_platt_calibrator", forbidden_fit)
    monkeypatch.setattr(evaluation_module, "fit_beta_calibrator", forbidden_fit)
    validate_frozen_inputs(
        rows,
        protocol(),
        frozen_manifest,
        collector_manifest(frozen_manifest),
        evidence_sha256=EVIDENCE_SHA,
        evidence_uncompressed_sha256=UNCOMPRESSED_SHA,
        protocol_sha256=PROTOCOL_SHA,
        manifest_sha256=MANIFEST_SHA,
        collector_manifest_sha256=COLLECTOR_SHA,
    )


def test_evaluate_frozen_reports_strong_baselines_and_projection() -> None:
    rows = evidence_rows()
    result = _evaluate(rows, manifest(rows))
    primary = result["splits"]["holdout"]["1800"]
    assert set(primary["methods"]) == {"raw", "pav_raw", "platt", "beta", "pav_beta"}
    assert primary["methods"]["raw"]["monotonicity_violation_edges"] == 4
    assert primary["methods"]["pav_raw"]["monotonicity_violation_edges"] == 0
    assert primary["comparisons"]["pav_raw_vs_raw"]["brier_delta"] > 0.0
    assert result["claim_gate"]["primary_improvement_passed"] is True


def test_read_evidence_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "evidence.csv.gz"
    rows = evidence_rows()
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FROZEN_FIELDS)
        writer.writeheader()
        writer.writerows(_row_dict(row) for row in rows)
    assert read_evidence(path) == rows


def test_read_evidence_rejects_nonmonotone_canonical_labels(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv.gz"
    rows = [
        row
        for row in evidence_rows()
        if row.event_id == "development-0" and row.horizon_seconds == 1800
    ]
    rows[0] = replace(rows[0], label=0.0, gamma_candidate_label="No", payout_vector=(0, 1))
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FROZEN_FIELDS)
        writer.writeheader()
        writer.writerows(_row_dict(row) for row in rows)
    with pytest.raises(ValueError, match="not non-increasing"):
        read_evidence(path)


def test_evaluate_rejects_manifest_self_hash_tampering() -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)
    frozen_manifest["onchain_verified"] = False
    with pytest.raises(ValueError, match="self-hash"):
        _evaluate(rows, frozen_manifest)


def test_evaluate_recomputes_coverage_from_evidence() -> None:
    rows = evidence_rows()
    frozen_manifest = manifest(rows)
    frozen_manifest["coverage"]["holdout"]["1800"]["retained_rows"] = 15
    _seal_manifest(frozen_manifest)
    with pytest.raises(ValueError, match="coverage count retained_rows"):
        _evaluate(rows, frozen_manifest)


def test_evaluate_rejects_event_crossing_splits_between_horizons() -> None:
    rows = evidence_rows()
    index = next(
        index
        for index, row in enumerate(rows)
        if row.event_id == "development-0" and row.horizon_seconds == 900
    )
    rows[index] = replace(rows[index], split="validation")
    with pytest.raises(ValueError, match="event properties disagree"):
        _evaluate(rows, manifest(evidence_rows()))
