"""Deterministic evaluation of a frozen EventFactorBench evidence table."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np
from numpy.typing import NDArray

from event_factor_bench.bootstrap import paired_event_block_bootstrap
from event_factor_bench.calibration import (
    LogisticCalibrator,
    fit_beta_calibrator,
    fit_platt_calibrator,
)
from event_factor_bench.metrics import event_macro_brier, event_macro_log_loss
from event_factor_bench.projection import project_threshold_probabilities


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """One canonical threshold forecast row."""

    event_id: str
    market_id: str
    condition_id: str
    asset: str
    scheduled_time: datetime
    split: str
    utc_day: str
    horizon_seconds: int
    cutoff_time: datetime
    threshold: float
    gamma_candidate_label: str
    yes_token: str
    label: float
    reference_probability: float
    reference_timestamp: datetime
    staleness_seconds: int
    source_event_sha256: str
    source_history_sha256: str
    event_horizon_contract_count: int
    payout_vector: tuple[int, int]
    resolution_block_number: int
    resolution_block_hash: str
    resolution_block_timestamp: datetime
    resolution_tx_hash: str
    resolution_log_index: int
    chain_source_sha256: str


_FROZEN_COLUMNS = {
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
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX_32_RE = re.compile(r"^0x[0-9a-f]{64}$")
_COLLECTOR_SCHEMA_VERSION = "event-factor-bench-collector-v1"
_CHAIN_SCHEMA_VERSION = "event-factor-bench-chain-freeze-v2"
_GAMMA_SOURCE = "gamma.events_keyset"
_CLOB_SOURCE = "clob.batch_prices_history"
_COLLECTOR_RAW_FIELDS = {
    "path",
    "source",
    "request",
    "archived_at_utc",
    "content_bytes",
    "content_sha256",
    "gzip_bytes",
    "gzip_sha256",
}


def read_evidence(path: str | Path) -> list[EvidenceRow]:
    """Read and validate the canonical gzip CSV evidence table."""

    source = Path(path)
    with gzip.open(source, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        missing = _FROZEN_COLUMNS - fields
        if missing:
            raise ValueError(f"evidence table is missing columns: {sorted(missing)!r}")
        unexpected = fields - _FROZEN_COLUMNS
        if unexpected:
            raise ValueError(f"evidence table has unexpected columns: {sorted(unexpected)!r}")
        rows = [_parse_row(row, line_number) for line_number, row in enumerate(reader, start=2)]
    _validate_evidence(rows)
    return rows


def evaluate_frozen(
    rows: list[EvidenceRow],
    protocol: dict[str, Any],
    manifest: dict[str, Any],
    collector_manifest: dict[str, Any],
    *,
    evidence_sha256: str,
    evidence_uncompressed_sha256: str,
    protocol_sha256: str,
    manifest_sha256: str,
    collector_manifest_sha256: str,
) -> dict[str, Any]:
    """Fit frozen comparators and evaluate every declared split and horizon."""

    validate_frozen_inputs(
        rows,
        protocol,
        manifest,
        collector_manifest,
        evidence_sha256=evidence_sha256,
        evidence_uncompressed_sha256=evidence_uncompressed_sha256,
        protocol_sha256=protocol_sha256,
        manifest_sha256=manifest_sha256,
        collector_manifest_sha256=collector_manifest_sha256,
    )
    forecast = protocol["forecast"]
    inference = protocol["inference"]
    primary_horizon = int(forecast["primary_horizon_seconds"])
    horizons = [primary_horizon, *map(int, forecast["secondary_horizons_seconds"])]
    if len(set(horizons)) != len(horizons):
        raise ValueError("forecast horizons must be unique")
    unexpected_horizons = {row.horizon_seconds for row in rows} - set(horizons)
    if unexpected_horizons:
        raise ValueError(f"evidence contains undeclared horizons: {sorted(unexpected_horizons)!r}")

    calibrators: dict[int, dict[str, LogisticCalibrator]] = {}
    predictions: dict[str, NDArray[np.float64]] = {
        "raw": np.asarray([row.reference_probability for row in rows], dtype=np.float64)
    }
    predictions["pav_raw"] = _project_all(rows, predictions["raw"])

    for horizon in horizons:
        train_indices = [
            index
            for index, row in enumerate(rows)
            if row.split == forecast["calibration_training_split"]
            and row.horizon_seconds == horizon
        ]
        if not train_indices:
            raise ValueError(f"no calibration rows for horizon {horizon}")
        event_ids = [rows[index].event_id for index in train_indices]
        labels = np.asarray([rows[index].label for index in train_indices])
        raw = predictions["raw"][train_indices]
        kwargs = {
            "epsilon": float(forecast["calibration_epsilon"]),
            "l2": float(forecast["calibration_l2"]),
            "tolerance": float(forecast["calibration_tolerance"]),
            "max_iterations": int(forecast["calibration_max_iterations"]),
        }
        calibrators[horizon] = {
            "platt": fit_platt_calibrator(event_ids, labels, raw, **kwargs),
            "beta": fit_beta_calibrator(event_ids, labels, raw, **kwargs),
        }

    platt = np.empty(len(rows), dtype=np.float64)
    beta = np.empty(len(rows), dtype=np.float64)
    for horizon, models in calibrators.items():
        indices = [index for index, row in enumerate(rows) if row.horizon_seconds == horizon]
        raw = predictions["raw"][indices]
        platt[indices] = models["platt"].predict(raw)
        beta[indices] = models["beta"].predict(raw)
    predictions["platt"] = platt
    predictions["beta"] = beta
    predictions["pav_beta"] = _project_all(rows, beta)

    split_order = list(protocol["splits"])
    log_epsilon = float(forecast["metric_log_loss_epsilon"])
    evaluated: dict[str, dict[str, Any]] = {}
    for split in split_order:
        evaluated[split] = {}
        for horizon in horizons:
            indices = [
                index
                for index, row in enumerate(rows)
                if row.split == split and row.horizon_seconds == horizon
            ]
            if not indices:
                raise ValueError(f"no evidence rows for {split=} and {horizon=}")
            evaluated[split][str(horizon)] = _evaluate_slice(
                rows,
                predictions,
                indices,
                inference=inference,
                log_epsilon=log_epsilon,
                bootstrap=split == "holdout",
            )

    gates = _claim_gate(
        protocol,
        manifest,
        evaluated["holdout"][str(primary_horizon)],
        primary_horizon,
    )
    return {
        "benchmark": protocol["benchmark"],
        "version": protocol["version"],
        "run_source_commit": manifest.get("run_source_commit"),
        "evidence": {
            "path": "data/frozen_v0.1.csv.gz",
            "sha256": evidence_sha256,
            "uncompressed_sha256": evidence_uncompressed_sha256,
            "rows": len(rows),
            "events": len({row.event_id for row in rows}),
            "protocol_sha256": protocol_sha256,
            "manifest_sha256": manifest_sha256,
            "collector_manifest_sha256": collector_manifest_sha256,
        },
        "calibrators": {
            str(horizon): {name: _calibrator_dict(model) for name, model in models.items()}
            for horizon, models in calibrators.items()
        },
        "splits": evaluated,
        "claim_gate": gates,
    }


def validate_frozen_inputs(
    rows: list[EvidenceRow],
    protocol: dict[str, Any],
    manifest: dict[str, Any],
    collector_manifest: dict[str, Any],
    *,
    evidence_sha256: str,
    evidence_uncompressed_sha256: str,
    protocol_sha256: str,
    manifest_sha256: str,
    collector_manifest_sha256: str,
) -> None:
    """Validate public evidence bindings without fitting models or computing scores."""

    if not rows:
        raise ValueError("evidence table is empty")
    _validate_frozen_bindings(
        rows,
        protocol,
        manifest,
        collector_manifest,
        evidence_sha256=evidence_sha256,
        evidence_uncompressed_sha256=evidence_uncompressed_sha256,
        protocol_sha256=protocol_sha256,
        manifest_sha256=manifest_sha256,
        collector_manifest_sha256=collector_manifest_sha256,
    )


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest of a file's exact bytes."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_gzip_content(path: str | Path) -> str:
    """Return the SHA-256 digest of a gzip file's exact decompressed bytes."""

    digest = hashlib.sha256()
    with gzip.open(Path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: str | Path) -> dict[str, Any]:
    """Load a JSON object and reject other top-level values."""

    value = json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _validate_frozen_bindings(
    rows: list[EvidenceRow],
    protocol: Mapping[str, Any],
    manifest: Mapping[str, Any],
    collector_manifest: Mapping[str, Any],
    *,
    evidence_sha256: str,
    evidence_uncompressed_sha256: str,
    protocol_sha256: str,
    manifest_sha256: str,
    collector_manifest_sha256: str,
) -> None:
    """Fail closed when evidence, protocol, and chain manifest are not one frozen run."""

    _validate_evidence(rows)
    for value, name in (
        (evidence_sha256, "evidence SHA-256"),
        (evidence_uncompressed_sha256, "uncompressed evidence SHA-256"),
        (protocol_sha256, "protocol SHA-256"),
        (manifest_sha256, "manifest file SHA-256"),
        (collector_manifest_sha256, "collector manifest file SHA-256"),
    ):
        _require_digest(value, name)

    if manifest.get("schema_version") != _CHAIN_SCHEMA_VERSION:
        raise ValueError("unsupported or missing chain-freeze manifest schema")
    self_digest = manifest.get("manifest_payload_sha256")
    _require_digest(self_digest, "manifest_payload_sha256")
    payload_without_self = dict(manifest)
    del payload_without_self["manifest_payload_sha256"]
    canonical = json.dumps(
        payload_without_self,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    if hashlib.sha256(canonical).hexdigest() != self_digest:
        raise ValueError("manifest payload self-hash does not match its contents")

    manifest_protocol = _mapping(manifest.get("protocol"), "manifest protocol")
    if manifest_protocol.get("sha256") != protocol_sha256:
        raise ValueError("manifest protocol SHA-256 does not match supplied protocol")
    files = _mapping(manifest.get("files"), "manifest files")
    collector_file = _mapping(
        files.get("collector_manifest_v0.1.json"), "collector manifest file record"
    )
    if collector_file.get("sha256") != collector_manifest_sha256:
        raise ValueError("public collector manifest SHA-256 does not match chain manifest")
    comparison_file = _mapping(
        files.get("collector_comparison_v0.1.1.json"),
        "collector comparison file record",
    )
    _require_digest(comparison_file.get("sha256"), "collector comparison file SHA-256")
    _require_digest(
        comparison_file.get("report_payload_sha256"),
        "collector comparison payload SHA-256",
    )
    _require_digest(
        comparison_file.get("new_collector_manifest_sha256"),
        "collector comparison new-collector SHA-256",
    )
    if comparison_file.get("new_collector_manifest_sha256") != collector_manifest_sha256:
        raise ValueError("collector comparison is not bound to the public collector manifest")
    frozen = _mapping(files.get("frozen_v0.1.csv.gz"), "frozen evidence file record")
    if frozen.get("sha256") != evidence_sha256:
        raise ValueError("frozen evidence SHA-256 does not match manifest")
    if frozen.get("uncompressed_sha256") != evidence_uncompressed_sha256:
        raise ValueError("decompressed evidence SHA-256 does not match manifest")
    if _strict_nonnegative_int(frozen.get("rows"), "frozen evidence rows") != len(rows):
        raise ValueError("frozen evidence row count does not match manifest")

    source_commit = manifest.get("run_source_commit")
    if not isinstance(source_commit, str) or _COMMIT_RE.fullmatch(source_commit) is None:
        raise ValueError("run_source_commit must be a full lowercase 40-hex commit")
    collector_source = _mapping(manifest.get("collector_manifest"), "collector manifest source")
    if collector_source.get("run_source_commit") != source_commit:
        raise ValueError("collector and chain source commits do not match")
    _require_digest(collector_source.get("sha256"), "collector manifest SHA-256")
    if collector_source.get("sha256") != collector_manifest_sha256:
        raise ValueError("collector source SHA-256 differs from the public collector file")
    candidate_source = _mapping(manifest.get("candidate_manifest"), "candidate manifest source")
    _require_digest(candidate_source.get("file_sha256"), "candidate file SHA-256")
    _require_digest(candidate_source.get("content_sha256"), "candidate content SHA-256")
    _validate_collector_manifest(
        collector_manifest,
        rows=rows,
        protocol=protocol,
        chain_manifest=manifest,
        protocol_sha256=protocol_sha256,
        run_source_commit=source_commit,
        candidate_sha256=str(candidate_source["file_sha256"]),
    )

    universe = _mapping(protocol.get("universe"), "protocol universe")
    chain = _mapping(manifest.get("chain"), "manifest chain")
    if _strict_nonnegative_int(chain.get("chain_id"), "chain_id") != 137:
        raise ValueError("manifest is not bound to Polygon chain 137")
    address = str(universe.get("conditional_tokens_address", "")).lower()
    topic = str(universe.get("condition_resolution_topic", "")).lower()
    if str(chain.get("conditional_tokens_address", "")).lower() != address:
        raise ValueError("manifest ConditionalTokens address differs from protocol")
    if str(chain.get("condition_resolution_topic", "")).lower() != topic:
        raise ValueError("manifest ConditionResolution topic differs from protocol")
    grace = _strict_nonnegative_int(
        universe.get("resolution_grace_seconds"), "protocol resolution grace"
    )
    if (
        _strict_nonnegative_int(chain.get("resolution_grace_seconds"), "manifest resolution grace")
        != grace
    ):
        raise ValueError("manifest resolution grace differs from protocol")

    splits = _mapping(protocol.get("splits"), "protocol splits")
    forecast = _mapping(protocol.get("forecast"), "protocol forecast")
    horizons = {
        int(forecast["primary_horizon_seconds"]),
        *map(int, forecast["secondary_horizons_seconds"]),
    }
    expected_cells = {(str(split), str(horizon)) for split in splits for horizon in horizons}
    actual_cells = {(row.split, str(row.horizon_seconds)) for row in rows}
    if actual_cells != expected_cells:
        raise ValueError("evidence cells differ from the protocol split-by-horizon grid")
    declared_assets = {str(asset) for asset in universe.get("assets", ())}
    actual_assets = {row.asset for row in rows}
    if not declared_assets or not actual_assets.issubset(declared_assets):
        raise ValueError("evidence contains undeclared assets or protocol declares none")
    _validate_rows_against_protocol(rows, protocol, grace=grace)
    _validate_chain_sources(rows, manifest)
    _validate_status_algebra(rows, manifest)
    _validate_coverage(rows, manifest, expected_cells)


def _validate_rows_against_protocol(
    rows: list[EvidenceRow], protocol: Mapping[str, Any], *, grace: int
) -> None:
    splits = _mapping(protocol.get("splits"), "protocol splits")
    universe = _mapping(protocol.get("universe"), "protocol universe")
    forecast = _mapping(protocol.get("forecast"), "protocol forecast")
    minimum = _strict_nonnegative_int(
        universe.get("minimum_thresholds_per_event"), "minimum thresholds per event"
    )
    max_staleness = _strict_nonnegative_int(
        forecast.get("max_staleness_seconds"), "maximum staleness"
    )
    boundaries = {
        str(split): (
            _parse_utc(_mapping(value, f"split {split}")["start_inclusive"], "split start"),
            _parse_utc(_mapping(value, f"split {split}")["end_exclusive"], "split end"),
        )
        for split, value in splits.items()
    }
    for row in rows:
        if row.split not in boundaries:
            raise ValueError(f"evidence contains undeclared split {row.split!r}")
        start, end = boundaries[row.split]
        if not start <= row.scheduled_time < end:
            raise ValueError(f"scheduled time lies outside split for event {row.event_id!r}")
        if row.utc_day != row.scheduled_time.date().isoformat():
            raise ValueError(f"UTC day differs from scheduled time for event {row.event_id!r}")
        if row.cutoff_time != row.scheduled_time - timedelta(seconds=row.horizon_seconds):
            raise ValueError(f"cutoff time differs from horizon for market {row.market_id!r}")
        if row.reference_timestamp > row.cutoff_time:
            raise ValueError(f"future reference point for market {row.market_id!r}")
        staleness = int((row.cutoff_time - row.reference_timestamp).total_seconds())
        if row.staleness_seconds != staleness or staleness > max_staleness:
            raise ValueError(f"invalid reference staleness for market {row.market_id!r}")
        if (
            not row.scheduled_time
            <= row.resolution_block_timestamp
            <= row.scheduled_time + timedelta(seconds=grace)
        ):
            raise ValueError(f"resolution timestamp outside grace for market {row.market_id!r}")
        if row.event_horizon_contract_count < minimum:
            raise ValueError(f"event ladder is smaller than the protocol minimum: {row.event_id!r}")

    grouped: dict[tuple[str, int], list[EvidenceRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.event_id, row.horizon_seconds)].append(row)
    for event_key, event_rows in grouped.items():
        declared = {row.event_horizon_contract_count for row in event_rows}
        if declared != {len(event_rows)}:
            raise ValueError(f"event contract count is inconsistent for {event_key!r}")


def _validate_collector_manifest(
    collector: Mapping[str, Any],
    *,
    rows: list[EvidenceRow],
    protocol: Mapping[str, Any],
    chain_manifest: Mapping[str, Any],
    protocol_sha256: str,
    run_source_commit: str,
    candidate_sha256: str,
) -> None:
    if collector.get("schema_version") != _COLLECTOR_SCHEMA_VERSION:
        raise ValueError("unsupported or missing collector manifest schema_version")
    if collector.get("protocol_sha256") != protocol_sha256:
        raise ValueError("collector manifest protocol SHA-256 does not match")
    if collector.get("run_source_commit") != run_source_commit:
        raise ValueError("public collector manifest source commit does not match")
    if collector.get("coverage_pre_chain") != chain_manifest.get("coverage_pre_chain"):
        raise ValueError("collector and chain pre-history coverage records differ")
    artifacts = collector.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("collector manifest has no artifact records")
    candidates = [
        record
        for record in artifacts
        if isinstance(record, Mapping)
        and Path(str(record.get("path", ""))).name == "candidate_rows_v0.1.csv.gz"
    ]
    if len(candidates) != 1 or candidates[0].get("sha256") != candidate_sha256:
        raise ValueError("collector candidate artifact does not match chain input")
    _validate_collector_raw_provenance(collector, rows=rows, protocol=protocol)


def _validate_collector_raw_provenance(
    collector: Mapping[str, Any],
    *,
    rows: list[EvidenceRow],
    protocol: Mapping[str, Any],
) -> None:
    try:
        retrieval = _mapping(protocol["retrieval"], "protocol retrieval")
        gamma_endpoint = str(retrieval["gamma_endpoint"])
        clob_endpoint = str(retrieval["clob_endpoint"])
        title_search = str(retrieval["gamma_title_search"])
        fidelity = int(retrieval["history_fidelity_minutes"])
        history_window = int(retrieval["history_window_seconds"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "protocol retrieval settings are incomplete for collector provenance"
        ) from error
    if not gamma_endpoint or not clob_endpoint or not title_search:
        raise ValueError("protocol collector endpoints and title search must be non-empty")

    raw_responses = collector.get("raw_responses")
    if not isinstance(raw_responses, list) or not raw_responses:
        raise ValueError("collector manifest has no API response provenance")
    paths: set[str] = set()
    signatures: set[tuple[str, str, str]] = set()
    gamma_hashes: set[str] = set()
    clob_tokens_by_hash: dict[str, set[str]] = defaultdict(set)
    for index, raw in enumerate(raw_responses):
        record = _mapping(raw, f"collector raw response {index}")
        if set(record) != _COLLECTOR_RAW_FIELDS:
            raise ValueError(f"collector raw response {index} must use the exact v1 field set")
        source = record.get("source")
        if source not in {_GAMMA_SOURCE, _CLOB_SOURCE}:
            raise ValueError(f"collector raw response {index} has an unknown source")
        relative = _collector_raw_path(record.get("path"), str(source), index)
        if relative in paths:
            raise ValueError("collector raw response paths must be unique")
        paths.add(relative)
        content_sha256 = record.get("content_sha256")
        _require_digest(content_sha256, f"collector raw response {index} content SHA-256")
        _require_digest(record.get("gzip_sha256"), f"collector raw response {index} gzip SHA-256")
        for field in ("content_bytes", "gzip_bytes"):
            if (
                _strict_nonnegative_int(
                    record.get(field), f"collector raw response {index} {field}"
                )
                == 0
            ):
                raise ValueError(f"collector raw response {index} {field} must be positive")
        archived_at = record.get("archived_at_utc")
        if not isinstance(archived_at, str) or not archived_at.endswith("Z"):
            raise ValueError(
                f"collector raw response {index} archived_at_utc must be canonical UTC"
            )
        _parse_utc(archived_at, f"collector raw response {index} archived_at_utc")
        request = _mapping(record.get("request"), f"collector raw response {index} request")
        if source == _GAMMA_SOURCE:
            signature = _validate_collector_gamma_request(
                request,
                endpoint=gamma_endpoint,
                title_search=title_search,
                index=index,
            )
            gamma_hashes.add(str(content_sha256))
        else:
            signature, tokens = _validate_collector_clob_request(
                request,
                endpoint=clob_endpoint,
                fidelity=fidelity,
                history_window=history_window,
                index=index,
            )
            clob_tokens_by_hash[str(content_sha256)].update(tokens)
        if signature in signatures:
            raise ValueError("collector raw response request signatures must be unique")
        signatures.add(signature)

    for row in rows:
        if row.source_event_sha256 not in gamma_hashes:
            raise ValueError(
                f"event {row.event_id!r} source_event_sha256 is absent from Gamma provenance"
            )
        if row.source_history_sha256 not in clob_tokens_by_hash:
            raise ValueError(
                f"event {row.event_id!r} source_history_sha256 is absent from CLOB provenance"
            )
        if row.yes_token not in clob_tokens_by_hash[row.source_history_sha256]:
            raise ValueError(
                f"event {row.event_id!r} yes_token is absent from its CLOB history request"
            )


def _collector_raw_path(value: Any, source: str, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"collector raw response {index} path must be non-empty")
    path = PurePosixPath(value)
    expected_prefix = ("raw", "gamma") if source == _GAMMA_SOURCE else ("raw", "clob")
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) < 3
        or path.parts[:2] != expected_prefix
        or any(part in {"", ".", ".."} for part in path.parts)
        or not path.name.endswith(".json.gz")
    ):
        raise ValueError(f"collector raw response {index} has an unsafe source path")
    return value


