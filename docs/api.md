# Implemented internal repair API

This document describes the candidate-generation MVP. [code-repair-design.md](code-repair-design.md) describes proposed WAS integration and broader verification/publishing behavior; those features are not implemented here.

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

The completed candidate response includes a candidate outcome and artifact references. `candidate_ready` is a proposed edit, with verification `status: not_run` and `owner: was`. Other model outcomes can request more evidence, identify configuration work, or report no change. They do not establish build success, runtime correctness, PR creation or deployment success.

## GET /internal/repairs/{requestId}

Returns the persistent request record and its result or error when available. Record states are `RUNNING`, `SUCCEEDED`, `FAILED`, or `UNKNOWN_OUTCOME`. `SUCCEEDED` means generation finished, including non-candidate model outcomes. It is distinct from verification success.

After restart or an uncertain provider outcome, `UNKNOWN_OUTCOME` prevents blind automatic retries and duplicate model cost. WAS must inspect the stored outcome and explicitly choose a new attempt when appropriate. Download URLs and provider credentials are not persisted in request records.

## GET /internal/repairs/{requestId}/artifacts/{artifactName}

Authenticated download of a completed request's `patch.diff`, `changes.json`, or `manifest.json`. Artifact names are restricted to this set. `changes.json` is a schema-tagged wrapper whose `files` include Base64 contents; `manifest.json` records metadata and hashes without file contents. The candidate digest seals the exact object `{"changes": ..., "patch": ..., "manifestSha256": ...}` using the shared canonical JSON encoding. WAS should verify the returned digests, reconstruct the candidate against the frozen base, and perform its own isolated verification before publishing.

## Integration boundary

The agent has no GitHub publishing credentials and no source-execution capability. WAS owns retained source snapshots, repair orchestration, validation profiles and receipts, Git branch/PR operations, and deployment tracking. The MVP has no automatic provider retry after uncertain calls. SQLite and filesystem locks support single-host process coordination on local storage; network filesystems and distributed replicas are unsupported. `FIX_MAX_CONCURRENT` applies per process.
