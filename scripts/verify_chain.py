#!/usr/bin/env python3
"""Freeze canonical Polymarket labels from Polygon ConditionalTokens logs.

This command is intentionally separate from feature collection.  It consumes a compact
candidate manifest, verifies each Gamma candidate against ``ConditionResolution`` logs, and
publishes only complete event/horizon ladders.  RPC transport or chain-integrity failures abort
the run; missing or ambiguous resolutions are never assigned a label.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import time as time_module
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

POLYGON_CHAIN_ID = 137
MAX_BLOCKS_PER_QUERY = 30_000
MAX_CONDITIONS_PER_LOG_QUERY = 500
RPC_MAX_ATTEMPTS = 5
RPC_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0)
RPC_RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
RPC_RETRYABLE_ERROR_CODES = {-32005, 429}
RPC_USER_AGENT = "event-factor-bench/0.1 (+canonical-label-freeze)"
COLLECTOR_SCHEMA_VERSION = "event-factor-bench-collector-v1"
GAMMA_SOURCE = "gamma.events_keyset"
CLOB_SOURCE = "clob.batch_prices_history"
COLLECTOR_RAW_FIELDS = frozenset(
    {
        "path",
        "source",
        "request",
        "archived_at_utc",
        "content_bytes",
        "content_sha256",
        "gzip_bytes",
        "gzip_sha256",
    }
)
FROZEN_INPUT_COLUMNS = (
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
)
RESERVED_OUTPUT_COLUMNS = (
    "label",
    "payout_vector",
    "resolution_block_number",
    "resolution_block_hash",
    "resolution_block_timestamp",
    "resolution_tx_hash",
    "resolution_log_index",
    "chain_source_sha256",
)


class ChainVerificationError(RuntimeError):
    """Base class for failures that must not produce a frozen dataset."""


class CandidateManifestError(ChainVerificationError):
    """Raised when candidate input is incomplete, conflicting, or unsafe."""


class RpcError(ChainVerificationError):
    """Raised when an RPC response cannot be trusted."""


class AbiDecodeError(ValueError):
    """Raised when a ConditionResolution data payload is not canonical ABI."""


@dataclass(frozen=True, slots=True)
class CandidateRow:
    original: dict[str, Any]
    event_id: str
    condition_id: str
    target_at: datetime
    split: str
    horizon: str
    gamma_candidate: int
    outcomes: tuple[str, str]


@dataclass(frozen=True, slots=True)
class RpcEvidence:
    sequence: int
    method: str
    params: list[Any]
    request_sha256: str
    response_sha256: str
    retrieved_at_utc: str
    response: bytes


@dataclass(frozen=True, slots=True)
class RpcResult:
    value: Any
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class BlockInfo:
    number: int
    block_hash: str
    timestamp: int
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class Resolution:
    status: str
    reason: str
    label: int | None = None
    payout_vector: tuple[int, ...] | None = None
    block_number: int | None = None
    block_hash: str | None = None
    block_timestamp: int | None = None
    transaction_hash: str | None = None
    log_index: int | None = None
    chain_source_sha256: str | None = None
    raw_response_sha256s: tuple[str, ...] = ()


Transport = Callable[[str, bytes, float], bytes]


class JsonRpcClient:
    """Minimal JSON-RPC client that retains exact response bytes in memory."""

    def __init__(
        self,
        rpc_url: str,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        if not rpc_url:
            raise ValueError("rpc_url must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.rpc_url = rpc_url
        self.timeout = timeout
        self._transport = transport or _http_transport
        self._next_id = 1
        self.evidence: list[RpcEvidence] = []

    @property
    def endpoint_sha256(self) -> str:
        """Hash the full endpoint so API credentials are not written to the manifest."""

        return _sha256(self.rpc_url.encode())

    @property
    def endpoint_origin(self) -> str:
        """Return only a non-secret origin hint; paths and query strings may contain keys."""

        parsed = urlsplit(self.rpc_url)
        if parsed.scheme and parsed.hostname:
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            port = f":{parsed.port}" if parsed.port is not None else ""
            return f"{parsed.scheme}://{host}{port}"
        return "redacted"

    def call(self, method: str, params: Sequence[Any]) -> RpcResult:
        for attempt in range(RPC_MAX_ATTEMPTS):
            request_id = self._next_id
            self._next_id += 1
            body = _canonical_json_bytes(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": list(params)}
            )
            try:
                raw = self._transport(self.rpc_url, body, self.timeout)
            except Exception as exc:  # custom transports may raise non-urllib exceptions
                raise RpcError(f"{method} transport failed: {exc}") from exc
            if not isinstance(raw, bytes):
                raise RpcError(f"{method} transport returned non-bytes response")

            response_sha256 = _sha256(raw)
            self.evidence.append(
                RpcEvidence(
                    sequence=len(self.evidence) + 1,
                    method=method,
                    params=list(params),
                    request_sha256=_sha256(body),
                    response_sha256=response_sha256,
                    retrieved_at_utc=_utc_now(),
                    response=raw,
                )
            )
            try:
                envelope = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RpcError(f"{method} returned malformed JSON") from exc
            if not isinstance(envelope, dict):
                raise RpcError(f"{method} returned a non-object JSON-RPC envelope")
            if envelope.get("jsonrpc") != "2.0" or envelope.get("id") != request_id:
                raise RpcError(f"{method} returned a mismatched JSON-RPC envelope")
            error = envelope.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, dict) else None
                if code in RPC_RETRYABLE_ERROR_CODES and attempt + 1 < RPC_MAX_ATTEMPTS:
                    time_module.sleep(RPC_BACKOFF_SECONDS[attempt])
                    continue
                raise RpcError(f"{method} RPC error: {error!r}")
            if "result" not in envelope:
                raise RpcError(f"{method} response has no result")
            return RpcResult(envelope["result"], response_sha256)
        raise AssertionError("unreachable JSON-RPC retry loop")


class BlockLocator:
    """Resolve UTC timestamps to the first block at or after them."""

    def __init__(self, rpc: JsonRpcClient, latest_number: int) -> None:
        if latest_number < 0:
            raise ValueError("latest_number must be non-negative")
        self.rpc = rpc
        self.latest_number = latest_number
        self._cache: dict[int, BlockInfo] = {}

    def block(self, number: int) -> BlockInfo:
        if not 0 <= number <= self.latest_number:
            raise RpcError(f"block {number} is outside [0, {self.latest_number}]")
        cached = self._cache.get(number)
        if cached is not None:
            return cached
        response = self.rpc.call("eth_getBlockByNumber", [_quantity(number), False])
        raw = response.value
        if not isinstance(raw, dict):
            raise RpcError(f"eth_getBlockByNumber returned no block for {number}")
        returned_number = _parse_quantity(raw.get("number"), "block.number")
        if returned_number != number:
            raise RpcError(f"requested block {number}, received block {returned_number}")
        block_hash = _normalize_hex(raw.get("hash"), 32, "block.hash")
        timestamp = _parse_quantity(raw.get("timestamp"), "block.timestamp")
        info = BlockInfo(number, block_hash, timestamp, response.evidence_sha256)
        self._cache[number] = info
        return info

    def first_at_or_after(self, timestamp: int) -> BlockInfo:
        if timestamp < 0:
            raise ValueError("timestamp must be non-negative")
        genesis = self.block(0)
        head = self.block(self.latest_number)
        if head.timestamp < timestamp:
            raise RpcError(
                f"chain head timestamp {head.timestamp} is before required boundary {timestamp}"
            )
        if genesis.timestamp >= timestamp:
            return genesis

        low = 1
        high = self.latest_number
        while low < high:
            middle = (low + high) // 2
            if self.block(middle).timestamp < timestamp:
                low = middle + 1
            else:
                high = middle
        selected = self.block(low)
        previous = self.block(low - 1)
        if not previous.timestamp < timestamp <= selected.timestamp:
            raise RpcError("block timestamps are not monotone around the binary-search boundary")
        return selected


def decode_condition_resolution(data: str) -> tuple[int, tuple[int, ...]]:
    """Decode ABI data for ``(uint outcomeSlotCount, uint[] payoutNumerators)``."""

    raw = _hex_bytes(data, "ConditionResolution.data")
    if len(raw) < 96 or len(raw) % 32:
        raise AbiDecodeError("ConditionResolution data must be at least three ABI words")
    slot_count = int.from_bytes(raw[0:32], "big")
    offset = int.from_bytes(raw[32:64], "big")
    if slot_count < 1 or slot_count > 256:
        raise AbiDecodeError("outcomeSlotCount is outside [1, 256]")
    if offset != 64:
        raise AbiDecodeError("payoutNumerators must use the canonical offset 64")
    array_length = int.from_bytes(raw[offset : offset + 32], "big")
    if array_length != slot_count:
        raise AbiDecodeError("payoutNumerators length differs from outcomeSlotCount")
    expected_length = offset + 32 + 32 * array_length
    if len(raw) != expected_length:
        raise AbiDecodeError("ConditionResolution data has truncated or trailing ABI words")
    payout = tuple(
        int.from_bytes(raw[offset + 32 + index * 32 : offset + 64 + index * 32], "big")
        for index in range(array_length)
    )
    return slot_count, payout


def iter_inclusive_block_ranges(
    start_block: int,
    end_block: int,
    *,
    max_blocks: int = MAX_BLOCKS_PER_QUERY,
) -> Iterator[tuple[int, int]]:
    """Yield inclusive ranges with a one-block overlap for boundary-safe retrieval."""

    if start_block < 0 or end_block < 0:
        raise ValueError("block numbers must be non-negative")
    if end_block < start_block:
        return
    if not 2 <= max_blocks <= MAX_BLOCKS_PER_QUERY:
        raise ValueError(f"max_blocks must be in [2, {MAX_BLOCKS_PER_QUERY}]")
    current = start_block
    while True:
        stop = min(current + max_blocks - 1, end_block)
        yield current, stop
        if stop == end_block:
            return
        current = stop


def load_candidate_rows(
    path: Path,
    *,
    required_outcomes: Sequence[str] = ("Yes", "No"),
) -> tuple[list[CandidateRow], dict[str, str]]:
    """Load flat CSV/JSON rows or nested ``{"events": [{"markets": ...}]}`` JSON."""

    source = path.read_bytes()
    content = gzip.decompress(source) if path.suffix.lower() == ".gz" else source
    logical_suffix = (
        path.with_suffix("").suffix.lower() if path.suffix.lower() == ".gz" else path.suffix.lower()
    )
    if logical_suffix == ".csv":
        try:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
            raw_rows: list[dict[str, Any]] = [dict(row) for row in reader]
        except (UnicodeDecodeError, csv.Error) as exc:
            raise CandidateManifestError(f"cannot parse candidate CSV: {exc}") from exc
    elif logical_suffix == ".json":
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateManifestError(f"cannot parse candidate JSON: {exc}") from exc
        raw_rows = _flatten_json_candidates(payload)
    else:
        raise CandidateManifestError(
            "candidate manifest must end in .json, .csv, .json.gz, or .csv.gz"
        )

    if not raw_rows:
        raise CandidateManifestError("candidate manifest has no rows")
    required = tuple(required_outcomes)
    if required != ("Yes", "No"):
        raise CandidateManifestError("only the canonical outcome order ['Yes', 'No'] is supported")

    rows = [_parse_candidate_row(row, required) for row in raw_rows]
    _validate_candidate_consistency(rows)
    return rows, {
        "path": str(path),
        "file_sha256": _sha256(source),
        "content_sha256": _sha256(content),
    }


def _load_collector_manifest(
    path: Path,
    *,
    candidate_path: Path,
    candidate_source: Mapping[str, str],
    candidates: Sequence[CandidateRow],
    protocol: Mapping[str, Any],
    protocol_sha256: str,
    run_source_commit: str,
) -> tuple[
    dict[str, str],
    dict[str, dict[str, dict[str, int | float]]],
    bytes,
]:
    payload = path.read_bytes()
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateManifestError(f"cannot parse collector manifest JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CandidateManifestError("collector manifest must contain a JSON object")
    if manifest.get("schema_version") != COLLECTOR_SCHEMA_VERSION:
        raise CandidateManifestError("unsupported or missing collector manifest schema_version")
    if manifest.get("protocol_sha256") != protocol_sha256:
        raise CandidateManifestError("collector manifest protocol_sha256 does not match protocol")
    if manifest.get("run_source_commit") != run_source_commit:
        raise CandidateManifestError("collector manifest source commit does not match chain run")

    _validate_collector_raw_provenance(manifest, candidates=candidates, protocol=protocol)

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise CandidateManifestError("collector manifest has no artifacts list")
    candidate_artifacts = [
        item
        for item in artifacts
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and Path(item["path"]).name == candidate_path.name
    ]
    if len(candidate_artifacts) != 1:
        raise CandidateManifestError(
            "collector manifest must identify exactly one candidate artifact by filename"
        )
    if candidate_artifacts[0].get("sha256") != candidate_source["file_sha256"]:
        raise CandidateManifestError("candidate file SHA-256 differs from collector manifest")

    raw_coverage = manifest.get("coverage_pre_chain")
    if not isinstance(raw_coverage, dict) or not raw_coverage:
        raise CandidateManifestError("collector manifest has no coverage_pre_chain")
    normalized: dict[str, dict[str, dict[str, int | float]]] = {}
    for split, horizons in raw_coverage.items():
        if not isinstance(split, str) or not isinstance(horizons, dict):
            raise CandidateManifestError("coverage_pre_chain must be nested by split and horizon")
        normalized[split] = {}
        for horizon, raw_metrics in horizons.items():
            if not isinstance(raw_metrics, dict):
                raise CandidateManifestError("coverage_pre_chain metric entry must be an object")
            metrics = {
                key: _nonnegative_int(raw_metrics.get(key), f"coverage {split}/{horizon} {key}")
                for key in (
                    "expected_events",
                    "history_retained_events",
                    "expected_rows",
                    "history_retained_rows",
                )
            }
            if metrics["history_retained_events"] > metrics["expected_events"]:
                raise CandidateManifestError(
                    "history-retained events exceed strict expected events"
                )
            if metrics["history_retained_rows"] > metrics["expected_rows"]:
                raise CandidateManifestError("history-retained rows exceed strict expected rows")
            expected_event_ratio = _safe_ratio(
                metrics["history_retained_events"], metrics["expected_events"]
            )
            expected_row_ratio = _safe_ratio(
                metrics["history_retained_rows"], metrics["expected_rows"]
            )
            _require_ratio(
                raw_metrics.get("event_history_coverage"),
                expected_event_ratio,
                f"coverage {split}/{horizon} event_history_coverage",
            )
            _require_ratio(
                raw_metrics.get("row_history_coverage"),
                expected_row_ratio,
                f"coverage {split}/{horizon} row_history_coverage",
            )
            normalized[split][str(horizon)] = {
                **metrics,
                "event_history_coverage": expected_event_ratio,
                "row_history_coverage": expected_row_ratio,
            }

    actual_rows: Counter[tuple[str, str]] = Counter()
    actual_events: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in candidates:
        key = (row.split, row.horizon)
        actual_rows[key] += 1
        actual_events[key].add(row.event_id)
    coverage_keys = {
        (split, horizon) for split, horizons in normalized.items() for horizon in horizons
    }
    if not set(actual_rows).issubset(coverage_keys):
        missing = sorted(set(actual_rows).difference(coverage_keys))
        raise CandidateManifestError(f"collector coverage omits candidate groups: {missing!r}")
    for split, horizon in coverage_keys:
        metrics = normalized[split][horizon]
        if metrics["history_retained_rows"] != actual_rows[(split, horizon)]:
            raise CandidateManifestError(
                f"candidate row count differs from collector coverage for {split}/{horizon}"
            )
        if metrics["history_retained_events"] != len(actual_events[(split, horizon)]):
            raise CandidateManifestError(
                f"candidate event count differs from collector coverage for {split}/{horizon}"
            )
    return (
        {
            "path": str(path),
            "published_path": "data/collector_manifest_v0.1.json",
            "sha256": _sha256(payload),
            "run_source_commit": run_source_commit,
        },
        normalized,
        payload,
    )


def _validate_collector_raw_provenance(
    manifest: Mapping[str, Any],
    *,
    candidates: Sequence[CandidateRow],
    protocol: Mapping[str, Any],
) -> None:
    """Bind every normalized candidate row to a typed archived API response."""

    try:
        retrieval = protocol["retrieval"]
        gamma_endpoint = str(retrieval["gamma_endpoint"])
        clob_endpoint = str(retrieval["clob_endpoint"])
        title_search = str(retrieval["gamma_title_search"])
        fidelity = int(retrieval["history_fidelity_minutes"])
        history_window = int(retrieval["history_window_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateManifestError(
            "protocol retrieval settings are incomplete for collector provenance"
        ) from exc
    if not gamma_endpoint or not clob_endpoint or not title_search:
        raise CandidateManifestError(
            "protocol collector endpoints and title search must be non-empty"
        )

    raw_records = manifest.get("raw_responses")
    if not isinstance(raw_records, list) or not raw_records:
        raise CandidateManifestError("collector manifest has no raw_responses")
    paths: set[str] = set()
    request_signatures: set[tuple[str, str, str]] = set()
    gamma_hashes: set[str] = set()
    clob_tokens_by_hash: dict[str, set[str]] = defaultdict(set)

    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict) or set(raw) != COLLECTOR_RAW_FIELDS:
            raise CandidateManifestError(
                f"collector raw response {index} must use the exact v1 field set"
            )
        source = raw.get("source")
        if source not in {GAMMA_SOURCE, CLOB_SOURCE}:
            raise CandidateManifestError(f"collector raw response {index} has an unknown source")
        relative = _collector_raw_path(raw.get("path"), source, index)
        if relative in paths:
            raise CandidateManifestError("collector raw response paths must be unique")
        paths.add(relative)
        content_sha256 = _collector_digest(
            raw.get("content_sha256"), f"collector raw response {index} content_sha256"
        )
        _collector_digest(raw.get("gzip_sha256"), f"collector raw response {index} gzip_sha256")
        for field in ("content_bytes", "gzip_bytes"):
            if _nonnegative_int(raw.get(field), f"collector raw response {index} {field}") == 0:
                raise CandidateManifestError(
                    f"collector raw response {index} {field} must be positive"
                )
        archived_at = raw.get("archived_at_utc")
        if not isinstance(archived_at, str) or not archived_at.endswith("Z"):
            raise CandidateManifestError(
                f"collector raw response {index} archived_at_utc must be canonical UTC"
            )
        _parse_datetime(archived_at)
        request = raw.get("request")
        if not isinstance(request, dict):
            raise CandidateManifestError(
                f"collector raw response {index} request must be an object"
            )

        if source == GAMMA_SOURCE:
            signature = _validate_gamma_request(
                request,
                endpoint=gamma_endpoint,
                title_search=title_search,
                index=index,
            )
            gamma_hashes.add(content_sha256)
        else:
            signature, tokens = _validate_clob_request(
                request,
                endpoint=clob_endpoint,
                fidelity=fidelity,
                history_window=history_window,
                index=index,
            )
            clob_tokens_by_hash[content_sha256].update(tokens)
        if signature in request_signatures:
            raise CandidateManifestError("collector raw response request signatures must be unique")
        request_signatures.add(signature)

    for row in candidates:
        event_hash = _collector_digest(
            _field(row.original, ("source_event_sha256",), "source_event_sha256"),
            f"candidate {row.event_id} source_event_sha256",
        )
        history_hash = _collector_digest(
            _field(row.original, ("source_history_sha256",), "source_history_sha256"),
            f"candidate {row.event_id} source_history_sha256",
        )
        yes_token = _text_field(row.original, ("yes_token",), "yes_token")
        if not yes_token.isdecimal():
            raise CandidateManifestError(f"candidate {row.event_id} yes_token must be decimal")
        if event_hash not in gamma_hashes:
            raise CandidateManifestError(
                f"candidate {row.event_id} source_event_sha256 is absent from Gamma provenance"
            )
        if history_hash not in clob_tokens_by_hash:
            raise CandidateManifestError(
                f"candidate {row.event_id} source_history_sha256 is absent from CLOB provenance"
            )
        if yes_token not in clob_tokens_by_hash[history_hash]:
            raise CandidateManifestError(
                f"candidate {row.event_id} yes_token is absent from its CLOB history request"
            )


def _collector_raw_path(value: Any, source: str, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise CandidateManifestError(f"collector raw response {index} path must be non-empty")
    path = PurePosixPath(value)
    expected_prefix = ("raw", "gamma") if source == GAMMA_SOURCE else ("raw", "clob")
    if (
        path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) < 3
        or path.parts[:2] != expected_prefix
        or any(part in {"", ".", ".."} for part in path.parts)
        or not path.name.endswith(".json.gz")
    ):
        raise CandidateManifestError(f"collector raw response {index} has an unsafe source path")
    return value


def _collector_digest(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CandidateManifestError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _validate_gamma_request(
    request: Mapping[str, Any],
    *,
    endpoint: str,
    title_search: str,
    index: int,
) -> tuple[str, str, str]:
    if set(request) != {"method", "url"} or request.get("method") != "GET":
        raise CandidateManifestError(
            f"collector Gamma request {index} must contain only method=GET and url"
        )
    url = request.get("url")
    if not isinstance(url, str):
        raise CandidateManifestError(f"collector Gamma request {index} url must be text")
    parsed = urlsplit(url)
    expected = urlsplit(endpoint)
    if (
        (parsed.scheme, parsed.netloc, parsed.path)
        != (expected.scheme, expected.netloc, expected.path)
        or parsed.fragment
        or expected.query
        or expected.fragment
    ):
        raise CandidateManifestError(
            f"collector Gamma request {index} endpoint differs from protocol"
        )
    try:
        query = parse_qs(parsed.query, strict_parsing=True)
    except ValueError as exc:
        raise CandidateManifestError(
            f"collector Gamma request {index} has an invalid query"
        ) from exc
    required = {"closed", "end_date_max", "end_date_min", "limit", "title_search"}
    if set(query) != required and set(query) != required | {"after_cursor"}:
        raise CandidateManifestError(f"collector Gamma request {index} query fields differ from v1")
    if any(len(values) != 1 for values in query.values()):
        raise CandidateManifestError(f"collector Gamma request {index} has repeated query fields")
    if (
        query["closed"] != ["true"]
        or query["limit"] != ["100"]
        or query["title_search"] != [title_search]
        or ("after_cursor" in query and not query["after_cursor"][0])
    ):
        raise CandidateManifestError(f"collector Gamma request {index} differs from v1 parameters")
    start = _parse_datetime(query["end_date_min"][0])
    end = _parse_datetime(query["end_date_max"][0])
    if end <= start or end - start > timedelta(days=1):
        raise CandidateManifestError(f"collector Gamma request {index} has an invalid daily window")
    return "GET", url, ""


def _validate_clob_request(
    request: Mapping[str, Any],
    *,
    endpoint: str,
    fidelity: int,
    history_window: int,
    index: int,
) -> tuple[tuple[str, str, str], set[str]]:
    if set(request) != {"method", "url", "body", "body_sha256"}:
        raise CandidateManifestError(f"collector CLOB request {index} has an invalid field set")
    if request.get("method") != "POST" or request.get("url") != endpoint:
        raise CandidateManifestError(
            f"collector CLOB request {index} differs from protocol endpoint"
        )
    body = request.get("body")
    if not isinstance(body, dict) or set(body) != {"end_ts", "fidelity", "markets", "start_ts"}:
        raise CandidateManifestError(f"collector CLOB request {index} has an invalid body")
    body_sha256 = _collector_digest(
        request.get("body_sha256"), f"collector CLOB request {index} body_sha256"
    )
    if _sha256(_canonical_json_bytes(body)) != body_sha256:
        raise CandidateManifestError(
            f"collector CLOB request {index} body_sha256 does not match body"
        )
    start = _nonnegative_int(body.get("start_ts"), f"collector CLOB request {index} start_ts")
    end = _nonnegative_int(body.get("end_ts"), f"collector CLOB request {index} end_ts")
    body_fidelity = _nonnegative_int(
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
        raise CandidateManifestError(f"collector CLOB request {index} violates v1 request rules")
    return ("POST", endpoint, body_sha256), set(markets)


def verify_and_freeze(
    *,
    protocol_path: Path,
    candidate_path: Path,
    collector_manifest_path: Path,
    data_dir: Path,
    rpc: JsonRpcClient,
    resolution_grace: timedelta | None = None,
    max_blocks_per_query: int = MAX_BLOCKS_PER_QUERY,
    overwrite: bool = False,
    run_source_commit: str = "",
) -> dict[str, Any]:
    """Verify candidates and atomically publish frozen CSV, manifest, and raw evidence."""

    if resolution_grace is not None and resolution_grace < timedelta(0):
        raise ValueError("resolution_grace must be non-negative")
    if not 2 <= max_blocks_per_query <= MAX_BLOCKS_PER_QUERY:
        raise ValueError(f"max_blocks_per_query must be in [2, {MAX_BLOCKS_PER_QUERY}]")
    if (
        len(run_source_commit) != 40
        or run_source_commit.lower() != run_source_commit
        or any(character not in "0123456789abcdef" for character in run_source_commit)
    ):
        raise CandidateManifestError("run_source_commit must be a full lowercase 40-hex commit")

    protocol_bytes = protocol_path.read_bytes()
    try:
        protocol = json.loads(protocol_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateManifestError(f"cannot parse protocol JSON: {exc}") from exc
    address, topic0, required_outcomes, accepted_vectors, protocol_grace_seconds = _parse_protocol(
        protocol
    )
    protocol_grace = timedelta(seconds=protocol_grace_seconds)
    if resolution_grace is None:
        resolution_grace = protocol_grace
    elif resolution_grace != protocol_grace:
        raise CandidateManifestError(
            "resolution grace override differs from protocol: "
            f"override={int(resolution_grace.total_seconds())}, "
            f"protocol={protocol_grace_seconds}"
        )
    candidates, candidate_source = load_candidate_rows(
        candidate_path, required_outcomes=required_outcomes
    )
    collector_source, coverage_pre_chain, collector_manifest_payload = _load_collector_manifest(
        collector_manifest_path,
        candidate_path=candidate_path,
        candidate_source=candidate_source,
        candidates=candidates,
        protocol=protocol,
        protocol_sha256=_sha256(protocol_bytes),
        run_source_commit=run_source_commit,
    )

    output_csv = data_dir / "frozen_v0.1.csv.gz"
    output_manifest = data_dir / "manifest_v0.1.json"
    output_collector_manifest = data_dir / "collector_manifest_v0.1.json"
    raw_dir = data_dir / "raw_chain_v0.1"
    _preflight_outputs(
        (output_csv, output_manifest, output_collector_manifest, raw_dir), overwrite=overwrite
    )

    chain_id_result = rpc.call("eth_chainId", [])
    chain_id = _parse_quantity(chain_id_result.value, "eth_chainId")
    if chain_id != POLYGON_CHAIN_ID:
        raise RpcError(f"expected Polygon chainId {POLYGON_CHAIN_ID}, received {chain_id}")
    head_result = rpc.call("eth_blockNumber", [])
    head_number = _parse_quantity(head_result.value, "eth_blockNumber")
    locator = BlockLocator(rpc, head_number)
    head = locator.block(head_number)

    conditions = _conditions_by_id(candidates)
    logs_by_condition: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    search_windows: list[dict[str, Any]] = []
    by_day: dict[date, list[str]] = defaultdict(list)
    for condition_id, row in conditions.items():
        by_day[row.target_at.date()].append(condition_id)
    latest_target = max(row.target_at for row in conditions.values())
    if head.timestamp < math.ceil(latest_target.timestamp()):
        raise RpcError(
            "observed chain head predates the latest candidate target: "
            f"head={_iso_timestamp(head.timestamp)}, target={_iso_utc(latest_target)}"
        )

    for target_day in sorted(by_day):
        condition_ids = sorted(by_day[target_day])
        start_at = datetime.combine(target_day, time.min, tzinfo=UTC)
        end_at = start_at + timedelta(days=1) + resolution_grace
        start_block = locator.first_at_or_after(int(start_at.timestamp())).number
        clamped_to_head = end_at.timestamp() > head.timestamp
        if clamped_to_head:
            end_block = head.number
            effective_end = datetime.fromtimestamp(head.timestamp, tz=UTC)
        else:
            end_boundary = locator.first_at_or_after(int(end_at.timestamp())).number
            end_block = end_boundary - 1
            effective_end = end_at
        if end_block < start_block:
            raise RpcError(f"empty block window for target day {target_day.isoformat()}")
        query_count = 0
        for offset in range(0, len(condition_ids), MAX_CONDITIONS_PER_LOG_QUERY):
            condition_batch = condition_ids[offset : offset + MAX_CONDITIONS_PER_LOG_QUERY]
            requested_conditions = set(condition_batch)
            for first, last in iter_inclusive_block_ranges(
                start_block, end_block, max_blocks=max_blocks_per_query
            ):
                query_count += 1
                response = rpc.call(
                    "eth_getLogs",
                    [
                        {
                            "address": address,
                            "fromBlock": _quantity(first),
                            "toBlock": _quantity(last),
                            "topics": [topic0, condition_batch],
                        }
                    ],
                )
                if not isinstance(response.value, list):
                    raise RpcError("eth_getLogs result must be a list")
                for raw_log in response.value:
                    if not isinstance(raw_log, dict):
                        raise RpcError("eth_getLogs returned a non-object log")
                    condition_id = _validate_filtered_log(
                        raw_log,
                        address=address,
                        topic0=topic0,
                        requested_conditions=requested_conditions,
                    )
                    returned_block = _parse_quantity(raw_log.get("blockNumber"), "log.blockNumber")
                    if not first <= returned_block <= last:
                        raise RpcError(
                            "eth_getLogs returned a log outside the requested block range"
                        )
                    logs_by_condition[condition_id].append((raw_log, response.evidence_sha256))
        search_windows.append(
            {
                "target_utc_day": target_day.isoformat(),
                "condition_count": len(condition_ids),
                "start_inclusive_utc": _iso_utc(start_at),
                "requested_end_exclusive_utc": _iso_utc(end_at),
                "effective_end_utc": _iso_utc(effective_end),
                "clamped_to_observed_head": clamped_to_head,
                "from_block_inclusive": start_block,
                "to_block_inclusive": end_block,
                "eth_getLogs_queries": query_count,
            }
        )

    resolutions: dict[str, Resolution] = {}
    source_bundles: dict[str, tuple[str, ...]] = {}
    for condition_id, candidate in conditions.items():
        resolution = _resolve_condition(
            condition_id,
            candidate,
            logs_by_condition.get(condition_id, []),
            locator=locator,
            accepted_vectors=accepted_vectors,
            resolution_grace=resolution_grace,
        )
        resolutions[condition_id] = resolution
        if resolution.chain_source_sha256 is not None:
            source_bundles[resolution.chain_source_sha256] = resolution.raw_response_sha256s

    retained_rows, coverage = _retained_rows_and_coverage(
        candidates, resolutions, coverage_pre_chain=coverage_pre_chain
    )
    csv_plain = _frozen_csv_bytes(retained_rows, resolutions)
    csv_gzip = gzip.compress(csv_plain, compresslevel=9, mtime=0)
    generated_at = _utc_now()
    raw_files, raw_payloads = _raw_evidence_manifest(rpc.evidence)
    status_conditions = Counter(resolution.status for resolution in resolutions.values())
    status_rows = Counter(resolutions[row.condition_id].status for row in candidates)
    onchain_verified = status_conditions["agreed"] == len(conditions)
    nested_coverage = _nested_coverage(coverage)

    manifest: dict[str, Any] = {
        "schema_version": "event-factor-bench-chain-freeze-v1",
        "generated_at_utc": generated_at,
        "run_source_commit": run_source_commit,
        "onchain_verified": onchain_verified,
        "gamma_candidate_mismatch_count": status_conditions["gamma_disagreement"],
        "protocol": {
            "path": str(protocol_path),
            "sha256": _sha256(protocol_bytes),
        },
        "candidate_manifest": candidate_source,
        "collector_manifest": collector_source,
        "coverage_pre_chain": coverage_pre_chain,
        "chain": {
            "chain_id": chain_id,
            "rpc_endpoint_origin": rpc.endpoint_origin,
            "rpc_endpoint_sha256": rpc.endpoint_sha256,
            "conditional_tokens_address": address,
            "condition_resolution_topic": topic0,
            "head_block_number": head.number,
            "head_block_hash": head.block_hash,
            "head_block_timestamp": _iso_timestamp(head.timestamp),
            "resolution_grace_seconds": int(resolution_grace.total_seconds()),
            "max_blocks_per_query": max_blocks_per_query,
            "max_conditions_per_log_query": MAX_CONDITIONS_PER_LOG_QUERY,
            "rpc_retry_policy": {
                "max_attempts": RPC_MAX_ATTEMPTS,
                "backoff_seconds": list(RPC_BACKOFF_SECONDS),
                "retryable_http_statuses": sorted(RPC_RETRYABLE_HTTP_STATUSES),
                "retryable_json_rpc_error_codes": sorted(RPC_RETRYABLE_ERROR_CODES),
            },
            "inclusive_chunk_boundary_deduplication": True,
            "search_windows": search_windows,
        },
        "resolution_counts": {
            "conditions": _status_count_dict(status_conditions),
            "candidate_rows": _status_count_dict(status_rows),
            "gamma_agreement_conditions": status_conditions["agreed"],
            "missing_conditions": status_conditions["missing"],
            "ambiguous_conditions": status_conditions["ambiguous"],
            "gamma_disagreement_conditions": status_conditions["gamma_disagreement"],
        },
        "coverage_by_split_horizon": coverage,
        "coverage": nested_coverage,
        "summary": {
            "expected_conditions": len(conditions),
            "verified_conditions": status_conditions["agreed"],
            "expected_rows": len(candidates),
            "directly_verified_rows": status_rows["agreed"],
            "retained_rows_after_complete_event_gate": len(retained_rows),
        },
        "exclusions": [
            {
                "condition_id": condition_id,
                "status": result.status,
                "reason": result.reason,
            }
            for condition_id, result in sorted(resolutions.items())
            if result.status != "agreed"
        ],
        "chain_source_sha256_algorithm": (
            "sha256(domain_separator || raw_eth_getLogs_sha256 || raw_eth_getBlockByNumber_sha256)"
        ),
        "chain_source_bundles": [
            {
                "chain_source_sha256": bundle,
                "raw_response_sha256s": list(source_bundles[bundle]),
            }
            for bundle in sorted(source_bundles)
        ],
        "raw_responses": raw_files,
        "files": {
            "collector_manifest_v0.1.json": {
                "sha256": _sha256(collector_manifest_payload),
                "bytes": len(collector_manifest_payload),
            },
            "frozen_v0.1.csv.gz": {
                "sha256": _sha256(csv_gzip),
                "uncompressed_sha256": _sha256(csv_plain),
                "rows": len(retained_rows),
            },
        },
    }
    manifest["manifest_payload_sha256"] = _sha256(_canonical_json_bytes(manifest))
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    _publish_outputs(
        data_dir=data_dir,
        csv_payload=csv_gzip,
        collector_manifest_payload=collector_manifest_payload,
        manifest_payload=manifest_bytes,
        raw_payloads=raw_payloads,
        overwrite=overwrite,
    )
    return manifest


def _resolve_condition(
    condition_id: str,
    candidate: CandidateRow,
    raw_logs: Sequence[tuple[dict[str, Any], str]],
    *,
    locator: BlockLocator,
    accepted_vectors: set[tuple[int, int]],
    resolution_grace: timedelta,
) -> Resolution:
    unique: dict[tuple[str, str, int], tuple[dict[str, Any], str]] = {}
    for raw_log, source_sha256 in raw_logs:
        key = (
            _normalize_hex(raw_log.get("blockHash"), 32, "log.blockHash"),
            _normalize_hex(raw_log.get("transactionHash"), 32, "log.transactionHash"),
            _parse_quantity(raw_log.get("logIndex"), "log.logIndex"),
        )
        previous = unique.get(key)
        if previous is not None:
            if _canonical_json_bytes(previous[0]) != _canonical_json_bytes(raw_log):
                raise RpcError(f"conflicting duplicate log at {key!r}")
            continue
        unique[key] = (raw_log, source_sha256)

    if not unique:
        return Resolution("missing", "no ConditionResolution log in the configured window")
    if len(unique) != 1:
        return Resolution("ambiguous", f"found {len(unique)} distinct ConditionResolution logs")
    (block_hash, transaction_hash, log_index), (raw_log, log_source_sha256) = next(
        iter(unique.items())
    )
    if raw_log.get("removed") is not False:
        return Resolution("ambiguous", "the only resolution log is removed or lacks finality state")
    try:
        slot_count, payout = decode_condition_resolution(raw_log.get("data"))
    except (AbiDecodeError, TypeError) as exc:
        return Resolution("ambiguous", f"invalid ConditionResolution ABI: {exc}")
    if slot_count != 2 or payout not in accepted_vectors:
        return Resolution("ambiguous", f"unsupported payout vector {list(payout)!r}")

    block_number = _parse_quantity(raw_log.get("blockNumber"), "log.blockNumber")
    block = locator.block(block_number)
    if block.block_hash != block_hash:
        raise RpcError(f"canonical block hash mismatch for condition {condition_id}")
    if block.timestamp < candidate.target_at.timestamp():
        return Resolution("ambiguous", "resolution block predates the scheduled target")
    resolution_deadline = candidate.target_at + resolution_grace
    if block.timestamp > resolution_deadline.timestamp():
        return Resolution(
            "ambiguous",
            "resolution block is later than the protocol target-plus-grace deadline",
        )
    label = 1 if payout == (1, 0) else 0
    if label != candidate.gamma_candidate:
        return Resolution("gamma_disagreement", "canonical payout disagrees with Gamma candidate")

    source_hashes = (log_source_sha256, block.evidence_sha256)
    source_bundle = _chain_source_hash(*source_hashes)
    return Resolution(
        "agreed",
        "canonical payout agrees with Gamma candidate",
        label=label,
        payout_vector=payout,
        block_number=block_number,
        block_hash=block_hash,
        block_timestamp=block.timestamp,
        transaction_hash=transaction_hash,
        log_index=log_index,
        chain_source_sha256=source_bundle,
        raw_response_sha256s=source_hashes,
    )


def _retained_rows_and_coverage(
    candidates: Sequence[CandidateRow],
    resolutions: Mapping[str, Resolution],
    *,
    coverage_pre_chain: Mapping[str, Mapping[str, Mapping[str, int | float]]],
) -> tuple[list[CandidateRow], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[CandidateRow]] = defaultdict(list)
    for row in candidates:
        groups[(row.split, row.horizon)].append(row)

    retained_ids: set[int] = set()
    coverage: list[dict[str, Any]] = []
    coverage_keys = sorted(
        (split, horizon) for split, horizons in coverage_pre_chain.items() for horizon in horizons
    )
    for split, horizon in coverage_keys:
        rows = groups[(split, horizon)]
        pre_chain = coverage_pre_chain[split][horizon]
        events: dict[str, list[CandidateRow]] = defaultdict(list)
        for row in rows:
            events[row.event_id].append(row)
        retained_events: set[str] = set()
        for event_id, event_rows in events.items():
            if all(resolutions[row.condition_id].status == "agreed" for row in event_rows):
                retained_events.add(event_id)
                retained_ids.update(id(row) for row in event_rows)
        retained_count = sum(len(events[event_id]) for event_id in retained_events)
        expected_events = int(pre_chain["expected_events"])
        expected_rows = int(pre_chain["expected_rows"])
        history_events = int(pre_chain["history_retained_events"])
        history_rows = int(pre_chain["history_retained_rows"])
        coverage.append(
            {
                "split": split,
                "horizon": horizon,
                "expected_events": expected_events,
                "retained_events": len(retained_events),
                "expected_rows": expected_rows,
                "retained_rows": retained_count,
                "event_coverage": _safe_ratio(len(retained_events), expected_events),
                "row_coverage": _safe_ratio(retained_count, expected_rows),
                "history_retained_events": history_events,
                "history_retained_rows": history_rows,
                "event_history_coverage": _safe_ratio(history_events, expected_events),
                "row_history_coverage": _safe_ratio(history_rows, expected_rows),
                "event_chain_given_history_coverage": _safe_ratio(
                    len(retained_events), history_events
                ),
                "row_chain_given_history_coverage": _safe_ratio(retained_count, history_rows),
                "history_coverage": {
                    "event": _safe_ratio(history_events, expected_events),
                    "row": _safe_ratio(history_rows, expected_rows),
                },
                "chain_given_history_coverage": {
                    "event": _safe_ratio(len(retained_events), history_events),
                    "row": _safe_ratio(retained_count, history_rows),
                },
            }
        )
    return [row for row in candidates if id(row) in retained_ids], coverage


def _frozen_csv_bytes(
    rows: Sequence[CandidateRow],
    resolutions: Mapping[str, Resolution],
) -> bytes:
    fieldnames = list(FROZEN_INPUT_COLUMNS) + list(RESERVED_OUTPUT_COLUMNS)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        resolution = resolutions[row.condition_id]
        if resolution.status != "agreed":
            raise AssertionError("only agreed rows may be serialized")
        output = {
            column: _csv_value(_frozen_input_value(row, column)) for column in FROZEN_INPUT_COLUMNS
        }
        output.update(
            {
                "label": resolution.label,
                "payout_vector": json.dumps(resolution.payout_vector, separators=(",", ":")),
                "resolution_block_number": resolution.block_number,
                "resolution_block_hash": resolution.block_hash,
                "resolution_block_timestamp": _iso_timestamp(resolution.block_timestamp),
                "resolution_tx_hash": resolution.transaction_hash,
                "resolution_log_index": resolution.log_index,
                "chain_source_sha256": resolution.chain_source_sha256,
            }
        )
        writer.writerow(output)
    return stream.getvalue().encode()


def _frozen_input_value(row: CandidateRow, column: str) -> Any:
    canonical = {
        "event_id": row.event_id,
        "condition_id": row.condition_id,
        "scheduled_time": _iso_utc(row.target_at),
        "utc_day": row.target_at.date().isoformat(),
        "split": row.split,
        "horizon_seconds": row.horizon,
        "gamma_candidate_label": "Yes" if row.gamma_candidate == 1 else "No",
    }
    if column in canonical:
        return canonical[column]
    aliases = {
        "market_id": ("market_id", "marketId"),
    }
    return _optional_field(row.original, aliases.get(column, (column,)))


def _parse_protocol(
    protocol: Any,
) -> tuple[str, str, tuple[str, str], set[tuple[int, int]], int]:
    try:
        universe = protocol["universe"]
    except (KeyError, TypeError) as exc:
        raise CandidateManifestError("protocol has no universe object") from exc
    if not isinstance(universe, dict):
        raise CandidateManifestError("protocol universe must be an object")
    address = _normalize_hex(universe.get("conditional_tokens_address"), 20, "address")
    topic0 = _normalize_hex(universe.get("condition_resolution_topic"), 32, "topic")
    outcomes = tuple(universe.get("required_outcomes", ()))
    if outcomes != ("Yes", "No"):
        raise CandidateManifestError("protocol must require the outcome order ['Yes', 'No']")
    raw_vectors = universe.get("accepted_canonical_payout_vectors")
    try:
        vectors = {tuple(vector) for vector in raw_vectors}
    except TypeError as exc:
        raise CandidateManifestError("protocol payout vectors are malformed") from exc
    if vectors != {(1, 0), (0, 1)}:
        raise CandidateManifestError("protocol must accept exactly [1,0] and [0,1]")
    probabilities = set(universe.get("accepted_gamma_candidate_yes_probabilities", ()))
    if probabilities != {0.0, 1.0}:
        raise CandidateManifestError("protocol must accept exactly Gamma candidates 0 and 1")
    resolution_grace_seconds = _nonnegative_int(
        universe.get("resolution_grace_seconds"), "protocol resolution_grace_seconds"
    )
    return address, topic0, outcomes, vectors, resolution_grace_seconds


def _parse_candidate_row(row: Mapping[str, Any], outcomes: tuple[str, str]) -> CandidateRow:
    original = dict(row)
    conflicts = sorted(set(original).intersection(RESERVED_OUTPUT_COLUMNS))
    if conflicts:
        raise CandidateManifestError(f"candidate row contains reserved output columns: {conflicts}")
    event_id = _text_field(original, ("event_id", "eventId"), "event_id")
    condition_id = _normalize_hex(
        _field(original, ("condition_id", "conditionId"), "condition_id"),
        32,
        "condition_id",
    )
    target_at = _parse_datetime(
        _field(
            original,
            (
                "target_timestamp",
                "target_time",
                "scheduled_at",
                "scheduled_time",
                "scheduled_timestamp",
                "event_timestamp",
                "end_date",
                "endDate",
            ),
            "target timestamp",
        )
    )
    split = _text_field(original, ("split",), "split")
    horizon = _text_field(
        original,
        ("horizon_seconds", "forecast_horizon_seconds", "horizon"),
        "horizon",
    )
    outcomes_value = _optional_field(original, ("outcomes", "outcome_order"))
    parsed_outcomes = (
        outcomes if outcomes_value is None else tuple(_parse_list(outcomes_value, "outcomes"))
    )
    if parsed_outcomes != outcomes:
        raise CandidateManifestError(
            f"condition {condition_id} outcome order must be {list(outcomes)!r}"
        )
    candidate = _gamma_candidate(original, parsed_outcomes)
    return CandidateRow(
        original,
        event_id,
        condition_id,
        target_at,
        split,
        horizon,
        candidate,
        parsed_outcomes,
    )


def _gamma_candidate(row: Mapping[str, Any], outcomes: Sequence[str]) -> int:
    explicit = _optional_field(
        row,
        (
            "gamma_candidate_yes_probability",
            "gamma_candidate_yes",
            "candidate_yes_probability",
            "gamma_candidate_label",
        ),
    )
    candidate: int | None = None
    if explicit is not None:
        if isinstance(explicit, bool):
            raise CandidateManifestError("Gamma candidate must not be boolean")
        if isinstance(explicit, str) and explicit.strip().lower() in {"yes", "no"}:
            value = 1.0 if explicit.strip().lower() == "yes" else 0.0
        else:
            try:
                value = float(explicit)
            except (TypeError, ValueError) as exc:
                raise CandidateManifestError(
                    "Gamma candidate must be Yes/No or numeric 0/1"
                ) from exc
        if not math.isfinite(value) or value not in (0.0, 1.0):
            raise CandidateManifestError("Gamma candidate must be exactly 0 or 1")
        candidate = int(value)

    prices_raw = _optional_field(row, ("outcome_prices", "outcomePrices"))
    if prices_raw is not None:
        prices = _parse_list(prices_raw, "outcome prices")
        if len(prices) != len(outcomes):
            raise CandidateManifestError("outcome prices length differs from outcomes")
        try:
            numeric = [float(value) for value in prices]
        except (TypeError, ValueError) as exc:
            raise CandidateManifestError("outcome prices must be numeric") from exc
        if numeric not in ([1.0, 0.0], [0.0, 1.0]):
            raise CandidateManifestError("Gamma outcome prices must be exactly [1,0] or [0,1]")
        derived = int(numeric[outcomes.index("Yes")])
        if candidate is not None and candidate != derived:
            raise CandidateManifestError("explicit Gamma candidate conflicts with outcome prices")
        candidate = derived
    if candidate is None:
        raise CandidateManifestError(
            "row needs gamma_candidate_yes_probability or final outcome_prices"
        )
    return candidate


def _validate_candidate_consistency(rows: Sequence[CandidateRow]) -> None:
    keys: set[tuple[str, str, str, str]] = set()
    conditions: dict[str, CandidateRow] = {}
    for row in rows:
        key = (row.split, row.horizon, row.event_id, row.condition_id)
        if key in keys:
            raise CandidateManifestError(f"duplicate candidate row {key!r}")
        keys.add(key)
        previous = conditions.setdefault(row.condition_id, row)
        if previous is row:
            continue
        comparable = (
            previous.event_id,
            previous.target_at,
            previous.split,
            previous.gamma_candidate,
            previous.outcomes,
        )
        current = (
            row.event_id,
            row.target_at,
            row.split,
            row.gamma_candidate,
            row.outcomes,
        )
        if comparable != current:
            raise CandidateManifestError(
                f"condition {row.condition_id} has conflicting candidate metadata"
            )


def _conditions_by_id(rows: Iterable[CandidateRow]) -> dict[str, CandidateRow]:
    result: dict[str, CandidateRow] = {}
    for row in rows:
        result.setdefault(row.condition_id, row)
    return result


def _validate_filtered_log(
    raw_log: Mapping[str, Any],
    *,
    address: str,
    topic0: str,
    requested_conditions: set[str],
) -> str:
    if _normalize_hex(raw_log.get("address"), 20, "log.address") != address:
        raise RpcError("eth_getLogs returned a log from a different address")
    topics = raw_log.get("topics")
    if not isinstance(topics, list) or len(topics) < 2:
        raise RpcError("ConditionResolution log has fewer than two topics")
    if _normalize_hex(topics[0], 32, "log.topic0") != topic0:
        raise RpcError("eth_getLogs returned a log with a different topic0")
    condition_id = _normalize_hex(topics[1], 32, "log.topic1")
    if condition_id not in requested_conditions:
        raise RpcError("eth_getLogs returned a condition outside the topic1 OR filter")
    return condition_id


def _flatten_json_candidates(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("rows", "candidates", "markets"):
            if key in payload:
                rows = payload[key]
                break
        else:
            events = payload.get("events")
            if not isinstance(events, list):
                raise CandidateManifestError(
                    "candidate JSON needs a list, rows/candidates/markets, or events"
                )
            flattened: list[dict[str, Any]] = []
            for event in events:
                if not isinstance(event, dict) or not isinstance(event.get("markets"), list):
                    raise CandidateManifestError("each JSON event must contain a markets list")
                event_fields = {key: value for key, value in event.items() if key != "markets"}
                for market in event["markets"]:
                    if not isinstance(market, dict):
                        raise CandidateManifestError("event markets must be objects")
                    flattened.append({**event_fields, **market})
            rows = flattened
    else:
        raise CandidateManifestError("candidate JSON top level must be an array or object")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise CandidateManifestError("candidate rows must be JSON objects")
    return [dict(row) for row in rows]


def _raw_evidence_manifest(
    evidence: Sequence[RpcEvidence],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    records: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for item in evidence:
        safe_method = "".join(
            character if character.isalnum() else "_" for character in item.method
        )
        relative = f"raw_chain_v0.1/{item.sequence:06d}_{safe_method}.json.gz"
        compressed = gzip.compress(item.response, compresslevel=9, mtime=0)
        payloads[relative] = compressed
        records.append(
            {
                "sequence": item.sequence,
                "method": item.method,
                "params": item.params,
                "request_sha256": item.request_sha256,
                "response_sha256": item.response_sha256,
                "retrieved_at_utc": item.retrieved_at_utc,
                "raw_size_bytes": len(item.response),
                "gzip_path": relative,
                "gzip_sha256": _sha256(compressed),
                "gzip_size_bytes": len(compressed),
            }
        )
    return records, payloads


def _publish_outputs(
    *,
    data_dir: Path,
    csv_payload: bytes,
    collector_manifest_payload: bytes,
    manifest_payload: bytes,
    raw_payloads: Mapping[str, bytes],
    overwrite: bool,
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".verify-chain-", dir=data_dir))
    raw_stage = stage / "raw_chain_v0.1"
    try:
        (stage / "frozen_v0.1.csv.gz").write_bytes(csv_payload)
        (stage / "collector_manifest_v0.1.json").write_bytes(collector_manifest_payload)
        (stage / "manifest_v0.1.json").write_bytes(manifest_payload)
        raw_stage.mkdir()
        for relative, payload in raw_payloads.items():
            relative_path = Path(relative)
            if relative_path.parts[0] != "raw_chain_v0.1" or len(relative_path.parts) != 2:
                raise AssertionError("unsafe raw evidence path")
            (raw_stage / relative_path.name).write_bytes(payload)

        targets = (
            data_dir / "frozen_v0.1.csv.gz",
            data_dir / "collector_manifest_v0.1.json",
            data_dir / "manifest_v0.1.json",
            data_dir / "raw_chain_v0.1",
        )
        if overwrite:
            for target in targets:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
        os.replace(raw_stage, targets[3])
        os.replace(stage / "frozen_v0.1.csv.gz", targets[0])
        os.replace(stage / "collector_manifest_v0.1.json", targets[1])
        os.replace(stage / "manifest_v0.1.json", targets[2])
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _preflight_outputs(paths: Sequence[Path], *, overwrite: bool) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing and not overwrite:
        raise ChainVerificationError(
            f"refusing to overwrite existing evidence: {existing}; pass --overwrite explicitly"
        )


def _status_count_dict(counts: Counter[str]) -> dict[str, int]:
    return {
        status: counts[status]
        for status in ("agreed", "missing", "ambiguous", "gamma_disagreement")
    }


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CandidateManifestError(f"{name} must be a non-negative integer")
    return value


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _require_ratio(value: Any, expected: float, name: str) -> None:
    if isinstance(value, bool):
        raise CandidateManifestError(f"{name} must be numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise CandidateManifestError(f"{name} must be numeric") from exc
    if not math.isfinite(numeric) or not math.isclose(numeric, expected, abs_tol=1e-12):
        raise CandidateManifestError(f"{name} does not match its counts")


def _nested_coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        split = str(row["split"])
        horizon = str(row["horizon"])
        metrics = {key: value for key, value in row.items() if key not in {"split", "horizon"}}
        result.setdefault(split, {})[horizon] = metrics
    return result


def _chain_source_hash(log_sha256: str, block_sha256: str) -> str:
    domain = b"event-factor-bench-chain-source-v1\x00"
    return _sha256(domain + bytes.fromhex(log_sha256) + bytes.fromhex(block_sha256))


def _field(row: Mapping[str, Any], aliases: Sequence[str], name: str) -> Any:
    value = _optional_field(row, aliases)
    if value is None or value == "":
        raise CandidateManifestError(f"candidate row is missing {name}")
    return value


def _optional_field(row: Mapping[str, Any], aliases: Sequence[str]) -> Any | None:
    found = [
        (alias, row[alias]) for alias in aliases if alias in row and row[alias] not in (None, "")
    ]
    if not found:
        return None
    first = found[0][1]
    if any(_comparable_value(value) != _comparable_value(first) for _, value in found[1:]):
        keys = [key for key, _ in found]
        raise CandidateManifestError(f"conflicting aliases for {aliases[0]}: {keys}")
    return first


def _text_field(row: Mapping[str, Any], aliases: Sequence[str], name: str) -> str:
    value = _field(row, aliases, name)
    if isinstance(value, bool):
        raise CandidateManifestError(f"{name} must be text or numeric, not boolean")
    text = str(value).strip()
    if not text:
        raise CandidateManifestError(f"{name} must not be empty")
    return text


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, bool):
        raise CandidateManifestError("target timestamp must not be boolean")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise CandidateManifestError("target timestamp epoch must be finite")
        result = datetime.fromtimestamp(float(value), tz=UTC)
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith(("Z", "z")):
            normalized = normalized[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise CandidateManifestError(f"invalid target timestamp {value!r}") from exc
        if result.tzinfo is None or result.utcoffset() is None:
            raise CandidateManifestError("target timestamp must include a UTC offset")
        result = result.astimezone(UTC)
    else:
        raise CandidateManifestError("target timestamp must be ISO-8601 text or epoch seconds")
    return result.astimezone(UTC)


def _parse_list(value: Any, name: str) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CandidateManifestError(f"{name} must be a JSON array") from exc
        if isinstance(parsed, list):
            return parsed
    raise CandidateManifestError(f"{name} must be an array")


def _comparable_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return str(value).lower()
    return value


def _parse_quantity(value: Any, name: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) < 3:
        raise RpcError(f"{name} must be a 0x-prefixed JSON-RPC quantity")
    if value != "0x0" and value[2] == "0":
        raise RpcError(f"{name} is not a canonical JSON-RPC quantity")
    try:
        result = int(value[2:], 16)
    except ValueError as exc:
        raise RpcError(f"{name} is not hexadecimal") from exc
    if result < 0:
        raise RpcError(f"{name} must be non-negative")
    return result


def _quantity(value: int) -> str:
    if value < 0:
        raise ValueError("JSON-RPC quantities must be non-negative")
    return hex(value)


def _normalize_hex(value: Any, byte_length: int, name: str) -> str:
    raw = _hex_bytes(value, name)
    if len(raw) != byte_length:
        raise RpcError(f"{name} must be exactly {byte_length} bytes")
    return "0x" + raw.hex()


def _hex_bytes(value: Any, name: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        error = AbiDecodeError if name.startswith("ConditionResolution") else RpcError
        raise error(f"{name} must be 0x-prefixed hex")
    body = value[2:]
    if len(body) % 2:
        error = AbiDecodeError if name.startswith("ConditionResolution") else RpcError
        raise error(f"{name} has odd-length hex")
    try:
        return bytes.fromhex(body)
    except ValueError as exc:
        error = AbiDecodeError if name.startswith("ConditionResolution") else RpcError
        raise error(f"{name} contains non-hex characters") from exc


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _iso_timestamp(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return _iso_utc(datetime.fromtimestamp(timestamp, tz=UTC))


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return _iso_utc(datetime.now(tz=UTC))


def _http_transport(url: str, body: bytes, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": RPC_USER_AGENT,
        },
        method="POST",
    )
    for attempt in range(RPC_MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise RpcError(f"HTTP status {response.status}")
                return response.read()
        except urllib.error.HTTPError as exc:
            retryable = exc.code in RPC_RETRYABLE_HTTP_STATUSES
            if not retryable or attempt + 1 == RPC_MAX_ATTEMPTS:
                raise RpcError(f"HTTP status {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt + 1 == RPC_MAX_ATTEMPTS:
                reason = getattr(exc, "reason", str(exc))
                raise RpcError(f"network error: {reason}") from exc
        time_module.sleep(RPC_BACKOFF_SECONDS[attempt])
    raise AssertionError("unreachable retry loop")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--input", dest="candidate_path", type=Path, required=True)
    parser.add_argument("--collector-manifest", type=Path, required=True)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--rpc-timeout", type=float, default=30.0)
    parser.add_argument(
        "--resolution-grace-seconds",
        type=int,
        default=None,
        help="optional assertion; when set it must equal the frozen protocol",
    )
    parser.add_argument("--max-blocks-per-query", type=int, default=MAX_BLOCKS_PER_QUERY)
    parser.add_argument("--run-source-commit", required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.resolution_grace_seconds is not None and args.resolution_grace_seconds < 0:
        raise SystemExit("--resolution-grace-seconds must be non-negative")
    rpc = JsonRpcClient(args.rpc_url, timeout=args.rpc_timeout)
    try:
        manifest = verify_and_freeze(
            protocol_path=args.protocol,
            candidate_path=args.candidate_path,
            collector_manifest_path=args.collector_manifest,
            data_dir=args.data_dir,
            rpc=rpc,
            resolution_grace=(
                timedelta(seconds=args.resolution_grace_seconds)
                if args.resolution_grace_seconds is not None
                else None
            ),
            max_blocks_per_query=args.max_blocks_per_query,
            overwrite=args.overwrite,
            run_source_commit=args.run_source_commit,
        )
    except (ChainVerificationError, OSError, ValueError) as exc:
        print(f"chain verification failed closed: {exc}", file=sys.stderr)
        return 2
    summary = manifest["summary"]
    if not manifest["onchain_verified"]:
        print(
            "chain verification failed closed: at least one candidate is missing, ambiguous, "
            "or disagrees with Gamma; inspect data/manifest_v0.1.json",
            file=sys.stderr,
        )
        return 2
    print(
        "frozen "
        f"{summary['retained_rows_after_complete_event_gate']}/{summary['expected_rows']} rows; "
        f"manifest={args.data_dir / 'manifest_v0.1.json'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
