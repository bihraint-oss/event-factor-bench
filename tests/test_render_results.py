from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from test_evaluation import _evaluate, evidence_rows, manifest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "render_results.py"
SPEC = importlib.util.spec_from_file_location("eventfactor_render_results", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
render_results = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = render_results
SPEC.loader.exec_module(render_results)


def test_renderer_uses_frozen_result_as_single_source_of_truth(tmp_path: Path) -> None:
    rows = evidence_rows()
    result = _evaluate(rows, manifest(rows))
    output_dir = tmp_path / "results/frozen_v0.1"
    markdown = tmp_path / "RESULTS.md"

    render_results.render(result, output_dir, markdown)

    report = markdown.read_text(encoding="utf-8")
    assert "Primary claim gate: PASSED" in report
    assert result["evidence"]["sha256"] in report
    assert "not realized P&L" in report
    csv_text = (output_dir / "holdout_metrics.csv").read_text(encoding="utf-8")
    assert "1800,pav_raw" in csv_text
    svg = (output_dir / "holdout_brier.svg").read_text(encoding="utf-8")
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert "Raw + PAV" in svg
    render_results.verify(result, output_dir, markdown)

    markdown.write_text(report + "tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        render_results.verify(result, output_dir, markdown)
