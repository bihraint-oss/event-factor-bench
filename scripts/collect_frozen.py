#!/usr/bin/env python3
"""Collect EventFactorBench candidate rows under the frozen v0.1 protocol.

Only the two official public endpoints pinned in the protocol are used.  Every raw
response is retained verbatim inside a deterministic gzip member and recorded in a
SHA-256 manifest.  Gamma terminal outcome prices are candidate labels only: this
collector does not verify the canonical on-chain CTF payout vector.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from event_factor_bench.history import (
    ConflictingHistoryPointError,
    NoEligibleHistoryPointError,
    PricePoint,
    StaleHistoryPointError,
    latest_at_or_before,
)
from event_factor_bench.thresholds import parse_threshold

LABEL_CAVEAT = (
    "gamma_candidate_label is derived only from a strict terminal Gamma outcomePrices "
    "vector of [1,0] or [0,1]; it is not a canonical label and has not been verified "
    "against the on-chain CTF payout vector."
)
LABEL_SOURCE = "gamma_terminal_outcome_prices_candidate"
MAX_BATCH_TOKENS = 20
MAX_HISTORY_WINDOW_SECONDS = 15 * 24 * 60 * 60
Transport = Callable[[str, str, bytes | None, Mapping[str, str]], bytes]


class CollectionError(RuntimeError):
    """Raised when an upstream response or frozen-protocol invariant is violated."""


@dataclass(slots=True)
class RawArchive:
    """Write byte-exact responses as deterministic gzip files with checksums."""

    output_dir: Path
    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(
        self,
        relative_path: str,
        payload: bytes,
        *,
        source: str,
        request: Mapping[str, Any],
    ) -> None:
        path = self.output_dir / relative_path
        if path.exists():
            raise CollectionError(f"refusing to overwrite raw response: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        compressed = gzip.compress(payload, compresslevel=9, mtime=0)
        path.write_bytes(compressed)
        self.entries.append(
            {
                "path": relative_path,
                "source": source,
                "request": dict(request),
                "archived_at_utc": format_timestamp(datetime.now(tz=UTC)),
                "content_bytes": len(payload),
                "content_sha256": _sha256(payload),
                "gzip_bytes": len(compressed),
                "gzip_sha256": _sha256(compressed),
            }
        )


@dataclass(frozen=True, slots=True)
class HistorySeries:
    """One token's parsed points plus the exact raw response that supplied them."""

    points: tuple[PricePoint, ...]
    source_sha256: str


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_source_commit(value: str) -> None:
    if (
        len(value) != 40
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CollectionError("run_source_commit must be a full lowercase 40-hex commit")


def _identifier_sort_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdecimal() else (1, value)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _load_json(payload: bytes, context: str) -> Any:
    try:
        return json.loads(payload, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CollectionError(f"invalid JSON from {context}") from exc


def parse_timestamp(value: Any, context: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""

    if not isinstance(value, str) or not value.strip():
        raise CollectionError(f"{context} must be a non-empty ISO-8601 string")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CollectionError(f"invalid timestamp for {context}: {value!r}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise CollectionError(f"{context} must be timezone-aware")
    return result.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def epoch_seconds(value: datetime, context: str) -> int:
    timestamp = value.timestamp()
    if not timestamp.is_integer():
        raise CollectionError(f"{context} must resolve to a whole UTC second")
    return int(timestamp)


def daily_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Return adjacent daily query windows whose API upper bounds are inclusive."""

    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("end must be timezone-aware")
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    if end <= start:
        raise ValueError("end must be later than start")
    result: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + timedelta(days=1), end)
        result.append((cursor, next_cursor))
        cursor = next_cursor
    return result


def make_http_transport(*, timeout: float, retries: int = 5) -> Transport:
    """Build a small retrying urllib transport for read-only official API calls."""

    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if retries < 1:
        raise ValueError("retries must be at least one")

    def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> bytes:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt + 1 == retries:
                    raise CollectionError(f"HTTP {exc.code} for {method} {url}") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt + 1 == retries:
                    raise CollectionError(f"request failed for {method} {url}") from exc
            time.sleep(min(0.5 * 2**attempt, 8.0))
        raise AssertionError("unreachable")

    return transport


def _validate_protocol(protocol: Mapping[str, Any]) -> None:
    try:
        retrieval = protocol["retrieval"]
        universe = protocol["universe"]
        forecast = protocol["forecast"]
        splits = protocol["splits"]
        start = parse_timestamp(
            retrieval["discovery_start_inclusive"], "retrieval.discovery_start_inclusive"
        )
        end = parse_timestamp(
            retrieval["discovery_end_exclusive"], "retrieval.discovery_end_exclusive"
        )
        window = int(retrieval["history_window_seconds"])
        fidelity = int(retrieval["history_fidelity_minutes"])
        primary = int(forecast["primary_horizon_seconds"])
        secondary = [int(item) for item in forecast["secondary_horizons_seconds"]]
        max_staleness = int(forecast["max_staleness_seconds"])
        minimum = int(universe["minimum_thresholds_per_event"])
        title_search = retrieval["gamma_title_search"]
        regex = universe["event_title_regex"]
    except (KeyError, TypeError, ValueError) as exc:
        raise CollectionError("protocol is missing a required field or has a bad type") from exc

    if end <= start:
        raise CollectionError("discovery_end_exclusive must be after discovery_start_inclusive")
    if not isinstance(title_search, str) or not title_search.strip():
        raise CollectionError("gamma_title_search must be a non-empty string")
    if not isinstance(regex, str):
        raise CollectionError("event_title_regex must be a string")
    try:
        re.compile(regex)
    except re.error as exc:
        raise CollectionError("event_title_regex is invalid") from exc
    if window <= 0 or window > MAX_HISTORY_WINDOW_SECONDS:
        raise CollectionError("history_window_seconds must be in (0, 1296000]")
    if fidelity <= 0:
        raise CollectionError("history_fidelity_minutes must be positive")
    horizons = [primary, *secondary]
    if not horizons or any(item <= 0 for item in horizons):
        raise CollectionError("all forecast horizons must be positive")
    if len(set(horizons)) != len(horizons):
        raise CollectionError("forecast horizons must be unique")
    if max_staleness < 0:
        raise CollectionError("max_staleness_seconds must be non-negative")
    if window < max(horizons) + max_staleness:
        raise CollectionError("history window does not cover the longest cutoff plus staleness")
    if minimum < 1:
        raise CollectionError("minimum_thresholds_per_event must be positive")
    if universe.get("required_outcomes") != ["Yes", "No"]:
        raise CollectionError("v0.1 requires outcomes in exact [Yes, No] order")
    if set(universe.get("accepted_gamma_candidate_yes_probabilities", [])) != {0.0, 1.0}:
        raise CollectionError("v0.1 candidate labels require final Yes values {0, 1}")
    if not isinstance(splits, Mapping) or not splits:
        raise CollectionError("at least one split must be configured")
    _validate_splits(splits)


def _validate_splits(splits: Mapping[str, Any]) -> None:
    intervals: list[tuple[datetime, datetime, str]] = []
    for name, raw in splits.items():
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            raise CollectionError("split definitions must be named objects")
        try:
            start = parse_timestamp(raw["start_inclusive"], f"splits.{name}.start_inclusive")
            end = parse_timestamp(raw["end_exclusive"], f"splits.{name}.end_exclusive")
        except KeyError as exc:
            raise CollectionError(f"split {name!r} is missing a boundary") from exc
        if end <= start:
            raise CollectionError(f"split {name!r} has an empty or reversed interval")
        intervals.append((start, end, name))
    intervals.sort()
    for previous, current in pairwise(intervals):
        if current[0] < previous[1]:
            raise CollectionError(f"splits {previous[2]!r} and {current[2]!r} overlap")


def _split_for(end_time: datetime, splits: Mapping[str, Any]) -> str | None:
    matches = []
    for name, raw in splits.items():
        start = parse_timestamp(raw["start_inclusive"], f"splits.{name}.start_inclusive")
        end = parse_timestamp(raw["end_exclusive"], f"splits.{name}.end_exclusive")
        if start <= end_time < end:
            matches.append(name)
    if len(matches) > 1:
        raise CollectionError(f"event at {end_time.isoformat()} belongs to multiple splits")
    return matches[0] if matches else None


def _request_headers(user_agent: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": user_agent}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def collect_gamma_events(
    protocol: Mapping[str, Any],
    *,
    transport: Transport,
    archive: RawArchive,
    user_agent: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Collect daily events/keyset windows and deduplicate inclusive boundaries by ID."""

    retrieval = protocol["retrieval"]
    start = parse_timestamp(
        retrieval["discovery_start_inclusive"], "retrieval.discovery_start_inclusive"
    )
    end = parse_timestamp(retrieval["discovery_end_exclusive"], "retrieval.discovery_end_exclusive")
    endpoint = str(retrieval["gamma_endpoint"])
    seen: dict[str, tuple[bytes, bytes, dict[str, Any]]] = {}
    raw_rows = 0
    duplicates = 0
    mutable_duplicates = 0
    pages = 0

    for window_index, (window_start, window_end) in enumerate(daily_windows(start, end)):
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_index = 0
        while True:
            params = {
                "closed": "true",
                "end_date_max": format_timestamp(window_end),
                "end_date_min": format_timestamp(window_start),
                "limit": 100,
                "title_search": str(retrieval["gamma_title_search"]),
            }
            if cursor is not None:
                params["after_cursor"] = cursor
            url = f"{endpoint}?{urllib.parse.urlencode(params)}"
            payload = transport("GET", url, None, _request_headers(user_agent))
            relative = (
                f"raw/gamma/window_{window_index:04d}_{window_start:%Y%m%dT%H%M%SZ}/"
                f"page_{page_index:04d}.json.gz"
            )
            archive.add(
                relative,
                payload,
                source="gamma.events_keyset",
                request={"method": "GET", "url": url},
            )
            page = _load_json(payload, url)
            if not isinstance(page, Mapping) or not isinstance(page.get("events"), list):
                raise CollectionError("events/keyset response must contain an events array")
            batch = page["events"]
            pages += 1
            raw_rows += len(batch)
            for item in batch:
                if not isinstance(item, dict):
                    raise CollectionError("events/keyset returned a non-object event")
                event_id_raw = item.get("id")
                if isinstance(event_id_raw, bool) or event_id_raw is None:
                    raise CollectionError("events/keyset event is missing a usable id")
                event_id = str(event_id_raw).strip()
                if not event_id:
                    raise CollectionError("events/keyset event has an empty id")
                canonical = _canonical_json(item)
                selection_canonical = _canonical_json(_event_selection_view(item))
                previous = seen.get(event_id)
                if previous is not None:
                    duplicates += 1
                    if previous[0] != selection_canonical:
                        raise CollectionError(
                            f"duplicate event id {event_id!r} changed selection-relevant fields"
                        )
                    mutable_duplicates += int(previous[1] != canonical)
                    continue
                item_with_source = dict(item)
                item_with_source["_source_event_sha256"] = _sha256(payload)
                seen[event_id] = (selection_canonical, canonical, item_with_source)

            next_cursor = page.get("next_cursor")
            if next_cursor is None or next_cursor == "":
                break
            if not isinstance(next_cursor, str):
                raise CollectionError("events/keyset next_cursor must be a string or null")
            if not batch:
                raise CollectionError("events/keyset returned a cursor with an empty page")
            if next_cursor in seen_cursors:
                raise CollectionError("events/keyset pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            page_index += 1
            if page_index >= 10_000:
                raise CollectionError("events/keyset pagination exceeded 10,000 pages")

    return [item for _, _, item in seen.values()], {
        "gamma_pages": pages,
        "gamma_event_rows": raw_rows,
        "gamma_unique_event_ids": len(seen),
        "gamma_inclusive_boundary_duplicates": duplicates,
        "gamma_duplicates_with_unused_field_changes": mutable_duplicates,
    }


def _event_selection_view(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return only Gamma fields that can affect eligibility or normalized evidence."""

    market_fields = (
        "id",
        "conditionId",
        "question",
        "groupItemTitle",
        "endDate",
        "closed",
        "enableOrderBook",
        "umaResolutionStatus",
        "outcomes",
        "outcomePrices",
        "clobTokenIds",
    )
    markets = event.get("markets")
    projected_markets: Any = markets
    if isinstance(markets, list):
        projected_markets = [
            {key: market.get(key) for key in market_fields}
            if isinstance(market, Mapping)
            else market
            for market in markets
        ]
        projected_markets.sort(
            key=lambda market: _canonical_json(market) if isinstance(market, Mapping) else b""
        )
    return {
        "id": event.get("id"),
        "title": event.get("title"),
        "endDate": event.get("endDate"),
        "closed": event.get("closed"),
        "markets": projected_markets,
    }


def _parse_json_list(value: Any, context: str) -> list[Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CollectionError(f"{context} is not valid JSON") from exc
    else:
        parsed = value
    if not isinstance(parsed, list):
        raise CollectionError(f"{context} must encode a JSON array")
    return parsed


def _market_candidate(
    market: Mapping[str, Any],
    *,
    event_id: str,
    event_title: str,
    event_end: datetime,
    source_event_sha256: str,
    asset: str,
    universe: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    market_id_raw = market.get("id")
    market_id = "" if isinstance(market_id_raw, bool) else str(market_id_raw or "").strip()
    if not market_id:
        reasons.append("missing_market_id")
    condition_id = market.get("conditionId")
    if not isinstance(condition_id, str) or not condition_id.strip():
        reasons.append("missing_condition_id")
    question = market.get("question")
    group_title = market.get("groupItemTitle")
    threshold: float | None = None
    if not isinstance(group_title, str) or not group_title.strip():
        reasons.append("missing_group_item_title")
    else:
        try:
            threshold = parse_threshold(group_title)
        except (TypeError, ValueError):
            reasons.append("invalid_group_item_threshold")
    expected_question = (
        event_title.replace("___", group_title, 1) if isinstance(group_title, str) else None
    )
    if not isinstance(question, str) or question != expected_question:
        reasons.append("question_does_not_match_event_template")
    if market.get("closed") is not True:
        reasons.append("market_not_closed")
    if market.get("enableOrderBook") is not True:
        reasons.append("order_book_not_enabled")
    if market.get("umaResolutionStatus") != universe["required_resolution_status"]:
        reasons.append("resolution_status_mismatch")

    try:
        market_end = parse_timestamp(market.get("endDate"), f"market {market_id} endDate")
    except CollectionError:
        market_end = None
        reasons.append("invalid_market_end_date")
    if market_end is not None and market_end != event_end:
        reasons.append("market_end_does_not_match_event_end")

    outcomes: list[Any] | None = None
    prices: list[float] | None = None
    tokens: list[str] | None = None
    try:
        outcomes = _parse_json_list(market.get("outcomes"), f"market {market_id} outcomes")
    except CollectionError:
        reasons.append("invalid_outcomes")
    if outcomes is not None and outcomes != universe["required_outcomes"]:
        reasons.append("outcomes_mismatch")

    try:
        raw_prices = _parse_json_list(
            market.get("outcomePrices"), f"market {market_id} outcomePrices"
        )
        if len(raw_prices) != 2 or any(isinstance(item, bool) for item in raw_prices):
            raise CollectionError("outcomePrices must have two numeric values")
        prices = [float(item) for item in raw_prices]
        if any(not math.isfinite(item) for item in prices):
            raise CollectionError("outcomePrices must be finite")
    except (CollectionError, TypeError, ValueError):
        reasons.append("invalid_outcome_prices")
    if prices is not None and prices not in ([1.0, 0.0], [0.0, 1.0]):
        reasons.append("non_terminal_gamma_outcome_prices")

    try:
        raw_tokens = _parse_json_list(
            market.get("clobTokenIds"), f"market {market_id} clobTokenIds"
        )
        tokens = [str(item).strip() for item in raw_tokens]
        if (
            len(tokens) != 2
            or tokens[0] == tokens[1]
            or any(not item.isdecimal() for item in tokens)
        ):
            raise CollectionError("clobTokenIds must contain two distinct decimal ids")
    except CollectionError:
        tokens = None
        reasons.append("invalid_clob_token_ids")

    if reasons:
        return None, reasons
    assert threshold is not None
    assert prices is not None
    assert tokens is not None
    assert isinstance(question, str)
    assert isinstance(condition_id, str)
    return {
        "asset": asset,
        "condition_id": condition_id,
        "event_end": event_end,
        "event_id": event_id,
        "event_title": event_title,
        "gamma_candidate_label": prices[0],
        "market_id": market_id,
        "no_token_id": tokens[1],
        "question": question,
        "source_event_sha256": source_event_sha256,
        "threshold": threshold,
        "yes_token_id": tokens[0],
    }, []


def select_event_contracts(
    events: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply all local event and nested-market universe filters."""

    retrieval = protocol["retrieval"]
    universe = protocol["universe"]
    start = parse_timestamp(
        retrieval["discovery_start_inclusive"], "retrieval.discovery_start_inclusive"
    )
    end = parse_timestamp(retrieval["discovery_end_exclusive"], "retrieval.discovery_end_exclusive")
    event_regex = re.compile(universe["event_title_regex"])
    title_search = str(retrieval["gamma_title_search"]).casefold()
    assets = set(universe["assets"])
    minimum = int(universe["minimum_thresholds_per_event"])
    selected: list[dict[str, Any]] = []

    for event in events:
        event_id = str(event.get("id", "")).strip()
        title = event.get("title")
        event_reasons: list[str] = []
        event_end: datetime | None = None
        split: str | None = None
        asset: str | None = None
        match: re.Match[str] | None = None
        if not isinstance(title, str):
            event_reasons.append("missing_event_title")
        else:
            match = event_regex.fullmatch(title)
            if match is None:
                event_reasons.append("event_title_regex_mismatch")
            if title_search not in title.casefold():
                event_reasons.append("local_title_search_mismatch")
            asset = next((item for item in assets if title.startswith(f"{item} above ")), None)
            if asset is None:
                event_reasons.append("asset_not_allowed")
        if event.get("closed") is not True:
            event_reasons.append("event_not_closed")
        try:
            event_end = parse_timestamp(event.get("endDate"), f"event {event_id} endDate")
        except CollectionError:
            event_reasons.append("invalid_event_end_date")
        if event_end is not None:
            if not start <= event_end < end:
                event_reasons.append("event_outside_discovery_half_open_interval")
            split = _split_for(event_end, protocol["splits"])
            if split is None:
                event_reasons.append("event_not_in_any_split")
        markets = event.get("markets")
        if not isinstance(markets, list):
            event_reasons.append("missing_nested_markets")
            markets = []

        candidates: list[dict[str, Any]] = []
        if not event_reasons and match is not None:
            assert isinstance(title, str)
            assert event_end is not None
            assert asset is not None
            source_event_sha256 = event.get("_source_event_sha256")
            if not isinstance(source_event_sha256, str):
                raise CollectionError(f"event {event_id} is missing its raw source checksum")
            for market in markets:
                market_id = market.get("id") if isinstance(market, Mapping) else None
                if not isinstance(market, Mapping):
                    audit["market_decisions"].append(
                        {
                            "event_id": event_id,
                            "market_id": market_id,
                            "reasons": ["nested_market_not_an_object"],
                            "status": "rejected",
                        }
                    )
                    continue
                candidate, reasons = _market_candidate(
                    market,
                    event_id=event_id,
                    event_title=title,
                    event_end=event_end,
                    source_event_sha256=source_event_sha256,
                    asset=asset,
                    universe=universe,
                )
                audit["market_decisions"].append(
                    {
                        "event_id": event_id,
                        "market_id": str(market_id) if market_id is not None else None,
                        "reasons": reasons,
                        "status": "passed" if candidate is not None else "rejected",
                    }
                )
                if candidate is not None:
                    candidates.append(candidate)

            thresholds = [item["threshold"] for item in candidates]
            market_ids = [item["market_id"] for item in candidates]
            token_ids = [
                token
                for item in candidates
                for token in (item["yes_token_id"], item["no_token_id"])
            ]
            if len(set(thresholds)) != len(thresholds):
                event_reasons.append("duplicate_thresholds_after_market_filter")
            if len(set(market_ids)) != len(market_ids):
                event_reasons.append("duplicate_market_ids_after_market_filter")
            if len(set(token_ids)) != len(token_ids):
                event_reasons.append("duplicate_token_ids_after_market_filter")
            if len(candidates) < minimum:
                event_reasons.append("too_few_strictly_eligible_markets")
            if len(candidates) != len(markets):
                event_reasons.append("nested_market_failed_strict_filter")

        status = "selected" if not event_reasons else "rejected"
        audit["event_decisions"].append(
            {
                "eligible_market_count": len(candidates),
                "event_end": format_timestamp(event_end) if event_end is not None else None,
                "event_id": event_id,
                "reasons": event_reasons,
                "split": split,
                "status": status,
                "title": title,
                "total_nested_market_count": len(markets),
            }
        )
        if status == "selected":
            assert split is not None
            for candidate in candidates:
                candidate["split"] = split
            selected.extend(candidates)

    market_ids = [item["market_id"] for item in selected]
    token_ids = [
        token for item in selected for token in (item["yes_token_id"], item["no_token_id"])
    ]
    if len(set(market_ids)) != len(market_ids):
        raise CollectionError("a selected market id appeared in multiple events")
    if len(set(token_ids)) != len(token_ids):
        raise CollectionError("a selected CLOB token id appeared in multiple events")
    return selected


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    if size < 1:
        raise ValueError("chunk size must be positive")
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def collect_histories(
    contracts: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
    *,
    transport: Transport,
    archive: RawArchive,
    user_agent: str,
    workers: int = 1,
) -> dict[str, HistorySeries]:
    """Fetch explicit-range price histories in official batches of at most 20 tokens."""

    retrieval = protocol["retrieval"]
    endpoint = str(retrieval["clob_endpoint"])
    window = int(retrieval["history_window_seconds"])
    fidelity = int(retrieval["history_fidelity_minutes"])
    by_event: dict[str, list[Mapping[str, Any]]] = {}
    for contract in contracts:
        by_event.setdefault(str(contract["event_id"]), []).append(contract)
    if workers < 1:
        raise ValueError("workers must be positive")

    def fetch_event(
        item: tuple[str, list[Mapping[str, Any]]],
    ) -> tuple[str, list[tuple[str, bytes, dict[str, Any], dict[str, HistorySeries]]]]:
        event_id, event_contracts = item
        event_end_values = {item["event_end"] for item in event_contracts}
        if len(event_end_values) != 1:
            raise CollectionError(f"event {event_id} contracts disagree on end time")
        event_end = next(iter(event_end_values))
        assert isinstance(event_end, datetime)
        end_ts = epoch_seconds(event_end, f"event {event_id} endDate")
        tokens = [str(item["yes_token_id"]) for item in event_contracts]
        batches: list[tuple[str, bytes, dict[str, Any], dict[str, HistorySeries]]] = []
        for batch_index, token_batch in enumerate(_chunks(tokens, MAX_BATCH_TOKENS)):
            body_object = {
                "end_ts": end_ts,
                "fidelity": fidelity,
                "markets": token_batch,
                "start_ts": end_ts - window,
            }
            body = _canonical_json(body_object)
            payload = transport(
                "POST",
                endpoint,
                body,
                _request_headers(user_agent, json_body=True),
            )
            relative = f"raw/clob/event_{event_id}/batch_{batch_index:04d}.json.gz"
            request_record = {
                "body": body_object,
                "body_sha256": _sha256(body),
                "method": "POST",
                "url": endpoint,
            }
            page = _load_json(payload, endpoint)
            if not isinstance(page, Mapping) or not isinstance(page.get("history"), Mapping):
                raise CollectionError("batch-prices-history response must contain a history object")
            history = page["history"]
            source_history_sha256 = _sha256(payload)
            batch_series: dict[str, HistorySeries] = {}
            for token in token_batch:
                raw_points = history.get(token, [])
                if not isinstance(raw_points, list):
                    raise CollectionError(f"history for token {token} is not an array")
                points: list[PricePoint] = []
                for point_index, raw_point in enumerate(raw_points):
                    if not isinstance(raw_point, Mapping):
                        raise CollectionError(
                            f"history point {point_index} for token {token} is not an object"
                        )
                    raw_t = raw_point.get("t")
                    raw_p = raw_point.get("p")
                    if isinstance(raw_t, bool) or isinstance(raw_p, bool):
                        raise CollectionError(f"history point for token {token} uses boolean data")
                    try:
                        timestamp = int(raw_t)
                        probability = float(raw_p)
                    except (TypeError, ValueError) as exc:
                        raise CollectionError(
                            f"history point for token {token} has invalid t or p"
                        ) from exc
                    if raw_t != timestamp and str(raw_t) != str(timestamp):
                        raise CollectionError(
                            f"history timestamp for token {token} is not integral"
                        )
                    try:
                        points.append(
                            PricePoint(datetime.fromtimestamp(timestamp, tz=UTC), probability)
                        )
                    except (TypeError, ValueError, OSError) as exc:
                        raise CollectionError(
                            f"history point for token {token} is out of range"
                        ) from exc
                batch_series[token] = HistorySeries(tuple(points), source_history_sha256)
            batches.append((relative, payload, request_record, batch_series))
        return event_id, batches

    event_items = [
        (event_id, by_event[event_id]) for event_id in sorted(by_event, key=_identifier_sort_key)
    ]
    if workers == 1:
        fetched = map(fetch_event, event_items)
    else:
        executor = ThreadPoolExecutor(max_workers=workers)
        fetched = executor.map(fetch_event, event_items)

    result: dict[str, HistorySeries] = {}
    try:
        for _, batches in fetched:
            for relative, payload, request_record, batch_series in batches:
                archive.add(
                    relative,
                    payload,
                    source="clob.batch_prices_history",
                    request=request_record,
                )
                overlap = result.keys() & batch_series.keys()
                if overlap:
                    raise CollectionError(
                        f"history token fetched more than once: {sorted(overlap)!r}"
                    )
                result.update(batch_series)
    finally:
        if workers != 1:
            executor.shutdown(wait=True)
    return result


def select_cutoff_rows(
    contracts: Sequence[Mapping[str, Any]],
    histories: Mapping[str, HistorySeries],
    protocol: Mapping[str, Any],
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    """Select the latest non-future point at each 30/15-minute protocol cutoff."""

    forecast = protocol["forecast"]
    universe = protocol["universe"]
    horizons = [
        int(forecast["primary_horizon_seconds"]),
        *[int(item) for item in forecast["secondary_horizons_seconds"]],
    ]
    max_staleness = timedelta(seconds=int(forecast["max_staleness_seconds"]))
    minimum = int(universe["minimum_thresholds_per_event"])
    candidates: dict[tuple[str, int], list[dict[str, Any]]] = {}

    for contract in contracts:
        event_end = contract["event_end"]
        assert isinstance(event_end, datetime)
        token = str(contract["yes_token_id"])
        series = histories.get(token)
        points: Sequence[PricePoint] = series.points if series is not None else ()
        for horizon in horizons:
            cutoff = event_end - timedelta(seconds=horizon)
            decision = {
                "cutoff": format_timestamp(cutoff),
                "event_id": contract["event_id"],
                "horizon_seconds": horizon,
                "market_id": contract["market_id"],
                "reason": None,
                "status": "selected",
                "yes_token_id": token,
            }
            try:
                point = latest_at_or_before(
                    points,
                    cutoff,
                    max_staleness=max_staleness,
                )
            except NoEligibleHistoryPointError:
                decision["reason"] = "no_point_at_or_before_cutoff"
                decision["status"] = "rejected"
            except StaleHistoryPointError:
                decision["reason"] = "latest_point_exceeds_max_staleness"
                decision["status"] = "rejected"
            except ConflictingHistoryPointError:
                decision["reason"] = "conflicting_probabilities_at_one_timestamp"
                decision["status"] = "rejected"
            if decision["status"] == "rejected":
                audit["history_decisions"].append(decision)
                continue

            staleness = int((cutoff - point.timestamp).total_seconds())
            decision["point_timestamp"] = format_timestamp(point.timestamp)
            decision["staleness_seconds"] = staleness
            audit["history_decisions"].append(decision)
            row = {
                "asset": contract["asset"],
                "condition_id": contract["condition_id"],
                "cutoff_time": format_timestamp(cutoff),
                "event_id": contract["event_id"],
                "event_title": contract["event_title"],
                "gamma_candidate_label": contract["gamma_candidate_label"],
                "gamma_candidate_label_onchain_verified": False,
                "gamma_candidate_label_source": LABEL_SOURCE,
                "horizon_seconds": horizon,
                "market_id": contract["market_id"],
                "no_token": contract["no_token_id"],
                "question": contract["question"],
                "reference_probability": point.probability,
                "reference_timestamp": format_timestamp(point.timestamp),
                "scheduled_time": format_timestamp(event_end),
                "source_event_sha256": contract["source_event_sha256"],
                "source_history_sha256": series.source_sha256 if series is not None else None,
                "split": contract["split"],
                "staleness_seconds": staleness,
                "threshold": contract["threshold"],
                "utc_day": event_end.date().isoformat(),
                "yes_token": token,
            }
            candidates.setdefault((str(contract["event_id"]), horizon), []).append(row)

    selected: list[dict[str, Any]] = []
    event_ids = sorted(
        {str(contract["event_id"]) for contract in contracts},
        key=_identifier_sort_key,
    )
    for event_id in event_ids:
        total = sum(str(item["event_id"]) == event_id for item in contracts)
        for horizon in horizons:
            rows = candidates.get((event_id, horizon), [])
            accepted = len(rows) == total and total >= minimum
            audit["curve_decisions"].append(
                {
                    "eligible_history_contract_count": len(rows),
                    "event_id": event_id,
                    "horizon_seconds": horizon,
                    "minimum_required": minimum,
                    "reason": None if accepted else "incomplete_fresh_curve",
                    "status": "selected" if accepted else "rejected",
                    "strict_market_contract_count": total,
                }
            )
            if accepted:
                for row in rows:
                    row["event_horizon_contract_count"] = len(rows)
                selected.extend(rows)

    split_order = {name: index for index, name in enumerate(protocol["splits"])}
    selected.sort(
        key=lambda row: (
            split_order[row["split"]],
            row["scheduled_time"],
            _identifier_sort_key(str(row["event_id"])),
            -int(row["horizon_seconds"]),
            float(row["threshold"]),
            row["market_id"],
        )
    )
    return selected


CSV_FIELDS = [
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
]


def _candidate_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return gzip.compress(stream.getvalue().encode("utf-8"), compresslevel=9, mtime=0)


def _artifact_record(output_dir: Path, path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "bytes": len(payload),
        "path": path.relative_to(output_dir).as_posix(),
        "sha256": _sha256(payload),
    }


def _pre_chain_coverage(
    audit: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
) -> dict[str, dict[str, dict[str, int | float]]]:
    """Measure history coverage against the strict pre-history event universe."""

    selected_events = [item for item in audit["event_decisions"] if item["status"] == "selected"]
    horizons = [
        int(protocol["forecast"]["primary_horizon_seconds"]),
        *map(int, protocol["forecast"]["secondary_horizons_seconds"]),
    ]
    result: dict[str, dict[str, dict[str, int | float]]] = {}
    for split in protocol["splits"]:
        expected = [item for item in selected_events if item["split"] == split]
        expected_events = len(expected)
        expected_rows = sum(int(item["eligible_market_count"]) for item in expected)
        result[split] = {}
        for horizon in horizons:
            retained = [
                row
                for row in rows
                if row["split"] == split and int(row["horizon_seconds"]) == horizon
            ]
            retained_events = len({str(row["event_id"]) for row in retained})
            retained_rows = len(retained)
            result[split][str(horizon)] = {
                "expected_events": expected_events,
                "history_retained_events": retained_events,
                "expected_rows": expected_rows,
                "history_retained_rows": retained_rows,
                "event_history_coverage": retained_events / expected_events
                if expected_events
                else 0.0,
                "row_history_coverage": retained_rows / expected_rows if expected_rows else 0.0,
            }
    return result


def run_collection(
    config_path: Path,
    output_dir: Path,
    *,
    run_source_commit: str,
    transport: Transport | None = None,
    timeout: float = 60.0,
    workers: int = 1,
    user_agent: str = "event-factor-bench/0.1 (+frozen-protocol-collector)",
) -> dict[str, Any]:
    """Run the frozen-protocol collector and return its candidate-row summary."""

    config_path = Path(config_path)
    output_dir = Path(output_dir)
    _require_source_commit(run_source_commit)
    config_bytes = config_path.read_bytes()
    protocol = _load_json(config_bytes, str(config_path))
    if not isinstance(protocol, Mapping):
        raise CollectionError("protocol config must contain a JSON object")
    _validate_protocol(protocol)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise CollectionError(f"output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    active_transport = transport or make_http_transport(timeout=timeout)
    archive = RawArchive(output_dir)
    audit: dict[str, Any] = {
        "benchmark": protocol.get("benchmark"),
        "curve_decisions": [],
        "event_decisions": [],
        "history_decisions": [],
        "label_policy": {
            "caveat": LABEL_CAVEAT,
            "field": "gamma_candidate_label",
            "onchain_verified": False,
            "source": LABEL_SOURCE,
        },
        "market_decisions": [],
        "protocol_version": protocol.get("version"),
    }

    events, discovery_counts = collect_gamma_events(
        protocol,
        transport=active_transport,
        archive=archive,
        user_agent=user_agent,
    )
    contracts = select_event_contracts(events, protocol, audit)
    histories = collect_histories(
        contracts,
        protocol,
        transport=active_transport,
        archive=archive,
        user_agent=user_agent,
        workers=workers,
    )
    rows = select_cutoff_rows(contracts, histories, protocol, audit)
    coverage_pre_chain = _pre_chain_coverage(audit, rows, protocol)
    audit["counts"] = {
        **discovery_counts,
        "candidate_rows": len(rows),
        "curves_selected": sum(item["status"] == "selected" for item in audit["curve_decisions"]),
        "events_selected": sum(item["status"] == "selected" for item in audit["event_decisions"]),
        "strict_markets_selected_before_history": len(contracts),
    }

    protocol_copy = output_dir / "protocol_v0.1.json"
    candidate_path = output_dir / "candidate_rows_v0.1.csv.gz"
    audit_path = output_dir / "selection_audit.json"
    protocol_copy.write_bytes(config_bytes)
    candidate_path.write_bytes(_candidate_csv(rows))
    audit_path.write_bytes(_json_bytes(audit))
    manifest = {
        "schema_version": "event-factor-bench-collector-v1",
        "artifacts": [
            _artifact_record(output_dir, protocol_copy),
            _artifact_record(output_dir, candidate_path),
            _artifact_record(output_dir, audit_path),
        ],
        "benchmark": protocol.get("benchmark"),
        "generated_at": format_timestamp(datetime.now(tz=UTC)),
        "label_caveat": LABEL_CAVEAT,
        "protocol_sha256": _sha256(config_bytes),
        "protocol_version": protocol.get("version"),
        "run_source_commit": run_source_commit,
        "coverage_pre_chain": coverage_pre_chain,
        "raw_responses": archive.entries,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return {
        "candidate_rows": len(rows),
        "manifest_path": str(manifest_path),
        "output_dir": str(output_dir),
        "raw_responses": len(archive.entries),
        "selected_events": audit["counts"]["events_selected"],
    }


def _parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=project_root / "configs" / "protocol_v0.1.json",
        help="frozen protocol JSON (default: configs/protocol_v0.1.json)",
    )
    parser.add_argument("--output", type=Path, required=True, help="new or empty output directory")
    parser.add_argument("--run-source-commit", required=True)
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--workers",
        type=int,
        default=24,
        help="parallel CLOB history requests (default: 24)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    summary = run_collection(
        args.config,
        args.output,
        run_source_commit=args.run_source_commit,
        timeout=args.timeout,
        workers=args.workers,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