def _validate_collector_gamma_request(
    request: Mapping[str, Any],
    *,
    endpoint: str,
    title_search: str,
    index: int,
) -> tuple[str, str, str]:
    if set(request) != {"method", "url"} or request.get("method") != "GET":
        raise ValueError(f"collector Gamma request {index} must contain only method=GET and url")
    url = request.get("url")
    if not isinstance(url, str):
        raise ValueError(f"collector Gamma request {index} url must be text")
    parsed = urlsplit(url)
    expected = urlsplit(endpoint)
    if (
        (parsed.scheme, parsed.netloc, parsed.path)
        != (expected.scheme, expected.netloc, expected.path)
        or parsed.fragment
        or expected.query
        or expected.fragment
    ):
        raise ValueError(f"collector Gamma request {index} endpoint differs from protocol")
    try:
        query = parse_qs(parsed.query, strict_parsing=True)
    except ValueError as error:
        raise ValueError(f"collector Gamma request {index} has an invalid query") from error
    required = {"closed", "end_date_max", "end_date_min", "limit", "title_search"}
    if set(query) != required and set(query) != required | {"after_cursor"}:
        raise ValueError(f"collector Gamma request {index} query fields differ from v1")
    if any(len(values) != 1 for values in query.values()):
        raise ValueError(f"collector Gamma request {index} has repeated query fields")
    if (
        query["closed"] != ["true"]
        or query["limit"] != ["100"]
        or query["title_search"] != [title_search]
        or ("after_cursor" in query and not query["after_cursor"][0])
    ):
        raise ValueError(f"collector Gamma request {index} differs from v1 parameters")
    start = _parse_utc(query["end_date_min"][0], "collector Gamma window start")
    end = _parse_utc(query["end_date_max"][0], "collector Gamma window end")
    if end <= start or end - start > timedelta(days=1):
        raise ValueError(f"collector Gamma request {index} has an invalid daily window")
    return "GET", url, ""


