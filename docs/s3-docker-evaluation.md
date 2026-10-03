# S3 retrieval, code/Docker repair and redeployment evaluation

The evaluation creates two author-owned HTTP service fixtures: a missing colon in
`add(a, b)` and a Dockerfile copying nonexistent `missing.py`. It proves the broken
baseline fails before checking a model candidate. `s3_docker_evaluate.py` uses the
real authenticated repair API through ASGI, the real configured model, actual S3
presigned HTTPS downloads, sealed artifact retrieval, and real GitHub repair
branches. Docker builds and executions are real and local.

The execution harness accepts only the exact narrow authored fixture repair. It
runs containers without outbound networking, host mounts or credentials, with a
read-only filesystem, nonroot user, dropped capabilities and resource limits.
It checks `/health` plus four addition cases, then retrieves the candidate archive
from S3, checks its size/digest/manifest and builds/runs it again. Replaying the
repair request and publishing the same repair ref must not produce another model
call or branch. `S3Artifacts.get` rejects wrong buckets/prefixes, bad sizes,
truncated/corrupt content, and pins S3 object versions when available.

`was_cloud_evaluate.py` additionally uses the existing production WAS deployment
API under the authenticated fixture owner's account. It creates a separate test
project/services, deploys the broken branches, then the already-pushed repair
commits, and requests `REDEPLOY` from the successful deployment. It checks public
HTTPS service health and arithmetic after redeployment. It journals deployment
IDs and uses stable idempotency keys for requests so polling can be resumed.
Neither script merges repair branches into an existing application's branch.

The production repair coordinator still stops at a draft PR. This evaluation's
WAS submission is performed by the explicitly invoked experiment harness; it does
not implement or prove an unattended diagnosis-to-repair-to-deployment loop.
The fixture diagnosis supplied to the repair API is author-created. Actual cloud
baseline build log excerpts independently confirm the same failures. The deployed
WAS source snapshot/build/deploy pipeline is tested separately from the in-process
repair API; a deployed fix-service network connection is not claimed.

## Reproduce

Use the repository's existing dotenv, an authenticated AWS profile, Docker and
GitHub credentials. No credential values belong in command arguments or reports.
AWS console login can establish a temporary profile with `aws login`; boto3's login
credential provider additionally needs `awscrt`, or an existing supported AWS SDK
credential method. Configure the profile region before SDK calls.

```sh
aws login --profile iris-evaluation --region ap-northeast-2
aws configure set region ap-northeast-2 --profile iris-evaluation
docker pull python:3.11-alpine
AWS_PROFILE=iris-evaluation .venv/bin/python scripts/s3_docker_evaluate.py \
  --output artifacts/aws-s3-docker-UNIQUE \
  --bucket PRIVATE_EVALUATION_BUCKET \
  --repository OWNER/DEDICATED_FIXTURE_REPOSITORY --publish
```

The bucket and fixture repository must already exist, and the IRIS GitHub App
must already be authorized to read that repository for the cloud stage. The
2026-10-03 run initially used the organization fix repository, which the signed-in
owner's IRIS installation could not access (403). The same sealed changes were
then published to `monitor5/iris-code-fix-e2e`, a new private fixture repository
covered by that owner's existing installation. This required no access expansion.
Its cloud publication also adds authored `iris.json` deployment metadata selecting
`/health`, the fixture's health endpoint, and the normal 300-second deadline.
New fixtures include this metadata from the start.

Obtain a WAS token using the normal `/auth/cli/sessions` browser approval flow.
Save `{"accessToken": "..."}` privately as `was-token.json` in the run directory;
do not commit or upload that file. Then run:

```sh
.venv/bin/python scripts/was_cloud_evaluate.py \
  --root artifacts/aws-s3-docker-UNIQUE
```

Report files contain IDs, artifact hashes, usage and validation results; no tokens
or presigned URLs. The initial build artifact bucket has a one-day lifecycle, so
this run also retained all eight sealed candidate artifacts in private, encrypted,
versioned bucket `iris-dev-code-fix-evaluation-187069338876-ap-northeast-2` under
`evaluations/cb0b72f35a7e/`. Local reports remain in ignored
`artifacts/aws-s3-docker-20261003-verified/`.

## Recorded results

- Both local broken builds failed; both model candidates built and served the five
  checks; both S3-retrieved archives built and served them again.
- Two real model calls in the successful run: estimated USD 0.011280. An earlier
  successful code generation followed by an overly short container readiness wait
  used an additional USD 0.005698; it is retained as a failed harness attempt.
- Actual AWS S3 upload/download, SHA-256 and byte-length verification, GitHub push,
  remote source reread, and same-request replay passed for both cases.
- Isolated repository checks: 104 passed, 1 skipped (the separately configured WAS
  database integration test). Ruff passed.
- Cloud experiment results are recorded in `cloud-report.json` and the summary
  below after the live deployment finishes.
