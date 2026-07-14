.PHONY: sync test lint check build assert-frozen-checkout assert-frozen-source assert-protocol-code assert-frozen-evidence collect freeze-labels audit-local-snapshot verify-evidence render-results verify-frozen

sync:
	uv sync --frozen --all-groups

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

check: lint test build

build:
	uv build --no-sources

assert-frozen-checkout:
	@test -z "$$(git status --porcelain --untracked-files=normal)" || (echo "checkout must be clean" >&2; exit 2)
	@test "$$(git rev-parse HEAD)" = "$$(git rev-parse 'protocol-v0.1^{commit}')" || (echo "checkout must be the protocol-v0.1 commit" >&2; exit 2)

assert-frozen-source:
	@test "$$(git rev-parse HEAD)" = "$$(git rev-parse 'protocol-v0.1^{commit}')" || (echo "checkout must be the protocol-v0.1 commit" >&2; exit 2)
	@test -z "$$(git status --porcelain --untracked-files=all -- . ':(exclude)data/**')" || (echo "source checkout differs from protocol-v0.1" >&2; exit 2)

assert-protocol-code:
	@test -z "$$(git diff --name-only 'protocol-v0.1^{commit}' -- .github configs audits src scripts tests CLAIM_GATE.md pyproject.toml uv.lock Makefile)" || (echo "benchmark protocol or code differs from protocol-v0.1" >&2; exit 2)

assert-frozen-evidence:
	@evidence_commit="$$(git rev-parse --verify 'refs/tags/frozen-v0.1-run^{commit}' 2>/dev/null)" || { echo "frozen-v0.1-run tag is required" >&2; exit 2; }; \
	protocol_commit="$$(git rev-parse --verify 'refs/tags/protocol-v0.1^{commit}' 2>/dev/null)" || { echo "protocol-v0.1 tag is required" >&2; exit 2; }; \
	git merge-base --is-ancestor "$$protocol_commit" "$$evidence_commit" || { echo "protocol-v0.1 must be an ancestor of frozen-v0.1-run" >&2; exit 2; }; \
	git merge-base --is-ancestor "$$evidence_commit" HEAD || { echo "frozen-v0.1-run must be an ancestor of the result commit" >&2; exit 2; }; \
	test "$$(git rev-parse HEAD)" != "$$evidence_commit" || { echo "result verification requires a commit after frozen-v0.1-run" >&2; exit 2; }; \
	for path in data/frozen_v0.1.csv.gz data/collector_manifest_v0.1.json data/manifest_v0.1.json; do \
		tagged_blob="$$(git rev-parse --verify "$${evidence_commit}:$${path}" 2>/dev/null)" || { echo "frozen-v0.1-run is missing $$path" >&2; exit 2; }; \
		head_blob="$$(git rev-parse --verify "HEAD:$${path}" 2>/dev/null)" || { echo "result commit is missing $$path" >&2; exit 2; }; \
		test "$$head_blob" = "$$tagged_blob" || { echo "$$path differs from frozen-v0.1-run" >&2; exit 2; }; \
		working_blob="$$(git hash-object -- "$$path" 2>/dev/null)" || { echo "working tree is missing $$path" >&2; exit 2; }; \
		test "$$working_blob" = "$$tagged_blob" || { echo "$$path differs from frozen-v0.1-run" >&2; exit 2; }; \
	done

collect: assert-frozen-checkout
	uv run python scripts/collect_frozen.py --config configs/protocol_v0.1.json --output artifacts/collection-v0.1 --workers 24 --run-source-commit "$$(git rev-parse 'protocol-v0.1^{commit}')"

freeze-labels: assert-frozen-checkout
	@test -n "$$POLYGON_RPC_URL" || (echo "POLYGON_RPC_URL is required" >&2; exit 2)
	@uv run python scripts/verify_chain.py --protocol configs/protocol_v0.1.json --input artifacts/collection-v0.1/candidate_rows_v0.1.csv.gz --collector-manifest artifacts/collection-v0.1/manifest.json --rpc-url "$$POLYGON_RPC_URL" --data-dir data --run-source-commit "$$(git rev-parse 'protocol-v0.1^{commit}')"

audit-local-snapshot: assert-frozen-source
	uv run python scripts/audit_local_snapshot.py --collection-dir artifacts/collection-v0.1 --data-dir data --expected-source-commit "$$(git rev-parse 'protocol-v0.1^{commit}')"

verify-evidence: assert-protocol-code
	uv run python scripts/evaluate_frozen.py --validate-only

render-results:
	uv run python scripts/render_results.py

verify-frozen: assert-protocol-code assert-frozen-evidence
	uv run python scripts/evaluate_frozen.py --verify results/frozen_v0.1/results.json
	uv run python scripts/render_results.py --verify
