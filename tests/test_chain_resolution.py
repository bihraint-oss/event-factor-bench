from __future__ import annotations

import csv
import gzip
import hashlib
import importlib.util
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from event_factor_bench.evaluation import read_evidence

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_chain.py"
SPEC = importlib.util.spec_from_file_location("event_factor_verify_chain", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
chain = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = chain
SPEC.loader.exec_module(chain)

DAY = int(datetime(2026, 6, 1, tzinfo=UTC).timestamp())
ADDRESS = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
TOPIC0 = "0x" + "ab" * 32


def condition(number: int) -> str:
    return f"0x{number:064x}"


def block_hash(number: int) -> str:
    return f"0x{number + 100:064x}"


def transaction_hash(number: int) -> str:
    return f"0x{number + 200:064x}"


def abi_resolution(payout: tuple[int, ...], *, offset: int = 64) -> str:
    words = (len(payout), offset, len(payout), *payout)
    return "0x" + "".join(value.to_bytes(32, "big").hex() for value in words)


def resolution_log(
    condition_id: str,
    payout: tuple[int, ...],
    *,
    block_number: int = 2,
    log_index: int = 0,
    tx_number: int = 1,
) -> dict[str, Any]:
    return {
        "address": ADDRESS,
        "topics": [TOPIC0, condition_id, "0x" + "00" * 32, "0x" + "00" * 32],
        "data": abi_resolution(payout),
        "blockNumber": hex(block_number),
        "blockHash": block_hash(block_number),
        "transactionHash": transaction_hash(tx_number),
        "transactionIndex": "0x0",
        "logIndex": hex(log_index),
        "removed": False,
    }


class FakeTransport:
    def __init__(
        self,
        logs: list[dict[str, Any]],
        *,
        timestamps: list[int] | None = None,
        chain_id: int = 137,
        fail_method: str | None = None,
    ) -> None:
        self.logs = logs
        self.timestamps = timestamps or [
            DAY - 10,
            DAY,
            DAY + 90 * 60,
            DAY + 2 * 3600,
            DAY + 12 * 3600,
            DAY + 24 * 3600,
            DAY + 26 * 3600,
        ]
        self.chain_id = chain_id
        self.fail_method = fail_method
        self.calls: list[dict[str, Any]] = []

    def __call__(self, _url: str, body: bytes, _timeout: float) -> bytes:
        request = json.loads(body)
        self.calls.append(request)
        method = request["method"]
        if method == self.fail_method:
            return self._response(request, error={"code": -32001, "message": "semantic failure"})
        if method == "eth_chainId":
            return self._response(request, result=hex(self.chain_id))
        if method == "eth_blockNumber":
            return self._response(request, result=hex(len(self.timestamps) - 1))
        if method == "eth_getBlockByNumber":
            number = int(request["params"][0], 16)
            result = {
                "number": hex(number),
                "hash": block_hash(number),
                "timestamp": hex(self.timestamps[number]),
            }
            return self._response(request, result=result)
        if method == "eth_getLogs":
            query = request["params"][0]
            first = int(query["fromBlock"], 16)
            last = int(query["toBlock"], 16)
            requested = set(query["topics"][1])
            matches = [
                item
                for item in self.logs
                if first <= int(item["blockNumber"], 16) <= last and item["topics"][1] in requested
            ]
            return self._response(request, result=matches)
        raise AssertionError(f"unexpected method {method}")

    @staticmethod
    def _response(request: dict[str, Any], *, result: Any = None, error: Any = None) -> bytes:
        envelope = {"jsonrpc": "2.0", "id": request["id"]}
        if error is None:
            envelope["result"] = result
        else:
            envelope["error"] = error
        return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


def candidate_row(
    *,
    event_id: str,
    condition_id: str,
    label: int,
    horizon: int = 1800,
    market_id: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "market_id": market_id or condition_id[-4:],
        "condition_id": condition_id,
        "asset": "Bitcoin",
        "scheduled_time": "2026-06-01T01:00:00Z",
        "utc_day": "2026-06-01",
        "split": "development",
        "horizon_seconds": horizon,
        "cutoff_time": "2026-06-01T00:30:00Z",
        "threshold": "70000",
        "gamma_candidate_label": float(label),
        "yes_token": str(int(condition_id, 16) + 10_000),
        "reference_probability": "0.5",
        "reference_timestamp": "2026-06-01T00:29:10Z",
        "staleness_seconds": "50",
        "source_event_sha256": "e" * 64,
        "source_history_sha256": "f" * 64,
        "event_horizon_contract_count": 2,
        "event_title": "third-party free text must not be copied",
        "question": "third-party free text must not be copied",
        "no_token": "not-needed",
    }


def write_inputs(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    *,
    coverage: dict[str, dict[str, dict[str, int | float]]] | None = None,
    grace_seconds: int = 7200,
) -> tuple[Path, Path, Path]:
    protocol = {
        "retrieval": {
            "gamma_endpoint": "https://gamma.invalid/events/keyset",
            "clob_endpoint": "https://clob.invalid/batch-prices-history",
            "gamma_title_search": "above",
            "history_fidelity_minutes": 1,
            "history_window_seconds": 2700,
        },
        "universe": {
            "required_outcomes": ["Yes", "No"],
            "accepted_gamma_candidate_yes_probabilities": [0.0, 1.0],
            "conditional_tokens_address": ADDRESS,
            "condition_resolution_topic": TOPIC0,
            "resolution_grace_seconds": grace_seconds,
            "accepted_canonical_payout_vectors": [[1, 0], [0, 1]],
        },
    }
    protocol_path = tmp_path / "protocol.json"
    protocol_bytes = json.dumps(protocol, sort_keys=True).encode()
    protocol_path.write_bytes(protocol_bytes)

    candidate_path = tmp_path / "candidate_rows_v0.1.csv.gz"
    columns = list(rows[0])
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    candidate_bytes = gzip.compress(stream.getvalue().encode(), mtime=0)
    candidate_path.write_bytes(candidate_bytes)

    if coverage is None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row["horizon_seconds"]), []).append(row)
        coverage = {"development": {}}
        for horizon, group in grouped.items():
            event_count = len({row["event_id"] for row in group})
            coverage["development"][horizon] = {
                "expected_events": event_count,
                "history_retained_events": event_count,
                "expected_rows": len(group),
                "history_retained_rows": len(group),
                "event_history_coverage": 1.0,
                "row_history_coverage": 1.0,
            }
    yes_tokens = sorted({str(row["yes_token"]) for row in rows}, key=int)
    history_records = []
    for batch_index in range(0, len(yes_tokens), 20):
        history_body = {
            "end_ts": DAY + 3600,
            "fidelity": 1,
            "markets": yes_tokens[batch_index : batch_index + 20],
            "start_ts": DAY + 900,
        }
        history_body_bytes = json.dumps(
            history_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        history_records.append(
            {
                "path": f"raw/clob/event_test/batch_{batch_index // 20:04d}.json.gz",
                "source": "clob.batch_prices_history",
                "request": {
                    "method": "POST",
                    "url": "https://clob.invalid/batch-prices-history",
                    "body": history_body,
                    "body_sha256": hashlib.sha256(history_body_bytes).hexdigest(),
                },
                "archived_at_utc": "2026-07-14T18:01:00Z",
                "content_bytes": 100,
                "content_sha256": "f" * 64,
                "gzip_bytes": 80,
                "gzip_sha256": "2" * 64,
            }
        )
    collector_manifest = {
        "schema_version": "event-factor-bench-collector-v1",
        "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
        "run_source_commit": "a" * 40,
        "artifacts": [
            {
                "path": candidate_path.name,
                "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            }
        ],
        "coverage_pre_chain": coverage,
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
                "content_sha256": "e" * 64,
                "gzip_bytes": 80,
                "gzip_sha256": "1" * 64,
            },
            *history_records,
        ],
    }
    collector_path = tmp_path / "collector_manifest.json"
    collector_path.write_text(json.dumps(collector_manifest))
    return protocol_path, candidate_path, collector_path


