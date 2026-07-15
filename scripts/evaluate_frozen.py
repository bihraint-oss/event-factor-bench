#!/usr/bin/env python3
"""Evaluate or verify the frozen EventFactorBench v0.1 evidence table."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from event_factor_bench.evaluation import (
    evaluate_frozen,
    load_json,
    read_evidence,
    sha256_file,
    sha256_gzip_content,
    validate_frozen_inputs,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _require_canonical_paths(args: argparse.Namespace) -> None:
    expected = {
        "data": PROJECT_ROOT / "data/frozen_v0.1.csv.gz",
        "protocol": PROJECT_ROOT / "configs/protocol_v0.1.json",
        "manifest": PROJECT_ROOT / "data/manifest_v0.1.json",
        "collector_manifest": PROJECT_ROOT / "data/collector_manifest_v0.1.json",
        "output": PROJECT_ROOT / "results/frozen_v0.1/results.json",
    }
    for name, canonical in expected.items():
        supplied = Path(getattr(args, name))
        if not supplied.is_absolute():
            supplied = Path.cwd() / supplied
        if supplied.resolve() != canonical.resolve():
            raise SystemExit(f"formal scoring requires canonical {name.replace('_', '-')} path")


def _run_git_guard(*targets: str) -> None:
    try:
        subprocess.run(
            ["make", *targets],
            cwd=PROJECT_ROOT,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("formal scoring Git/evidence guard failed") from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data/frozen_v0.1.csv.gz"))
    parser.add_argument("--protocol", type=Path, default=Path("configs/protocol_v0.1.json"))
    parser.add_argument("--manifest", type=Path, default=Path("data/manifest_v0.1.json"))
    parser.add_argument(
        "--collector-manifest", type=Path, default=Path("data/collector_manifest_v0.1.json")
    )
    parser.add_argument("--output", type=Path, default=Path("results/frozen_v0.1/results.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", type=Path)
    mode.add_argument(
        "--validate-only",
        action="store_true",
        help="validate evidence bindings without fitting models or computing metrics",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.validate_only:
        _require_canonical_paths(args)
        if args.verify is None:
            _run_git_guard("assert-score-ready", "verify-collector-comparison")
            if args.output.exists():
                raise SystemExit("refusing to overwrite an existing formal result")
        else:
            expected_verify = PROJECT_ROOT / "results/frozen_v0.1/results.json"
            supplied_verify = args.verify if args.verify.is_absolute() else Path.cwd() / args.verify
            if supplied_verify.resolve() != expected_verify.resolve():
                raise SystemExit("formal verification requires the canonical committed result")
            _run_git_guard("assert-frozen-evidence", "verify-collector-comparison")
    rows = read_evidence(args.data)
    protocol = load_json(args.protocol)
    manifest = load_json(args.manifest)
    collector_manifest = load_json(args.collector_manifest)
    hashes = {
        "evidence_sha256": sha256_file(args.data),
        "evidence_uncompressed_sha256": sha256_gzip_content(args.data),
        "protocol_sha256": sha256_file(args.protocol),
        "manifest_sha256": sha256_file(args.manifest),
        "collector_manifest_sha256": sha256_file(args.collector_manifest),
    }
    if args.validate_only:
        validate_frozen_inputs(rows, protocol, manifest, collector_manifest, **hashes)
        print(f"validated {args.data} without computing metrics")
        return
    result = evaluate_frozen(rows, protocol, manifest, collector_manifest, **hashes)
    if args.verify is not None:
        expected = load_json(args.verify)
        if result != expected:
            raise SystemExit("frozen result verification failed")
        print(f"verified {args.verify}")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
