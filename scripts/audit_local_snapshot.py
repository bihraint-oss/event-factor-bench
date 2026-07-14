#!/usr/bin/env python3
"""Audit byte-level hashes for the non-redistributed local API and RPC snapshot."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import sys
import tempfile
from collections import defaultdict, deque
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

COLLECTOR_SCHEMA_VERSION = "event-factor-bench-collector-v1"
REPLAY_ARTIFACTS = (
    "candidate_rows_v0.1.csv.gz",
    "selection_audit.json",
    "protocol_v0.1.json",
)
RequestKey = tuple[str, str, str]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def safe_child(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("manifest path must be a non-empty string")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"manifest path escapes snapshot root: {relative!r}") from error
    return path


def require_digest(actual: str, expected: Any, name: str) -> None:
    if not isinstance(expected, str) or actual != expected:
        raise ValueError(f"SHA-256 mismatch for {name}")


def require_source_commit(actual: Any, expected: str, name: str) -> None:
    if (
        not isinstance(expected, str)
        or len(expected) != 40
        or expected.lower() != expected
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise ValueError("expected source commit must be a full lowercase 40-hex commit")
    if actual != expected:
        raise ValueError(f"{name} does not match the expected source commit")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ArchivedTransport:
    """Serve each hash-verified collector response once, without a network fallback."""

    def __init__(self, root: Path, records: Any) -> None:
        if not isinstance(records, list) or not records:
            raise ValueError("collector manifest lacks non-empty raw-response records")
        self._remaining: dict[RequestKey, deque[bytes]] = defaultdict(deque)
        self._remaining_count = 0
        for index, raw in enumerate(records):
            if not isinstance(raw, Mapping):
                raise ValueError("collector raw-response record must be an object")
            path = safe_child(root, raw.get("path"))
            payload = path.read_bytes()
            require_digest(
                hashlib.sha256(payload).hexdigest(),
                raw.get("gzip_sha256"),
                str(path),
            )
            content = gzip.decompress(payload)
            require_digest(
                hashlib.sha256(content).hexdigest(),
                raw.get("content_sha256"),
                f"{path} content",
            )
            request = raw.get("request")
            if not isinstance(request, Mapping):
                raise ValueError(f"collector raw response {index} lacks request metadata")
            method = request.get("method")
            url = request.get("url")
            if method not in {"GET", "POST"} or not isinstance(url, str) or not url:
                raise ValueError(f"collector raw response {index} has invalid method or URL")
            body_sha = request.get("body_sha256", "")
            if method == "GET":
                if body_sha != "" or "body" in request:
                    raise ValueError("archived GET request must have an empty body identity")
            else:
                body = request.get("body")
                if not isinstance(body, Mapping) or not isinstance(body_sha, str):
                    raise ValueError("archived POST request lacks body metadata")
                require_digest(
                    hashlib.sha256(canonical_json(body)).hexdigest(), body_sha, "request body"
                )
            key = (method, url, body_sha)
            self._remaining[key].append(content)
            self._remaining_count += 1

    def __call__(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
    ) -> bytes:
        del headers
        body_sha = "" if body is None else hashlib.sha256(body).hexdigest()
        key = (method, url, body_sha)
        payloads = self._remaining.get(key)
        if not payloads:
            raise ValueError(
                "offline replay requested an unarchived request: "
                f"method={method!r}, url={url!r}, body_sha256={body_sha!r}"
            )
        self._remaining_count -= 1
        return payloads.popleft()

    def assert_exhausted(self) -> None:
        if self._remaining_count:
            raise ValueError(
                f"offline replay left {self._remaining_count} archived request(s) unused"
            )


def load_collector_module() -> ModuleType:
    """Load the frozen collector implementation without copying its normalization logic."""

    name = "_event_factor_bench_collect_frozen_replay"
    existing = sys.modules.get(name)
    if isinstance(existing, ModuleType):
        return existing
    path = Path(__file__).with_name("collect_frozen.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load collector implementation from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def audit_collection(root: Path, manifest: Mapping[str, Any]) -> tuple[int, int, str]:
    if manifest.get("schema_version") != COLLECTOR_SCHEMA_VERSION:
        raise ValueError("collector manifest has an unsupported schema version")
    artifacts = manifest.get("artifacts")
    raw_records = manifest.get("raw_responses")
    if not isinstance(artifacts, list) or not isinstance(raw_records, list):
        raise ValueError("collector manifest lacks artifact or raw-response records")
    candidate_sha = ""
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            raise ValueError("collector artifact record must be an object")
        path = safe_child(root, raw.get("path"))
        require_digest(sha256(path), raw.get("sha256"), str(path))
        if path.name == "candidate_rows_v0.1.csv.gz":
            if candidate_sha:
                raise ValueError("collector manifest has duplicate candidate artifacts")
            candidate_sha = str(raw["sha256"])
    if not candidate_sha:
        raise ValueError("collector manifest has no candidate artifact")

    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise ValueError("collector raw-response record must be an object")
        path = safe_child(root, raw.get("path"))
        payload = path.read_bytes()
        require_digest(hashlib.sha256(payload).hexdigest(), raw.get("gzip_sha256"), str(path))
        content = gzip.decompress(payload)
        require_digest(
            hashlib.sha256(content).hexdigest(), raw.get("content_sha256"), f"{path} content"
        )
    return len(artifacts), len(raw_records), candidate_sha


def artifact_records(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ValueError("collector manifest lacks artifact records")
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_artifacts:
        if not isinstance(raw, Mapping):
            raise ValueError("collector artifact record must be an object")
        relative = raw.get("path")
        if not isinstance(relative, str) or not relative or relative in result:
            raise ValueError("collector artifact paths must be unique non-empty strings")
        result[relative] = dict(raw)
    return result


def stable_collector_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    stable = json.loads(json.dumps(manifest, allow_nan=False))
    if not isinstance(stable, dict):  # pragma: no cover - callers already load objects
        raise ValueError("collector manifest must be an object")
    stable.pop("generated_at", None)
    raw_records = stable.get("raw_responses")
    if isinstance(raw_records, list):
        for raw in raw_records:
            if isinstance(raw, dict):
                raw.pop("archived_at_utc", None)
    return stable


def replay_collection(root: Path, manifest: Mapping[str, Any]) -> None:
    """Re-run normalization from archived responses and compare deterministic outputs."""

    transport = ArchivedTransport(root, manifest.get("raw_responses"))
    collector = load_collector_module()
    run_collection = getattr(collector, "run_collection", None)
    if not callable(run_collection):  # pragma: no cover - frozen source contract
        raise ValueError("collector implementation has no callable run_collection")
    protocol_path = safe_child(root, "protocol_v0.1.json")
    source_commit = manifest.get("run_source_commit")
    if not isinstance(source_commit, str):
        raise ValueError("collector manifest lacks run_source_commit")

    with tempfile.TemporaryDirectory(prefix="event-factor-bench-replay-") as temporary:
        replay_root = Path(temporary) / "collection"
        try:
            run_collection(
                protocol_path,
                replay_root,
                run_source_commit=source_commit,
                transport=transport,
                workers=1,
            )
            transport.assert_exhausted()
        except Exception as exc:
            raise ValueError(f"offline collector replay failed: {exc}") from exc

        replay_manifest = load_object(replay_root / "manifest.json")
        archived_artifacts = artifact_records(manifest)
        replayed_artifacts = artifact_records(replay_manifest)
        if archived_artifacts != replayed_artifacts:
            raise ValueError("offline replay static artifact records differ from the archive")
        if set(archived_artifacts) != set(REPLAY_ARTIFACTS):
            raise ValueError("collector manifest has an unexpected static artifact set")

        for relative in REPLAY_ARTIFACTS:
            archived = safe_child(root, relative).read_bytes()
            replayed = safe_child(replay_root, relative).read_bytes()
            if relative == "candidate_rows_v0.1.csv.gz" and gzip.decompress(
                archived
            ) != gzip.decompress(replayed):
                raise ValueError("offline replay candidate CSV content differs from the archive")
            if archived != replayed:
                raise ValueError(f"offline replay bytes differ for {relative}")

        if manifest.get("coverage_pre_chain") != replay_manifest.get("coverage_pre_chain"):
            raise ValueError("offline replay coverage_pre_chain differs from the archive")
        if stable_collector_manifest(manifest) != stable_collector_manifest(replay_manifest):
            raise ValueError("offline replay collector manifest differs from the archive")


def audit_chain(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    collector_manifest_sha: str,
    collector_source_commit: str,
    candidate_sha: str,
    expected_source_commit: str,
) -> int:
    collector = manifest.get("collector_manifest")
    candidate = manifest.get("candidate_manifest")
    if not isinstance(collector, Mapping) or not isinstance(candidate, Mapping):
        raise ValueError("chain manifest lacks collector/candidate provenance")
    if collector.get("sha256") != collector_manifest_sha:
        raise ValueError("chain manifest is not bound to this collector manifest")
    if candidate.get("file_sha256") != candidate_sha:
        raise ValueError("chain manifest is not bound to this candidate artifact")
    if collector.get("run_source_commit") != collector_source_commit:
        raise ValueError("chain manifest is not bound to the collector source commit")
    if collector.get("run_source_commit") != manifest.get("run_source_commit"):
        raise ValueError("collector and chain manifests cite different source commits")
    require_source_commit(
        manifest.get("run_source_commit"),
        expected_source_commit,
        "chain manifest",
    )

    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("chain manifest lacks frozen file records")
    for relative, raw in files.items():
        if not isinstance(raw, Mapping):
            raise ValueError("chain frozen-file record must be an object")
        path = safe_child(root, relative)
        require_digest(sha256(path), raw.get("sha256"), str(path))

    raw_records = manifest.get("raw_responses")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("chain manifest lacks non-empty raw-response records")
    response_hashes: set[str] = set()
    response_methods: dict[str, set[str]] = {}
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise ValueError("chain raw-response record must be an object")
        path = safe_child(root, raw.get("gzip_path"))
        payload = path.read_bytes()
        require_digest(hashlib.sha256(payload).hexdigest(), raw.get("gzip_sha256"), str(path))
        content = gzip.decompress(payload)
        require_digest(
            hashlib.sha256(content).hexdigest(), raw.get("response_sha256"), f"{path} content"
        )
        response_sha = str(raw["response_sha256"])
        method = raw.get("method")
        if not isinstance(method, str) or not method:
            raise ValueError("chain raw-response record lacks an RPC method")
        response_hashes.add(response_sha)
        response_methods.setdefault(response_sha, set()).add(method)

    bundles = manifest.get("chain_source_bundles")
    if not isinstance(bundles, list) or not bundles:
        raise ValueError("chain manifest lacks non-empty source bundles")
    bundle_hashes: set[str] = set()
    domain = b"event-factor-bench-chain-source-v1\x00"
    for bundle in bundles:
        if not isinstance(bundle, Mapping):
            raise ValueError("chain source bundle must be an object")
        sources = bundle.get("raw_response_sha256s")
        if (
            not isinstance(sources, list)
            or len(sources) != 2
            or not all(isinstance(source, str) for source in sources)
        ):
            raise ValueError("chain source bundle must contain ordered log and block digests")
        if len(set(sources)) != 2 or not set(sources).issubset(response_hashes):
            raise ValueError("chain source bundle cites an absent raw response")
        if (
            "eth_getLogs" not in response_methods[sources[0]]
            or "eth_getBlockByNumber" not in response_methods[sources[1]]
        ):
            raise ValueError("chain source bundle must order log evidence before block evidence")
        digest = bundle.get("chain_source_sha256")
        computed = hashlib.sha256(
            domain + bytes.fromhex(sources[0]) + bytes.fromhex(sources[1])
        ).hexdigest()
        if not isinstance(digest, str) or digest != computed or digest in bundle_hashes:
            raise ValueError("invalid or duplicate chain source bundle")
        bundle_hashes.add(digest)
    return len(raw_records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-dir", type=Path, default=Path("artifacts/collection-v0.1"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--expected-source-commit", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    collector_path = args.collection_dir / "manifest.json"
    chain_path = args.data_dir / "manifest_v0.1.json"
    collector = load_object(collector_path)
    chain = load_object(chain_path)
    require_source_commit(
        collector.get("run_source_commit"),
        args.expected_source_commit,
        "collector manifest",
    )
    artifact_count, api_count, candidate_sha = audit_collection(args.collection_dir, collector)
    replay_collection(args.collection_dir, collector)
    rpc_count = audit_chain(
        args.data_dir,
        chain,
        collector_manifest_sha=sha256(collector_path),
        collector_source_commit=str(collector["run_source_commit"]),
        candidate_sha=candidate_sha,
        expected_source_commit=args.expected_source_commit,
    )
    print(
        f"offline replay matched; audited {artifact_count} collector artifacts, "
        f"{api_count} API responses, "
        f"and {rpc_count} RPC responses"
    )


if __name__ == "__main__":
    main()
