from __future__ import annotations

import csv
import gzip
import hashlib
import http.client
import importlib.util
import io
import json
import socket
import ssl
import sys
import urllib.parse
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from event_factor_bench.history import PricePoint

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "collect_frozen.py"
SPEC = importlib.util.spec_from_file_location("collect_frozen_for_tests", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
collection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collection
SPEC.loader.exec_module(collection)


@pytest.mark.parametrize(
    "payload",
    [b'{"id":1,"id":2}', b'{"value":NaN}', b'{"value":Infinity}'],
)
def test_collection_rejects_ambiguous_or_nonstandard_json(payload: bytes) -> None:
    with pytest.raises(collection.CollectionError, match="invalid JSON"):
        collection._load_json(payload, "fixture")


def _protocol(*, minimum: int = 2) -> dict[str, Any]:
    return {
        "benchmark": "EventFactorBench",
        "version": "0.1.0-test",
        "retrieval": {
            "gamma_endpoint": "https://gamma.test/events/keyset",
            "clob_endpoint": "https://clob.test/batch-prices-history",
            "discovery_start_inclusive": "2026-06-01T00:00:00Z",
            "discovery_end_exclusive": "2026-06-03T00:00:00Z",
            "gamma_title_search": "above",
            "history_fidelity_minutes": 1,
            "history_window_seconds": 2700,
        },
        "universe": {
            "event_title_regex": (
                r"^(Bitcoin|Ethereum) above ___ on .+, (?:1[0-2]|[1-9])(?:AM|PM) ET\?$"
            ),
            "assets": ["Bitcoin", "Ethereum"],
            "minimum_thresholds_per_event": minimum,
            "required_outcomes": ["Yes", "No"],
            "required_resolution_status": "resolved",
            "accepted_gamma_candidate_yes_probabilities": [0.0, 1.0],
        },
        "splits": {
            "development": {
                "start_inclusive": "2026-06-01T00:00:00Z",
                "end_exclusive": "2026-06-03T00:00:00Z",
            }
        },
        "forecast": {
            "primary_horizon_seconds": 1800,
            "secondary_horizons_seconds": [900],
            "max_staleness_seconds": 120,
        },
    }


def _market(
    market_id: int,
    threshold: str,
    prices: list[str],
    yes_token: int,
    *,
    question: str | None = None,
) -> dict[str, Any]:
    return {
        "id": str(market_id),
        "conditionId": f"0xcondition{market_id}",
        "question": question or f"Bitcoin above {threshold} on June 1, 8PM ET?",
        "groupItemTitle": threshold,
        "endDate": "2026-06-02T00:00:00Z",
        "closed": True,
        "enableOrderBook": True,
        "umaResolutionStatus": "resolved",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(prices),
        "clobTokenIds": json.dumps([str(yes_token), str(yes_token + 1000)]),
    }


def _event() -> dict[str, Any]:
    return {
        "id": "5001",
        "title": "Bitcoin above ___ on June 1, 8PM ET?",
        "endDate": "2026-06-02T00:00:00Z",
        "closed": True,
        "markets": [
            _market(7001, "100", ["1", "0"], 101),
            _market(7002, "200", ["0", "1"], 102),
        ],
    }


def _rejected_event() -> dict[str, Any]:
    return {
        "id": "5002",
        "title": "Bitcoin above ___ on June 1, 8PM ET?",
        "endDate": "2026-06-02T00:00:00Z",
        "closed": True,
        "markets": [
            _market(7003, "300", ["0.5", "0.5"], 103),
            _market(
                7004,
                "400",
                ["1", "0"],
                104,
                question="Bitcoin above 401 on June 1, 8PM ET?",
            ),
        ],
    }


class FrozenFixtureTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.gamma_payloads: list[bytes] = []
        self.history_payload: bytes | None = None

    def __call__(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> bytes:
        self.calls.append((method, url, body, dict(headers)))
        if method == "GET":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            assert query["title_search"] == ["above"]
            assert query["closed"] == ["true"]
            events = [deepcopy(_event()), deepcopy(_rejected_event())]
            for event in events:
                event["series"] = [
                    {
                        "updatedAt": query["end_date_min"][0],
                        "liquidity": 100
                        if query["end_date_min"] == ["2026-06-01T00:00:00Z"]
                        else 99,
                    }
                ]
            payload = json.dumps(
                {"events": events},
                sort_keys=query["end_date_min"] == ["2026-06-02T00:00:00Z"],
            ).encode()
            self.gamma_payloads.append(payload)
            return payload

        assert method == "POST"
        assert body is not None
        request = json.loads(body)
        assert set(request) == {"markets", "start_ts", "end_ts", "fidelity"}
        assert "interval" not in request
        end_ts = request["end_ts"]
        history = {
            "101": [
                {"t": end_ts - 1800 - 60, "p": "0.40"},
                {"t": end_ts - 1800 + 1, "p": "0.95"},
                {"t": end_ts - 900 - 120, "p": "0.30"},
                {"t": end_ts - 900 + 1, "p": "0.99"},
            ],
            "102": [
                {"t": end_ts - 1800, "p": 0.6},
                {"t": end_ts - 900, "p": 0.2},
            ],
        }
        self.history_payload = json.dumps({"history": history}, sort_keys=True).encode()
        return self.history_payload


def _read_candidate_rows(path: Path) -> list[dict[str, str]]:
    text = gzip.decompress(path.read_bytes()).decode()
    return list(csv.DictReader(io.StringIO(text)))


def test_frozen_collection_deduplicates_inclusive_windows_and_audits_sources(
    tmp_path: Path,
) -> None:
    protocol = _protocol()
    config_path = tmp_path / "protocol.json"
    config_path.write_text(json.dumps(protocol))
    output_dir = tmp_path / "candidate"
    transport = FrozenFixtureTransport()

    summary = collection.run_collection(
        config_path,
        output_dir,
        run_source_commit="a" * 40,
        transport=transport,
    )

    assert summary["selected_events"] == 1
    assert summary["candidate_rows"] == 4
    get_calls = [call for call in transport.calls if call[0] == "GET"]
    assert len(get_calls) == 2
    assert {call[3]["User-Agent"] for call in transport.calls} == {
        "event-factor-bench/0.1.1 (+frozen-protocol-collector)"
    }
    queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(call[1]).query) for call in get_calls]
    assert queries[0]["end_date_max"] == ["2026-06-02T00:00:00Z"]
    assert queries[1]["end_date_min"] == ["2026-06-02T00:00:00Z"]

    candidate_path = output_dir / "candidate_rows_v0.1.csv.gz"
    assert candidate_path.exists()
    assert not (output_dir / "compact.csv.gz").exists()
    rows = _read_candidate_rows(candidate_path)
    required = {
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
    }
    assert required <= rows[0].keys()
    assert "label" not in rows[0]
    assert {row["gamma_candidate_label"] for row in rows} == {"0.0", "1.0"}
    assert {row["gamma_candidate_label_onchain_verified"] for row in rows} == {"False"}
    assert {row["source_event_sha256"] for row in rows} == {
        hashlib.sha256(transport.gamma_payloads[0]).hexdigest()
    }
    assert transport.history_payload is not None
    assert {row["source_history_sha256"] for row in rows} == {
        hashlib.sha256(transport.history_payload).hexdigest()
    }
    token_101 = [row for row in rows if row["yes_token"] == "101"]
    assert {row["staleness_seconds"] for row in token_101} == {"60", "120"}

    audit = json.loads((output_dir / "selection_audit.json").read_text())
    assert audit["counts"]["gamma_inclusive_boundary_duplicates"] == 2
    assert audit["counts"]["gamma_duplicates_with_unused_field_changes"] == 2
    assert audit["label_policy"]["field"] == "gamma_candidate_label"
    assert audit["label_policy"]["onchain_verified"] is False
    reasons = {reason for decision in audit["market_decisions"] for reason in decision["reasons"]}
    assert "non_terminal_gamma_outcome_prices" in reasons
    assert "question_does_not_match_event_template" in reasons

    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == "event-factor-bench-collector-v1"
    assert manifest["run_source_commit"] == "a" * 40
    assert manifest["collector_user_agent"] == (
        "event-factor-bench/0.1.1 (+frozen-protocol-collector)"
    )
    assert manifest["http_retry_policy"] == {
        "max_attempts": 10,
        "backoff_seconds": [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 60.0, 60.0, 60.0],
        "timeout_seconds": 60.0,
        "retryable_http_statuses": [408, 425, 429, 500, 502, 503, 504],
        "retryable_exception_classes": [
            "TimeoutError",
            "ConnectionError",
            "http.client.IncompleteRead",
            "ssl.SSLEOFError",
            "socket.gaierror(EAI_AGAIN)",
        ],
        "certificate_errors_retryable": False,
        "response_archive_scope": "successful_response_bytes_only",
    }
    assert "not a canonical label" in manifest["label_caveat"]
    assert {item["path"] for item in manifest["artifacts"]} == {
        "protocol_v0.1.json",
        "candidate_rows_v0.1.csv.gz",
        "selection_audit.json",
    }
    coverage = manifest["coverage_pre_chain"]["development"]
    assert coverage["1800"] == {
        "expected_events": 1,
        "history_retained_events": 1,
        "expected_rows": 2,
        "history_retained_rows": 2,
        "event_history_coverage": 1.0,
        "row_history_coverage": 1.0,
    }
    assert len(manifest["raw_responses"]) == 3
    for entry in manifest["raw_responses"]:
        compressed = (output_dir / entry["path"]).read_bytes()
        content = gzip.decompress(compressed)
        assert entry["archived_at_utc"].endswith("Z")
        assert hashlib.sha256(compressed).hexdigest() == entry["gzip_sha256"]
        assert hashlib.sha256(content).hexdigest() == entry["content_sha256"]