def _validate_collector_clob_request(
    request: Mapping[str, Any],
    *,
    endpoint: str,
    fidelity: int,
    history_window: int,
    index: int,
) -> tuple[tuple[str, str, str], set[str]]:
    if set(request) != {"method", "url", "body", "body_sha256"}:
        raise ValueError(f"collector CLOB request {index} has an invalid field set")
    if request.get("method") != "POST" or request.get("url") != endpoint:
        raise ValueError(f"collector CLOB request {index} differs from protocol endpoint")
    body = _mapping(request.get("body"), f"collector CLOB request {index} body")
    if set(body) != {"end_ts", "fidelity", "markets", "start_ts"}:
        raise ValueError(f"collector CLOB request {index} has an invalid body")
    body_sha256 = request.get("body_sha256")
    _require_digest(body_sha256, f"collector CLOB request {index} body SHA-256")
    canonical = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if hashlib.sha256(canonical).hexdigest() != body_sha256:
        raise ValueError(f"collector CLOB request {index} body_sha256 does not match body")
    start = _strict_nonnegative_int(body.get("start_ts"), f"collector CLOB request {index} start")
    end = _strict_nonnegative_int(body.get("end_ts"), f"collector CLOB request {index} end")
    body_fidelity = _strict_nonnegative_int(
        body.get("fidelity"), f"collector CLOB request {index} fidelity"
    )
    markets = body.get("markets")
    if (
        end - start != history_window
        or body_fidelity != fidelity
        or not isinstance(markets, list)
        or not 1 <= len(markets) <= 20
        or any(not isinstance(token, str) or not token.isdecimal() for token in markets)
        or len(set(markets)) != len(markets)
    ):
        raise ValueError(f"collector CLOB request {index} violates v1 request rules")
    return ("POST", endpoint, str(body_sha256)), set(markets)


