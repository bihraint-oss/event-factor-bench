.PHONY: sync test lint check build assert-published-protocol assert-frozen-checkout assert-frozen-source assert-protocol-code assert-score-ready assert-frozen-evidence collect compare-collectors freeze-labels audit-local-snapshot verify-collector-comparison verify-evidence score-frozen render-results verify-frozen verify-api-snapshot score-api-snapshot render-api-snapshot verify-api-release

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

assert-published-protocol:
	@protocol_ref='refs/tags/protocol-v0.1.1'; \
	test "$$(git cat-file -t "$$protocol_ref" 2>/dev/null)" = tag || { echo "protocol-v0.1.1 must be an annotated tag" >&2; exit 2; }; \
	protocol_commit="$$(git rev-parse "$$protocol_ref^{commit}")"; \
	test "$$(git rev-parse HEAD)" = "$$protocol_commit" || { echo "checkout must be the protocol-v0.1.1 commit" >&2; exit 2; }; \
	remote_commit="$$(git ls-remote --tags origin 'refs/tags/protocol-v0.1.1^{}' | awk 'NR == 1 {print $$1}')"; \
	test "$$remote_commit" = "$$protocol_commit" || { echo "origin must publish the same annotated protocol-v0.1.1 before collection" >&2; exit 2; }

assert-frozen-checkout: assert-published-protocol
	@test -z "$$(git status --porcelain --untracked-files=normal)" || (echo "checkout must be clean" >&2; exit 2)

assert-frozen-source: assert-published-protocol
	@test -z "$$(git status --porcelain --untracked-files=all -- . ':(exclude)data/**')" || (echo "source checkout differs from protocol-v0.1.1" >&2; exit 2)

assert-protocol-code:
	@test -z "$$(git diff --name-only 'protocol-v0.1.1^{commit}' -- .github configs audits src scripts tests CLAIM_GATE.md pyproject.toml uv.lock Makefile)" || (echo "benchmark protocol or code differs from protocol-v0.1.1" >&2; exit 2)
	@test -z "$$(git status --porcelain --untracked-files=all -- .github configs audits src scripts tests CLAIM_GATE.md pyproject.toml uv.lock Makefile)" || (echo "protected protocol paths contain uncommitted or untracked files" >&2; exit 2)

assert-score-ready: assert-protocol-code
	@evidence_ref='refs/tags/frozen-v0.1.1-run'; \
	protocol_ref='refs/tags/protocol-v0.1.1'; \
	test "$$(git cat-file -t "$$evidence_ref" 2>/dev/null)" = tag || { echo "frozen-v0.1.1-run must be an annotated tag" >&2; exit 2; }; \
	test "$$(git cat-file -t "$$protocol_ref" 2>/dev/null)" = tag || { echo "protocol-v0.1.1 must be an annotated tag" >&2; exit 2; }; \
	evidence_commit="$$(git rev-parse "$$evidence_ref^{commit}")"; \
	protocol_commit="$$(git rev-parse "$$protocol_ref^{commit}")"; \
	test "$$(git rev-parse HEAD)" = "$$evidence_commit" || { echo "formal scoring must run exactly at frozen-v0.1.1-run" >&2; exit 2; }; \
	git merge-base --is-ancestor "$$protocol_commit" "$$evidence_commit" || { echo "protocol-v0.1.1 must be an ancestor of frozen-v0.1.1-run" >&2; exit 2; }; \
	if git cat-file -e "$${evidence_commit}:results" 2>/dev/null; then echo "the evidence tag must not contain results" >&2; exit 2; fi; \
	if git cat-file -e "$${evidence_commit}:RESULTS.md" 2>/dev/null; then echo "the evidence tag must not contain RESULTS.md" >&2; exit 2; fi; \
	test -z "$$(git status --porcelain --untracked-files=all -- data)" || { echo "working data differs from frozen-v0.1.1-run" >&2; exit 2; }; \
	test ! -e results/frozen_v0.1 || { echo "refusing to overwrite existing formal result artifacts" >&2; exit 2; }; \
	test ! -e RESULTS.md || { echo "refusing to overwrite an existing RESULTS.md" >&2; exit 2; }; \
	remote_commit="$$(git ls-remote --tags origin 'refs/tags/frozen-v0.1.1-run^{}' | awk 'NR == 1 {print $$1}')"; \
	test "$$remote_commit" = "$$evidence_commit" || { echo "origin must publish the same annotated frozen-v0.1.1-run before scoring" >&2; exit 2; }

