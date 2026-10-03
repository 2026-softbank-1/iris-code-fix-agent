# IRIS Code Fix Agent

Python 3.11 service that turns a frozen source snapshot and an original `diagnosis-result.v3` into a bounded repair candidate. The default model is `gpt-6.1-sol` with medium reasoning. The candidate API never executes downloaded source or pushes Git branches. A separate preauthorized coordinator can store candidates in S3, publish a repair branch and open a draft PR; see [repair coordination](docs/auto-repair.md).

The implemented MVP generates candidates synchronously, persists request outcomes, and serves sealed artifacts through authenticated endpoints. `candidate_ready` means a proposed change exists. Verification is `not_run`, owned by WAS; it does not indicate a successful build or deployment. The coordinator stops at `PR_OPENED` for review, preserving the service branch. Candidate validation, PR merge and deployment remain separate steps.

## Run locally

```sh
uv sync --extra dev
cp .env.example .env
# Configure the secrets, exact trusted source hosts, and verified model prices.
set -a
. ./.env
set +a
uv run iris-code-fix-agent
```

`API_KEY` must contain at least 32 non-whitespace ASCII characters. `OPENAI_API_KEY` and both price settings are required for live model use; prices are supplied by the operator, not guessed. Source downloads require HTTPS and an exact hostname in `FIX_ALLOWED_SOURCE_HOSTS` (default `s3.amazonaws.com`). Redirects and unsafe archives are rejected. Set the list to the hostname of the actual trusted bucket; signed download URLs are not persisted.

`FIX_DATA_DIR` stores request records and artifacts. Keep it on persistent storage. A process restart treats interrupted generation as `UNKNOWN_OUTCOME`: it does not automatically repeat a potentially charged model call. SQLite receipts and filesystem locks coordinate requests across processes on one host. Use local persistent storage; network filesystems and distributed replicas are unsupported. The concurrency cap is per process, so run one API process when a single global cap is required.

## API and local fixture

See [the API contract](docs/api.md) for request fields, idempotency, results and recovery. `examples/make_request.py` creates a deliberately broken Python source archive, actual archive/manifest digests, a request, and a fake-provider proposal without calling a model:

```sh
uv run python examples/make_request.py --download-url https://s3.amazonaws.com/example-bucket/source.tar
```

Upload the generated archive to your allowed HTTPS source host before using its request with the running API. The example URL is a placeholder. The fake proposal is an offline fixture; the production endpoint uses its configured provider.

```sh
curl -sS -X POST http://localhost:8000/internal/repairs \
  -H "X-API-Key: $API_KEY" -H 'Idempotency-Key: example-repair-001' \
  -H 'Content-Type: application/json' --data-binary @examples/generated/request.json
```

## Checks and container

[구현 검증 기록](docs/validation.md)에 테스트 범위와 실제 모델·WAS 연동의 남은 검증을 정리했습니다.

```sh
uv run ruff check .
uv run pytest
docker build -t iris-code-fix-agent .
docker run --rm -p 8000:8000 --env-file .env -v iris-fix-data:/data iris-code-fix-agent
```

The container runs as UID 10001 and persists state under `/data`; bind-mounted directories must be writable by that user. Keep the internal API behind your service network and provide the same authentication header for artifact downloads.