def test_cutoff_selection_accepts_120_seconds_and_rejects_121_seconds() -> None:
    event_end = datetime(2026, 6, 2, tzinfo=UTC)
    contract = {
        "asset": "Bitcoin",
        "condition_id": "0x1",
        "event_end": event_end,
        "event_id": "1",
        "event_title": "Bitcoin above ___ on June 1, 8PM ET?",
        "gamma_candidate_label": 1.0,
        "market_id": "2",
        "no_token_id": "12",
        "question": "Bitcoin above 100 on June 1, 8PM ET?",
        "source_event_sha256": "e" * 64,
        "split": "development",
        "threshold": 100.0,
        "yes_token_id": "11",
    }
    histories = {
        "11": collection.HistorySeries(
            (
                PricePoint(event_end - timedelta(seconds=1800 + 121), 0.4),
                PricePoint(event_end - timedelta(seconds=900 + 120), 0.3),
                PricePoint(event_end - timedelta(seconds=899), 0.9),
            ),
            "h" * 64,
        )
    }
    protocol = _protocol(minimum=1)
    audit = {"history_decisions": [], "curve_decisions": []}

    rows = collection.select_cutoff_rows([contract], histories, protocol, audit)

    assert len(rows) == 1
    assert rows[0]["horizon_seconds"] == 900
    assert rows[0]["staleness_seconds"] == 120
    assert rows[0]["reference_probability"] == pytest.approx(0.3)
    rejected = [item for item in audit["history_decisions"] if item["status"] == "rejected"]
    assert rejected == [
        {
            "cutoff": "2026-06-01T23:30:00Z",
            "event_id": "1",
            "horizon_seconds": 1800,
            "market_id": "2",
            "reason": "latest_point_exceeds_max_staleness",
            "status": "rejected",
            "yes_token_id": "11",
        }
    ]