assert-frozen-evidence: assert-protocol-code
	@evidence_ref='refs/tags/frozen-v0.1.1-run'; \
	protocol_ref='refs/tags/protocol-v0.1.1'; \
	test "$$(git cat-file -t "$$evidence_ref" 2>/dev/null)" = tag || { echo "frozen-v0.1.1-run must be an annotated tag" >&2; exit 2; }; \
	test "$$(git cat-file -t "$$protocol_ref" 2>/dev/null)" = tag || { echo "protocol-v0.1.1 must be an annotated tag" >&2; exit 2; }; \
	evidence_commit="$$(git rev-parse "$$evidence_ref^{commit}")"; \
	protocol_commit="$$(git rev-parse "$$protocol_ref^{commit}")"; \
	git merge-base --is-ancestor "$$protocol_commit" "$$evidence_commit" || { echo "protocol-v0.1.1 must be an ancestor of frozen-v0.1.1-run" >&2; exit 2; }; \
	git merge-base --is-ancestor "$$evidence_commit" HEAD || { echo "frozen-v0.1.1-run must be an ancestor of the result commit" >&2; exit 2; }; \
	test "$$(git rev-parse HEAD)" != "$$evidence_commit" || { echo "result verification requires a commit after frozen-v0.1.1-run" >&2; exit 2; }; \
	if git cat-file -e "$${evidence_commit}:results" 2>/dev/null; then echo "the evidence tag must not contain results" >&2; exit 2; fi; \
	if git cat-file -e "$${evidence_commit}:RESULTS.md" 2>/dev/null; then echo "the evidence tag must not contain RESULTS.md" >&2; exit 2; fi; \
	tagged_data="$$(git rev-parse --verify "$${evidence_commit}:data" 2>/dev/null)" || { echo "frozen-v0.1.1-run is missing the data tree" >&2; exit 2; }; \
	head_data="$$(git rev-parse --verify 'HEAD:data' 2>/dev/null)" || { echo "result commit is missing the data tree" >&2; exit 2; }; \
	test "$$head_data" = "$$tagged_data" || { echo "the result commit data tree differs from frozen-v0.1.1-run" >&2; exit 2; }; \
	test -z "$$(git status --porcelain --untracked-files=all -- data)" || { echo "working data differs from the committed result data tree" >&2; exit 2; }; \
	for result_path in results/frozen_v0.1/results.json results/frozen_v0.1/holdout_metrics.csv results/frozen_v0.1/holdout_brier.svg RESULTS.md; do \
		head_result="$$(git rev-parse --verify "HEAD:$${result_path}" 2>/dev/null)" || { echo "result commit is missing $$result_path" >&2; exit 2; }; \
		working_result="$$(git hash-object -- "$$result_path" 2>/dev/null)" || { echo "working tree is missing $$result_path" >&2; exit 2; }; \
		test "$$working_result" = "$$head_result" || { echo "$$result_path differs from the result commit" >&2; exit 2; }; \
	done; \
	test -z "$$(git status --porcelain --untracked-files=all -- results)" || { echo "working results differ from the result commit" >&2; exit 2; }

collect: assert-frozen-checkout
	uv run python scripts/collect_frozen.py --config configs/protocol_v0.1.json --output artifacts/collection-v0.1.1 --workers 24 --run-source-commit "$$(git rev-parse 'protocol-v0.1.1^{commit}')"

compare-collectors: assert-frozen-source
	@explanations="$${COLLECTOR_COMPARISON_EXPLANATIONS-}"; \
	if test -z "$$explanations"; then explanations='{}'; fi; \
	uv run python scripts/compare_collectors.py generate --old-dir artifacts/collection-v0.1 --new-dir artifacts/collection-v0.1.1 --failure-audit audits/formal_freeze_failures_v0.1.json --output artifacts/collection-v0.1.1/collector_comparison_v0.1.1.json --explanations "$$explanations"

freeze-labels: assert-frozen-checkout compare-collectors
	@test -n "$$POLYGON_RPC_URL" || (echo "POLYGON_RPC_URL is required" >&2; exit 2)
	@uv run python scripts/verify_chain.py --protocol configs/protocol_v0.1.json --input artifacts/collection-v0.1.1/candidate_rows_v0.1.csv.gz --collector-manifest artifacts/collection-v0.1.1/manifest.json --collector-comparison artifacts/collection-v0.1.1/collector_comparison_v0.1.1.json --failure-audit audits/formal_freeze_failures_v0.1.json --rpc-url "$$POLYGON_RPC_URL" --data-dir data --run-source-commit "$$(git rev-parse 'protocol-v0.1.1^{commit}')"

audit-local-snapshot: assert-frozen-source
	uv run python scripts/audit_local_snapshot.py --collection-dir artifacts/collection-v0.1.1 --data-dir data --expected-source-commit "$$(git rev-parse 'protocol-v0.1.1^{commit}')"
	$(MAKE) verify-collector-comparison

verify-collector-comparison:
	uv run python scripts/compare_collectors.py verify-public --report data/collector_comparison_v0.1.1.json --failure-audit audits/formal_freeze_failures_v0.1.json --collector-manifest data/collector_manifest_v0.1.json --chain-manifest data/manifest_v0.1.json

verify-evidence: assert-protocol-code
	$(MAKE) verify-collector-comparison
	uv run python scripts/evaluate_frozen.py --validate-only

score-frozen: assert-score-ready verify-evidence
	uv run python scripts/evaluate_frozen.py

render-results:
	uv run python scripts/render_results.py

verify-frozen: assert-protocol-code assert-frozen-evidence verify-collector-comparison
	uv run python scripts/evaluate_frozen.py --verify results/frozen_v0.1/results.json
	uv run python scripts/render_results.py --verify

verify-api-snapshot:
	uv run python scripts/evaluate_api_snapshot.py --validate-only

score-api-snapshot: verify-api-snapshot
	uv run python scripts/evaluate_api_snapshot.py

render-api-snapshot:
	uv run python scripts/render_results.py --input results/gamma_snapshot_v0.2/results.json --output-dir results/gamma_snapshot_v0.2 --results-markdown RESULTS.md

verify-api-release: verify-api-snapshot
	shasum -a 256 -c SHA256SUMS
	uv run python scripts/evaluate_api_snapshot.py --verify
	uv run python scripts/render_results.py --input results/gamma_snapshot_v0.2/results.json --output-dir results/gamma_snapshot_v0.2 --results-markdown RESULTS.md --verify
