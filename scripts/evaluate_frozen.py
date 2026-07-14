#!/usr/bin/env python3
"""Evaluate or verify the frozen EventFactorBench v0.1 evidence table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from event_factor_bench.evaluation import (
    evaluate_frozen,
    load_json,
    read_evidence,
    sha256_file,
    sha256_gzip_content,
    validate_frozen_inputs,
)


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
