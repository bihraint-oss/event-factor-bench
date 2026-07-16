# Gamma snapshot v0.2 results

**Statistical reporting screen: MET.**

At 30 minutes, label-free PAV reduced holdout event-macro Brier loss from 0.08586873 to 0.06251235, a 27.2001% relative reduction. The paired 95% UTC-day/event bootstrap interval for the absolute reduction was [0.02137460, 0.02558609].

**Label scope:** outcomes are retrospective terminal Polymarket Gamma `outcomePrices` labels. They were not independently verified on Polygon, so this release makes no canonical-chain claim.

## Holdout metrics

| Horizon | Method | Event-macro Brier | Event-macro log loss | Violation edges |
|---:|---|---:|---:|---:|
| 30 min | Raw CLOB reference | 0.08586873 | 0.26154148 | 1,596 |
| 30 min | Raw + PAV | 0.06251235 | 0.23493559 | 0 |
| 30 min | Development Platt | 0.07690186 | 0.24159113 | 1,596 |
| 30 min | Development beta | 0.07947659 | 0.24584725 | 1,596 |
| 30 min | Beta + PAV | 0.05408174 | 0.20935117 | 0 |
| 15 min | Raw CLOB reference | 0.07326594 | 0.22300761 | 1,534 |
| 15 min | Raw + PAV | 0.05167655 | 0.19949593 | 0 |
| 15 min | Development Platt | 0.07752980 | 0.22267846 | 1,534 |
| 15 min | Development beta | 0.07855741 | 0.22444105 | 1,534 |
| 15 min | Beta + PAV | 0.05154148 | 0.18990512 | 0 |

## Primary comparison and coverage

- Holdout events: 597 across 14 UTC days.
- Event coverage: 597/619 (96.446%).
- Contract-row coverage: 10,748/11,140 (96.481%).
- Raw-minus-PAV event-macro log-loss delta: 0.02660589.

## Asset subgroups

| Asset | Events | Absolute Brier reduction |
|---|---:|---:|
| Bitcoin | 299 | 0.02272546 |
| Ethereum | 298 | 0.02398943 |

## Statistical reporting screen

- PASS — `all_projection_violations_removed`
- PASS — `brier_ci_lower_above_zero`
- PASS — `log_loss_regression_within_limit`
- PASS — `minimum_contract_coverage`
- PASS — `minimum_event_coverage`
- PASS — `minimum_holdout_events`
- PASS — `minimum_holdout_utc_days`
- PASS — `minimum_relative_brier_improvement`
- PASS — `nonnegative_asset_subgroups`

## Snapshot provenance

- Release version: `0.2.0`
- Collector source commit: `0696ff267f4abc9167772c2336fe4c1d9413bf09`
- Rows / events / markets: 67,156 / 1,877 / 33,784
- Data SHA-256: `0e0af86d8f87cb55c0a77337439b6ede95bc3440c86735faf4f689b49ad5202a`
- Data content SHA-256: `e703dff2a94f60ef3e6bd8d94240375a20f0a0a320c95e9f4adafc452e5e9c87`
- Collector manifest SHA-256: `0fc7a29f536a43e5ba4ab9f391cf121994ef23d30717a8665f4cf8f78594c1e1`
- Protocol SHA-256: `79877551a7fa2ff8b26f15fb9d70aeb27c9b111ce204c1ac9d9f1aac3fc86320`
- Label source: `gamma_terminal_outcome_prices_candidate`
- On-chain verified: `false`
- Raw API response bytes redistributed: `false` (hash provenance only).

This is a retrospective probability-quality benchmark. It does not measure P&L, tradable alpha, executable fills, latency, or order-book performance.

![Holdout event-macro Brier loss](results/gamma_snapshot_v0.2/holdout_brier.svg)