def run_verify(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    logs: list[dict[str, Any]],
    *,
    coverage: dict[str, dict[str, dict[str, int | float]]] | None = None,
    timestamps: list[int] | None = None,
    fail_method: str | None = None,
) -> tuple[dict[str, Any], Path, FakeTransport]:
    protocol, candidates, collector = write_inputs(tmp_path, rows, coverage=coverage)
    transport = FakeTransport(logs, timestamps=timestamps, fail_method=fail_method)
    rpc = chain.JsonRpcClient("https://rpc.invalid/v2/secret", transport=transport)
    data_dir = tmp_path / "data"
    manifest = chain.verify_and_freeze(
        protocol_path=protocol,
        candidate_path=candidates,
        collector_manifest_path=collector,
        data_dir=data_dir,
        rpc=rpc,
        max_blocks_per_query=2,
        run_source_commit="a" * 40,
    )
    return manifest, data_dir, transport


def test_decodes_condition_resolution_and_rejects_noncanonical_abi() -> None:
    assert chain.decode_condition_resolution(abi_resolution((1, 0))) == (2, (1, 0))
    assert chain.decode_condition_resolution(abi_resolution((3, 2, 1))) == (3, (3, 2, 1))

    with pytest.raises(chain.AbiDecodeError, match="offset"):
        chain.decode_condition_resolution(abi_resolution((1, 0), offset=96))
    with pytest.raises(chain.AbiDecodeError, match="truncated or trailing"):
        chain.decode_condition_resolution(abi_resolution((1, 0)) + "00" * 32)
    with pytest.raises(chain.AbiDecodeError, match="length differs"):
        words = (2, 64, 1, 1)
        chain.decode_condition_resolution(
            "0x" + "".join(value.to_bytes(32, "big").hex() for value in words)
        )