def _validate_chain_sources(rows: list[EvidenceRow], manifest: Mapping[str, Any]) -> None:
    raw_records = manifest.get("raw_responses")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("manifest has no raw chain response records")
    raw_hashes: set[str] = set()
    response_methods: dict[str, set[str]] = defaultdict(set)
    allowed_methods = {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getLogs",
    }
    for index, raw in enumerate(raw_records):
        record = _mapping(raw, f"raw response {index}")
        expected_sequence = index + 1
        sequence = _strict_nonnegative_int(record.get("sequence"), f"raw response {index} sequence")
        request_id = _strict_nonnegative_int(
            record.get("request_id"), f"raw response {index} request_id"
        )
        method = record.get("method")
        params = record.get("params")
        if sequence != expected_sequence or request_id != sequence:
            raise ValueError("raw response request IDs must be contiguous from one")
        if not isinstance(method, str) or not method or not isinstance(params, list):
            raise ValueError(f"raw response {index} lacks canonical request metadata")
        if method not in allowed_methods:
            raise ValueError(f"raw response {index} uses an unknown JSON-RPC method")
        request_digest = record.get("request_sha256")
        _require_digest(request_digest, f"raw response {index} request SHA-256")
        canonical_request = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if hashlib.sha256(canonical_request).hexdigest() != request_digest:
            raise ValueError(f"raw response {index} request SHA-256 does not match metadata")
        digest = record.get("response_sha256")
        _require_digest(digest, f"raw response {index} SHA-256")
        digest_text = str(digest)
        raw_hashes.add(digest_text)
        response_methods[digest_text].add(method)

    bundles = manifest.get("chain_source_bundles")
    if not isinstance(bundles, list) or not bundles:
        raise ValueError("manifest has no chain source bundles")
    bundle_hashes: set[str] = set()
    domain = b"event-factor-bench-chain-source-v1\x00"
    for index, raw_bundle in enumerate(bundles):
        bundle = _mapping(raw_bundle, f"chain source bundle {index}")
        digest = bundle.get("chain_source_sha256")
        _require_digest(digest, f"chain source bundle {index} SHA-256")
        sources = bundle.get("raw_response_sha256s")
        if not isinstance(sources, list) or len(sources) != 2:
            raise ValueError("each chain source bundle must contain log and block evidence")
        for source in sources:
            _require_digest(source, "chain source raw response SHA-256")
            if source not in raw_hashes:
                raise ValueError("chain source bundle cites an absent raw response")
        if (
            "eth_getLogs" not in response_methods[sources[0]]
            or "eth_getBlockByNumber" not in response_methods[sources[1]]
        ):
            raise ValueError("chain source bundle must order log evidence before block evidence")
        computed = hashlib.sha256(
            domain + bytes.fromhex(sources[0]) + bytes.fromhex(sources[1])
        ).hexdigest()
        if computed != digest or digest in bundle_hashes:
            raise ValueError("invalid or duplicate chain source bundle")
        bundle_hashes.add(digest)
    missing = {row.chain_source_sha256 for row in rows} - bundle_hashes
    if missing:
        raise ValueError("evidence cites chain source bundles absent from manifest")


