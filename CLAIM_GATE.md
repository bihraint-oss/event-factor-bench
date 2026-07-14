# Frozen v0.1 claim gate

This file defines what the v0.1 run may say before the July holdout is scored. The public
Polymarket data are not blinded; the protection is a source-and-protocol freeze, immutable
raw-response hashes, strict point-in-time assertions, and a chronological event-grouped
holdout.

## Primary hypothesis

At 30 minutes before the scheduled timestamp, projecting each event's raw YES reference
probabilities onto the non-increasing threshold cone reduces event-macro Brier loss relative
to the unmodified raw YES probabilities.

The holdout is `[2026-06-30T00:00:00Z, 2026-07-14T00:00:00Z)`. It contains 14 complete UTC
days and ends more than 18 hours before the source freeze; unresolved future hours are not
silently treated as missing data. One BTC or ETH threshold
ladder is one event. Outcomes within an event are never counted as independent observations.

## Required gates

An “improved probability quality” claim requires all of the following:

1. At least 500 eligible holdout events spanning at least 14 UTC dates.
2. At least 95% event coverage and 95% contract coverage after the frozen 120-second
   staleness rule.
3. At least 0.10% relative reduction in event-macro Brier loss.
4. A positive lower endpoint of the paired 95% hierarchical UTC-day/event bootstrap
   interval for `Brier(raw) - Brier(projected)` (10,000 resamples; seed 260715).
5. Event-macro clipped log loss does not regress by more than 0.001.
6. BTC and ETH subgroup Brier differences are each non-negative.
7. All leakage assertions, row/hash audits, and projection property tests pass.
8. Every retained contract has an unambiguous on-chain ConditionalTokens payout vector and it
   agrees with the Gamma candidate label; 50/50, missing, or conflicting resolutions fail
   closed.

The 15-minute horizon is secondary and must be labeled as such. Development and validation
results may diagnose the pipeline but cannot substitute for the holdout.

The 18-hour resolution window and 120-second price-age cap were fixed before the public protocol
tag using development-only operational audits. No validation or holdout probabilities, labels,
coverage, or scores were used to choose them. The exact precommit evidence and hashes are in
[`audits/development_precommit_v0.1.json`](audits/development_precommit_v0.1.json); these choices
may not be revised after seeing the frozen run.

An additional “incremental over beta calibration” claim is allowed only if applying the same
PAV projection after the frozen event-balanced beta calibrator clears the 0.10% relative Brier
threshold and has a positive paired 95% UTC-day/event bootstrap lower endpoint versus beta
calibration alone. Otherwise beta remains the stronger comparator and no incremental factor
claim is allowed.

## Failure language

If any required gate fails, the release must say the method did **not** pass the frozen
improvement gate. A positive point estimate with a confidence interval crossing zero is not
an improvement claim. Subgroup-only results must be described as subgroup results.

## Prohibited claims

`/prices-history` supplies timestamps and historical reference probabilities, not historical
L2 depth, queue position, executable size, or latency. Therefore this benchmark may not claim
realized P&L, tradable alpha, realistic fills, an execution-aware backtest, or that it “beat
Polymarket.” Any settlement-value calculation is descriptive only and excluded from the
primary result.

## Data caveat

The API snapshot is retrieved after resolution. The audit proves that selected feature
timestamps precede each cutoff and that post-outcome fields are excluded from features; it
does not turn vendor-retained history into an independently archived live snapshot.
