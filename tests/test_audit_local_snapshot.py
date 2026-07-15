from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "audit_local_snapshot.py"
SPEC = importlib.util.spec_from_file_location("audit_local_snapshot_for_tests", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)
collection = audit.load_collector_module()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _gzip(path: Path, content: bytes) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(content, mtime=0)
    path.write_bytes(payload)
    return _sha256(payload), _sha256(content)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _chain_source_hash(log_sha256: str, block_sha256: str) -> str:
    domain = b"event-factor-bench-chain-source-v1\x00"
    return _sha256(domain + bytes.fromhex(log_sha256) + bytes.fromhex(block_sha256))


def _rpc_request_sha256(request_id: int, method: str, params: list[Any]) -> str:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return _sha256(payload)


def _protocol() -> dict[str, Any]:
    return {
        "benchmark": "EventFactorBench",
        "version": "0.1.0-test",
        "retrieval": {
            "gamma_endpoint": "https://gamma.test/events/keyset",
            "clob_endpoint": "https://clob.test/batch-prices-history",
            "discovery_start_inclusive": "2026-06-01T00:00:00Z",
            "discovery_end_exclusive": "2026-06-02T00:00:00Z",
            "gamma_title_search": "above",
            "history_fidelity_minutes": 1,
            "history_window_seconds": 2700,
        },
        "universe": {
            "event_title_regex": (
                r"^(Bitcoin|Ethereum) above ___ on .+, (?:1[0-2]|[1-9])(?:AM|PM) ET\?$"
            ),
            "assets": ["Bitcoin", "Ethereum"],
            "minimum_thresholds_per_event": 2,
            "required_outcomes": ["Yes", "No"],
            "required_resolution_status": "resolved",
            "accepted_gamma_candidate_yes_probabilities": [0.0, 1.0],
        },
        "splits": {
            "development": {
                "start_inclusive": "2026-06-01T00:00:00Z",
                "end_exclusive": "2026-06-02T00:00:00Z",
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
    hour: str,
    end_time: str,
) -> dict[str, Any]:
    return {
        "id": str(market_id),
        "conditionId": f"0xcondition{market_id}",
        "question": f"Bitcoin above {threshold} on June 1, {hour} ET?",
        "groupItemTitle": threshold,
        "endDate": end_time,
        "closed": True,
        "enableOrderBook": True,
        "umaResolutionStatus": "resolved",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(prices),
        "clobTokenIds": json.dumps([str(yes_token), str(yes_token + 1000)]),
    }


def _event(
    event_id: int,
    *,
    hour: str,
    end_time: str,
    market_start: int,
    token_start: int,
) -> dict[str, Any]:
    return {
        "id": str(event_id),
        "title": f"Bitcoin above ___ on June 1, {hour} ET?",
        "endDate": end_time,
        "closed": True,
        "markets": [
            _market(
                market_start,
                "100",
                ["1", "0"],
                token_start,
                hour=hour,
                end_time=end_time,
            ),
            _market(
                market_start + 1,
                "200",
                ["0", "1"],
                token_start + 1,
                hour=hour,
                end_time=end_time,
            ),
        ],
    }


class ReplayFixtureTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> bytes:
        del headers
        self.calls.append((method, url, body))
        if method == "GET":
            events = [
                _event(
                    5001,
                    hour="6PM",
                    end_time="2026-06-01T22:00:00Z",
                    market_start=7001,
                    token_start=101,
                ),
                _event(
                    5002,
                    hour="7PM",
                    end_time="2026-06-01T23:00:00Z",
                    market_start=7101,
                    token_start=201,
                ),
            ]
            return json.dumps({"events": events, "next_cursor": None}, sort_keys=True).encode()
        assert method == "POST"
        assert body is not None
        request = json.loads(body)
        end_ts = int(request["end_ts"])
        probabilities = {
            "101": (0.4, 0.3),
            "102": (0.6, 0.2),
            "201": (0.45, 0.35),
            "202": (0.65, 0.25),
        }
        history = {
            token: [
                {"t": end_ts - 1800, "p": probabilities[token][0]},
                {"t": end_ts - 900, "p": probabilities[token][1]},
            ]
            for token in request["markets"]
        }
        return json.dumps({"history": history}, sort_keys=True).encode()


def _snapshot(tmp_path: Path, *, collector_workers: int = 24) -> dict[str, Path]:
    collection_dir = tmp_path / "collection"
    data_dir = tmp_path / "data"
    config_path = tmp_path / "protocol.json"
    _write_json(config_path, _protocol())
    transport = ReplayFixtureTransport()
    collection.run_collection(
        config_path,
        collection_dir,
        run_source_commit="a" * 40,
        transport=transport,
        workers=collector_workers,
        user_agent="offline-replay-fixture/1",
    )
    collector_path = collection_dir / "manifest.json"
    collector = _load_json(collector_path)
    candidate_path = collection_dir / "candidate_rows_v0.1.csv.gz"
    candidate_sha = next(
        item["sha256"]
        for item in collector["artifacts"]
        if item["path"] == "candidate_rows_v0.1.csv.gz"
    )
    api_path = collection_dir / collector["raw_responses"][0]["path"]

    public_collector_path = data_dir / "collector_manifest_v0.1.json"
    public_collector_path.parent.mkdir(parents=True, exist_ok=True)
    public_collector_path.write_bytes(collector_path.read_bytes())
    frozen_path = data_dir / "frozen_v0.1.csv.gz"
    frozen_sha, _ = _gzip(frozen_path, b"event_id,label\n1,1\n")
    comparison_path = data_dir / "collector_comparison_v0.1.1.json"
    comparison_path.write_bytes(b'{"gate_passed":true}\n')

    log_method = "eth_getLogs"
    log_params: list[Any] = [
        {
            "address": "0x0000000000000000000000000000000000000001",
            "fromBlock": "0x1",
            "toBlock": "0x2",
            "topics": ["0x" + "1" * 64, ["0x" + "2" * 64]],
        }
    ]
    log_path = data_dir / "raw_chain_v0.1" / "000001_eth_getLogs.json.gz"
    log_gzip_sha, log_content_sha = _gzip(
        log_path,
        b'{"jsonrpc":"2.0","id":1,"result":[]}',
    )
    block_method = "eth_getBlockByNumber"
    block_params: list[Any] = ["0x1", False]
    block_path = data_dir / "raw_chain_v0.1" / "000002_eth_getBlockByNumber.json.gz"
    block_gzip_sha, block_content_sha = _gzip(
        block_path,
        b'{"jsonrpc":"2.0","id":2,"result":{"number":"0x1"}}',
    )
    chain = {
        "schema_version": "event-factor-bench-chain-freeze-v2",
        "run_source_commit": "a" * 40,
        "collector_manifest": {
            "sha256": _sha256(collector_path.read_bytes()),
            "run_source_commit": "a" * 40,
        },
        "candidate_manifest": {"file_sha256": candidate_sha},
        "files": {
            "collector_manifest_v0.1.json": {"sha256": _sha256(public_collector_path.read_bytes())},
            "collector_comparison_v0.1.1.json": {"sha256": _sha256(comparison_path.read_bytes())},
            "frozen_v0.1.csv.gz": {"sha256": frozen_sha},
        },
        "raw_responses": [
            {
                "sequence": 1,
                "request_id": 1,
                "method": log_method,
                "params": log_params,
                "request_sha256": _rpc_request_sha256(1, log_method, log_params),
                "gzip_path": "raw_chain_v0.1/000001_eth_getLogs.json.gz",
                "gzip_sha256": log_gzip_sha,
                "response_sha256": log_content_sha,
            },
            {
                "sequence": 2,
                "request_id": 2,
                "method": block_method,
                "params": block_params,
                "request_sha256": _rpc_request_sha256(2, block_method, block_params),
                "gzip_path": "raw_chain_v0.1/000002_eth_getBlockByNumber.json.gz",
                "gzip_sha256": block_gzip_sha,
                "response_sha256": block_content_sha,
            },
        ],
        "chain_source_bundles": [
            {
                "chain_source_sha256": _chain_source_hash(log_content_sha, block_content_sha),
                "raw_response_sha256s": [log_content_sha, block_content_sha],
            }
        ],
    }
    chain_path = data_dir / "manifest_v0.1.json"
    _write_json(chain_path, chain)
    return {
        "collection_dir": collection_dir,
        "collector": collector_path,
        "candidate": candidate_path,
        "collector_raw": api_path,
        "selection_audit": collection_dir / "selection_audit.json",
        "data_dir": data_dir,
        "chain": chain_path,
        "public_collector": public_collector_path,
        "frozen": frozen_path,
    }


def _run_main(
    paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    expected_source_commit: str = "a" * 40,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--collection-dir",
            str(paths["collection_dir"]),
            "--data-dir",
            str(paths["data_dir"]),
            "--expected-source-commit",
            expected_source_commit,
        ],
    )
    audit.main()


def _update_artifact_record(paths: dict[str, Path], relative: str) -> None:
    collector = _load_json(paths["collector"])
    payload = (paths["collection_dir"] / relative).read_bytes()
    record = next(item for item in collector["artifacts"] if item["path"] == relative)
    record["bytes"] = len(payload)
    record["sha256"] = _sha256(payload)
    _write_json(paths["collector"], collector)


def _rewrite_first_probability(path: Path, probability: str) -> None:
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(path.read_bytes()).decode("utf-8"))))
    assert rows
    rows[0]["reference_probability"] = probability
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_bytes(gzip.compress(stream.getvalue().encode("utf-8"), mtime=0))


