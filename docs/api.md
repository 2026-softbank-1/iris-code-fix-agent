# Implemented internal repair API

This document describes the candidate-generation MVP. [code-repair-design.md](code-repair-design.md) distinguishes the current implementation from proposed WAS repair jobs and isolated verification. A separate [coordinator](auto-repair.md) implements repair-branch and draft-PR publication.

All repair endpoints require `X-API-Key`, compared against the configured `API_KEY` secret. Run behind an internal TLS/network boundary. Request contracts reject unexpected fields. `GET /healthz` is an unauthenticated readiness endpoint. Validation errors include field locations and error types without echoing rejected input or signed URLs.

## POST /internal/repairs

The synchronous request waits for candidate generation. Send `Idempotency-Key` equal to the body's `requestId`; reuse the same ID only with the same logical payload. A refreshed `source.downloadUrl` is excluded from the semantic input digest; all pinned source hashes and other fields must remain identical. A completed matching request returns its stored result without another model call. Conflicting input or an already running/uncertain attempt must not be retried as a new model call under that ID. The service preserves outcome records on disk before returning.

Top-level fields:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | `iris.repair-request.v1` |
| `requestId` | Stable request identifier |
| `scope` | Positive integer `serviceId`, `deploymentId`, `diagnosisId` |
| `diagnosisResult` | Entire original `diagnosis-result.v3`; preserve its snake_case field names |
| `planIds` | Selected original remediation plan IDs |
| `source` | `repositoryId`, 40-character `baseCommitSha`, repository-relative `rootDirectory`, HTTPS `downloadUrl`, expected `archiveSha256` and canonical `manifestSha256` |
| `policy` | `allowedPaths`, `protectedPaths`, `maxChangedFiles` (at most 5), `maxChangedBytes` (at most 65536), timezone-aware `deadline`, positive `maxCostUsd` |

The server combines request bounds with configured concurrency, token, cost and model-time limits. Source archive and manifest hashes bind the snapshot; updates also require an exact file preimage hash and unique text match. Paths and protected files are enforced before artifacts are sealed. Source evidence and logs are data, never trusted execution instructions.

The internal model context separates editable `files` from read-only `referenceFiles`. Known dependency/runtime/build manifests inside `rootDirectory` may be supplied as references even outside `allowedPaths`; `allowedPaths` remains the write allowlist. Explicit `protectedPaths`, built-in protection (including `.env*`), and secret detection still exclude these files. Reference content is capped at 24,000 bytes within the shared 180,000-byte source budget. Reference files never become eligible update/create targets. Ancestor workspace files outside the service root are not exposed. Selected plan targets take priority over other files. See the [prompt and architecture review](code-repair-design.md#프롬프트와-현재-설계-점검-2026-10-03).

Plans containing non-code changes return `configuration_required` before source download/model invocation. For code plans the model can identify an external configuration prerequisite, request specific missing evidence, or propose an allowed repository-owned configuration edit. `checksRequired` contains advisory verification descriptions, never trusted shell commands or completed check results. Human-facing model summaries, limitations and checks are requested in Korean.

The completed candidate response includes a candidate outcome and artifact references. `candidate_ready` is a proposed edit, with verification `status: not_run` and `owner: was`. Other model outcomes can request more evidence, identify configuration work, or report no change. They do not establish build success, runtime correctness, PR creation or deployment success.

### Attempts are single-use

Every terminal record, including `FAILED`, is kept under its `requestId`. Resending the same ID returns that record with 409 and never repeats a model call. A transient failure such as an expired source URL, `MODEL_RATE_LIMITED` or `MODEL_PROVIDER_ERROR` therefore needs a new `requestId` (a new attempt). The semantic digest includes `policy.deadline`, so a retry that changes the deadline is a different request.

When the model call completed but its proposal could not be applied (for example `AMBIGUOUS_EDIT` or `PREIMAGE_MISMATCH`), the raw proposal and usage are written to `proposal.json` under the request's private results directory before it is applied. It is not served by the API; operators and WAS recovery can inspect it without paying for the call again.

Source limits are reported as `SOURCE_TOO_LARGE` (more than 1000 files, a file over 5 MiB, or more than 20 MiB expanded). `SOURCE_UNSAFE` is reserved for traversal, links, special files and path collisions. Archives containing symlinks are rejected whole.

## GET /internal/repairs/{requestId}

Returns the persistent request record and its result or error when available. Record states are `RUNNING`, `SUCCEEDED`, `FAILED`, or `UNKNOWN_OUTCOME`. `SUCCEEDED` means generation finished, including non-candidate model outcomes. It is distinct from verification success.

After restart or an uncertain provider outcome, `UNKNOWN_OUTCOME` prevents blind automatic retries and duplicate model cost. WAS must inspect the stored outcome and explicitly choose a new attempt when appropriate. Download URLs and provider credentials are not persisted in request records.

## GET /internal/repairs/{requestId}/artifacts/{artifactName}

Authenticated download of a completed request's `patch.diff`, `changes.json`, or `manifest.json`. Artifact names are restricted to this set. `changes.json` is a schema-tagged wrapper whose `files` include Base64 contents; `manifest.json` records metadata and hashes without file contents. The candidate digest seals the exact object `{"changes": ..., "patch": ..., "manifestSha256": ...}` using the shared canonical JSON encoding. WAS should verify the returned digests, reconstruct the candidate against the frozen base, and perform its own isolated verification before publishing.

## Integration boundary

The agent has no GitHub publishing credentials and no source-execution capability. WAS owns retained source snapshots, repair orchestration, validation profiles and receipts, Git branch/PR operations, and deployment tracking. The MVP has no automatic provider retry after uncertain calls. SQLite and filesystem locks support single-host process coordination on local storage; network filesystems and distributed replicas are unsupported. `FIX_MAX_CONCURRENT` applies per process.
