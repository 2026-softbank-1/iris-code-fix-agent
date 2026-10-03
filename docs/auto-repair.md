# Repair branches and draft pull requests

`iris-auto-repair` is a separate trusted coordinator. The candidate API only generates proposals; the coordinator holds separate WAS, GitHub and S3 credentials. Starting it authorizes creating a repair branch and a draft PR for the selected service. It preserves the original service branch and stops at review.

The coordinator uses upstream WAS deployment/diagnosis APIs and `GET /api/v1/services/{serviceId}/deployments/{deploymentId}/repair-context`. It selects the exact diagnosis ID returned by diagnosis creation and requests that original `diagnosis-result.v3`, service repository/branch, and a short-lived source snapshot. Install the updated WAS endpoint before running the worker.

One incident diagnoses the failed deployment, selects code-only plans and generates one candidate with a stable request ID. It checks sealed artifact hashes and the candidate manifest, then stores `patch.diff`, `changes.json`, `manifest.json` and patched `source.tar.gz` in a private S3 bucket. Content-addressed keys and SHA-256 checksums bind the stored bytes; no public ACL is added. S3 storage must finish before branch publication.

GitHub preimages are checked against the frozen failed source commit. The candidate commit uses that commit as its only parent and preserves other files through the original Git tree. Publication creates a deterministic `iris/repair/{requestId}` branch. An existing ref pointing to the same commit is reused; a different commit conflicts. The coordinator opens a **draft PR** from that repair branch to the original service branch, or reuses an existing open PR with the exact head/base pair. It never updates the service branch, merges a PR, or submits a redeployment.

`PR_OPENED` is a terminal review state. It does not mean the defect is resolved or the deployment succeeded. Validation remains `not_run`; review and independent candidate validation are required before merge. Deployment after merge follows the service's ordinary deployment process. A future verified repair/deployment loop requires a separate implementation.

## Source pinning

`repair-context.source` should include the `archiveSha256` and `manifestSha256` that WAS froze when it stored the snapshot. With both present the coordinator verifies the downloaded archive and manifest against them (`sourcePinned: true` in the journal). Without them the archive can only vouch for itself: the coordinator still requires that the service branch head equals the failed commit and that every changed file's preimage matches GitHub's bytes at that commit, and records `sourcePinned: false`. Treat unpinned attempts as weaker evidence and have WAS supply the hashes. The manifest digest uses the canonical encoding in `source.manifest_digest`: sorted paths, content SHA-256, size, and mode `100755` when any executable bit is set, otherwise `100644`; WAS must compute the same value.

WAS implements this contract in `GET /api/v1/services/{serviceId}/deployments/{deploymentId}/repair-context?diagnosisId=` (iris-was branch `feat/repair-context`). It returns `409 DIAGNOSIS_NOT_SUCCEEDED` unless the diagnosis succeeded, `404` for a diagnosis that belongs to another deployment, and `409 SOURCE_SNAPSHOT_UNAVAILABLE` once the snapshot's 23-hour window has passed. WAS records `archiveSha256`/`manifestSha256` when it uploads a snapshot, so builds made before that migration return them as null and run unpinned. The hash rules above were cross-checked against `source.from_archive` in pinned mode.

## Credentials

Use a fine-grained GitHub token limited to the one service repository with only Contents (read/write) and Pull requests (write). A classic `ghp_` token spans every repository the owner can reach and the coordinator warns when it sees one. The generation API never receives GitHub, WAS or S3 credentials.

## Configuration and launch

Run the updated WAS endpoint and fix API. Configure these values plus normal AWS SDK credentials. GitHub credentials need read/write contents and pull-request permission for the service repository; WAS_TOKEN must belong to its owner. Credentials and presigned source URLs are not journaled.

```dotenv
FIX_S3_BUCKET=your-private-repair-bucket
FIX_BASE_URL=https://your-fix-service
WAS_BASE_URL=https://your-was-service
WAS_TOKEN=
GITHUB_TOKEN=
FIX_ALLOWED_SOURCE_HOSTS=your-source-bucket.s3.ap-northeast-2.amazonaws.com
```

Observe failed deployments and open one draft repair PR per incident:

```sh
uv run iris-auto-repair --watch --service-id 123 \
  --allowed-path 'src/**' --allowed-path 'app/**'
```

Run or resume a selected incident:

```sh
uv run iris-auto-repair --run-id repair-service123-deploy456 \
  --service-id 123 --deployment-id 456 --allowed-path 'src/**'
```

Defaults are one repair generation, 30 minutes for the entire incident and USD 1 maximum repair generation cost. WAS diagnosis has separate cost settings. Limits are persisted and cannot change when resuming a run. Watch mode skips incidents already in `PR_OPENED`; it does not reset their generation budget or treat their draft PR as a deployment success.

Keep FIX_DATA_DIR on persistent local storage. Journals contain candidate source/artifacts with mode 0600 and use a process lock. Use one watcher per service; distributed coordination is unsupported. Restart with the same command and directory after transient failures. Saved candidate bytes survive S3 failure without another generation call. The prepared commit SHA is saved before branch publication. A lost ref or PR response can be reconciled using the same repair branch and head/base PR lookup.

## Verification scope

Offline tests verify repair-branch-only publication, unchanged service head, draft PR creation/reuse, S3 failure/resume and checksums, persisted `PR_OPENED`, and absence of WAS deployment POSTs. HTTP/S3/GitHub dependencies are controlled fixtures. They perform no operational AWS upload, source push, PR creation or production deployment.