def test_inclusive_block_ranges_overlap_once_and_never_exceed_cap() -> None:
    ranges = list(chain.iter_inclusive_block_ranges(10, 17, max_blocks=3))
    assert ranges == [(10, 12), (12, 14), (14, 16), (16, 17)]
    assert all(last - first + 1 <= 3 for first, last in ranges)
    assert list(chain.iter_inclusive_block_ranges(8, 8, max_blocks=2)) == [(8, 8)]
    with pytest.raises(ValueError):
        list(chain.iter_inclusive_block_ranges(1, 2, max_blocks=30_001))


def test_freeze_deduplicates_boundaries_filters_incomplete_events_and_hashes_raw(
    tmp_path: Path,
) -> None:
    c1, c2, c3 = condition(1), condition(2), condition(3)
    rows = [
        candidate_row(event_id="good", condition_id=c1, label=1, horizon=horizon)
        for horizon in (1800, 900)
    ]
    rows += [
        candidate_row(event_id="good", condition_id=c2, label=0, horizon=horizon)
        for horizon in (1800, 900)
    ]
    rows += [
        candidate_row(event_id="missing", condition_id=c3, label=1, horizon=horizon)
        for horizon in (1800, 900)
    ]
    coverage = {
        "development": {
            str(horizon): {
                "expected_events": 4,
                "history_retained_events": 2,
                "expected_rows": 10,
                "history_retained_rows": 3,
                "event_history_coverage": 0.5,
                "row_history_coverage": 0.3,
            }
            for horizon in (1800, 900)
        }
    }
    manifest, data_dir, transport = run_verify(
        tmp_path,
        rows,
        [resolution_log(c1, (1, 0), block_number=2), resolution_log(c2, (0, 1), block_number=3)],
        coverage=coverage,
    )

    assert manifest["onchain_verified"] is False
    assert manifest["run_source_commit"] == "a" * 40
    assert manifest["resolution_counts"]["conditions"] == {
        "agreed": 2,
        "missing": 1,
        "ambiguous": 0,
        "gamma_disagreement": 0,
    }
    primary = manifest["coverage"]["development"]["1800"]
    assert primary["expected_events"] == 4
    assert primary["retained_events"] == 1
    assert primary["expected_rows"] == 10
    assert primary["retained_rows"] == 2
    assert primary["history_coverage"] == {"event": 0.5, "row": 0.3}
    assert primary["chain_given_history_coverage"]["event"] == 0.5
    assert primary["chain_given_history_coverage"]["row"] == pytest.approx(2 / 3)
    assert primary["event_coverage"] == 0.25
    assert primary["row_coverage"] == 0.2

    with gzip.open(data_dir / "frozen_v0.1.csv.gz", "rt", newline="") as stream:
        frozen = list(csv.DictReader(stream))
    assert len(frozen) == 4
    assert {row["event_id"] for row in frozen} == {"good"}
    assert set(frozen[0]) == set(chain.FROZEN_INPUT_COLUMNS + chain.RESERVED_OUTPUT_COLUMNS)
    assert "event_title" not in frozen[0]
    assert "question" not in frozen[0]
    assert {row["payout_vector"] for row in frozen} == {"[1,0]", "[0,1]"}
    assert all(len(row["chain_source_sha256"]) == 64 for row in frozen)

    get_log_calls = [call for call in transport.calls if call["method"] == "eth_getLogs"]
    ranges = [
        (int(call["params"][0]["fromBlock"], 16), int(call["params"][0]["toBlock"], 16))
        for call in get_log_calls
    ]
    assert ranges == [(1, 2), (2, 3), (3, 4), (4, 5)]
    assert all(call["params"][0]["topics"][1] == sorted((c1, c2, c3)) for call in get_log_calls)

    disk_manifest = json.loads((data_dir / "manifest_v0.1.json").read_text())
    public_collector = data_dir / "collector_manifest_v0.1.json"
    assert public_collector.exists()
    assert (
        hashlib.sha256(public_collector.read_bytes()).hexdigest()
        == manifest["collector_manifest"]["sha256"]
    )
    assert (
        manifest["files"]["collector_manifest_v0.1.json"]["sha256"]
        == manifest["collector_manifest"]["sha256"]
    )
    payload_hash = disk_manifest.pop("manifest_payload_sha256")
    assert hashlib.sha256(chain._canonical_json_bytes(disk_manifest)).hexdigest() == payload_hash
    for item in manifest["raw_responses"]:
        compressed = (data_dir / item["gzip_path"]).read_bytes()
        raw = gzip.decompress(compressed)
        assert hashlib.sha256(compressed).hexdigest() == item["gzip_sha256"]
        assert hashlib.sha256(raw).hexdigest() == item["response_sha256"]


