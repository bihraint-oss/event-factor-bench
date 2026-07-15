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

## Formal-freeze lineage

The v0.1 collector completed, but its Polygon label-freeze did not publish evidence. Archived
stderr records three completed fail-closed invocations with two transport signatures: one
`eth_getLogs` connection close and two `eth_getBlockByNumber` TLS EOF failures. It also contains
one terminated `make` record (`143`) without a chain-verifier diagnostic. No per-invocation
timestamps are available, so none are inferred.

No frozen evidence, evidence tag, result, evaluator output, or score was produced. Operational
holdout coverage from the collector manifest was visible before the successor—597/619 events
and 10,748/11,140 rows at 30 minutes—but no completed frozen-label set or scoring metrics were
available. See
[`audits/formal_freeze_failures_v0.1.json`](audits/formal_freeze_failures_v0.1.json) for the log
hashes and exact records.

`protocol-v0.1.1` is a transport/evidence-only successor. The v0.1 statistical config, claim
gate, splits, method, metrics, inference, and data/result filenames are unchanged. Because the
old collector manifest is source-commit-bound, a full collector rerun at the successor tag is
mandatory; its hashes, counts, and coverage must be compared with the failed-attempt collector
before scoring. Every substantive delta requires an explanation in the public
`data/collector_comparison_v0.1.1.json`; the chain manifest binds that report and CI verifies it
independently. The comparison is provenance-only: it does not inspect or use a label field, open
frozen on-chain evidence or results, or compute scores; its candidate parser uses only event IDs
and horizons. The successor evidence commit is tagged `frozen-v0.1.1-run`.

The amendment gives both collection and chain access a fixed ten-attempt HTTP transport budget.
Collection publishes a completed local snapshot with one sibling-directory rename. Chain output
uses staged files, caught-failure rollback, and a manifest-last commit marker. The public v2
chain schema records canonical request parameters, request IDs and request/response hashes;
local and public validators reject duplicate JSON keys, non-finite numbers, non-integer IDs, and
provenance mismatches. Polygon JSON-RPC error retries use a separate five-attempt budget. None of
these changes alters event selection, price selection, labels, splits, factors, metrics, or
claim thresholds.

Formal scoring is available only through a guarded evidence-to-result transition. The annotated
`frozen-v0.1.1-run` tag must be published by `origin`, point at the exact current commit, descend
from the annotated protocol tag, contain the complete public evidence data tree, and contain no
result artifacts. Protected code and evidence must be clean and the collector comparison must
verify before `make score-frozen` computes metrics. The later result commit must retain the exact
evidence data tree and commit `results.json`, deterministic CSV/SVG renderings, and `RESULTS.md`;
`make verify-frozen` then recomputes and verifies them. Raw API/RPC bytes remain local audit
material; public evidence carries their provenance hashes rather than redistributing them.

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
