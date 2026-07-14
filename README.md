# EventFactorBench

[![CI](https://github.com/bihraint-oss/event-factor-bench/actions/workflows/ci.yml/badge.svg)](https://github.com/bihraint-oss/event-factor-bench/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/code%20license-MIT-green.svg)](LICENSE)

**A leakage-audited, event-balanced benchmark for shape-constrained prediction-market
probability factors.**

Hourly Polymarket contracts such as “Bitcoin above ___ at 1PM ET?” form a threshold curve:
the probability of clearing a higher strike should never exceed the probability of clearing a
lower strike. Separately traded contracts can violate that constraint because their reference
prices update asynchronously. EventFactorBench asks a narrow, falsifiable question:

> Does a label-free monotone projection improve out-of-sample probability quality relative to
> the unmodified CLOB historical reference probabilities?

The benchmark uses proper scoring rules, treats the complete strike ladder as one statistical
event, clusters uncertainty by UTC day, verifies final outcomes on Polygon, and fails closed on
post-cutoff observations. It is a forecasting benchmark—not a trading or fill simulator.

> **Frozen-run status:** the v0.1 protocol and claim gate are fixed. The July holdout remains
> unscored in this source snapshot; the result commit will be added only after the frozen tag is
> public.

## Why this is not a contract-count leaderboard

A BTC ladder can contain about 20 binary contracts and an ETH ladder about 16. Counting all
contracts independently would inflate the effective sample size and overweight BTC. This
benchmark first averages loss within each ladder, then weights events equally. The paired
bootstrap resamples UTC days and events within days while retaining every threshold belonging
to an event.

## Frozen v0.1 design

| Item | Predeclared choice |
|---|---|
| Universe | Hourly BTC/ETH `above ___` ladders; daily ladders excluded |
| Development | 2026-06-01 through 2026-06-19 UTC |
| Validation | 2026-06-20 through 2026-06-29 UTC |
| Holdout | 2026-06-30 through 2026-07-13 UTC (14 complete days) |
| Primary horizon | 30 minutes before the scheduled timestamp |
| Secondary horizon | 15 minutes |
| Price rule | Newest `p` at or before cutoff; maximum age 120 seconds |
| Resolution window | Scheduled timestamp through +18 hours; chosen from development-only latency audit |
| Baseline | Unmodified YES CLOB historical reference probability |
| Primary method | Unweighted non-increasing PAV projection by numeric threshold |
| Primary metric | Event-macro Brier loss |
| Inference | 10,000 paired UTC-day/event bootstrap resamples; seed 260715 |
| Canonical label | Polygon ConditionalTokens `ConditionResolution` payout vector |

The exact machine-readable choices live in
[`configs/protocol_v0.1.json`](configs/protocol_v0.1.json). The allowed result language is fixed
in [`CLAIM_GATE.md`](CLAIM_GATE.md), and limitations are summarized in
[`BENCHMARK_CARD.md`](BENCHMARK_CARD.md).

## Method

For thresholds \(k_1 < \cdots < k_m\), let \(p_i\) be the official historical YES reference
probability selected at the cutoff. The primary method computes the Euclidean projection

\[
\hat p = \arg\min_{q_1\ge q_2\ge\cdots\ge q_m}\sum_i(q_i-p_i)^2
\]

with the pool-adjacent-violators algorithm. The resolved threshold vector is itself in this
monotone cone. Therefore, for event label vector \(y\),

\[
\lVert\hat p-y\rVert_2^2 \le \lVert p-y\rVert_2^2.
\]

That geometric guarantee prevents squared-error regressions inside an eligible event; it does
**not** guarantee a practically meaningful aggregate gain. The frozen holdout and claim gate
test whether the realized improvement is large, consistent, and accompanied by acceptable log
loss.

## Comparators

The result table will include more than the weak baseline:

- raw CLOB reference probability (primary baseline);
- event-balanced Platt recalibration trained only on development events;
- event-balanced beta calibration trained only on development events;
- raw PAV shape repair (primary method);
- beta calibration followed by PAV, reported as a secondary combination.

All learned calibrators have fixed clipping, L2, tolerance, and iteration limits. They fail
closed rather than silently returning an unconverged fit.

## Leakage and provenance controls

- Gamma discovery uses keyset pagination with daily windows; inclusive midnight boundaries are
  deduplicated by stable IDs.
- The discovery frontier is 2026-07-14T00:00:00Z, safely behind the source-freeze time; future
  unresolved hours are outside the universe rather than counted as API missingness.
- Event titles and every nested market are checked locally; server-side search is never trusted
  as the sole eligibility filter.
- No price timestamp may be later than its forecast cutoff, and no gap longer than 120 seconds
  is forward-filled.
- Current Gamma price, volume, liquidity, bid/ask, and updated text fields are forbidden model
  features.
- Gamma's post-resolution outcome is only a candidate cross-check. Retained labels must match
  unambiguous on-chain `[1, 0]` or `[0, 1]` payout vectors.
- All contracts in an event remain in one chronological split.
- Raw response hashes, request parameters, archive timestamps, and normalized evidence hashes
  are recorded. Detailed row-level selection exclusions remain in the locally replayed audit
  snapshot; their artifact hash is public, while on-chain exclusions are published directly.

The API snapshot is retrieved after resolution. These checks prove cutoff ordering and feature
exclusion, but they do not turn vendor-retained history into an independently archived live
feed.

## Quick start

```bash
uv sync --frozen --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

After the frozen evidence dataset and result commit exist:

```bash
make verify-frozen
```

Network collection is a separate, explicit operation because the official APIs are mutable.
The Make targets require a clean checkout exactly at the public `protocol-v0.1` commit:

```bash
make collect
POLYGON_RPC_URL="https://your-polygon-rpc.example" make freeze-labels
```

The chain freeze refuses an empty or abbreviated source commit. It also reads the 64,800-second
resolution deadline from the committed protocol; any optional CLI assertion must match that
value exactly. The evidence commit fixes `data/frozen_v0.1.csv.gz`, the public collector
manifest, and the chain manifest before the separate evaluator/result commit. Their hashes make
any later byte change detectable; this protocol does not claim that public outcomes were blind
to the operator.
The non-redistributed raw snapshot can then be checked byte-for-byte with
`make audit-local-snapshot`. That audit also replays normalization with a zero-network archived
transport and requires an exact candidate CSV and selection-audit match. On the evidence-only
commit, CI validates all public bindings without fitting or scoring; after the result commit, it
also recomputes the committed metrics.

Before the public protocol tag, a development-only operational audit checked both extreme
thresholds in every strict development event: 1,768/1,768 conditions had one accepted Polygon
payout agreeing with Gamma. The maximum resolution latency was 39,472 seconds, so v0.1 fixes an
18-hour window with 25,328 seconds of development margin. A separate archived-history replay
showed that increasing the 120-second price-age cap through 300 seconds recovered no rows; the
cap and the 95% holdout coverage gate therefore remain unchanged. Exact counts and artifact
hashes are in [`audits/development_precommit_v0.1.json`](audits/development_precommit_v0.1.json).

## What this project does not claim

The official historical endpoint returns timestamped `p` values. It does not expose historical
L2 depth, bid/ask, queue position, executable size, cancellation order, or network latency.
Consequently, EventFactorBench does not claim realized P&L, tradable alpha, realistic fills,
an execution-aware backtest, or that it “beat Polymarket.” A future execution track would need
prospectively archived WebSocket books and fee-aware fill validation.

## Data and code licensing

The implementation is MIT licensed. Polymarket API responses and Polygon data remain subject
to their source terms; MIT does not relicense third-party data. The repository publishes the
minimal normalized evidence needed to recompute the benchmark plus provenance hashes, not a
general-purpose mirror of raw API responses. See [`THIRD_PARTY.md`](THIRD_PARTY.md).

## Citation

See [`CITATION.cff`](CITATION.cff). If you use the benchmark, cite the frozen release tag and
record the evidence-manifest SHA-256 rather than citing a moving branch.