def test_accepts_missing_outcomes_and_yes_no_gamma_alias(tmp_path: Path) -> None:
    c1 = condition(11)
    row = candidate_row(event_id="one", condition_id=c1, label=1)
    row["gamma_candidate_label"] = "Yes"
    manifest, data_dir, _ = run_verify(tmp_path, [row], [resolution_log(c1, (1, 0))])

    assert manifest["onchain_verified"] is True
    assert manifest["gamma_candidate_mismatch_count"] == 0
    with gzip.open(data_dir / "frozen_v0.1.csv.gz", "rt") as stream:
        frozen = next(csv.DictReader(stream))
    assert frozen["gamma_candidate_label"] == "Yes"
    assert frozen["label"] == "1"
    parsed = read_evidence(data_dir / "frozen_v0.1.csv.gz")
    assert parsed[0].condition_id == c1
    assert parsed[0].chain_source_sha256 == frozen["chain_source_sha256"]


def test_marks_nonbinary_multiple_logs_and_gamma_disagreement_without_labels(
    tmp_path: Path,
) -> None:
    c1, c2, c3 = condition(21), condition(22), condition(23)
    rows = [
        candidate_row(event_id="nonbinary", condition_id=c1, label=1),
        candidate_row(event_id="multiple", condition_id=c2, label=1),
        candidate_row(event_id="mismatch", condition_id=c3, label=0),
    ]
    logs = [
        resolution_log(c1, (1, 1), block_number=2),
        resolution_log(c2, (1, 0), block_number=2, log_index=0, tx_number=2),
        resolution_log(c2, (1, 0), block_number=3, log_index=1, tx_number=3),
        resolution_log(c3, (1, 0), block_number=3, tx_number=4),
    ]
    manifest, data_dir, _ = run_verify(tmp_path, rows, logs)

    assert manifest["resolution_counts"]["conditions"] == {
        "agreed": 0,
        "missing": 0,
        "ambiguous": 2,
        "gamma_disagreement": 1,
    }
    assert manifest["gamma_candidate_mismatch_count"] == 1
    assert manifest["onchain_verified"] is False
    with gzip.open(data_dir / "frozen_v0.1.csv.gz", "rt") as stream:
        assert list(csv.DictReader(stream)) == []


