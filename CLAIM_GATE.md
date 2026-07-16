# v0.2 statistical reporting screen and claim limits

This file governs claims about the public Gamma snapshot result. It does not retroactively alter
the historical v0.1 on-chain protocol.

## Primary comparison

- Split: chronological holdout.
- Horizon: 30 minutes.
- Baseline: raw point-in-time CLOB Yes probability.
- Method: unweighted non-increasing PAV projection within each threshold ladder.
- Primary metric: event-macro Brier loss.
- Statistical unit: event.
- Dependence block: UTC calendar day.

## Numerical reporting screen

A headline PAV improvement may be reported only if all of the following are true:

1. at least 500 holdout events are present;
2. all 14 holdout UTC days are represented;
3. event coverage is at least 95%;
4. contract-row coverage is at least 95%;
5. relative event-macro Brier improvement is at least 0.1%;
6. the paired 95% day/event bootstrap interval for the absolute Brier reduction has lower bound
   above zero;
7. event-macro log loss regresses by no more than 0.001;
8. Bitcoin and Ethereum subgroup Brier reductions are both nonnegative;
9. the projected forecasts have zero monotonicity-violation edges.

The committed v0.2 result meets all nine conditions. CI recomputes them from the public CSV.

## Required wording

Any headline result must identify:

- the exact data version;
- the number of holdout events and days;
- baseline and post-processed Brier loss;
- relative improvement;
- paired confidence interval for the absolute reduction;
- that labels come from terminal Gamma `outcomePrices`;
- that labels were not independently verified on-chain.

“Met the statistical reporting screen” is allowed. “Passed the frozen confirmatory chain gate”
is not allowed for v0.2.

## Prohibited claims

The v0.2 artifacts do not support claims about:

- canonical Polygon/ConditionalTokens labels;
- tradable alpha or realized P&L;
- fills, fees, slippage, latency, liquidity, or market impact;
- venue-level superiority over Polymarket;
- causal or live-forward performance;
- investment suitability or financial advice.

## Label provenance invariant

The evaluator must fail if any row does not state both:

```text
gamma_candidate_label_source=gamma_terminal_outcome_prices_candidate
gamma_candidate_label_onchain_verified=False
```

The result JSON must state `canonical_chain_claim_allowed=false`. Changing the label source is a
new benchmark scope and requires a new release version, new results, and updated disclosures.