def _validate_status_algebra(rows: list[EvidenceRow], manifest: Mapping[str, Any]) -> None:
    counts = _mapping(manifest.get("resolution_counts"), "resolution counts")
    condition_counts = _status_counts(counts.get("conditions"), "condition status counts")
    row_counts = _status_counts(counts.get("candidate_rows"), "candidate-row status counts")
    summary = _mapping(manifest.get("summary"), "manifest summary")
    expected_conditions = _strict_nonnegative_int(
        summary.get("expected_conditions"), "summary expected conditions"
    )
    expected_rows = _strict_nonnegative_int(summary.get("expected_rows"), "summary expected rows")
    retained_rows = _strict_nonnegative_int(
        summary.get("retained_rows_after_complete_event_gate"), "summary retained rows"
    )
    if sum(condition_counts.values()) != expected_conditions:
        raise ValueError("condition status counts do not sum to expected conditions")
    if sum(row_counts.values()) != expected_rows:
        raise ValueError("candidate-row status counts do not sum to expected rows")
    if (
        _strict_nonnegative_int(summary.get("verified_conditions"), "summary verified conditions")
        != condition_counts["agreed"]
    ):
        raise ValueError("verified condition count differs from agreed conditions")
    if (
        _strict_nonnegative_int(
            summary.get("directly_verified_rows"), "summary directly verified rows"
        )
        != row_counts["agreed"]
    ):
        raise ValueError("directly verified row count differs from agreed rows")
    if retained_rows != len(rows) or retained_rows > row_counts["agreed"]:
        raise ValueError("retained evidence count conflicts with verification counts")
    mismatch = _strict_nonnegative_int(
        manifest.get("gamma_candidate_mismatch_count"), "Gamma mismatch count"
    )
    if mismatch != condition_counts["gamma_disagreement"]:
        raise ValueError("Gamma mismatch scalar differs from condition status counts")
    expected_onchain = condition_counts["agreed"] == expected_conditions
    if manifest.get("onchain_verified") is not expected_onchain:
        raise ValueError("onchain_verified does not follow condition status counts")
    if counts.get("gamma_agreement_conditions") != condition_counts["agreed"]:
        raise ValueError("Gamma agreement count differs from agreed conditions")
    if counts.get("missing_conditions") != condition_counts["missing"]:
        raise ValueError("missing condition count differs from status counts")
    if counts.get("ambiguous_conditions") != condition_counts["ambiguous"]:
        raise ValueError("ambiguous condition count differs from status counts")
    if counts.get("gamma_disagreement_conditions") != condition_counts["gamma_disagreement"]:
        raise ValueError("Gamma disagreement count differs from status counts")


