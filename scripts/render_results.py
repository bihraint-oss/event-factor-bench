#!/usr/bin/env python3
"""Render deterministic Markdown, CSV, and SVG artifacts from frozen results.json."""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
from pathlib import Path
from typing import Any

METHOD_LABELS = {
    "raw": "Raw CLOB reference",
    "pav_raw": "Raw + PAV",
    "platt": "Development Platt",
    "beta": "Development beta",
    "pav_beta": "Beta + PAV",
}


def load_result(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("benchmark") != "EventFactorBench":
        raise ValueError("input is not an EventFactorBench result object")
    return value


def render(result: dict[str, Any], output_dir: Path, results_markdown: Path) -> None:
    holdout = result["splits"]["holdout"]
    horizons = sorted(map(int, holdout), reverse=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "holdout_metrics.csv").write_text(_metrics_csv(holdout, horizons))
    (output_dir / "holdout_brier.svg").write_text(
        _brier_svg(holdout, horizons, _chart_subtitle(result)), encoding="utf-8"
    )
    results_markdown.write_text(_markdown(result, horizons), encoding="utf-8")


def verify(result: dict[str, Any], output_dir: Path, results_markdown: Path) -> None:
    """Fail if committed rendered artifacts differ from the frozen result object."""

    holdout = result["splits"]["holdout"]
    horizons = sorted(map(int, holdout), reverse=True)
    expected = {
        output_dir / "holdout_metrics.csv": _metrics_csv(holdout, horizons),
        output_dir / "holdout_brier.svg": _brier_svg(holdout, horizons, _chart_subtitle(result)),
        results_markdown: _markdown(result, horizons),
    }
    for path, content in expected.items():
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            raise ValueError(f"rendered result artifact does not match results.json: {path}")


def _metrics_csv(holdout: dict[str, Any], horizons: list[int]) -> str:
    stream = io.StringIO(newline="")
    fields = [
        "horizon_seconds",
        "method",
        "event_macro_brier",
        "event_macro_log_loss",
        "monotonicity_violation_edges",
        "events_with_violations",
    ]
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for horizon in horizons:
        for method in METHOD_LABELS:
            metrics = holdout[str(horizon)]["methods"][method]
            writer.writerow(
                {
                    "horizon_seconds": horizon,
                    "method": method,
                    **{field: metrics[field] for field in fields[2:]},
                }
            )
    return stream.getvalue()


def _markdown(result: dict[str, Any], horizons: list[int]) -> str:
    if "label_provenance" in result:
        return _snapshot_markdown(result, horizons)
    return _frozen_markdown(result, horizons)


def _frozen_markdown(result: dict[str, Any], horizons: list[int]) -> str:
    holdout = result["splits"]["holdout"]
    gate = result["claim_gate"]
    primary = holdout[str(horizons[0])]
    comparison = primary["comparisons"]["pav_raw_vs_raw"]
    evidence = result["evidence"]
    passed = bool(gate["primary_improvement_passed"])
    lines = [
        "# Frozen v0.1 results",
        "",
        f"**Primary claim gate: {'PASSED' if passed else 'DID NOT PASS'}.**",
        "",
        (
            f"At {horizons[0] // 60} minutes, PAV changed event-macro Brier loss from "
            f"{primary['methods']['raw']['event_macro_brier']:.8f} to "
            f"{primary['methods']['pav_raw']['event_macro_brier']:.8f}, a "
            f"{comparison['relative_brier_improvement'] * 100:.4f}% relative reduction. "
            f"The paired 95% UTC-day/event bootstrap interval for the absolute reduction was "
            f"[{comparison['brier_delta_ci'][0]:.8f}, "
            f"{comparison['brier_delta_ci'][1]:.8f}]."
        ),
        "",
        "## Holdout metrics",
        "",
        "| Horizon | Method | Event-macro Brier | Event-macro log loss | Violation edges |",
        "|---:|---|---:|---:|---:|",
    ]
    for horizon in horizons:
        for method, label in METHOD_LABELS.items():
            values = holdout[str(horizon)]["methods"][method]
            lines.append(
                f"| {horizon // 60} min | {label} | "
                f"{values['event_macro_brier']:.8f} | "
                f"{values['event_macro_log_loss']:.8f} | "
                f"{values['monotonicity_violation_edges']:,} |"
            )
    lines.extend(
        [
            "",
            "## Primary comparison and coverage",
            "",
            f"- Holdout events: {primary['events']:,} across {primary['utc_days']:,} UTC days.",
            (
                f"- Strict-universe event coverage: "
                f"{gate['coverage']['retained_events']:,}/"
                f"{gate['coverage']['expected_events']:,} "
                f"({gate['coverage']['event_coverage'] * 100:.3f}%)."
            ),
            (
                f"- Strict-universe contract-row coverage: "
                f"{gate['coverage']['retained_rows']:,}/"
                f"{gate['coverage']['expected_rows']:,} "
                f"({gate['coverage']['row_coverage'] * 100:.3f}%)."
            ),
            (f"- Event-macro log-loss delta, raw minus PAV: {comparison['log_loss_delta']:.8f}."),
            "",
            "## Asset subgroups",
            "",
            "| Asset | Events | Absolute Brier reduction |",
            "|---|---:|---:|",
        ]
    )
    for asset, values in sorted(primary["asset_subgroups"].items()):
        lines.append(f"| {asset} | {values['events']:,} | {values['brier_delta']:.8f} |")
    lines.extend(
        [
            "",
            "## Predeclared gate audit",
            "",
        ]
    )
    for name, value in gate["conditions"].items():
        lines.append(f"- {'PASS' if value else 'FAIL'} — `{name}`")
    lines.extend(
        [
            "",
            "## Frozen provenance",
            "",
            f"- Source commit: `{result['run_source_commit']}`",
            f"- Evidence SHA-256: `{evidence['sha256']}`",
            f"- Evidence content SHA-256: `{evidence['uncompressed_sha256']}`",
            f"- Protocol SHA-256: `{evidence['protocol_sha256']}`",
            f"- Chain manifest SHA-256: `{evidence['manifest_sha256']}`",
            f"- Collector manifest SHA-256: `{evidence['collector_manifest_sha256']}`",
            "",
            "The benchmark measures retrospective probability quality, not realized P&L, "
            "tradable alpha, executable fills, or order-book performance.",
            "",
            "![Holdout event-macro Brier loss](results/frozen_v0.1/holdout_brier.svg)",
            "",
        ]
    )
    return "\n".join(lines)


def _snapshot_markdown(result: dict[str, Any], horizons: list[int]) -> str:
    holdout = result["splits"]["holdout"]
    screen = result["reporting_screen"]
    primary = holdout[str(horizons[0])]
    comparison = primary["comparisons"]["pav_raw_vs_raw"]
    evidence = result["evidence"]
    provenance = result["label_provenance"]
    passed = bool(screen["statistical_screen_passed"])
    lines = [
        "# Gamma snapshot v0.2 results",
        "",
        f"**Statistical reporting screen: {'MET' if passed else 'NOT MET'}.**",
        "",
        (
            f"At {horizons[0] // 60} minutes, label-free PAV reduced holdout "
            f"event-macro Brier loss from {primary['methods']['raw']['event_macro_brier']:.8f} "
            f"to {primary['methods']['pav_raw']['event_macro_brier']:.8f}, a "
            f"{comparison['relative_brier_improvement'] * 100:.4f}% relative reduction. "
            f"The paired 95% UTC-day/event bootstrap interval for the absolute reduction was "
            f"[{comparison['brier_delta_ci'][0]:.8f}, "
            f"{comparison['brier_delta_ci'][1]:.8f}]."
        ),
        "",
        (
            "**Label scope:** outcomes are retrospective terminal Polymarket Gamma "
            "`outcomePrices` labels. They were not independently verified on Polygon, so this "
            "release makes no canonical-chain claim."
        ),
        "",
        "## Holdout metrics",
        "",
        "| Horizon | Method | Event-macro Brier | Event-macro log loss | Violation edges |",
        "|---:|---|---:|---:|---:|",
    ]
    for horizon in horizons:
        for method, label in METHOD_LABELS.items():
            values = holdout[str(horizon)]["methods"][method]
            lines.append(
                f"| {horizon // 60} min | {label} | "
                f"{values['event_macro_brier']:.8f} | "
                f"{values['event_macro_log_loss']:.8f} | "
                f"{values['monotonicity_violation_edges']:,} |"
            )
    lines.extend(
        [
            "",
            "## Primary comparison and coverage",
            "",
            f"- Holdout events: {primary['events']:,} across {primary['utc_days']:,} UTC days.",
            (
                f"- Event coverage: {screen['coverage']['retained_events']:,}/"
                f"{screen['coverage']['expected_events']:,} "
                f"({screen['coverage']['event_coverage'] * 100:.3f}%)."
            ),
            (
                f"- Contract-row coverage: {screen['coverage']['retained_rows']:,}/"
                f"{screen['coverage']['expected_rows']:,} "
                f"({screen['coverage']['row_coverage'] * 100:.3f}%)."
            ),
            f"- Raw-minus-PAV event-macro log-loss delta: {comparison['log_loss_delta']:.8f}.",
            "",
            "## Asset subgroups",
            "",
            "| Asset | Events | Absolute Brier reduction |",
            "|---|---:|---:|",
        ]
    )
    for asset, values in sorted(primary["asset_subgroups"].items()):
        lines.append(f"| {asset} | {values['events']:,} | {values['brier_delta']:.8f} |")
    lines.extend(["", "## Statistical reporting screen", ""])
    for name, value in screen["conditions"].items():
        lines.append(f"- {'PASS' if value else 'FAIL'} — `{name}`")
    lines.extend(
        [
            "",
            "## Snapshot provenance",
            "",
            f"- Release version: `{result['release_version']}`",
            f"- Collector source commit: `{result['run_source_commit']}`",
            f"- Rows / events / markets: {evidence['rows']:,} / {evidence['events']:,} / "
            f"{evidence['markets']:,}",
            f"- Data SHA-256: `{evidence['sha256']}`",
            f"- Data content SHA-256: `{evidence['uncompressed_sha256']}`",
            f"- Collector manifest SHA-256: `{evidence['manifest_sha256']}`",
            f"- Protocol SHA-256: `{evidence['protocol_sha256']}`",
            f"- Label source: `{provenance['source']}`",
            f"- On-chain verified: `{str(provenance['onchain_verified']).lower()}`",
            "- Raw API response bytes redistributed: `false` (hash provenance only).",
            "",
            "This is a retrospective probability-quality benchmark. It does not measure P&L, "
            "tradable alpha, executable fills, latency, or order-book performance.",
            "",
            "![Holdout event-macro Brier loss](results/gamma_snapshot_v0.2/holdout_brier.svg)",
            "",
        ]
    )
    return "\n".join(lines)


def _chart_subtitle(result: dict[str, Any]) -> str:
    if "label_provenance" in result:
        return "Lower is better · event-balanced · Gamma API snapshot v0.2"
    return "Lower is better · event-balanced · frozen v0.1"


def _brier_svg(holdout: dict[str, Any], horizons: list[int], subtitle: str) -> str:
    methods = ("raw", "pav_raw", "beta", "pav_beta")
    values = [
        float(holdout[str(horizon)]["methods"][method]["event_macro_brier"])
        for horizon in horizons
        for method in methods
    ]
    maximum = max(values) * 1.12 if values else 1.0
    width, height = 960, 520
    left, top, plot_width, plot_height = 105, 65, 805, 350
    group_width = plot_width / len(horizons)
    bar_width = min(62.0, group_width / (len(methods) + 1))
    colors = {"raw": "#64748b", "pav_raw": "#0f766e", "beta": "#7c3aed", "pav_beta": "#db2777"}
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
        ),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        (
            '<text x="105" y="32" font-family="system-ui,sans-serif" font-size="22" '
            'font-weight="700" fill="#0f172a">Holdout event-macro Brier loss</text>'
        ),
        (
            '<text x="105" y="52" font-family="system-ui,sans-serif" font-size="13" '
            f'fill="#475569">{html.escape(subtitle)}</text>'
        ),
    ]
    for tick in range(6):
        value = maximum * tick / 5
        y = top + plot_height - plot_height * tick / 5
        parts.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" '
            f'y2="{y:.2f}" stroke="#e2e8f0"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" '
            'font-family="ui-monospace,monospace" font-size="11" '
            f'fill="#64748b">{value:.4f}</text>'
        )
    for group_index, horizon in enumerate(horizons):
        group_x = left + group_index * group_width
        total_bars = len(methods) * bar_width
        start_x = group_x + (group_width - total_bars) / 2
        for method_index, method in enumerate(methods):
            value = float(holdout[str(horizon)]["methods"][method]["event_macro_brier"])
            bar_height = plot_height * value / maximum
            x = start_x + method_index * bar_width
            y = top + plot_height - bar_height
            parts.append(
                f'<rect x="{x + 4:.2f}" y="{y:.2f}" width="{bar_width - 8:.2f}" '
                f'height="{bar_height:.2f}" rx="4" fill="{colors[method]}"/>'
            )
            parts.append(
                f'<text x="{x + bar_width / 2:.2f}" y="{y - 7:.2f}" '
                'text-anchor="middle" font-family="ui-monospace,monospace" '
                f'font-size="10" fill="#334155">{value:.5f}</text>'
            )
        parts.append(
            f'<text x="{group_x + group_width / 2:.2f}" y="{top + plot_height + 30}" '
            'text-anchor="middle" font-family="system-ui,sans-serif" font-size="14" '
            f'font-weight="600" fill="#0f172a">{horizon // 60} minutes</text>'
        )
    legend_x = 150
    for index, method in enumerate(methods):
        x = legend_x + index * 190
        parts.append(
            f'<rect x="{x}" y="472" width="12" height="12" rx="2" fill="{colors[method]}"/>'
        )
        parts.append(
            f'<text x="{x + 18}" y="482" font-family="system-ui,sans-serif" '
            f'font-size="12" fill="#334155">{html.escape(METHOD_LABELS[method])}</text>'
        )
    parts.append("</svg>\n")
    return "\n".join(parts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("results/frozen_v0.1/results.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/frozen_v0.1"))
    parser.add_argument("--results-markdown", type=Path, default=Path("RESULTS.md"))
    parser.add_argument("--verify", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = load_result(args.input)
    if args.verify:
        verify(result, args.output_dir, args.results_markdown)
        print(f"verified {args.results_markdown}")
    else:
        render(result, args.output_dir, args.results_markdown)
        print(args.results_markdown)


if __name__ == "__main__":
    main()
