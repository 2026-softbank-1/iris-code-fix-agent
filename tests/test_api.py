import ast
import base64
import copy
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from iris_code_fix_agent.api import Settings, create_app
from iris_code_fix_agent.canonical import semantic_digest, sha256
from iris_code_fix_agent.contracts import ModelProposal, RepairRequest
from iris_code_fix_agent.errors import RepairError
from iris_code_fix_agent.runner import RunnerError, RunnerResponse
from iris_code_fix_agent.store import ResultStore

API_KEY = "integration-secret-" + "x" * 32


class FakeRunner:
    def __init__(self, proposal, error=None):
        self.proposal = ModelProposal.model_validate(proposal)
        self.error = error
        self.calls = 0

    async def propose(self, context, max_cost_usd):
        self.calls += 1
        assert "downloadUrl" not in str(context)
        if self.error:
            raise self.error
        return RunnerResponse(
            self.proposal, {"inputTokens": 10, "outputTokens": 10}, "fake-offline"
        )


@pytest.fixture
def api_fixture(tmp_path, syntax_repair_fixture):
    archive, request, proposal = syntax_repair_fixture
    runner = FakeRunner(proposal)
    downloads = []

    def download(http_request):
        downloads.append(str(http_request.url))
        return httpx.Response(200, content=archive)

    source_client = httpx.AsyncClient(transport=httpx.MockTransport(download))
    store = ResultStore(tmp_path / "data")
    app = create_app(Settings(API_KEY, tmp_path / "data"), runner, source_client, store)
    return app, store, runner, downloads, request, source_client


def headers(request):
    return {"X-API-Key": API_KEY, "Idempotency-Key": request["requestId"]}


async def test_candidate_artifacts_and_exact_replay(api_fixture):
    app, store, runner, downloads, request, source_client = api_fixture
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "candidate_ready"
        assert result["validation"] == {"status": "not_run", "owner": "was"}
        assert result["repositoryPushAuthorized"] is False
        artifacts = {}
        for reference in result["artifacts"]:
            artifact = await client.get(reference["url"], headers=headers(request))
            assert artifact.status_code == 200
            assert sha256(artifact.content) == reference["sha256"]
            artifacts[reference["name"]] = artifact
        candidate = base64.b64decode(
            artifacts["changes.json"].json()["files"][0]["contentBase64"]
        )
        with pytest.raises(SyntaxError):
            ast.parse(candidate.replace(b"def add(a, b):", b"def add(a, b)"))
        ast.parse(candidate)  # Syntax parsing only; never executes the target code.
        assert b"def add(a, b):" in candidate
        record = (
            await client.get(
                f"/internal/repairs/{request['requestId']}", headers=headers(request)
            )
        ).json()
        assert record["status"] == "SUCCEEDED"
        assert "downloadUrl" not in str(record)
        assert "s3.amazonaws.com" not in str(record)
        request["source"]["downloadUrl"] += "?X-Amz-Signature=rotated"
        replay = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert replay.json() == result
        assert runner.calls == 1 and len(downloads) == 1
        assert store.get(request["requestId"])["result"] == result


async def test_auth_bad_input_and_idempotency_conflict(api_fixture):
    app, _store, runner, _downloads, request, source_client = api_fixture
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.post("/internal/repairs", json=request)).status_code == 401
        bad = copy.deepcopy(request)
        bad["source"]["downloadUrl"] = "http://forbidden.example?secret=hidden"
        invalid = await client.post("/internal/repairs", json=bad, headers=headers(bad))
        assert invalid.status_code == 422
        assert "hidden" not in invalid.text and "forbidden.example" not in invalid.text
        assert (
            await client.post(
                "/internal/repairs", json=request, headers=headers(request)
            )
        ).status_code == 200
        changed = copy.deepcopy(request)
        changed["source"]["baseCommitSha"] = "b" * 40
        conflict = await client.post(
            "/internal/repairs", json=changed, headers=headers(changed)
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        )
        assert runner.calls == 1


async def test_ambiguous_call_and_crash_receipt_never_replay(api_fixture):
    app, store, runner, _downloads, request, source_client = api_fixture
    runner.error = RunnerError("MODEL_CALL_UNKNOWN", "Call outcome unknown.")
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        assert (
            await client.post(
                "/internal/repairs", json=request, headers=headers(request)
            )
        ).status_code == 502
        assert (
            await client.post(
                "/internal/repairs", json=request, headers=headers(request)
            )
        ).status_code == 409
        assert store.get(request["requestId"])["status"] == "UNKNOWN_OUTCOME"
        assert runner.calls == 1
        crash = copy.deepcopy(request)
        crash["requestId"] = "crashed-attempt"
        store.begin(
            crash["requestId"], semantic_digest(RepairRequest.model_validate(crash))
        )
        recovered = await client.get(
            f"/internal/repairs/{crash['requestId']}", headers=headers(crash)
        )
        assert recovered.json()["status"] == "UNKNOWN_OUTCOME"
        assert (
            await client.post("/internal/repairs", json=crash, headers=headers(crash))
        ).status_code == 409
        assert runner.calls == 1


