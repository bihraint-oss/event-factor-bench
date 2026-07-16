#!/usr/bin/env python3
"""Evaluate or verify the public Gamma-API-labeled EventFactorBench snapshot."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from event_factor_bench.calibration import fit_beta_calibrator, fit_platt_calibrator
from event_factor_bench.evaluation import (
    _calibrator_dict,
    _evaluate_slice,
    _project_all,
    sha256_file,
    sha256_gzip_content,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = PROJECT_ROOT / "data/gamma_snapshot_v0.2.csv.gz"
DEFAULT_MANIFEST = PROJECT_ROOT / "data/gamma_snapshot_manifest_v0.2.json"
DEFAULT_PROTOCOL = PROJECT_ROOT / "configs/protocol_v0.1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "results/gamma_snapshot_v0.2/results.json"
LABEL_SOURCE = "gamma_terminal_outcome_prices_candidate"
SNAPSHOT_VERSION = "0.2.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HEX32_RE = re.compile(r"^0x[0-9a-f]{64}$")

EXPECTED_COLUMNS = {
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
    "event_title",
    "question",
    "no_token",
    "event_horizon_contract_count",
    "gamma_candidate_label_source",
    "gamma_candidate_label_onchain_verified",
}


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """One point-in-time threshold forecast with a retrospective Gamma label."""

    event_id: str
    market_id: str
    condition_id: str
    asset: str
    scheduled_time: datetime
    utc_day: str
    split: str
    horizon_seconds: int
    cutoff_time: datetime
    threshold: float
    yes_token: str
    label: float
    reference_probability: float
    reference_timestamp: datetime
    staleness_seconds: int
    source_event_sha256: str
    source_history_sha256: str
    event_horizon_contract_count: int


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _parse_utc(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError(f"invalid {name}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be explicitly UTC")
    return parsed.astimezone(UTC)


def _parse_row(raw: dict[str, str], line_number: int) -> SnapshotRow:
    try:
        label = float(raw["gamma_candidate_label"])
        if raw["gamma_candidate_label_source"] != LABEL_SOURCE:
            raise ValueError("unexpected Gamma label source")
        if raw["gamma_candidate_label_onchain_verified"] != "False":
            raise ValueError("snapshot must explicitly declare labels as not on-chain verified")
        row = SnapshotRow(
            event_id=raw["event_id"],
            market_id=raw["market_id"],
            condition_id=raw["condition_id"].lower(),
            asset=raw["asset"],
            scheduled_time=_parse_utc(raw["scheduled_time"], "scheduled_time"),
            utc_day=raw["utc_day"],
            split=raw["split"],
            horizon_seconds=int(raw["horizon_seconds"]),
            cutoff_time=_parse_utc(raw["cutoff_time"], "cutoff_time"),
            threshold=float(raw["threshold"]),
            yes_token=raw["yes_token"],
            label=label,
            reference_probability=float(raw["reference_probability"]),
            reference_timestamp=_parse_utc(raw["reference_timestamp"], "reference_timestamp"),
            staleness_seconds=int(raw["staleness_seconds"]),
            source_event_sha256=raw["source_event_sha256"],
            source_history_sha256=raw["source_history_sha256"],
            event_horizon_contract_count=int(raw["event_horizon_contract_count"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid snapshot row at line {line_number}: {error}") from error
    if not all((row.event_id, row.market_id, row.asset, row.split, row.utc_day, row.yes_token)):
        raise ValueError(f"empty identifier at line {line_number}")
    if HEX32_RE.fullmatch(row.condition_id) is None or not row.yes_token.isdecimal():
        raise ValueError(f"invalid market identifier at line {line_number}")
    if row.label not in (0.0, 1.0):
        raise ValueError(f"non-binary Gamma label at line {line_number}")
    if not math.isfinite(row.threshold) or not math.isfinite(row.reference_probability):
        raise ValueError(f"non-finite numeric field at line {line_number}")
    if not 0.0 <= row.reference_probability <= 1.0:
        raise ValueError(f"probability outside [0, 1] at line {line_number}")
    if row.horizon_seconds <= 0 or row.staleness_seconds < 0:
        raise ValueError(f"invalid horizon or staleness at line {line_number}")
    if row.event_horizon_contract_count <= 0:
        raise ValueError(f"invalid event ladder size at line {line_number}")
    for digest in (row.source_event_sha256, row.source_history_sha256):
        if SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"invalid source digest at line {line_number}")
    return row


def read_snapshot(path: Path) -> list[SnapshotRow]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        if fields != EXPECTED_COLUMNS:
            raise ValueError(
                "snapshot columns differ: "
                f"missing={sorted(EXPECTED_COLUMNS - fields)!r}, "
                f"unexpected={sorted(fields - EXPECTED_COLUMNS)!r}"
            )
        rows = [_parse_row(raw, line) for line, raw in enumerate(reader, start=2)]
    if not rows:
        raise ValueError("snapshot is empty")
    return rows


def validate_snapshot(
    rows: list[SnapshotRow],
    protocol: dict[str, Any],
    manifest: dict[str, Any],
    *,
    data_path: Path,
    manifest_path: Path,
    protocol_path: Path,
) -> dict[str, Any]:
    if manifest.get("schema_version") != "event-factor-bench-collector-v1":
        raise ValueError("unsupported collector manifest schema")
    if manifest.get("benchmark") != "EventFactorBench":
        raise ValueError("collector manifest benchmark mismatch")
    protocol_sha = sha256_file(protocol_path)
    if manifest.get("protocol_sha256") != protocol_sha:
        raise ValueError("collector manifest protocol hash mismatch")
    source_commit = manifest.get("run_source_commit")
    if not isinstance(source_commit, str) or COMMIT_RE.fullmatch(source_commit) is None:
        raise ValueError("collector source commit is invalid")

    artifact_records = manifest.get("artifacts")
    if not isinstance(artifact_records, list):
        raise ValueError("collector manifest artifacts are absent")
    candidate_records = [
        item
        for item in artifact_records
        if isinstance(item, dict) and item.get("path") == "candidate_rows_v0.1.csv.gz"
    ]
    if len(candidate_records) != 1:
        raise ValueError("collector manifest must bind exactly one candidate snapshot")
    candidate = candidate_records[0]
    if candidate.get("sha256") != sha256_file(data_path):
        raise ValueError("snapshot file hash differs from collector manifest")
    if candidate.get("bytes") != data_path.stat().st_size:
        raise ValueError("snapshot byte size differs from collector manifest")

    caveat = manifest.get("label_caveat")
    if not isinstance(caveat, str) or "not a canonical label" not in caveat:
        raise ValueError("collector manifest lacks the Gamma-label caveat")
    raw_responses = manifest.get("raw_responses")
    if not isinstance(raw_responses, list) or not raw_responses:
        raise ValueError("collector manifest has no raw-response provenance")
    raw_paths: set[str] = set()
    source_content_hashes: dict[str, set[str]] = defaultdict(set)
    for index, record in enumerate(raw_responses):
        if not isinstance(record, dict):
            raise ValueError(f"raw-response record {index} is not an object")
        path = record.get("path")
        if not isinstance(path, str) or not path or path in raw_paths:
            raise ValueError("raw-response paths must be unique non-empty strings")
        raw_paths.add(path)
        source = record.get("source")
        if source not in {"gamma.events_keyset", "clob.batch_prices_history"}:
            raise ValueError(f"raw-response record {index} has an unexpected source")
        for name in ("content_sha256", "gzip_sha256"):
            digest = str(record.get(name, ""))
            if SHA256_RE.fullmatch(digest) is None:
                raise ValueError(f"raw-response record {index} has invalid {name}")
            if name == "content_sha256":
                source_content_hashes[str(source)].add(digest)

    forecast = protocol["forecast"]
    allowed_splits = protocol["splits"]
    allowed_assets = set(protocol["universe"]["assets"])
    allowed_horizons = {
        int(forecast["primary_horizon_seconds"]),
        *map(int, forecast["secondary_horizons_seconds"]),
    }
    maximum_staleness = int(forecast["max_staleness_seconds"])
    split_bounds = {
        split: (
            _parse_utc(bounds["start_inclusive"], f"{split} start"),
            _parse_utc(bounds["end_exclusive"], f"{split} end"),
        )
        for split, bounds in allowed_splits.items()
    }

    keys: set[tuple[str, str, int]] = set()
    market_owner: dict[str, str] = {}
    market_properties: dict[tuple[str, str], tuple[Any, ...]] = {}
    event_properties: dict[str, tuple[Any, ...]] = {}
    curve_rows: dict[tuple[str, int], list[SnapshotRow]] = defaultdict(list)
    for row in rows:
        key = (row.event_id, row.market_id, row.horizon_seconds)
        if key in keys:
            raise ValueError(f"duplicate snapshot row: {key!r}")
        keys.add(key)
        if row.split not in split_bounds or row.asset not in allowed_assets:
            raise ValueError(f"row lies outside the declared split or asset universe: {key!r}")
        if row.horizon_seconds not in allowed_horizons:
            raise ValueError(f"undeclared forecast horizon: {key!r}")
        start, end = split_bounds[row.split]
        if not start <= row.scheduled_time < end:
            raise ValueError(f"scheduled time lies outside its split: {key!r}")
        if row.utc_day != row.scheduled_time.date().isoformat():
            raise ValueError(f"UTC day disagrees with scheduled time: {key!r}")
        if row.cutoff_time != row.scheduled_time - timedelta(seconds=row.horizon_seconds):
            raise ValueError(f"cutoff does not match the horizon: {key!r}")
        age = (row.cutoff_time - row.reference_timestamp).total_seconds()
        if age != row.staleness_seconds or not 0 <= age <= maximum_staleness:
            raise ValueError(f"reference timestamp violates point-in-time rules: {key!r}")
        if row.source_event_sha256 not in source_content_hashes["gamma.events_keyset"]:
            raise ValueError(f"event source hash is absent from collector provenance: {key!r}")
        if row.source_history_sha256 not in source_content_hashes["clob.batch_prices_history"]:
            raise ValueError(f"history source hash is absent from collector provenance: {key!r}")

        owner = market_owner.setdefault(row.market_id, row.event_id)
        if owner != row.event_id:
            raise ValueError(f"market appears in multiple events: {row.market_id!r}")
        properties = (
            row.condition_id,
            row.threshold,
            row.label,
            row.yes_token,
            row.source_history_sha256,
        )
        previous = market_properties.setdefault((row.event_id, row.market_id), properties)
        if previous != properties:
            raise ValueError(f"market properties differ across horizons: {row.market_id!r}")
        event = (row.asset, row.split, row.utc_day, row.scheduled_time, row.source_event_sha256)
        previous_event = event_properties.setdefault(row.event_id, event)
        if previous_event != event:
            raise ValueError(f"event properties disagree: {row.event_id!r}")
        curve_rows[(row.event_id, row.horizon_seconds)].append(row)

    ladders: dict[str, set[tuple[str, float, float]]] = {}
    for curve, members in curve_rows.items():
        if len(members) != members[0].event_horizon_contract_count:
            raise ValueError(f"event ladder count mismatch: {curve!r}")
        if len({member.threshold for member in members}) != len(members):
            raise ValueError(f"event ladder contains duplicate thresholds: {curve!r}")
        ordered_labels = [
            member.label for member in sorted(members, key=lambda item: item.threshold)
        ]
        if any(left < right for left, right in pairwise(ordered_labels)):
            raise ValueError(f"Gamma outcome ladder is not non-increasing: {curve!r}")
        ladder = {(member.market_id, member.threshold, member.label) for member in members}
        previous_ladder = ladders.setdefault(curve[0], ladder)
        if previous_ladder != ladder:
            raise ValueError(f"event ladder differs across horizons: {curve[0]!r}")

    coverage = manifest.get("coverage_pre_chain")
    if not isinstance(coverage, dict):
        raise ValueError("collector manifest lacks coverage records")
    cell_counts = Counter((row.split, str(row.horizon_seconds)) for row in rows)
    expected_cells = {
        (split, str(horizon)) for split in allowed_splits for horizon in allowed_horizons
    }
    if set(cell_counts) != expected_cells:
        raise ValueError("snapshot does not contain the complete split-by-horizon grid")
    cell_events = {
        cell: len({row.event_id for row in rows if (row.split, str(row.horizon_seconds)) == cell})
        for cell in cell_counts
    }
    for cell, row_count in cell_counts.items():
        try:
            record = coverage[cell[0]][cell[1]]
        except (KeyError, TypeError) as error:
            raise ValueError(f"collector coverage is missing {cell!r}") from error
        if record.get("history_retained_rows") != row_count:
            raise ValueError(f"collector row coverage disagrees for {cell!r}")
        if record.get("history_retained_events") != cell_events[cell]:
            raise ValueError(f"collector event coverage disagrees for {cell!r}")
        expected_events = int(record.get("expected_events", -1))
        expected_rows = int(record.get("expected_rows", -1))
        if expected_events < cell_events[cell] or expected_rows < row_count:
            raise ValueError(f"collector coverage denominator is invalid for {cell!r}")
        expected_event_ratio = cell_events[cell] / expected_events
        expected_row_ratio = row_count / expected_rows
        if not math.isclose(
            float(record.get("event_history_coverage", -1.0)),
            expected_event_ratio,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(f"collector event coverage ratio disagrees for {cell!r}")
        if not math.isclose(
            float(record.get("row_history_coverage", -1.0)),
            expected_row_ratio,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(f"collector row coverage ratio disagrees for {cell!r}")

    return {
        "data_sha256": sha256_file(data_path),
        "data_content_sha256": sha256_gzip_content(data_path),
        "manifest_sha256": sha256_file(manifest_path),
        "protocol_sha256": protocol_sha,
        "source_commit": source_commit,
        "raw_response_records": len(raw_responses),
    }


def _coverage(manifest: dict[str, Any], split: str, horizon: int) -> dict[str, Any]:
    record = manifest["coverage_pre_chain"][split][str(horizon)]
    return {
        "expected_events": int(record["expected_events"]),
        "retained_events": int(record["history_retained_events"]),
        "expected_rows": int(record["expected_rows"]),
        "retained_rows": int(record["history_retained_rows"]),
        "event_coverage": float(record["event_history_coverage"]),
        "row_coverage": float(record["row_history_coverage"]),
    }


def evaluate_snapshot(
    rows: list[SnapshotRow],
    protocol: dict[str, Any],
    manifest: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    forecast = protocol["forecast"]
    inference = protocol["inference"]
    primary_horizon = int(forecast["primary_horizon_seconds"])
    horizons = [primary_horizon, *map(int, forecast["secondary_horizons_seconds"])]
    raw = np.asarray([row.reference_probability for row in rows], dtype=np.float64)
    predictions = {"raw": raw, "pav_raw": _project_all(rows, raw)}

    calibrators: dict[int, dict[str, Any]] = {}
    for horizon in horizons:
        indices = [
            index
            for index, row in enumerate(rows)
            if row.split == forecast["calibration_training_split"]
            and row.horizon_seconds == horizon
        ]
        event_ids = [rows[index].event_id for index in indices]
        labels = np.asarray([rows[index].label for index in indices])
        probabilities = raw[indices]
        kwargs = {
            "epsilon": float(forecast["calibration_epsilon"]),
            "l2": float(forecast["calibration_l2"]),
            "tolerance": float(forecast["calibration_tolerance"]),
            "max_iterations": int(forecast["calibration_max_iterations"]),
        }
        calibrators[horizon] = {
            "platt": fit_platt_calibrator(event_ids, labels, probabilities, **kwargs),
            "beta": fit_beta_calibrator(event_ids, labels, probabilities, **kwargs),
        }

    platt = np.empty(len(rows), dtype=np.float64)
    beta = np.empty(len(rows), dtype=np.float64)
    for horizon, models in calibrators.items():
        indices = [index for index, row in enumerate(rows) if row.horizon_seconds == horizon]
        platt[indices] = models["platt"].predict(raw[indices])
        beta[indices] = models["beta"].predict(raw[indices])
    predictions.update({"platt": platt, "beta": beta, "pav_beta": _project_all(rows, beta)})

    evaluated: dict[str, dict[str, Any]] = {}
    for split in protocol["splits"]:
        evaluated[split] = {}
        for horizon in horizons:
            indices = [
                index
                for index, row in enumerate(rows)
                if row.split == split and row.horizon_seconds == horizon
            ]
            evaluated[split][str(horizon)] = _evaluate_slice(
                rows,
                predictions,
                indices,
                inference=inference,
                log_epsilon=float(forecast["metric_log_loss_epsilon"]),
                bootstrap=split == "holdout",
            )

    primary = evaluated["holdout"][str(primary_horizon)]
    comparison = primary["comparisons"]["pav_raw_vs_raw"]
    coverage = _coverage(manifest, "holdout", primary_horizon)
    configured = protocol["claim_gate"]
    declared_assets = set(protocol["universe"]["assets"])
    subgroups = primary["asset_subgroups"]
    conditions = {
        "minimum_holdout_events": primary["events"] >= int(configured["minimum_holdout_events"]),
        "minimum_holdout_utc_days": primary["utc_days"]
        >= int(configured["minimum_holdout_utc_days"]),
        "minimum_event_coverage": coverage["event_coverage"]
        >= float(configured["minimum_event_coverage"]),
        "minimum_contract_coverage": coverage["row_coverage"]
        >= float(configured["minimum_contract_coverage"]),
        "minimum_relative_brier_improvement": comparison["relative_brier_improvement"]
        >= float(configured["minimum_relative_brier_improvement"]),
        "brier_ci_lower_above_zero": comparison["brier_delta_ci"][0] > 0.0,
        "log_loss_regression_within_limit": comparison["log_loss_delta"]
        >= -float(configured["maximum_event_macro_log_loss_regression"]),
        "nonnegative_asset_subgroups": set(subgroups) == declared_assets
        and all(values["brier_delta"] >= -1e-15 for values in subgroups.values()),
        "all_projection_violations_removed": primary["methods"]["pav_raw"][
            "monotonicity_violation_edges"
        ]
        == 0,
    }

    return {
        "benchmark": "EventFactorBench",
        "release_version": SNAPSHOT_VERSION,
        "protocol_version": protocol["version"],
        "run_source_commit": provenance["source_commit"],
        "scope": "retrospective descriptive benchmark",
        "label_provenance": {
            "source": LABEL_SOURCE,
            "semantics": "strict terminal Gamma outcomePrices [1,0] or [0,1]",
            "onchain_verified": False,
            "canonical_chain_claim_allowed": False,
        },
        "evidence": {
            "path": "data/gamma_snapshot_v0.2.csv.gz",
            "sha256": provenance["data_sha256"],
            "uncompressed_sha256": provenance["data_content_sha256"],
            "rows": len(rows),
            "events": len({row.event_id for row in rows}),
            "markets": len({row.market_id for row in rows}),
            "event_horizon_curves": len({(row.event_id, row.horizon_seconds) for row in rows}),
            "manifest_path": "data/gamma_snapshot_manifest_v0.2.json",
            "manifest_sha256": provenance["manifest_sha256"],
            "protocol_sha256": provenance["protocol_sha256"],
            "raw_response_records": provenance["raw_response_records"],
            "raw_response_bytes_redistributed": False,
        },
        "calibrators": {
            str(horizon): {name: _calibrator_dict(model) for name, model in models.items()}
            for horizon, models in calibrators.items()
        },
        "splits": evaluated,
        "reporting_screen": {
            "statistical_screen_passed": all(conditions.values()),
            "conditions": conditions,
            "coverage": coverage,
            "confirmatory_frozen_protocol_claim": False,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true")
    mode.add_argument("--verify", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rows = read_snapshot(args.data)
    protocol = _load_json(args.protocol)
    manifest = _load_json(args.manifest)
    provenance = validate_snapshot(
        rows,
        protocol,
        manifest,
        data_path=args.data,
        manifest_path=args.manifest,
        protocol_path=args.protocol,
    )
    if args.validate_only:
        print(f"validated {len(rows):,} Gamma-labeled rows; onchain_verified=False")
        return
    result = evaluate_snapshot(rows, protocol, manifest, provenance)
    if args.verify:
        expected = _load_json(args.output)
        if expected != result:
            raise SystemExit("Gamma snapshot result verification failed")
        print(f"verified {args.output}")
        return
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite existing result: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
