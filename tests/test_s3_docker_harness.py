import importlib.util
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "cloud_harness", ROOT / "scripts/was_cloud_evaluate.py"
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


async def test_cloud_harness_checks_failed_baseline_pinned_redeployment_and_public_health(
    tmp_path, monkeypatch
):
    cases = [
        {
            "case": name,
            "fixtureBranch": "broken-" + name,
            "repairBranch": "fixed-" + name,
            "pushedCommit": digit * 40,
        }
        for name, digit in [("code", "a"), ("docker", "b")]
    ]
    (tmp_path / "report.json").write_text(
        json.dumps({"runId": "owned", "cases": cases})
    )
    (tmp_path / "was-token.json").write_text(
        json.dumps({"accessToken": "private-token"})
    )
    services = []
    deployments = {}
    submitted = []
    transient_reads = []

    def backend(request):
        assert request.headers["authorization"] == "Bearer private-token"
        path = request.url.path
        if path == "/api/v1/projects":
            result = {"id": 6}
        elif path.endswith("/services"):
            service = len(services) + 15
            services.append(service)
            result = {"id": service}
        elif request.method == "PATCH":
            result = {"id": int(path.split("/")[-1])}
        elif path.endswith("/deployments"):
            body = json.loads(request.content)
            submitted.append(body)
            number = len(deployments) + 26
            if body["triggerType"] == "REDEPLOY":
                source = deployments[body["sourceDeploymentId"]]["sourceSha"]
            else:
                source = body.get("sourceSha", "c" * 40)
            result = {
                "id": number,
                "status": "FAILED"
                if "sourceSha" not in body and body["triggerType"] == "MANUAL"
                else "SUCCEEDED",
                "sourceSha": source,
            }
            if result["status"] == "FAILED":
                result["failureCode"] = "BUILD_FAILED"
            deployments[number] = result
        elif path.endswith("/domains"):
            result = [{"url": "https://owned.example", "isConnected": True}]
        else:
            if not transient_reads:
                transient_reads.append(path)
                return httpx.Response(504, text="Gateway timeout")
            result = deployments[int(path.split("/")[-1])]
        return httpx.Response(200, json={"data": result})

    def public(request):
        assert "authorization" not in request.headers
        result = (
            {"status": "ok"}
            if request.url.path == "/health"
            else {"result": int(request.url.params["a"]) + int(request.url.params["b"])}
        )
        return httpx.Response(200, json=result)

    original_client = httpx.AsyncClient

    def client(**kwargs):
        handler = backend if "base_url" in kwargs else public
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(harness.httpx, "AsyncClient", client)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(harness.asyncio, "sleep", no_sleep)
    report = await harness.evaluate(tmp_path, "https://was.example")
    assert report["allPassed"]
    assert len(submitted) == 6
    assert len(transient_reads) == 1
    assert [body["triggerType"] for body in submitted].count("REDEPLOY") == 2
    assert all(row["publicChecks"][0]["assertions"] == 5 for row in report["cases"])
    assert "private-token" not in (tmp_path / "cloud-report.json").read_text()