async def test_configuration_diagnosis_and_mismatched_source_do_not_call_model(
    api_fixture,
):
    app, _store, runner, downloads, request, source_client = api_fixture
    request["diagnosisResult"]["analysis"]["remediation"]["plans"][0]["changes"][0][
        "kind"
    ] = "configuration"
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.status_code == 200
        assert response.json()["status"] == "configuration_required"
        mismatch = copy.deepcopy(request)
        mismatch["requestId"] = "mismatch"
        mismatch["diagnosisResult"]["source_analysis"]["commit_sha"] = "b" * 40
        response = await client.post(
            "/internal/repairs", json=mismatch, headers=headers(mismatch)
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "DIAGNOSIS_SOURCE_MISMATCH"
        assert runner.calls == 0 and downloads == []


async def test_unexposed_edit_bad_reference_expired_and_tampered_artifact(api_fixture):
    app, store, runner, _downloads, request, source_client = api_fixture
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        runner.proposal.edits[0].path = "not-exposed.py"
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.json()["error"]["code"] == "UNEXPOSED_EDIT"
        request["requestId"] = "bad-reference"
        runner.proposal.edits[0].path = "app.py"
        runner.proposal.edits[0].evidence_ids = ["forged"]
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.json()["error"]["code"] == "INVALID_PROPOSAL_REFERENCE"
        assert store.get(request["requestId"])["result"]["usage"]["inputTokens"] == 10
        request["requestId"] = "expired"
        request["policy"]["deadline"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.json()["error"]["code"] == "DEADLINE_EXCEEDED"
        assert runner.calls == 2
        request["requestId"] = "valid-candidate"
        request["policy"]["deadline"] = (
            datetime.now(UTC) + timedelta(hours=1)
        ).isoformat()
        runner.proposal.edits[0].evidence_ids = ["L000001"]
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.status_code == 200
        store.artifact(request["requestId"], "patch.diff").write_bytes(b"tampered")
        response = await client.get(
            response.json()["artifacts"][0]["url"], headers=headers(request)
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "ARTIFACT_INTEGRITY_ERROR"


async def test_cross_request_locks_and_body_limit(api_fixture):
    app, store, _runner, _downloads, request, source_client = api_fixture
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        with store.lock(request["requestId"]):
            response = await client.post(
                "/internal/repairs", json=request, headers=headers(request)
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "REPAIR_IN_PROGRESS"
        response = await client.post(
            "/internal/repairs",
            content=b" " * 1_048_577,
            headers={**headers(request), "Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert store.get(request["requestId"]) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_analysis", [1]),
        ("scope", [1]),
        ("backend_context", [1]),
        ("evidence", ["not-an-evidence-object"]),
    ],
)
async def test_malformed_diagnosis_is_rejected_before_call(api_fixture, field, value):
    app, store, runner, downloads, request, source_client = api_fixture
    request["diagnosisResult"][field] = value
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_DIAGNOSIS"
        assert store.get(request["requestId"])["status"] == "FAILED"
        assert runner.calls == 0 and downloads == []


async def test_deadline_interrupts_slow_provider_without_replay(api_fixture):
    import asyncio

    app, store, runner, _downloads, request, source_client = api_fixture

    async def slow(context, max_cost_usd):
        runner.calls += 1
        await asyncio.sleep(1)

    runner.propose = slow
    request["policy"]["deadline"] = (
        datetime.now(UTC) + timedelta(milliseconds=100)
    ).isoformat()
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.status_code == 504
        assert store.get(request["requestId"])["status"] == "UNKNOWN_OUTCOME"
        assert (
            await client.post(
                "/internal/repairs", json=request, headers=headers(request)
            )
        ).status_code == 409
        assert runner.calls == 1


def test_store_isolation_and_empty_allowlist(tmp_path):
    store = ResultStore(tmp_path / "store")
    for request_id in ("Repair", "repair"):
        store.begin(request_id, "digest")
        store.write_artifacts(request_id, {"patch.diff": request_id.encode()})
        store.finish(request_id, "SUCCEEDED", {"artifacts": []})
    assert store.artifact("Repair", "patch.diff").read_bytes() == b"Repair"
    assert store.artifact("repair", "patch.diff").read_bytes() == b"repair"
    with pytest.raises(ValueError, match="allowlist"):
        Settings(API_KEY, allowed_source_hosts=())


async def test_large_diagnosis_does_not_discard_plan_and_guess(api_fixture):
    app, _store, runner, _downloads, request, source_client = api_fixture
    request["diagnosisResult"]["analysis"]["summary"] = "x" * 61_000
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
        assert response.status_code == 200
        assert response.json()["status"] == "needs_more_evidence"
        assert runner.calls == 0


async def test_model_proposal_is_kept_when_applying_it_fails(
    tmp_path, syntax_repair_fixture
):
    archive, request, proposal = syntax_repair_fixture
    proposal["edits"][0]["oldText"] = "text that is not in the source"
    runner = FakeRunner(proposal)
    source_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=archive))
    )
    store = ResultStore(tmp_path / "data")
    app = create_app(Settings(API_KEY, tmp_path / "data"), runner, source_client, store)
    async with (
        source_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        response = await client.post(
            "/internal/repairs", json=request, headers=headers(request)
        )
    assert response.status_code == 422
    assert store.get(request["requestId"])["status"] == "FAILED"
    saved = (
        store.root
        / "results"
        / sha256(request["requestId"].encode())
        / "proposal.json"
    )
    body = json.loads(saved.read_text())
    assert body["proposal"]["edits"][0]["oldText"] == "text that is not in the source"
    assert body["usage"]["outputTokens"] == 10
    # Proposals are for operators/WAS recovery, never an API download.
    assert (store.root / "results").exists()
    with pytest.raises(RepairError):
        store.artifact(request["requestId"], "proposal.json")