def _validate_coverage(
    rows: list[EvidenceRow],
    manifest: Mapping[str, Any],
    expected_cells: set[tuple[str, str]],
) -> None:
    coverage = _mapping(manifest.get("coverage"), "manifest coverage")
    pre_chain = _mapping(manifest.get("coverage_pre_chain"), "pre-chain coverage")
    coverage_cells = _nested_cells(coverage, "manifest coverage")
    pre_chain_cells = _nested_cells(pre_chain, "pre-chain coverage")
    if set(coverage_cells) != expected_cells or set(pre_chain_cells) != expected_cells:
        raise ValueError("coverage cells differ from the protocol split-by-horizon grid")

    row_counts: dict[tuple[str, str], int] = defaultdict(int)
    event_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        key = (row.split, str(row.horizon_seconds))
        row_counts[key] += 1
        event_ids[key].add(row.event_id)

    for cell in sorted(expected_cells):
        final = coverage_cells[cell]
        history = pre_chain_cells[cell]
        expected_events = _strict_nonnegative_int(
            history.get("expected_events"), f"{cell} expected events"
        )
        expected_rows = _strict_nonnegative_int(
            history.get("expected_rows"), f"{cell} expected rows"
        )
        history_events = _strict_nonnegative_int(
            history.get("history_retained_events"), f"{cell} history events"
        )
        history_rows = _strict_nonnegative_int(
            history.get("history_retained_rows"), f"{cell} history rows"
        )
        retained_events = len(event_ids[cell])
        retained_rows = row_counts[cell]
        if not retained_events <= history_events <= expected_events:
            raise ValueError(f"event coverage counts are not nested for {cell!r}")
        if not retained_rows <= history_rows <= expected_rows:
            raise ValueError(f"row coverage counts are not nested for {cell!r}")
        exact_counts = {
            "expected_events": expected_events,
            "retained_events": retained_events,
            "expected_rows": expected_rows,
            "retained_rows": retained_rows,
            "history_retained_events": history_events,
            "history_retained_rows": history_rows,
        }
        for name, expected in exact_counts.items():
            if _strict_nonnegative_int(final.get(name), f"{cell} {name}") != expected:
                raise ValueError(f"coverage count {name} disagrees for {cell!r}")
        ratios = {
            "event_coverage": _safe_ratio(retained_events, expected_events),
            "row_coverage": _safe_ratio(retained_rows, expected_rows),
            "event_history_coverage": _safe_ratio(history_events, expected_events),
            "row_history_coverage": _safe_ratio(history_rows, expected_rows),
            "event_chain_given_history_coverage": _safe_ratio(retained_events, history_events),
            "row_chain_given_history_coverage": _safe_ratio(retained_rows, history_rows),
        }
        for name, expected in ratios.items():
            _require_exact_ratio(final.get(name), expected, f"{cell} {name}")
        _require_exact_ratio(
            history.get("event_history_coverage"),
            ratios["event_history_coverage"],
            f"{cell} pre-chain event history coverage",
        )
        _require_exact_ratio(
            history.get("row_history_coverage"),
            ratios["row_history_coverage"],
            f"{cell} pre-chain row history coverage",
        )

    summary = _mapping(manifest.get("summary"), "manifest summary")
    if sum(int(cell["history_retained_rows"]) for cell in pre_chain_cells.values()) != int(
        summary["expected_rows"]
    ):
        raise ValueError("pre-chain candidate rows do not sum to summary expected rows")
    flat = manifest.get("coverage_by_split_horizon")
    if not isinstance(flat, list) or len(flat) != len(expected_cells):
        raise ValueError("flat coverage audit is absent or has the wrong number of cells")
    for item in flat:
        record = _mapping(item, "flat coverage record")
        key = (str(record.get("split")), str(record.get("horizon")))
        if key not in coverage_cells:
            raise ValueError("flat coverage audit contains an undeclared cell")
        nested = coverage_cells[key]
        if {
            name: value for name, value in record.items() if name not in {"split", "horizon"}
        } != nested:
            raise ValueError("flat and nested coverage audits disagree")


def _nested_cells(value: Mapping[str, Any], name: str) -> dict[tuple[str, str], Mapping[str, Any]]:
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for split, horizons in value.items():
        for horizon, metrics in _mapping(horizons, f"{name} split {split}").items():
            result[(str(split), str(horizon))] = _mapping(metrics, f"{name} cell {split}/{horizon}")
    return result


