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

> **Frozen-run status:** the v0.1 statistical config and claim gate remain fixed and byte
> unchanged. `protocol-v0.1.1` is a transport/evidence-only successor to an aborted v0.1 label
> freeze. The July holdout remains unscored; a result commit can follow only after the new frozen
> evidence tag is public.

## Freeze lineage: v0.1 to v0.1.1

The first formal collector completed at `protocol-v0.1`, but the separate Polygon label-freeze
step did not publish evidence. The retained stderr contains three completed fail-closed command
records with two transport signatures: one `eth_getLogs` connection close and two
`eth_getBlockByNumber` TLS EOF errors. A fourth record is only a terminated `make` session
(`143`) and has no chain-verifier diagnostic. The local log has no per-invocation timestamps, so
the project does not infer them.

No `data/frozen_v0.1.csv.gz`, evidence tag, result file, evaluator output, or score was produced
by those attempts. The completed collector manifest did expose operational holdout coverage—597
of 619 events and 10,748 of 11,140 rows at the primary horizon—but no completed frozen-label
set, model fit, Brier score, log loss, interval, or gate result. The exact log hashes and failure
records are in
[`audits/formal_freeze_failures_v0.1.json`](audits/formal_freeze_failures_v0.1.json).

Version 0.1.1 changes only collector/chain transport robustness, evidence publication and
raw-RPC provenance, and the source/evidence tags. The machine-readable statistical protocol
remains [`configs/protocol_v0.1.json`](configs/protocol_v0.1.json), the claim gate remains
[`CLAIM_GATE.md`](CLAIM_GATE.md), and data/result filenames remain in the `v0.1` namespace. The
old collector manifest is still bound to the old source commit, so it cannot be relabeled: the
entire collector must run again exactly at `protocol-v0.1.1`. Before any scoring, the old and new
protocol hashes, candidate hashes and counts, archived-response counts, selection-audit hashes,
and split-by-horizon coverage must be compared and every substantive delta explained. That
comparison is published as `data/collector_comparison_v0.1.1.json`, bound into the chain
manifest, and independently checked by CI before validation or scoring. The old local snapshot
stays at `artifacts/collection-v0.1`; the successor writes
`artifacts/collection-v0.1.1` while retaining v0.1 filenames inside it.

Both network stages now use a fixed ten-attempt HTTP transport budget with retryable status and
exception allowlists. Collection builds a complete sibling staging directory and exposes it only
with one directory rename. Chain evidence is staged separately, restores any prior files after a
caught publication failure, and publishes the chain manifest last as the commit marker. Polygon
JSON-RPC envelope retries remain a separate, smaller budget; request IDs, canonical parameters,
request hashes, raw response hashes, and exact response IDs are archived under the v2 chain
schema. Strict JSON parsing rejects duplicate keys and non-finite numbers. These are operational
and evidence-integrity changes, not new modeling choices.

## Why this is not a contract-count leaderboard

A BTC ladder can contain about 20 binary contracts and an ETH ladder about 16. Counting all
contracts independently would inflate the effective sample size and overweight BTC. This
benchmark first averages loss within each ladder, then weights events equally. The paired
bootstrap resamples UTC days and events within days while retaining every threshold belonging
to an event.

## Frozen v0.1 statistical design

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
First create and push the annotated `protocol-v0.1.1` tag, wait for its CI run to pass, and stay
on that exact commit. The Make targets check that `origin` publishes the same dereferenced tag
before collection. The command still consumes the unchanged v0.1 statistical config and writes
the v0.1 public data namespace. Its local collector snapshot is isolated at
`artifacts/collection-v0.1.1`, preserving `artifacts/collection-v0.1` for the mandatory pre-score
comparison:

```bash
make collect
make compare-collectors
POLYGON_RPC_URL="https://your-polygon-rpc.example" make freeze-labels
```

`make compare-collectors` fails when a substantive old/new difference lacks a non-empty
explanation. Explanations, when required, are supplied as a JSON object through
`COLLECTOR_COMPARISON_EXPLANATIONS`; the generated report binds them to the exact old failure
audit and new collector bytes. The comparison reads provenance and candidate coverage only—it
does not inspect or use any label field, open frozen on-chain evidence or results, fit a model,
or compute a score. Its candidate CSV parser uses only `event_id` and `horizon_seconds`.

The chain freeze refuses an empty or abbreviated source commit. It also reads the 64,800-second
resolution deadline from the committed protocol; any optional CLI assertion must match that
value exactly. The evidence commit fixes `data/frozen_v0.1.csv.gz`, the public collector
manifest, the collector-comparison report, and the chain manifest before the separate
evaluator/result commit; that commit is tagged `frozen-v0.1.1-run`. Their hashes make any later
byte change detectable; this protocol does not claim that public outcomes were blind to the
operator.
The non-redistributed raw snapshot can then be checked byte-for-byte with
`make audit-local-snapshot`. That audit also replays normalization with a zero-network archived
transport and requires an exact candidate CSV and selection-audit match. On the evidence-only
commit, CI validates all public bindings without fitting or scoring; after the result commit, it
also recomputes the committed metrics.

The evidence-to-result sequence is fail-closed:

1. Commit only the four public evidence files above; keep `results/` and `RESULTS.md` absent.
2. Create the annotated `frozen-v0.1.1-run` tag on that exact commit.
3. Push both the evidence commit and annotated tag to `origin`, then wait for the evidence-tag CI
   run to pass.
4. Stay on the exact evidence commit and run `make score-frozen`. The target refuses to compute
   metrics unless the local tag is annotated, its dereferenced commit matches the tag published
   by `origin`, the protocol is its ancestor, protected source paths are clean, the comparison
   gate passes, and no result artifact exists.
5. Run `make render-results`, review the gate-allowed wording, and commit `results.json`, the CSV
   and SVG renderings, `RESULTS.md`, plus any README summary as a separate result commit.
6. Run `make verify-frozen`; it requires the complete data tree to remain byte-identical to the
   evidence tag, requires all rendered result artifacts to be committed and clean, and recomputes
   the result from the frozen evidence.

Direct non-validation use of `scripts/evaluate_frozen.py` invokes the same Git and comparison
guards, so bypassing the Make target does not permit pre-tag scoring. The guard can prove that the
tag is published by `origin`; waiting for its green CI run remains an explicit operator step.

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
general-purpose mirror of raw API responses. Raw collector and Polygon RPC response bytes remain
in the non-redistributed local audit snapshot. See [`THIRD_PARTY.md`](THIRD_PARTY.md).

## Citation

See [`CITATION.cff`](CITATION.cff). If you use the benchmark, cite the frozen release tag and
record the evidence-manifest SHA-256 rather than citing a moving branch.
