# Benchmark card

## Task

Repair cross-contract incoherence in hourly BTC/ETH “above threshold” probability curves,
then measure predictive quality on a frozen chronological holdout.

## Unit and weighting

- Row: one threshold contract at one forecast horizon.
- Event: the complete threshold ladder for one asset and scheduled UTC hour.
- Primary estimand: mean within-event Brier loss, then equal weight across events.
- Uncertainty: paired hierarchical resampling of UTC days and events within days.

This prevents a 20-strike BTC ladder from counting as 20 independent forecasts or receiving
more weight than a smaller ETH ladder.

## Data source

- Event and resolution metadata: public Gamma `events/keyset` API.
- Historical reference probability: public CLOB `batch-prices-history` API.
- Canonical binary outcome: Polygon ConditionalTokens `ConditionResolution` logs.
- Raw response bytes and detailed selection exclusions are retained in the local audit snapshot.
  The public evidence release contains request parameters, archive timestamps, SHA-256 hashes,
  aggregate coverage, on-chain exclusions, and the minimal normalized rows needed to recompute
  the result. The local auditor replays normalization exactly; third-party raw responses and
  free-text selection records are not relicensed as an API mirror.

The benchmark uses only hourly events (daily ladders are excluded to avoid duplicate economic
cells) whose nested markets are binary Yes/No, order-book enabled, and marked
`umaResolutionStatus == resolved`. Gamma's final outcome is retained only as a candidate
cross-check. The canonical label is the Polygon ConditionalTokens `ConditionResolution`
payout vector; only unambiguous `[1, 0]` or `[0, 1]` vectors are accepted.

## Splits

- Development: 2026-06-01 through 2026-06-19 UTC.
- Validation: 2026-06-20 through 2026-06-29 UTC.
- Holdout: 2026-06-30 through 2026-07-13 UTC (14 complete UTC days).

Split membership follows the scheduled event timestamp. All contracts in an event remain in
the same split.

## Development-only operational precommit

Before the public v0.1 protocol tag, the lowest and highest threshold from each of 884 strict
development events were checked against Polygon. All 1,768 conditions had one accepted binary
payout agreeing with Gamma; the maximum scheduled-to-resolution latency was 39,472 seconds.
Because 805 events resolved their two extremes at different block timestamps, the formal run
checks every condition rather than inheriting one event-level label. The protocol fixes an
18-hour resolution window, leaving 25,328 seconds of margin over the observed development
maximum.

The development history replay also found 93.33% 30-minute event coverage under the 120-second
freshness rule. Relaxing the cap to 180, 240, or 300 seconds recovered no events; 600 seconds
recovered only two and still remained below 95%. The 120-second rule and 95% holdout claim gate
were therefore retained. Validation and holdout data were not used for either decision. See
[`audits/development_precommit_v0.1.json`](audits/development_precommit_v0.1.json).

## Baseline and method

The baseline is the latest raw YES reference probability no later than the cutoff and no more
than 120 seconds old. The primary method sorts contracts by numeric threshold and applies the
unweighted pool-adjacent-violators projection so probabilities cannot increase at higher
thresholds.

This is a label-free, deterministic structural factor. Because the resolved threshold vector
is itself non-increasing, Euclidean projection cannot increase within-event squared error;
the benchmark tests whether the realized reduction is large and consistent enough to clear
the predeclared practical and uncertainty gates.

## Metrics

- Primary: event-macro Brier loss and relative Brier reduction.
- Secondary: clipped event-macro log loss, violation rate/count, coverage, staleness, and
  asset-specific results.

## Leakage controls

- No selected price timestamp may exceed the forecast cutoff.
- Post-resolution Gamma values are labels only, never model features.
- Gamma candidate labels must agree with canonical on-chain payout vectors.
- Event IDs cannot cross splits.
- Duplicate timestamps with conflicting values fail closed.
- Boundary-inclusive API pages are deduplicated by stable IDs and content hashed.

## Scope limits

This is a retrospective forecasting benchmark, not a trading backtest. It does not reconstruct
historical order books or claim executable returns. It covers a short, homogeneous CLOB V2
regime and hourly crypto threshold ladders; it does not establish generalization to elections,
sports, longer horizons, other venues, or future market regimes.
