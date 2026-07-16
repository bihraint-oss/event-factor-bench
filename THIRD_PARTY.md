# Third-party data and services

EventFactorBench's source code is MIT licensed. That license applies only to this repository's
original code and documentation; it does not relicense data returned by external services.

## Polymarket

The collector uses public, unauthenticated endpoints documented by Polymarket:

- Gamma event discovery: `https://gamma-api.polymarket.com/events/keyset`
- CLOB history: `https://clob.polymarket.com/batch-prices-history`

Documentation:

- <https://docs.polymarket.com/api-reference/introduction>
- <https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination>
- <https://docs.polymarket.com/api-reference/markets/get-batch-prices-history>
- <https://docs.polymarket.com/api-reference/rate-limits>

The official documentation describes these as public APIs but does not state, on the cited
pages, that their returned data are MIT licensed. The public snapshot release therefore keeps
only the minimal normalized rows needed to recompute the stated benchmark, request metadata,
and cryptographic provenance hashes. It is not a raw-data mirror. Users who recollect data are
responsible for the source terms and applicable law.

## Outcome labels

The v0.2 benchmark uses strict terminal Gamma `outcomePrices` vectors as retrospective labels.
Every row states that the label has not been independently verified on-chain. The public result
does not claim canonical Polygon/ConditionalTokens provenance.

The repository also contains an experimental verifier for public Polygon PoS logs emitted by the
ConditionalTokens contract:

- contract: `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- event: `ConditionResolution`

That optional verifier is preserved for historical protocol research but is not the source of the
v0.2 result. RPC endpoints are transport providers, not data licensors.

## No endorsement

Polymarket, Polygon, and RPC providers do not sponsor or endorse this benchmark. Product and
company names are used only to identify data provenance.
