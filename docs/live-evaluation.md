# Live evaluation evidence

On 2026-10-03, actual `gpt-6.1-sol` Responses calls passed the standalone repair evaluation and the WAS integration evaluation. The checked-in [evaluation-summary.json](evaluation-summary.json) contains a deliberately limited summary. Local raw reports and receipts remain under ignored `artifacts/`; they are not published with this document.

| Evaluation | Result | Real model calls | Estimated usage cost (USD) |
| --- | --- | ---: | ---: |
| Standalone syntax, inclusive interval behavior, unresolved symbol, configuration | 4/4 passed | 3 | 0.012462 |
| WAS integration with isolated PostgreSQL: syntax and configuration | 2/2 passed | 1 | 0.004620 |
| Earlier WAS attempt using a shared test database | Identifier collision; superseded by isolated run | 1 | 0.004650 |
| Latest WAS main rebase: syntax and configuration | 2/2 passed | 1 | 0.004590 |
| Total observed calls | | 6 | 0.026322 |

Costs are estimates calculated from provider token usage and explicitly configured price rates, not invoice reconciliation. The standalone harness reserved at most $1 per attempt and $3 in aggregate. Each WAS evaluation allowed one attempt with a $1 cap. The shared-database attempt is included in the accounting; its failure was not treated as successful integration evidence.

## What ran

The standalone harness used the real authenticated fix API through ASGI HTTP transport and the real OpenAI runner. A controlled HTTPS source transport served complete, pinned, author-created tar archives. Each code baseline failed an independent AST check; each returned candidate passed four arithmetic assertions. The configuration case returned `configuration_required` without a model call.

The successful WAS run used the actual WAS main app, JWT Bearer authentication, PostgreSQL database, repositories, repair service, handoff service, and HTTP repair clients. The repair clients contacted the real fix API through ASGI HTTP transport; the fix runner contacted OpenAI. AWS snapshot presigning and source HTTPS downloads were controlled fixtures. Session tokens were created locally for persisted fixture users; GitHub login and real AWS infrastructure were outside this evaluation.

The WAS evaluation used main baseline `5a7e10cc68c9a7247e10be80aa6aaedb621a9d08` plus this PR’s uncommitted repair integration changes. User POST returned `202` with `RUNNING`; subsequent GET returned persisted `SUCCEEDED`. Cross-service input digests agreed, all three candidate artifacts matched their SHA-256 and byte length, and the failed deployment remained `FAILED`. Configuration bypass and cached replay produced no additional model call. Anonymous access returned `401`; another owner received `404` for submission, lookup, and artifact download. Deliberate corruption of an owned local artifact was rejected with `502` through the WAS endpoint.

After rebasing onto latest main `460f4cb`, the complete WAS suite passed **1,178 tests**, with Ruff/format and strict mypy. The latest paid evaluation passed on integration commit `8c329b0cc58acf821e949f52746a36459ae43ca3`; the original main-based run below remains historical evidence. Migration upgrade, schema comparison/check, downgrade, and re-upgrade completed successfully. These checks are complementary to the model evaluation.

## Limits

These are small synthetic fixtures. The independent checker parses Python AST and evaluates only a narrow arithmetic whitelist; it never runs arbitrary candidate code. It establishes fixture syntax and behavior, not production runtime, dependency, build, deployment, or service health validation. The repair result continues to report `validation.status=not_run`, with validation owned by WAS. No user repository was pushed and no deployment was authorized.

ASGI exercises actual application routes and clients in process. It does not demonstrate a deployed network path between WAS and the fix service, real S3 delivery, or a complete production WAS workflow. Separate process health checks do not change that scope.

## Reproducing the checks

Use the existing local `.env` credential without copying values into reports or commits. The local `OPENAI_API` credential name was adapted to the runner's `OPENAI_API_KEY` name; only the variable names are recorded here. Supply the internal `API_KEY` and explicit input/output price rates. Do not print environment contents.

From the fix repository with the required environment already loaded:

```sh
.venv/bin/python scripts/live_evaluate.py \
  --output artifacts/live-evaluation-UNIQUE \
  --duration 120 --per-call-budget 1 --total-budget 3
```

For WAS integration, use a **fresh, isolated, migrated PostgreSQL database** in `TEST_DATABASE_URL`, a WAS-compatible Python environment, and the fix repository's `src` directory on `PYTHONPATH`. Database credentials belong in the environment, not command arguments. Run the offline integration test before authorizing a paid attempt:

```sh
WAS_EVALUATION_ROOT=/path/to/was \
  /path/to/was/.venv/bin/python -m pytest tests/test_was_harness.py -q

/path/to/was/.venv/bin/python scripts/was_evaluate.py \
  --was-root /path/to/was \
  --output artifacts/was-evaluation-UNIQUE
```

The harnesses do not retry ambiguous provider outcomes. Preserve their receipts and investigate before starting a new paid attempt. The WAS harness preserves its own fixture SQL records for review and intentionally corrupts only its own local test artifact after verifying it.

Private local evidence:

- `artifacts/live-evaluation-20261003T032746Z/report.json`
- `artifacts/was-evaluation-isolated-20261003T035451Z/report.json`

Latest evidence: `artifacts/was-evaluation-latest-20261003T044738Z/report.json`. Companion PRs: [fix agent](https://github.com/2026-softbank-1/iris-code-fix-agent/pull/1), [WAS](https://github.com/2026-softbank-1/iris-was/pull/56).