def test_workers_24_archive_replays_with_one_worker_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _snapshot(tmp_path, collector_workers=24)

    def forbid_network(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"network access attempted: {args!r} {kwargs!r}")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_network)

    _run_main(paths, monkeypatch)

    assert capsys.readouterr().out == (
        "offline replay matched; audited 3 collector artifacts, 3 API responses, "
        "and 2 RPC responses\n"
    )


def test_probability_edit_with_updated_artifact_hash_fails_offline_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _snapshot(tmp_path)
    _rewrite_first_probability(paths["candidate"], "0.987654321")
    _update_artifact_record(paths, "candidate_rows_v0.1.csv.gz")

    with pytest.raises(ValueError, match="offline replay"):
        _run_main(paths, monkeypatch)


def test_selection_audit_edit_with_updated_artifact_hash_fails_offline_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _snapshot(tmp_path)
    selection = _load_json(paths["selection_audit"])
    selection["counts"]["candidate_rows"] += 1
    _write_json(paths["selection_audit"], selection)
    _update_artifact_record(paths, "selection_audit.json")

    with pytest.raises(ValueError, match="offline replay"):
        _run_main(paths, monkeypatch)


@pytest.mark.parametrize("mutation", ["deleted", "added"])
def test_deleted_or_added_raw_archive_record_fails_offline_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    paths = _snapshot(tmp_path)
    collector = _load_json(paths["collector"])
    if mutation == "deleted":
        collector["raw_responses"].pop()
    else:
        duplicate = copy.deepcopy(collector["raw_responses"][0])
        original = paths["collection_dir"] / duplicate["path"]
        duplicate["path"] = "raw/extra_unused_response.json.gz"
        extra = paths["collection_dir"] / duplicate["path"]
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(original.read_bytes())
        collector["raw_responses"].append(duplicate)
    _write_json(paths["collector"], collector)

    with pytest.raises(ValueError, match="offline collector replay failed"):
        _run_main(paths, monkeypatch)