def test_clamps_future_grace_window_to_observed_head(tmp_path: Path) -> None:
    c1 = condition(31)
    timestamps = [DAY - 10, DAY, DAY + 90 * 60, DAY + 20 * 3600]
    manifest, _, _ = run_verify(
        tmp_path,
        [candidate_row(event_id="one", condition_id=c1, label=1)],
        [resolution_log(c1, (1, 0), block_number=2)],
        timestamps=timestamps,
    )

    window = manifest["chain"]["search_windows"][0]
    assert window["clamped_to_observed_head"] is True
    assert window["to_block_inclusive"] == 3
    assert manifest["onchain_verified"] is True


def test_resolution_deadline_accepts_equality_and_rejects_later_block(tmp_path: Path) -> None:
    c1, c2 = condition(32), condition(33)
    timestamps = [
        DAY - 10,
        DAY,
        DAY + 3 * 3600,
        DAY + 4 * 3600,
        DAY + 24 * 3600,
        DAY + 26 * 3600,
    ]
    manifest, data_dir, _ = run_verify(
        tmp_path,
        [
            candidate_row(event_id="at-deadline", condition_id=c1, label=1),
            candidate_row(event_id="late", condition_id=c2, label=1),
        ],
        [
            resolution_log(c1, (1, 0), block_number=2),
            resolution_log(c2, (1, 0), block_number=3),
        ],
        timestamps=timestamps,
    )

    assert manifest["resolution_counts"]["conditions"]["agreed"] == 1
    assert manifest["resolution_counts"]["conditions"]["ambiguous"] == 1
    assert "target-plus-grace" in manifest["exclusions"][0]["reason"]
    with gzip.open(data_dir / "frozen_v0.1.csv.gz", "rt") as stream:
        assert [row["event_id"] for row in csv.DictReader(stream)] == ["at-deadline"]


def test_rpc_error_fails_closed_without_publishing(tmp_path: Path) -> None:
    c1 = condition(41)
    protocol, candidates, collector = write_inputs(
        tmp_path, [candidate_row(event_id="one", condition_id=c1, label=1)]
    )
    transport = FakeTransport([], fail_method="eth_getLogs")
    rpc = chain.JsonRpcClient("https://rpc.invalid", transport=transport)
    data_dir = tmp_path / "data"

    with pytest.raises(chain.RpcError, match="RPC error"):
        chain.verify_and_freeze(
            protocol_path=protocol,
            candidate_path=candidates,
            collector_manifest_path=collector,
            data_dir=data_dir,
            rpc=rpc,
            max_blocks_per_query=2,
            run_source_commit="a" * 40,
        )
    assert not (data_dir / "frozen_v0.1.csv.gz").exists()
    assert not (data_dir / "manifest_v0.1.json").exists()
    assert not (data_dir / "collector_manifest_v0.1.json").exists()
    assert not (data_dir / "raw_chain_v0.1").exists()