def _status_counts(value: Any, name: str) -> dict[str, int]:
    raw = _mapping(value, name)
    statuses = ("agreed", "missing", "ambiguous", "gamma_disagreement")
    if set(raw) != set(statuses):
        raise ValueError(f"{name} must contain exactly the frozen status vocabulary")
    return {status: _strict_nonnegative_int(raw[status], f"{name} {status}") for status in statuses}


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _strict_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _require_exact_ratio(value: Any, expected: float, name: str) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(numeric) or not math.isclose(
        numeric, expected, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError(f"{name} does not match its counts")


def _require_digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _parse_utc(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid {name}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be explicitly UTC")
    return parsed.astimezone(UTC)


def _parse_row(row: dict[str, str], line_number: int) -> EvidenceRow:
    try:
        payout_raw = json.loads(row["payout_vector"])
        if not isinstance(payout_raw, list) or len(payout_raw) != 2:
            raise ValueError("payout_vector must be a two-item JSON array")
        parsed = EvidenceRow(
            event_id=row["event_id"],
            market_id=row["market_id"],
            condition_id=row["condition_id"].lower(),
            asset=row["asset"],
            scheduled_time=_parse_utc(row["scheduled_time"], "scheduled_time"),
            split=row["split"],
            utc_day=row["utc_day"],
            horizon_seconds=int(row["horizon_seconds"]),
            cutoff_time=_parse_utc(row["cutoff_time"], "cutoff_time"),
            threshold=float(row["threshold"]),
            gamma_candidate_label=row["gamma_candidate_label"],
            yes_token=row["yes_token"],
            label=float(row["label"]),
            reference_probability=float(row["reference_probability"]),
            reference_timestamp=_parse_utc(row["reference_timestamp"], "reference_timestamp"),
            staleness_seconds=int(row["staleness_seconds"]),
            source_event_sha256=row["source_event_sha256"],
            source_history_sha256=row["source_history_sha256"],
            event_horizon_contract_count=int(row["event_horizon_contract_count"]),
            payout_vector=(int(payout_raw[0]), int(payout_raw[1])),
            resolution_block_number=int(row["resolution_block_number"]),
            resolution_block_hash=row["resolution_block_hash"].lower(),
            resolution_block_timestamp=_parse_utc(
                row["resolution_block_timestamp"], "resolution_block_timestamp"
            ),
            resolution_tx_hash=row["resolution_tx_hash"].lower(),
            resolution_log_index=int(row["resolution_log_index"]),
            chain_source_sha256=row["chain_source_sha256"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid evidence row at line {line_number}") from error
    for value, name in (
        (parsed.threshold, "threshold"),
        (parsed.label, "label"),
        (parsed.reference_probability, "reference_probability"),
    ):
        if not math.isfinite(value):
            raise ValueError(f"non-finite {name} at line {line_number}")
    if parsed.label not in (0.0, 1.0):
        raise ValueError(f"non-binary label at line {line_number}")
    if not 0.0 <= parsed.reference_probability <= 1.0:
        raise ValueError(f"probability outside [0, 1] at line {line_number}")
    if parsed.horizon_seconds <= 0:
        raise ValueError(f"non-positive horizon at line {line_number}")
    if parsed.staleness_seconds < 0:
        raise ValueError(f"negative staleness at line {line_number}")
    if parsed.event_horizon_contract_count <= 0:
        raise ValueError(f"non-positive event contract count at line {line_number}")
    if parsed.resolution_block_number < 0 or parsed.resolution_log_index < 0:
        raise ValueError(f"negative chain coordinate at line {line_number}")
    if not all(
        [
            parsed.event_id,
            parsed.market_id,
            parsed.condition_id,
            parsed.asset,
            parsed.split,
            parsed.utc_day,
            parsed.yes_token,
        ]
    ):
        raise ValueError(f"empty identifier at line {line_number}")
    if not parsed.yes_token.isdecimal():
        raise ValueError(f"non-decimal yes_token at line {line_number}")
    for value, name in (
        (parsed.source_event_sha256, "source_event_sha256"),
        (parsed.source_history_sha256, "source_history_sha256"),
        (parsed.chain_source_sha256, "chain_source_sha256"),
    ):
        _require_digest(value, f"{name} at line {line_number}")
    for value, name in (
        (parsed.condition_id, "condition_id"),
        (parsed.resolution_block_hash, "resolution_block_hash"),
        (parsed.resolution_tx_hash, "resolution_tx_hash"),
    ):
        if _HEX_32_RE.fullmatch(value) is None:
            raise ValueError(f"invalid {name} at line {line_number}")
    expected_payout = (1, 0) if parsed.label == 1.0 else (0, 1)
    expected_gamma = "Yes" if parsed.label == 1.0 else "No"
    if parsed.payout_vector != expected_payout:
        raise ValueError(f"label/payout disagreement at line {line_number}")
    if parsed.gamma_candidate_label != expected_gamma:
        raise ValueError(f"Gamma/canonical-label disagreement at line {line_number}")
    return parsed


def _validate_evidence(rows: list[EvidenceRow]) -> None:
    if not rows:
        raise ValueError("evidence table is empty")
    keys: set[tuple[str, str, int]] = set()
    event_properties: dict[str, tuple[Any, ...]] = {}
    market_properties: dict[tuple[str, str], tuple[Any, ...]] = {}
    market_owner: dict[str, str] = {}
    condition_properties: dict[str, tuple[Any, ...]] = {}
    event_thresholds: dict[tuple[str, int], set[float]] = defaultdict(set)
    event_labels: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        key = (row.event_id, row.market_id, row.horizon_seconds)
        if key in keys:
            raise ValueError(f"duplicate evidence row: {key!r}")
        keys.add(key)
        event_key = (row.event_id, row.horizon_seconds)
        properties = (
            row.asset,
            row.split,
            row.utc_day,
            row.scheduled_time,
            row.source_event_sha256,
        )
        prior = event_properties.setdefault(row.event_id, properties)
        if prior != properties:
            raise ValueError(f"event properties disagree for {row.event_id!r}")
        owner = market_owner.setdefault(row.market_id, row.event_id)
        if owner != row.event_id:
            raise ValueError(f"market appears in multiple events: {row.market_id!r}")
        market = (
            row.condition_id,
            row.threshold,
            row.label,
            row.gamma_candidate_label,
            row.yes_token,
            row.source_history_sha256,
        )
        prior_market = market_properties.setdefault((row.event_id, row.market_id), market)
        if prior_market != market:
            raise ValueError(f"market properties disagree across horizons: {row.market_id!r}")
        condition = (
            row.label,
            row.payout_vector,
            row.resolution_block_number,
            row.resolution_block_hash,
            row.resolution_block_timestamp,
            row.resolution_tx_hash,
            row.resolution_log_index,
            row.chain_source_sha256,
        )
        prior_condition = condition_properties.setdefault(row.condition_id, condition)
        if prior_condition != condition:
            raise ValueError(f"chain evidence disagrees for condition {row.condition_id!r}")
        if row.threshold in event_thresholds[event_key]:
            raise ValueError(f"duplicate threshold in event {event_key!r}")
        event_thresholds[event_key].add(row.threshold)
        event_labels[event_key].append((row.threshold, row.label))
    for event_key, values in event_labels.items():
        ordered = [label for _, label in sorted(values)]
        if any(left < right for left, right in pairwise(ordered)):
            raise ValueError(f"canonical labels are not non-increasing for {event_key!r}")
    ladders: dict[str, set[tuple[str, float, float]]] = {}
    for event_id, horizon in event_labels:
        markets = {
            (row.market_id, row.threshold, row.label)
            for row in rows
            if row.event_id == event_id and row.horizon_seconds == horizon
        }
        prior_ladder = ladders.setdefault(event_id, markets)
        if prior_ladder != markets:
            raise ValueError(f"event ladder differs across horizons: {event_id!r}")


def _project_all(
    rows: list[EvidenceRow], probabilities: NDArray[np.float64]
) -> NDArray[np.float64]:
    if probabilities.shape != (len(rows),):
        raise ValueError("probability vector length does not match evidence")
    result = np.empty_like(probabilities)
    groups: dict[tuple[str, int], list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        groups[(row.event_id, row.horizon_seconds)].append(index)
    for indices in groups.values():
        thresholds = [rows[index].threshold for index in indices]
        result[indices] = project_threshold_probabilities(thresholds, probabilities[indices])
    return result


def _evaluate_slice(
    rows: list[EvidenceRow],
    predictions: dict[str, NDArray[np.float64]],
    indices: list[int],
    *,
    inference: dict[str, Any],
    log_epsilon: float,
    bootstrap: bool,
) -> dict[str, Any]:
    event_ids = [rows[index].event_id for index in indices]
    block_ids = [rows[index].utc_day for index in indices]
    labels = np.asarray([rows[index].label for index in indices])
    methods: dict[str, Any] = {}
    for name, all_probabilities in predictions.items():
        probabilities = all_probabilities[indices]
        methods[name] = {
            "event_macro_brier": event_macro_brier(event_ids, labels, probabilities),
            "event_macro_log_loss": event_macro_log_loss(
                event_ids, labels, probabilities, epsilon=log_epsilon
            ),
            **_violations(rows, indices, probabilities),
        }

    comparisons: dict[str, Any] = {}
    for name, baseline_name, model_name in (
        ("pav_raw_vs_raw", "raw", "pav_raw"),
        ("beta_vs_raw", "raw", "beta"),
        ("pav_beta_vs_beta", "beta", "pav_beta"),
        ("pav_beta_vs_raw", "raw", "pav_beta"),
    ):
        baseline = predictions[baseline_name][indices]
        model = predictions[model_name][indices]
        baseline_brier = methods[baseline_name]["event_macro_brier"]
        model_brier = methods[model_name]["event_macro_brier"]
        baseline_log = methods[baseline_name]["event_macro_log_loss"]
        model_log = methods[model_name]["event_macro_log_loss"]
        if not math.isfinite(baseline_brier) or baseline_brier <= 0.0:
            raise ValueError(
                f"relative Brier improvement is undefined for {baseline_name} in this slice"
            )
        comparison: dict[str, Any] = {
            "baseline": baseline_name,
            "model": model_name,
            "brier_delta": baseline_brier - model_brier,
            "relative_brier_improvement": (baseline_brier - model_brier) / baseline_brier,
            "log_loss_delta": baseline_log - model_log,
        }
        if bootstrap:
            brier_interval = paired_event_block_bootstrap(
                event_ids,
                block_ids,
                np.square(baseline - labels),
                np.square(model - labels),
                n_resamples=int(inference["resamples"]),
                confidence=float(inference["confidence"]),
                seed=int(inference["seed"]),
            )
            baseline_log_rows = _log_losses(labels, baseline, log_epsilon)
            model_log_rows = _log_losses(labels, model, log_epsilon)
            log_interval = paired_event_block_bootstrap(
                event_ids,
                block_ids,
                baseline_log_rows,
                model_log_rows,
                n_resamples=int(inference["resamples"]),
                confidence=float(inference["confidence"]),
                seed=int(inference["seed"]) + 1,
            )
            comparison["brier_delta_ci"] = [brier_interval.lower, brier_interval.upper]
            comparison["log_loss_delta_ci"] = [log_interval.lower, log_interval.upper]
        comparisons[name] = comparison

    asset_subgroups: dict[str, Any] = {}
    for asset in sorted({rows[index].asset for index in indices}):
        asset_indices = [index for index in indices if rows[index].asset == asset]
        asset_events = [rows[index].event_id for index in asset_indices]
        asset_labels = np.asarray([rows[index].label for index in asset_indices])
        raw = predictions["raw"][asset_indices]
        pav = predictions["pav_raw"][asset_indices]
        asset_subgroups[asset] = {
            "events": len(set(asset_events)),
            "brier_delta": event_macro_brier(asset_events, asset_labels, raw)
            - event_macro_brier(asset_events, asset_labels, pav),
        }

    return {
        "rows": len(indices),
        "events": len(set(event_ids)),
        "utc_days": len(set(block_ids)),
        "methods": methods,
        "comparisons": comparisons,
        "asset_subgroups": asset_subgroups,
    }


def _violations(
    rows: list[EvidenceRow], indices: list[int], probabilities: NDArray[np.float64]
) -> dict[str, int]:
    groups: dict[str, list[int]] = defaultdict(list)
    local_by_global = {
        global_index: local_index for local_index, global_index in enumerate(indices)
    }
    for global_index in indices:
        groups[rows[global_index].event_id].append(global_index)
    edges = 0
    events = 0
    for event_indices in groups.values():
        ordered = sorted(event_indices, key=lambda index: rows[index].threshold)
        values = np.asarray([probabilities[local_by_global[index]] for index in ordered])
        count = int(np.sum(np.diff(values) > 1e-12))
        edges += count
        events += int(count > 0)
    return {"monotonicity_violation_edges": edges, "events_with_violations": events}


def _log_losses(
    labels: NDArray[np.float64], probabilities: NDArray[np.float64], epsilon: float
) -> NDArray[np.float64]:
    clipped = np.clip(probabilities, epsilon, 1.0 - epsilon)
    return -(labels * np.log(clipped) + (1.0 - labels) * np.log1p(-clipped))


def _claim_gate(
    protocol: dict[str, Any],
    manifest: dict[str, Any],
    primary: dict[str, Any],
    primary_horizon: int,
) -> dict[str, Any]:
    configured = protocol["claim_gate"]
    coverage = _manifest_coverage(manifest, "holdout", primary_horizon)
    comparison = primary["comparisons"]["pav_raw_vs_raw"]
    projected = primary["methods"]["pav_raw"]
    declared_assets = {str(asset) for asset in protocol["universe"]["assets"]}
    subgroup_assets = set(primary["asset_subgroups"])
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
        "nonnegative_asset_subgroups": subgroup_assets == declared_assets
        and all(
            subgroup["brier_delta"] >= -1e-15 for subgroup in primary["asset_subgroups"].values()
        ),
        "all_projection_violations_removed": projected["monotonicity_violation_edges"] == 0,
        "canonical_chain_labels_verified": manifest.get("onchain_verified") is True,
        "gamma_candidate_mismatches_zero": manifest.get("gamma_candidate_mismatch_count") == 0,
    }
    incremental = primary["comparisons"]["pav_beta_vs_beta"]
    incremental_conditions = {
        "minimum_relative_brier_improvement": incremental["relative_brier_improvement"]
        >= float(configured["minimum_relative_brier_improvement"]),
        "brier_ci_lower_above_zero": incremental["brier_delta_ci"][0] > 0.0,
    }
    return {
        "primary_improvement_passed": all(conditions.values()),
        "conditions": conditions,
        "incremental_over_beta_passed": all(incremental_conditions.values()),
        "incremental_over_beta_conditions": incremental_conditions,
        "coverage": coverage,
    }


def _manifest_coverage(
    manifest: dict[str, Any], split: str, horizon: int
) -> dict[str, float | int]:
    try:
        value = manifest["coverage"][split][str(horizon)]
        result = {
            "expected_events": int(value["expected_events"]),
            "retained_events": int(value["retained_events"]),
            "expected_rows": int(value["expected_rows"]),
            "retained_rows": int(value["retained_rows"]),
            "event_coverage": float(value["event_coverage"]),
            "row_coverage": float(value["row_coverage"]),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"manifest lacks coverage for {split}/{horizon}") from error
    for name in ("event_coverage", "row_coverage"):
        if not 0.0 <= result[name] <= 1.0:
            raise ValueError(f"manifest {name} lies outside [0, 1]")
    return result


def _calibrator_dict(model: LogisticCalibrator) -> dict[str, Any]:
    return {
        "method": model.method,
        "intercept": model.intercept,
        "coefficients": list(model.coefficients),
        "epsilon": model.epsilon,
        "l2": model.l2,
        "iterations": model.iterations,
        "objective": model.objective,
    }