def test_batch_history_uses_at_most_20_tokens_and_omits_interval(tmp_path: Path) -> None:
    event_end = datetime(2026, 6, 2, tzinfo=UTC)
    contracts = [
        {"event_id": "9", "event_end": event_end, "yes_token_id": str(index)}
        for index in range(1, 22)
    ]
    bodies: list[dict[str, Any]] = []

    def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> bytes:
        assert method == "POST"
        assert url == "https://clob.test/batch-prices-history"
        assert headers["Content-Type"] == "application/json"
        assert body is not None
        parsed = json.loads(body)
        bodies.append(parsed)
        return json.dumps({"history": {token: [] for token in parsed["markets"]}}).encode()

    archive = collection.RawArchive(tmp_path)
    histories = collection.collect_histories(
        contracts,
        _protocol(),
        transport=transport,
        archive=archive,
        user_agent="fixture/1",
    )

    assert [len(body["markets"]) for body in bodies] == [20, 1]
    assert all(set(body) == {"markets", "start_ts", "end_ts", "fidelity"} for body in bodies)
    assert all("interval" not in body for body in bodies)
    assert len(histories) == 21
    assert len(archive.entries) == 2


class _CollectionHttpResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        read_failure: str | None = None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.read_failure = read_failure

    def __enter__(self) -> _CollectionHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        if self.read_failure == "incomplete_read":
            raise http.client.IncompleteRead(b'{"events":', 64)
        return self.payload