def test_collector_hash_mismatch_fails_before_rpc(tmp_path: Path) -> None:
    c1 = condition(51)
    protocol, candidates, collector = write_inputs(
        tmp_path, [candidate_row(event_id="one", condition_id=c1, label=1)]
    )
    collector_payload = json.loads(collector.read_text())
    collector_payload["artifacts"][0]["sha256"] = "0" * 64
    collector.write_text(json.dumps(collector_payload))
    transport = FakeTransport([])
    rpc = chain.JsonRpcClient("https://rpc.invalid", transport=transport)

    with pytest.raises(chain.CandidateManifestError, match="SHA-256"):
        chain.verify_and_freeze(
            protocol_path=protocol,
            candidate_path=candidates,
            collector_manifest_path=collector,
            data_dir=tmp_path / "data",
            rpc=rpc,
            run_source_commit="a" * 40,
        )
    assert transport.calls == []


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
def test_collector_provenance_tampering_fails_before_rpc(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    c1 = condition(52)
    row = candidate_row(event_id="one", condition_id=c1, label=1)
    if mutation == "history_hash":
        row["source_history_sha256"] = "e" * 64
    protocol, candidates, collector = write_inputs(tmp_path, [row])
    payload = json.loads(collector.read_text())
    gamma = payload["raw_responses"][0]
    clob = payload["raw_responses"][1]
    if mutation == "schema":
        payload["schema_version"] = "v2"
    elif mutation == "unknown_source":
        gamma["source"] = "unknown"
    elif mutation == "unsafe_path":
        gamma["path"] = "../gamma.json.gz"
    elif mutation == "event_hash":
        gamma["content_sha256"] = "d" * 64
    elif mutation == "history_token":
        clob["request"]["body"]["markets"] = ["999999"]
        body = json.dumps(
            clob["request"]["body"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        clob["request"]["body_sha256"] = hashlib.sha256(body).hexdigest()
    elif mutation == "body_hash":
        clob["request"]["body_sha256"] = "0" * 64
    collector.write_text(json.dumps(payload))
    transport = FakeTransport([])
    rpc = chain.JsonRpcClient("https://rpc.invalid", transport=transport)

    with pytest.raises(chain.CandidateManifestError, match=expected):
        chain.verify_and_freeze(
            protocol_path=protocol,
            candidate_path=candidates,
            collector_manifest_path=collector,
            data_dir=tmp_path / "data",
            rpc=rpc,
            run_source_commit="a" * 40,
        )
    assert transport.calls == []


def test_wrong_chain_and_head_before_candidate_fail_closed(tmp_path: Path) -> None:
    c1 = condition(61)
    protocol, candidates, collector = write_inputs(
        tmp_path, [candidate_row(event_id="one", condition_id=c1, label=1)]
    )
    wrong_rpc = chain.JsonRpcClient("https://rpc.invalid", transport=FakeTransport([], chain_id=1))
    with pytest.raises(chain.RpcError, match="expected Polygon"):
        chain.verify_and_freeze(
            protocol_path=protocol,
            candidate_path=candidates,
            collector_manifest_path=collector,
            data_dir=tmp_path / "wrong-chain",
            rpc=wrong_rpc,
            run_source_commit="a" * 40,
        )

    old_head = FakeTransport([], timestamps=[DAY - 100, DAY + 100])
    old_rpc = chain.JsonRpcClient("https://rpc.invalid", transport=old_head)
    with pytest.raises(chain.RpcError, match="predates the latest candidate"):
        chain.verify_and_freeze(
            protocol_path=protocol,
            candidate_path=candidates,
            collector_manifest_path=collector,
            data_dir=tmp_path / "old-head",
            rpc=old_rpc,
            run_source_commit="a" * 40,
        )


def test_protocol_grace_override_must_match_frozen_value(tmp_path: Path) -> None:
    c1 = condition(71)
    protocol, candidates, collector = write_inputs(
        tmp_path, [candidate_row(event_id="one", condition_id=c1, label=1)]
    )
    rpc = chain.JsonRpcClient("https://rpc.invalid", transport=FakeTransport([]))

    with pytest.raises(chain.CandidateManifestError, match="differs from protocol"):
        chain.verify_and_freeze(
            protocol_path=protocol,
            candidate_path=candidates,
            collector_manifest_path=collector,
            data_dir=tmp_path / "data",
            rpc=rpc,
            resolution_grace=timedelta(hours=24),
            run_source_commit="a" * 40,
        )


def test_source_commit_is_full_and_required_before_rpc(tmp_path: Path) -> None:
    c1 = condition(81)
    protocol, candidates, collector = write_inputs(
        tmp_path, [candidate_row(event_id="one", condition_id=c1, label=1)]
    )
    transport = FakeTransport([])
    rpc = chain.JsonRpcClient("https://rpc.invalid", transport=transport)

    for invalid in ("", "abc123", "A" * 40, "z" * 40):
        with pytest.raises(chain.CandidateManifestError, match="40-hex"):
            chain.verify_and_freeze(
                protocol_path=protocol,
                candidate_path=candidates,
                collector_manifest_path=collector,
                data_dir=tmp_path / "data",
                rpc=rpc,
                run_source_commit=invalid,
            )
    assert transport.calls == []


def test_condition_topic_or_filter_is_bounded(tmp_path: Path) -> None:
    rows = [
        candidate_row(event_id=f"event-{number}", condition_id=condition(number), label=1)
        for number in range(1, chain.MAX_CONDITIONS_PER_LOG_QUERY + 3)
    ]
    _, _, transport = run_verify(tmp_path, rows, [])
    batches = [
        call["params"][0]["topics"][1]
        for call in transport.calls
        if call["method"] == "eth_getLogs"
    ]
    assert {len(batch) for batch in batches} == {2, chain.MAX_CONDITIONS_PER_LOG_QUERY}
    assert all(len(batch) <= chain.MAX_CONDITIONS_PER_LOG_QUERY for batch in batches)


def test_http_transport_retries_rate_limits_with_fixed_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"jsonrpc":"2.0","id":1,"result":"0x89"}'

    def urlopen(_request: object, *, timeout: float) -> Response:
        nonlocal attempts
        assert timeout == 3.0
        attempts += 1
        if attempts < 3:
            raise chain.urllib.error.HTTPError("https://rpc.invalid", 429, "rate limited", {}, None)
        return Response()

    monkeypatch.setattr(chain.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(chain.time_module, "sleep", delays.append)
    payload = chain._http_transport("https://rpc.invalid", b"{}", 3.0)
    assert json.loads(payload)["result"] == "0x89"
    assert attempts == 3
    assert delays == list(chain.RPC_BACKOFF_SECONDS[:2])


def test_json_rpc_client_retries_only_whitelisted_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    def transport(_url: str, body: bytes, _timeout: float) -> bytes:
        nonlocal attempts
        attempts += 1
        request = json.loads(body)
        if attempts < 3:
            return FakeTransport._response(
                request, error={"code": -32005, "message": "query limit"}
            )
        return FakeTransport._response(request, result="0x89")

    monkeypatch.setattr(chain.time_module, "sleep", delays.append)
    rpc = chain.JsonRpcClient("https://rpc.invalid", transport=transport)
    assert rpc.call("eth_chainId", []).value == "0x89"
    assert attempts == 3
    assert len(rpc.evidence) == 3
    assert delays == list(chain.RPC_BACKOFF_SECONDS[:2])
