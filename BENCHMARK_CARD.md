# EventFactorBench v0.2 benchmark card

## Purpose

EventFactorBench evaluates whether a label-free shape constraint improves retrospective
probability quality on hourly Bitcoin and Ethereum threshold ladders from Polymarket. It is an
offline benchmark for factor research and calibration infrastructure, not a trading system.

## Public snapshot

- 67,156 normalized forecast rows.
- 1,877 events and 33,784 distinct markets/conditions.
- 3,731 event × horizon curves.
- 43 UTC calendar days from 2026-06-01 through 2026-07-13.
- Bitcoin and Ethereum “above threshold” ladders with at least eight contracts.
- Forecast references at 30-minute and 15-minute cutoffs.
- Maximum accepted point-in-time staleness: 120 seconds.

The snapshot is [`data/gamma_snapshot_v0.2.csv.gz`](data/gamma_snapshot_v0.2.csv.gz). Its exact
compressed SHA-256 is
`0e0af86d8f87cb55c0a77337439b6ede95bc3440c86735faf4f689b49ad5202a`; the decompressed content
SHA-256 is `e703dff2a94f60ef3e6bd8d94240375a20f0a0a320c95e9f4adafc452e5e9c87`.

## Label semantics

The evaluation label is derived retrospectively from a strict terminal Polymarket Gamma
`outcomePrices` vector:

- `[1, 0]` maps to Yes / 1;
- `[0, 1]` maps to No / 0;
- every other vector is excluded by the collector.

Every public row states:

```text
gamma_candidate_label_source=gamma_terminal_outcome_prices_candidate
gamma_candidate_label_onchain_verified=False
```

These labels were not independently verified against Polygon ConditionalTokens payouts. The
v0.2 release therefore supports retrospective descriptive comparisons only. It is not the
confirmatory on-chain run originally designed under the historical v0.1 protocol tags.

## Features and leakage controls

The reference feature is the last eligible CLOB Yes probability at or before the forecast
cutoff. The evaluator rejects:

- a source timestamp after the cutoff;
- reported staleness inconsistent with the timestamps;
- staleness above 120 seconds;
- a cutoff inconsistent with scheduled time minus horizon;
- rows outside their chronological split;
- duplicate market × horizon rows;
- event ladders that differ across horizons;
- non-binary, inconsistent, or non-monotone terminal labels.

PAV projection reads thresholds and forecast probabilities only. It does not read any label.
Platt and beta calibration fit only on the development split.

## Splits

| Split | UTC interval | Role |
|---|---|---|
| Development | 2026-06-01 00:00 through 2026-06-19 23:59 | fit Platt/Beta |
| Validation | 2026-06-20 00:00 through 2026-06-29 23:59 | diagnostics |
| Holdout | 2026-06-30 00:00 through 2026-07-13 23:59 | reported comparison |

The primary 30-minute holdout contains 10,748 rows from 597 events across all 14 UTC days.
Event coverage is 597/619 (96.446%); row coverage is 10,748/11,140 (96.481%).

## Methods

- `raw`: point-in-time CLOB Yes reference probability.
- `pav_raw`: raw probability followed by unweighted non-increasing PAV within each ladder.
- `platt`: event-balanced Platt calibration trained on development.
- `beta`: event-balanced beta calibration trained on development.
- `pav_beta`: beta calibration followed by the same label-free PAV projection.

## Metrics and inference

- Primary: holdout event-macro Brier loss at 30 minutes.
- Secondary: event-macro log loss and 15-minute metrics.
- Weighting: equal weight per event, not per contract row.
- Interval: paired hierarchical percentile bootstrap.
- Dependence block: UTC calendar day, then event within sampled days.
- Resamples: 10,000.
- Confidence: 95%.
- Seed: 260715.

## v0.2 result

At 30 minutes, `pav_raw` changed event-macro Brier loss from 0.08586873 to 0.06251235, a 27.2001%
relative reduction. The paired 95% interval for the absolute reduction is
[0.02137460, 0.02558609]. Bitcoin and Ethereum subgroup reductions are both positive, and PAV
reduces 1,596 monotonicity-violation edges to zero.

This result meets every numerical condition in the v0.2 statistical reporting screen. That
screen is not the historical v0.1 canonical-chain claim gate.

## Reproducibility and provenance

The collector manifest is published as
[`data/gamma_snapshot_manifest_v0.2.json`](data/gamma_snapshot_manifest_v0.2.json), SHA-256
`0fc7a29f536a43e5ba4ab9f391cf121994ef23d30717a8665f4cf8f78594c1e1`. It binds:

- the protocol and source commit;
- the public snapshot file and byte count;
- pre-evaluation coverage by split and horizon;
- 2,006 Gamma/CLOB request records;
- successful-response content and gzip hashes;
- the exact label caveat and HTTP retry policy.

Raw API response bytes remain local and are not redistributed. CI validates the public table and
manifest, recomputes the complete benchmark, and compares all result artifacts byte-for-byte.

```bash
uv sync --frozen --all-groups
make verify-api-release
```

## Intended uses

- research on threshold-ladder shape repair;
- calibration and proper-scoring-rule experiments;
- reproducible prediction-market data pipelines;
- examples of event-balanced and dependence-aware benchmark design.

## Out-of-scope uses and claims

- live trading decisions or financial advice;
- P&L, alpha, Sharpe ratio, or execution-quality claims;
- fills, fees, slippage, latency, liquidity, or market impact;
- canonical-chain or dispute-resolution guarantees;
- causal claims about future markets;
- claims that a post-processor “beats Polymarket.”

## Historical protocol note

The repository retains the annotated `protocol-v0.1` and `protocol-v0.1.1` tags and the optional
Polygon verifier for audit history. No v0.2 metric or claim depends on that unfinished chain
freeze. The change from chain-verified labels to Gamma snapshot labels is disclosed as a
substantive scope change, which is why the public snapshot release is versioned v0.2.0.
