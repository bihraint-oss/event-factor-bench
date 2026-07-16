from __future__ import annotations

import csv
import gzip
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/evaluate_api_snapshot.py"
SPEC = importlib.util.spec_from_file_location("eventfactor_evaluate_api_snapshot", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
snapshot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = snapshot
SPEC.loader.exec_module(snapshot)


def _first_raw_row() -> dict[str, str]:
    with gzip.open(snapshot.DEFAULT_DATA, "rt", encoding="utf-8", newline="") as handle:
        return next(csv.DictReader(handle))


def test_public_gamma_snapshot_and_manifest_validate() -> None:
    rows = snapshot.read_snapshot(snapshot.DEFAULT_DATA)
    protocol = snapshot._load_json(snapshot.DEFAULT_PROTOCOL)
    manifest = snapshot._load_json(snapshot.DEFAULT_MANIFEST)
    provenance = snapshot.validate_snapshot(
        rows,
        protocol,
        manifest,
        data_path=snapshot.DEFAULT_DATA,
        manifest_path=snapshot.DEFAULT_MANIFEST,
        protocol_path=snapshot.DEFAULT_PROTOCOL,
    )

    assert len(rows) == 67_156
    assert len({row.event_id for row in rows}) == 1_877
    assert provenance["data_sha256"] == (
        "0e0af86d8f87cb55c0a77337439b6ede95bc3440c86735faf4f689b49ad5202a"
    )
    assert provenance["raw_response_records"] == 2_006


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("gamma_candidate_label_source", "unknown", "label source"),
        ("gamma_candidate_label_onchain_verified", "True", "not on-chain verified"),
    ],
)
def test_snapshot_parser_rejects_false_label_provenance(
    field: str, value: str, message: str
) -> None:
    row = _first_raw_row()
    row[field] = value

    with pytest.raises(ValueError, match=message):
        snapshot._parse_row(row, 2)