def _collection_transient_error(kind: str) -> BaseException:
    if kind == "remote_disconnected":
        return http.client.RemoteDisconnected("peer closed without a response")
    if kind == "ssl_eof_direct":
        return ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
    if kind == "ssl_eof_wrapped":
        return collection.urllib.error.URLError(
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        )
    if kind.startswith("eai_again"):
        error = socket.gaierror(socket.EAI_AGAIN, "temporary name resolution failure")
        return collection.urllib.error.URLError(error) if kind.endswith("wrapped") else error
    raise AssertionError(f"unknown transient failure kind: {kind}")


@pytest.mark.parametrize(
    "failure_kind",
    [
        "remote_disconnected",
        "ssl_eof_direct",
        "ssl_eof_wrapped",
        "incomplete_read",
        "eai_again_direct",
        "eai_again_wrapped",
    ],
)
def test_collection_http_transport_recovers_from_whitelisted_network_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    attempts = 0
    delays: list[float] = []
    payload = b'{"events":[]}'

    def urlopen(_request: object, *, timeout: float) -> _CollectionHttpResponse:
        nonlocal attempts
        assert timeout == 3.0
        attempts += 1
        if attempts < 3:
            if failure_kind == "incomplete_read":
                return _CollectionHttpResponse(payload, read_failure=failure_kind)
            raise _collection_transient_error(failure_kind)
        return _CollectionHttpResponse(payload)

    monkeypatch.setattr(collection.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(collection.time, "sleep", delays.append)
    transport = collection.make_http_transport(timeout=3.0)

    assert (
        transport("GET", "https://gamma.invalid", None, {"Accept": "application/json"}) == payload
    )
    assert attempts == 3
    assert delays == list(collection.HTTP_BACKOFF_SECONDS[:2])


@pytest.mark.parametrize(
    "failure_kind",
    [
        "remote_disconnected",
        "ssl_eof_direct",
        "ssl_eof_wrapped",
        "incomplete_read",
        "eai_again_direct",
        "eai_again_wrapped",
    ],
)
def test_collection_http_transport_exhausts_whitelisted_network_failures_exactly(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    attempts = 0
    delays: list[float] = []

    def urlopen(_request: object, *, timeout: float) -> _CollectionHttpResponse:
        nonlocal attempts
        assert timeout == 3.0
        attempts += 1
        if failure_kind == "incomplete_read":
            return _CollectionHttpResponse(b"unused", read_failure=failure_kind)
        raise _collection_transient_error(failure_kind)

    monkeypatch.setattr(collection.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(collection.time, "sleep", delays.append)
    transport = collection.make_http_transport(timeout=3.0)

    with pytest.raises(collection.CollectionError, match="after 10 attempts"):
        transport("GET", "https://gamma.invalid", None, {"Accept": "application/json"})
    assert attempts == collection.HTTP_MAX_ATTEMPTS
    assert delays == list(collection.HTTP_BACKOFF_SECONDS)


@pytest.mark.parametrize("delivery", ["returned", "raised"])
@pytest.mark.parametrize("status", [429, 503])
def test_collection_http_transport_retries_whitelisted_http_statuses(
    monkeypatch: pytest.MonkeyPatch,
    delivery: str,
    status: int,
) -> None:
    attempts = 0
    delays: list[float] = []
    payload = b'{"events":[]}'

    def urlopen(_request: object, *, timeout: float) -> _CollectionHttpResponse:
        nonlocal attempts
        assert timeout == 3.0
        attempts += 1
        if attempts == 3:
            return _CollectionHttpResponse(payload)
        if delivery == "raised":
            raise collection.urllib.error.HTTPError(
                "https://gamma.invalid", status, "retryable", {}, None
            )
        return _CollectionHttpResponse(payload, status=status)

    monkeypatch.setattr(collection.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(collection.time, "sleep", delays.append)
    transport = collection.make_http_transport(timeout=3.0)

    assert transport("GET", "https://gamma.invalid", None, {}) == payload
    assert attempts == 3
    assert delays == list(collection.HTTP_BACKOFF_SECONDS[:2])


@pytest.mark.parametrize("delivery", ["returned", "raised"])
@pytest.mark.parametrize("status", [429, 503])
def test_collection_http_transport_exhausts_whitelisted_http_statuses_exactly(
    monkeypatch: pytest.MonkeyPatch,
    delivery: str,
    status: int,
) -> None:
    attempts = 0
    delays: list[float] = []

    def urlopen(_request: object, *, timeout: float) -> _CollectionHttpResponse:
        nonlocal attempts
        assert timeout == 3.0
        attempts += 1
        if delivery == "raised":
            raise collection.urllib.error.HTTPError(
                "https://gamma.invalid", status, "retryable", {}, None
            )
        return _CollectionHttpResponse(b"unused", status=status)

    monkeypatch.setattr(collection.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(collection.time, "sleep", delays.append)
    transport = collection.make_http_transport(timeout=3.0)

    with pytest.raises(collection.CollectionError, match=rf"HTTP status {status}.*10 attempts"):
        transport("GET", "https://gamma.invalid", None, {})
    assert attempts == collection.HTTP_MAX_ATTEMPTS
    assert delays == list(collection.HTTP_BACKOFF_SECONDS)


@pytest.mark.parametrize("delivery", ["returned", "raised"])
@pytest.mark.parametrize("status", [401, 418])
def test_collection_http_transport_rejects_nonretryable_http_statuses_immediately(
    monkeypatch: pytest.MonkeyPatch,
    delivery: str,
    status: int,
) -> None:
    attempts = 0
    delays: list[float] = []

    def urlopen(_request: object, *, timeout: float) -> _CollectionHttpResponse:
        nonlocal attempts
        assert timeout == 3.0
        attempts += 1
        if delivery == "raised":
            raise collection.urllib.error.HTTPError(
                "https://gamma.invalid", status, "permanent", {}, None
            )
        return _CollectionHttpResponse(b"unused", status=status)

    monkeypatch.setattr(collection.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(collection.time, "sleep", delays.append)
    transport = collection.make_http_transport(timeout=3.0)

    with pytest.raises(collection.CollectionError, match="not retryable"):
        transport("GET", "https://gamma.invalid", None, {})
    assert attempts == 1
    assert delays == []


def _collection_permanent_error(kind: str) -> BaseException:
    if kind.startswith("certificate"):
        error: BaseException = ssl.SSLCertVerificationError(1, "certificate verify failed")
    elif kind.startswith("eai_noname"):
        error = socket.gaierror(socket.EAI_NONAME, "name or service not known")
    else:
        raise AssertionError(f"unknown permanent failure kind: {kind}")
    return collection.urllib.error.URLError(error) if kind.endswith("wrapped") else error


@pytest.mark.parametrize(
    "failure_kind",
    ["certificate_direct", "certificate_wrapped", "eai_noname_direct", "eai_noname_wrapped"],
)
def test_collection_http_transport_fails_fast_for_certificate_and_permanent_dns_errors(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    attempts = 0
    delays: list[float] = []

    def urlopen(_request: object, *, timeout: float) -> _CollectionHttpResponse:
        nonlocal attempts
        assert timeout == 3.0
        attempts += 1
        raise _collection_permanent_error(failure_kind)

    monkeypatch.setattr(collection.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(collection.time, "sleep", delays.append)
    transport = collection.make_http_transport(timeout=3.0)

    with pytest.raises(collection.CollectionError, match="non-retryable network error"):
        transport("GET", "https://gamma.invalid", None, {})
    assert attempts == 1
    assert delays == []


def test_collection_http_retry_budget_is_exact() -> None:
    assert collection.HTTP_MAX_ATTEMPTS == 10
    assert collection.HTTP_BACKOFF_SECONDS == (
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        30.0,
        60.0,
        60.0,
        60.0,
    )
    assert {408, 425, 429, 500, 502, 503, 504} == collection.RETRYABLE_HTTP_STATUSES
    assert len(collection.HTTP_BACKOFF_SECONDS) == collection.HTTP_MAX_ATTEMPTS - 1


def _write_protocol(path: Path) -> None:
    path.write_text(json.dumps(_protocol()))


def test_run_collection_commits_one_complete_sibling_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "protocol.json"
    output_dir = tmp_path / "candidate"
    _write_protocol(config_path)
    real_replace = collection.os.replace
    replacements: list[tuple[Path, Path]] = []

    def replace(source: Path, target: Path) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(collection.os, "replace", replace)
    summary = collection.run_collection(
        config_path,
        output_dir,
        run_source_commit="a" * 40,
        transport=FrozenFixtureTransport(),
    )

    assert Path(summary["output_dir"]) == output_dir
    assert Path(summary["manifest_path"]) == output_dir / "manifest.json"
    assert len(replacements) == 1
    staged, committed = replacements[0]
    assert staged.parent == output_dir.parent
    assert committed == output_dir
    assert not staged.exists()
    assert (output_dir / "manifest.json").exists()


def test_run_collection_commit_failure_leaves_no_output_or_staging_fragments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "protocol.json"
    output_dir = tmp_path / "candidate"
    _write_protocol(config_path)

    def replace(_source: Path, _target: Path) -> None:
        raise OSError("injected directory commit failure")

    monkeypatch.setattr(collection.os, "replace", replace)

    with pytest.raises(OSError, match="injected directory commit failure"):
        collection.run_collection(
            config_path,
            output_dir,
            run_source_commit="a" * 40,
            transport=FrozenFixtureTransport(),
        )
    assert not output_dir.exists()
    assert list(tmp_path.iterdir()) == [config_path]


def test_run_collection_transport_failure_leaves_no_output_or_staging_fragments(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "protocol.json"
    output_dir = tmp_path / "candidate"
    _write_protocol(config_path)
    fixture = FrozenFixtureTransport()

    def transport(
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> bytes:
        if method == "POST":
            raise collection.CollectionError("injected history request failure")
        return fixture(method, url, body, headers)

    with pytest.raises(collection.CollectionError, match="injected history request failure"):
        collection.run_collection(
            config_path,
            output_dir,
            run_source_commit="a" * 40,
            transport=transport,
        )
    assert not output_dir.exists()
    assert list(tmp_path.iterdir()) == [config_path]