def test_changed_archived_request_identity_causes_missing_request_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _snapshot(tmp_path)
    collector = _load_json(paths["collector"])
    collector["raw_responses"][0]["request"]["url"] += "&tampered=true"
    _write_json(paths["collector"], collector)

    with pytest.raises(ValueError, match="unarchived request"):
        _run_main(paths, monkeypatch)


def test_changed_archived_post_body_hash_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _snapshot(tmp_path)
    collector = _load_json(paths["collector"])
    post = next(raw for raw in collector["raw_responses"] if raw["request"]["method"] == "POST")
    post["request"]["body_sha256"] = "f" * 64
    _write_json(paths["collector"], collector)

    with pytest.raises(ValueError, match="SHA-256 mismatch for request body"):
        _run_main(paths, monkeypatch)


def test_expected_source_commit_is_mandatory_evidence_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _snapshot(tmp_path)

    with pytest.raises(ValueError, match="collector manifest does not match"):
        _run_main(paths, monkeypatch, expected_source_commit="b" * 40)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _guard_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "guard-repo"
    repo.mkdir()
    (repo / "Makefile").write_bytes((PROJECT_ROOT / "Makefile").read_bytes())
    (repo / ".gitignore").write_bytes((PROJECT_ROOT / ".gitignore").read_bytes())
    source = repo / "scripts" / "source.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "guard@example.invalid")
    _git(repo, "config", "user.name", "Frozen Guard Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "protocol source")
    _git(repo, "tag", "-a", "protocol-v0.1.1", "-m", "frozen protocol")
    origin = tmp_path / "guard-origin.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "--quiet", "origin", "HEAD:refs/heads/main", "--tags")
    return repo


def _run_source_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "assert-frozen-source"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_evidence_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "assert-frozen-evidence"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_protocol_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "assert-protocol-code"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_score_guard(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "assert-score-ready"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _commit_frozen_evidence(repo: Path) -> None:
    payloads = {
        "data/frozen_v0.1.csv.gz": b"frozen evidence\n",
        "data/collector_manifest_v0.1.json": b'{"collector": "frozen"}\n',
        "data/collector_comparison_v0.1.1.json": b'{"comparison": "frozen"}\n',
        "data/manifest_v0.1.json": b'{"manifest": "frozen"}\n',
    }
    for relative, payload in payloads.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    _git(repo, "add", "data")
    _git(repo, "commit", "--quiet", "-m", "frozen evidence")
    _git(repo, "tag", "-a", "frozen-v0.1.1-run", "-m", "frozen evidence")


def _commit_result(repo: Path) -> None:
    payloads = {
        "results/frozen_v0.1/results.json": '{"result": "computed"}\n',
        "results/frozen_v0.1/holdout_metrics.csv": "metric,value\nfixture,1\n",
        "results/frozen_v0.1/holdout_brier.svg": "<svg/>\n",
        "RESULTS.md": "# Frozen result\n",
    }
    for relative, payload in payloads.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    _git(repo, "add", "results", "RESULTS.md")
    _git(repo, "commit", "--quiet", "-m", "publish result")


def test_makefile_guard_allows_frozen_data_outputs_and_ignored_artifacts(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    for relative in (
        "data/frozen_v0.1.csv.gz",
        "data/collector_manifest_v0.1.json",
        "data/collector_comparison_v0.1.1.json",
        "data/manifest_v0.1.json",
        "data/raw_chain_v0.1/rpc.json.gz",
        "artifacts/collection-v0.1/manifest.json",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n", encoding="utf-8")

    result = _run_source_guard(repo)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mutation", ["tracked_source", "untracked_source"])
def test_makefile_guard_rejects_source_changes(tmp_path: Path, mutation: str) -> None:
    repo = _guard_repo(tmp_path)
    if mutation == "tracked_source":
        (repo / "scripts" / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    else:
        (repo / "scripts" / "untracked.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = _run_source_guard(repo)

    assert result.returncode != 0
    assert "source checkout differs from protocol-v0.1.1" in result.stderr


def test_makefile_guard_rejects_head_after_protocol_tag(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _git(repo, "commit", "--allow-empty", "--quiet", "-m", "post-protocol commit")

    result = _run_source_guard(repo)

    assert result.returncode != 0
    assert "checkout must be the protocol-v0.1.1 commit" in result.stderr


def test_protocol_guard_rejects_untracked_code_in_protected_paths(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    (repo / "src" / "sitecustomize.py").parent.mkdir(parents=True)
    (repo / "src" / "sitecustomize.py").write_text("raise SystemExit\n", encoding="utf-8")

    result = _run_protocol_guard(repo)

    assert result.returncode != 0
    assert "uncommitted or untracked" in result.stderr


def test_frozen_evidence_guard_accepts_a_byte_identical_result_successor(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_frozen_evidence(repo)
    _commit_result(repo)

    result = _run_evidence_guard(repo)

    assert result.returncode == 0, result.stderr


def test_frozen_evidence_guard_requires_the_public_evidence_tag(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_result(repo)

    result = _run_evidence_guard(repo)

    assert result.returncode != 0
    assert "frozen-v0.1.1-run must be an annotated tag" in result.stderr


def test_frozen_evidence_guard_rejects_the_evidence_commit_itself(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_frozen_evidence(repo)

    result = _run_evidence_guard(repo)

    assert result.returncode != 0
    assert "requires a commit after frozen-v0.1.1-run" in result.stderr


def test_frozen_evidence_guard_requires_protocol_to_precede_evidence(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    protocol_commit = _git(repo, "rev-parse", "protocol-v0.1.1^{commit}").stdout.strip()
    _commit_frozen_evidence(repo)
    evidence_commit = _git(repo, "rev-parse", "frozen-v0.1.1-run^{commit}").stdout.strip()
    _git(repo, "tag", "-d", "protocol-v0.1.1")
    unrelated_protocol = _git(
        repo,
        "commit-tree",
        _git(repo, "rev-parse", f"{protocol_commit}^{{tree}}").stdout.strip(),
        "-m",
        "unrelated protocol root",
    ).stdout.strip()
    _git(
        repo,
        "tag",
        "-a",
        "protocol-v0.1.1",
        "-m",
        "unrelated protocol",
        unrelated_protocol,
    )
    _git(repo, "switch", "--detach", evidence_commit)
    _commit_result(repo)

    result = _run_evidence_guard(repo)

    assert result.returncode != 0
    assert "protocol-v0.1.1 must be an ancestor" in result.stderr


def test_frozen_evidence_guard_requires_evidence_to_precede_result(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    protocol_commit = _git(repo, "rev-parse", "protocol-v0.1.1^{commit}").stdout.strip()
    _commit_frozen_evidence(repo)
    _git(repo, "switch", "--detach", protocol_commit)
    _commit_result(repo)

    result = _run_evidence_guard(repo)

    assert result.returncode != 0
    assert "frozen-v0.1.1-run must be an ancestor" in result.stderr


def test_frozen_evidence_guard_rejects_rehashed_data_in_result_commit(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_frozen_evidence(repo)
    for relative in (
        "data/frozen_v0.1.csv.gz",
        "data/collector_manifest_v0.1.json",
        "data/collector_comparison_v0.1.1.json",
        "data/manifest_v0.1.json",
    ):
        path = repo / relative
        path.write_bytes(path.read_bytes() + b"coherently rehashed after evidence tag\n")
    result_path = repo / "results" / "frozen_v0.1" / "results.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text('{"result": "recomputed over tampered bytes"}\n', encoding="utf-8")
    _git(repo, "add", "data", "results")
    _git(repo, "commit", "--quiet", "-m", "tamper, rehash, and recompute result")

    result = _run_evidence_guard(repo)

    assert result.returncode != 0
    assert "data tree differs from frozen-v0.1.1-run" in result.stderr


def test_frozen_evidence_guard_rejects_results_inside_evidence_tag(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_frozen_evidence(repo)
    _git(repo, "tag", "-d", "frozen-v0.1.1-run")
    _commit_result(repo)
    _git(repo, "tag", "-a", "frozen-v0.1.1-run", "-m", "tainted evidence")
    _git(repo, "commit", "--allow-empty", "--quiet", "-m", "successor")

    result = _run_evidence_guard(repo)

    assert result.returncode != 0
    assert "evidence tag must not contain results" in result.stderr


def test_frozen_evidence_guard_rejects_uncommitted_or_dirty_results(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_frozen_evidence(repo)
    _commit_result(repo)
    result_path = repo / "results" / "frozen_v0.1" / "results.json"
    result_path.write_text('{"result": "working-only"}\n', encoding="utf-8")

    result = _run_evidence_guard(repo)

    assert result.returncode != 0
    assert "differs from the result commit" in result.stderr


def _add_published_origin(repo: Path, tmp_path: Path, *, push_tags: bool) -> Path:
    del tmp_path
    origin = Path(_git(repo, "remote", "get-url", "origin").stdout.strip())
    _git(repo, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    if push_tags:
        _git(repo, "push", "--quiet", "origin", "--tags")
    return origin


def test_score_guard_accepts_exact_published_annotated_evidence_tag(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_frozen_evidence(repo)
    _add_published_origin(repo, tmp_path, push_tags=True)

    result = _run_score_guard(repo)

    assert result.returncode == 0, result.stderr


def test_score_guard_rejects_unpublished_evidence_tag(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_frozen_evidence(repo)
    _add_published_origin(repo, tmp_path, push_tags=False)

    result = _run_score_guard(repo)

    assert result.returncode != 0
    assert "origin must publish" in result.stderr


def test_score_guard_requires_exact_evidence_commit(tmp_path: Path) -> None:
    repo = _guard_repo(tmp_path)
    _commit_frozen_evidence(repo)
    _git(repo, "commit", "--allow-empty", "--quiet", "-m", "too late")

    result = _run_score_guard(repo)

    assert result.returncode != 0
    assert "exactly at frozen-v0.1.1-run" in result.stderr


def test_makefile_uses_the_right_pre_and_post_freeze_guards() -> None:
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "collect: assert-frozen-checkout" in makefile
    assert "freeze-labels: assert-frozen-checkout" in makefile
    assert "audit-local-snapshot: assert-frozen-source" in makefile
    assert "verify-frozen: assert-protocol-code assert-frozen-evidence" in makefile
    assert "--output artifacts/collection-v0.1.1" in makefile
    assert "--input artifacts/collection-v0.1.1/candidate_rows_v0.1.csv.gz" in makefile
    assert "collector_comparison_v0.1.1.json" in makefile
    assert "scripts/compare_collectors.py verify-public" in makefile
    assert "--collection-dir artifacts/collection-v0.1.1" in makefile
    assert "--expected-source-commit \"$$(git rev-parse 'protocol-v0.1.1^{commit}')\"" in makefile


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, "{}"),
        (
            '{"candidate_rows":"upstream changed"}',
            '{"candidate_rows":"upstream changed"}',
        ),
    ],
)
def test_makefile_passes_collector_explanations_as_exact_json(
    tmp_path: Path,
    configured: str | None,
    expected: str,
) -> None:
    repo = _guard_repo(tmp_path)
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    capture = tmp_path / "uv-arguments.txt"
    uv = stub_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n', encoding="utf-8")
    uv.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{stub_dir}{os.pathsep}{environment['PATH']}"
    environment["CAPTURE"] = str(capture)
    if configured is None:
        environment.pop("COLLECTOR_COMPARISON_EXPLANATIONS", None)
    else:
        environment["COLLECTOR_COMPARISON_EXPLANATIONS"] = configured

    result = subprocess.run(
        ["make", "compare-collectors"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    arguments = capture.read_text(encoding="utf-8").splitlines()
    assert arguments[-2:] == ["--explanations", expected]


def test_collector_candidate_artifact_tampering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _snapshot(tmp_path)
    paths["candidate"].write_bytes(paths["candidate"].read_bytes() + b"tamper")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _run_main(paths, monkeypatch)


@pytest.mark.parametrize("mutation", ["gzip_bytes", "gzip_digest", "content_digest"])
def test_collector_raw_gzip_tampering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    paths = _snapshot(tmp_path)
    if mutation == "gzip_bytes":
        paths["collector_raw"].write_bytes(paths["collector_raw"].read_bytes() + b"tamper")
    else:
        collector = _load_json(paths["collector"])
        field = "gzip_sha256" if mutation == "gzip_digest" else "content_sha256"
        collector["raw_responses"][0][field] = "f" * 64
        _write_json(paths["collector"], collector)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _run_main(paths, monkeypatch)


@pytest.mark.parametrize("record", ["collector_artifact", "chain_file", "chain_raw"])
def test_manifest_paths_cannot_escape_snapshot_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: str,
) -> None:
    paths = _snapshot(tmp_path)
    if record == "collector_artifact":
        collector = _load_json(paths["collector"])
        collector["artifacts"][0]["path"] = "../outside.bin"
        _write_json(paths["collector"], collector)
    else:
        chain = _load_json(paths["chain"])
        if record == "chain_file":
            file_record = chain["files"].pop("frozen_v0.1.csv.gz")
            chain["files"]["../outside.bin"] = file_record
        else:
            chain["raw_responses"][0]["gzip_path"] = "../outside.json.gz"
        _write_json(paths["chain"], chain)

    with pytest.raises(ValueError, match="manifest path escapes snapshot root"):
        _run_main(paths, monkeypatch)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("collector", "not bound to this collector manifest"),
        ("candidate", "not bound to this candidate artifact"),
        ("source_commit", "not bound to the collector source commit"),
    ],
)
def test_chain_manifest_is_bound_to_collector_and_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected: str,
) -> None:
    paths = _snapshot(tmp_path)
    chain = _load_json(paths["chain"])
    if mutation == "collector":
        chain["collector_manifest"]["sha256"] = "f" * 64
    elif mutation == "candidate":
        chain["candidate_manifest"]["file_sha256"] = "f" * 64
    else:
        chain["collector_manifest"]["run_source_commit"] = "b" * 40
    _write_json(paths["chain"], chain)

    with pytest.raises(ValueError, match=expected):
        _run_main(paths, monkeypatch)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", "non-empty source bundles"),
        ("empty", "non-empty source bundles"),
        ("short", "ordered log and block digests"),
        ("absent_raw", "absent raw response"),
        ("reversed", "log evidence before block evidence"),
        ("forged_digest", "invalid or duplicate chain source bundle"),
        ("duplicate", "invalid or duplicate chain source bundle"),
    ],
)
def test_chain_source_bundle_tampering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected: str,
) -> None:
    paths = _snapshot(tmp_path)
    chain = _load_json(paths["chain"])
    if mutation == "missing":
        chain.pop("chain_source_bundles")
    elif mutation == "empty":
        chain["chain_source_bundles"] = []
    elif mutation == "short":
        del chain["chain_source_bundles"][0]["raw_response_sha256s"][1]
    elif mutation == "absent_raw":
        chain["chain_source_bundles"][0]["raw_response_sha256s"][1] = "f" * 64
    elif mutation == "reversed":
        chain["chain_source_bundles"][0]["raw_response_sha256s"].reverse()
    elif mutation == "forged_digest":
        chain["chain_source_bundles"][0]["chain_source_sha256"] = "f" * 64
    else:
        chain["chain_source_bundles"].append(copy.deepcopy(chain["chain_source_bundles"][0]))
    _write_json(paths["chain"], chain)

    with pytest.raises(ValueError, match=expected):
        _run_main(paths, monkeypatch)


def test_empty_chain_raw_response_set_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _snapshot(tmp_path)
    chain = _load_json(paths["chain"])
    chain["raw_responses"] = []
    _write_json(paths["chain"], chain)

    with pytest.raises(ValueError, match="non-empty raw-response records"):
        _run_main(paths, monkeypatch)


@pytest.mark.parametrize(
    "mutation",
    ["request_id", "params", "request_sha256", "duplicate_sequence", "method"],
)
def test_chain_rpc_request_provenance_tampering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    paths = _snapshot(tmp_path)
    chain = _load_json(paths["chain"])
    if mutation == "request_id":
        chain["raw_responses"][0]["request_id"] = 7
    elif mutation == "params":
        chain["raw_responses"][0]["params"][0]["toBlock"] = "0x3"
    elif mutation == "request_sha256":
        chain["raw_responses"][0]["request_sha256"] = "f" * 64
    elif mutation == "duplicate_sequence":
        chain["raw_responses"][1]["sequence"] = 1
    else:
        raw = chain["raw_responses"][0]
        raw["method"] = "eth_fakeMethod"
        raw["request_sha256"] = _rpc_request_sha256(raw["request_id"], raw["method"], raw["params"])
    _write_json(paths["chain"], chain)

    with pytest.raises(ValueError):
        _run_main(paths, monkeypatch)


@pytest.mark.parametrize("file_name", ["public_collector", "frozen"])
def test_chain_manifest_file_hash_tampering_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_name: str,
) -> None:
    paths = _snapshot(tmp_path)
    path = paths[file_name]
    path.write_bytes(path.read_bytes() + b"tamper")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _run_main(paths, monkeypatch)


def test_chain_manifest_forged_file_digest_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _snapshot(tmp_path)
    chain = _load_json(paths["chain"])
    chain["files"]["frozen_v0.1.csv.gz"]["sha256"] = "f" * 64
    _write_json(paths["chain"], chain)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _run_main(paths, monkeypatch)
