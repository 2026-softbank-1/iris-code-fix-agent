"""Cross-repository evaluation with real WAS routes, auth, SQL and repair clients.

Use an isolated, migrated PostgreSQL database via TEST_DATABASE_URL. Only owned
fixture rows are added; no schema mutation, deployment, or Git publication occurs.
AWS snapshot presigning and source HTTPS are controlled fixtures. Both application
APIs use ASGI HTTP transport. The provider call is real unless runner is injected.
"""

import argparse
import asyncio
import base64
import copy
import json
import os
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from live_evaluate import BoundedRunner, check_source, make_fixture

from iris_code_fix_agent.api import Settings, create_app
from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.configuration import load_environment, openai_api_key
from iris_code_fix_agent.runner import OpenAIRepairRunner, RunnerConfig


def full_diagnosis(case, run_id):
    archive, request = make_fixture(case, run_id, duration=180)
    raw = copy.deepcopy(request["diagnosisResult"])
    observation = raw["analysis"]["summary"]
    raw["evidence"][0].update(
        source_id="fixture-build",
        stage="build",
        stream="stderr",
        timestamp="2026-10-03T03:00:00Z",
        event_line=1,
    )
    raw["analysis"]["observations"] = [
        {
            "id": "O1",
            "kind": "failure",
            "text": observation,
            "evidence_ids": ["EV000001"],
        }
    ]
    raw["analysis"]["hypotheses"] = [
        {
            "id": "H1",
            "category": "configuration" if case == "configuration" else "build_compile",
            "support_level": "direct",
            "statement": observation,
            "observation_ids": ["O1"],
            "evidence_ids": ["EV000001"],
            "counter_evidence_ids": [],
            "uncertainty": "Synthetic owned fixture only.",
        }
    ]
    plan = raw["analysis"]["remediation"]["plans"][0]
    plan.update(
        hypothesis_ids=["H1"],
        apply_when=[observation],
        verification=[
            {
                "instruction": "Parse and check arithmetic behavior independently.",
                "expected_result": "Fixture checks pass.",
            }
        ],
        rollback=["Restore the pinned original file."],
        risks=["Production behavior is not validated."],
    )
    plan["changes"][0].update(
        target_known=True,
        language="dotenv" if case == "configuration" else "python",
        snippet_kind="template",
        snippet="",
        placeholders=[],
    )
    return archive, raw


def write_report(output, report):
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n"
    )


async def evaluate(was_root, output, runner, api_key, *, database_url=None):
    database_url = database_url or os.environ.get("TEST_DATABASE_URL")
    if not database_url or not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError(
            "An isolated migrated PostgreSQL TEST_DATABASE_URL is required"
        )
    os.environ["DATABASE_URL"] = database_url
    session_secret = secrets.token_urlsafe(48)
    os.environ["SESSION_SECRET"] = session_secret
    os.environ["LOG_LEVEL"] = "WARNING"
    sys.path.insert(0, str(was_root.resolve()))
    from app import dependencies
    from app.clients.repair_agent_client import (
        HttpRepairAgentClient,
        HttpRepairSourceClient,
    )
    from app.core.config import get_settings
    from app.core.database import get_engine, get_session_factory
    from app.core.security import create_session_token
    from app.enums import (
        BuildStatus,
        DeploymentStatus,
        DeploymentTrigger,
        DiagnosisStatus,
        Environment,
        FailureCode,
    )
    from app.main import app
    from app.models.build import Build
    from app.models.deployment_diagnosis import DeploymentDiagnosis
    from app.models.deployment_repair import DeploymentRepair
    from app.models.deployment_request import DeploymentRequest
    from app.models.project import Project
    from app.models.service import Service
    from app.models.user import GithubInstallation, User
    from app.repositories.build_repository import BuildRepository
    from app.repositories.deployment_diagnosis_repository import (
        DeploymentDiagnosisRepository,
    )
    from app.repositories.deployment_repair_repository import DeploymentRepairRepository
    from app.repositories.deployment_request_repository import (
        DeploymentRequestRepository,
    )
    from app.repositories.service_repository import ServiceRepository
    from app.schemas.diagnosis import AgentDiagnosisResult
    from app.schemas.repair import RepairResponse
    from app.services.repair_handoff_service import RepairHandoffService
    from app.services.repair_service import RepairService
    from sqlalchemy import select

    get_settings.cache_clear()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    factory = get_session_factory()
    run_id = uuid.uuid4().hex[:12]
    fixtures = {
        case: full_diagnosis(case, run_id) for case in ("syntax", "configuration")
    }
    for _, raw in fixtures.values():
        AgentDiagnosisResult.model_validate(raw)  # Normal diagnosis response contract.
    seeded = {}
    async with factory() as session:
        owner = User(github_id=int(run_id, 16), login=f"eval-{run_id}")
        outsider = User(github_id=int(run_id, 16) + 1, login=f"outside-{run_id}")
        installation = GithubInstallation(
            installation_id=int(run_id, 16),
            account_login="evaluation",
            account_type="User",
        )
        session.add_all([owner, outsider, installation])
        await session.flush()
        project = Project(name=f"eval-{run_id}", owner_id=owner.id)
        session.add(project)
        await session.flush()
        service = Service(
            project_id=project.id,
            name="owned-fixture",
            source_repository_url="https://github.com/evaluation/owned-fixture",
            github_installation_id=installation.id,
            source_branch="main",
            root_directory=None,
            is_auto_deploy=False,
        )
        session.add(service)
        await session.flush()
        for case, (_, raw) in fixtures.items():
            deployment = DeploymentRequest(
                service_id=service.id,
                environment=Environment.PROD,
                source_sha="a" * 40,
                trigger_type=DeploymentTrigger.MANUAL,
                idempotency_key=f"eval-{run_id}-{case}",
                requested_by=owner.id,
                status=DeploymentStatus.FAILED,
                failure_code=FailureCode.BUILD_FAILED,
            )
            session.add(deployment)
            await session.flush()
            raw["scope"] = raw["backend_context"] = {
                "service_id": service.id,
                "deployment_id": deployment.id,
            }
            diagnosis = DeploymentDiagnosis(
                deployment_request_id=deployment.id,
                requested_by=owner.id,
                status=DiagnosisStatus.SUCCEEDED,
                result=raw,
            )
            build = Build(
                deployment_request_id=deployment.id,
                status=BuildStatus.FAILED,
                source_sha="a" * 40,
                codebuild_build_id=f"eval-{run_id}-{case}",
            )
            session.add_all([diagnosis, build])
            await session.flush()
            seeded[case] = {
                "deploymentId": deployment.id,
                "diagnosisId": diagnosis.id,
                "buildId": build.id,
            }
        await session.commit()
        owner_id, outsider_id, service_id = owner.id, outsider.id, service.id

    archives = {
        f"https://s3.amazonaws.com/evaluation/{values['buildId']}.tar": fixtures[case][
            0
        ]
        for case, values in seeded.items()
    }

    class ControlledSnapshot:
        async def presign_snapshot(self, build_id):
            return f"https://s3.amazonaws.com/evaluation/{build_id}.tar"

    downloads = []

    def source_download(request):
        downloads.append(1)
        return httpx.Response(200, content=archives[str(request.url)])

    git = await asyncio.create_subprocess_exec(
        "git",
        "rev-parse",
        "HEAD",
        cwd=was_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    git_stdout, _ = await git.communicate()
    if git.returncode:
        raise ValueError("WAS root is not a Git checkout")
    was_commit = git_stdout.decode().strip()
    bounded = BoundedRunner(runner, 1, 1)
    report = {
        "runId": run_id,
        "transport": "real WAS main app + JWT auth + PostgreSQL + repair services/HTTP clients; real fix API via ASGI; controlled AWS presign/source HTTPS; real provider unless runner injected",
        "wasCommit": was_commit,
        "seeded": seeded,
        "serviceId": service_id,
        "cases": [],
        "checks": {},
    }
    write_report(output, report)
    overrides = dict(app.dependency_overrides)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(source_download)
        ) as source_http:
            fixapp = create_app(
                Settings(
                    api_key,
                    output / "fix-receipts",
                    ("s3.amazonaws.com",),
                    max_concurrent=1,
                    max_cost_usd=1,
                ),
                bounded,
                source_http,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(fixapp), base_url="http://fix-evaluation"
            ) as fix_http:
                agent = HttpRepairAgentClient(
                    fix_http, "http://fix-evaluation", api_key, 180
                )
                source = HttpRepairSourceClient(source_http, ("s3.amazonaws.com",))
                handoff = RepairHandoffService(agent, source)

                def build_service(session):
                    return RepairService(
                        session,
                        ServiceRepository(session),
                        DeploymentRequestRepository(session),
                        BuildRepository(session),
                        DeploymentDiagnosisRepository(session),
                        DeploymentRepairRepository(session),
                        agent,
                        handoff,
                        ControlledSnapshot(),
                        max_cost_usd=1,
                        deadline_seconds=180,
                    )

                async def service_dependency():
                    async with factory() as session:
                        yield build_service(session)

                @asynccontextmanager
                async def open_service():
                    async with factory() as session:
                        yield build_service(session)

                app.dependency_overrides[dependencies.get_repair_service] = (
                    service_dependency
                )
                app.dependency_overrides[dependencies.get_repair_service_opener] = (
                    lambda: open_service
                )
                headers = {
                    "Authorization": "Bearer "
                    + create_session_token(
                        owner_id, session_secret, timedelta(minutes=10)
                    )
                }
                outsider_headers = {
                    "Authorization": "Bearer "
                    + create_session_token(
                        outsider_id, session_secret, timedelta(minutes=10)
                    )
                }
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://was-evaluation"
                ) as http:
                    report["healthHttpStatus"] = (
                        await http.get("/healthz")
                    ).status_code
                    for case, values in seeded.items():
                        path = f"/api/v1/services/{service_id}/deployments/{values['deploymentId']}/repairs"
                        body = {"diagnosisId": values["diagnosisId"], "planIds": ["R1"]}
                        post_headers = {
                            **headers,
                            "Idempotency-Key": f"eval-{run_id}-{case}",
                        }
                        if case == "syntax":
                            report["checks"]["unauthorizedHttpStatus"] = (
                                await http.post(
                                    path,
                                    json=body,
                                    headers={"Idempotency-Key": "unauthorized"},
                                )
                            ).status_code
                            report["checks"]["outsiderPostHttpStatus"] = (
                                await http.post(
                                    path,
                                    json=body,
                                    headers={
                                        **outsider_headers,
                                        "Idempotency-Key": "outsider",
                                    },
                                )
                            ).status_code
                        post = await http.post(path, json=body, headers=post_headers)
                        entry = {
                            "case": case,
                            "postHttpStatus": post.status_code,
                            "post": post.json(),
                        }
                        report["cases"].append(entry)
                        write_report(output, report)
                        if post.status_code != 202:
                            break
                        repair_id = post.json()["data"]["id"]
                        get_path = f"/api/v1/services/{service_id}/repairs/{repair_id}"
                        polled = await http.get(get_path, headers=headers)
                        data = polled.json()["data"]
                        RepairResponse.model_validate(data)
                        entry.update(
                            getHttpStatus=polled.status_code,
                            repair=data,
                            baseline=None
                            if case == "configuration"
                            else check_source(
                                case,
                                "def add(a, b)\n    return a + b\n",
                            ),
                        )
                        async with factory() as session:
                            row = (
                                await session.scalars(
                                    select(DeploymentRepair).where(
                                        DeploymentRepair.id == repair_id
                                    )
                                )
                            ).one()
                            deployment = await session.get(
                                DeploymentRequest, values["deploymentId"]
                            )
                            entry["sql"] = {
                                "status": row.status,
                                "deploymentStatus": deployment.status,
                                "inputDigest": row.input_digest,
                                "agentInputDigest": row.request_metadata.get(
                                    "agentInputDigest"
                                ),
                                "generationClaimed": row.generation_started_at
                                is not None,
                                "persistedResultMatches": row.result
                                == data.get("result"),
                                "crossContractDigestMatches": row.request_metadata.get(
                                    "agentInputDigest"
                                )
                                == (data.get("result") or {}).get("inputDigest"),
                            }
                            report["checks"]["signedSourceUrlNotPersisted"] = (
                                "downloadUrl" not in json.dumps(row.request_metadata)
                            )
                        replay = await http.post(path, json=body, headers=post_headers)
                        entry["replay"] = {
                            "httpStatus": replay.status_code,
                            "sameRepairId": replay.json().get("data", {}).get("id")
                            == repair_id,
                        }
                        result = data.get("result") or {}
                        if (
                            case == "syntax"
                            and result.get("status") == "candidate_ready"
                        ):
                            artifact_checks = []
                            candidate = None
                            for item in result["artifacts"]:
                                fetched = await http.get(item["url"], headers=headers)
                                artifact_checks.append(
                                    {
                                        "name": item["name"],
                                        "httpStatus": fetched.status_code,
                                        "digestMatches": sha256(fetched.content)
                                        == item["sha256"],
                                        "byteLengthMatches": len(fetched.content)
                                        == item["byteLength"],
                                    }
                                )
                                if item["name"] == "changes.json":
                                    candidate = base64.b64decode(
                                        fetched.json()["files"][0]["contentBase64"],
                                        validate=True,
                                    )
                            entry["artifacts"] = artifact_checks
                            entry["candidateCheck"] = check_source(case, candidate)
                            report["checks"]["outsiderGetHttpStatus"] = (
                                await http.get(get_path, headers=outsider_headers)
                            ).status_code
                            report["checks"]["outsiderArtifactHttpStatus"] = (
                                await http.get(
                                    result["artifacts"][0]["url"],
                                    headers=outsider_headers,
                                )
                            ).status_code
                            # Corrupt only our sealed local artifact, then verify both apps fail closed.
                            fixapp.state.store.artifact(
                                f"was-repair-{repair_id}", "patch.diff"
                            ).write_bytes(b"owned-fixture-tampering")
                            tampered_url = next(
                                item["url"]
                                for item in result["artifacts"]
                                if item["name"] == "patch.diff"
                            )
                            report["checks"]["tamperedArtifactHttpStatus"] = (
                                await http.get(tampered_url, headers=headers)
                            ).status_code
                        entry["passed"] = (
                            data.get("status") == "SUCCEEDED"
                            and entry["post"]["data"]["status"] == "RUNNING"
                            and entry["getHttpStatus"] == 200
                            and entry["replay"]["httpStatus"] == 200
                            and entry["sql"]["generationClaimed"]
                            and all(
                                item["httpStatus"] == 200
                                and item["digestMatches"]
                                and item["byteLengthMatches"]
                                for item in entry.get("artifacts", [])
                            )
                            and entry["sql"]["persistedResultMatches"]
                            and entry["sql"]["crossContractDigestMatches"]
                            and entry["sql"]["deploymentStatus"] == "FAILED"
                            and entry["replay"]["sameRepairId"]
                            and (
                                result.get("status") == "configuration_required"
                                if case == "configuration"
                                else bool(entry.get("candidateCheck", {}).get("passed"))
                            )
                        )
                        report.update(
                            modelCalls=bounded.calls,
                            reservedBudgetUsd=bounded.reserved,
                            sourceDownloads=len(downloads),
                        )
                        write_report(output, report)
                        if data.get("status") == "UNKNOWN_OUTCOME":
                            break
        checks = report["checks"]
        report["allPassed"] = (
            len(report["cases"]) == 2
            and all(item.get("passed") for item in report["cases"])
            and checks.get("unauthorizedHttpStatus") == 401
            and all(
                checks.get(key) == 404
                for key in (
                    "outsiderPostHttpStatus",
                    "outsiderGetHttpStatus",
                    "outsiderArtifactHttpStatus",
                )
            )
            and checks.get("tamperedArtifactHttpStatus", 200) >= 400
            and report.get("modelCalls") == 1
        )
        report["knownCostUsd"] = sum(
            (item.get("repair", {}).get("result") or {})
            .get("usage", {})
            .get("costUsd", 0)
            or 0
            for item in report["cases"]
            if (item.get("repair", {}).get("result") or {}).get("usage")
        )
        write_report(output, report)
        return report
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        await get_engine().dispose()


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--was-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = RunnerConfig(
        api_key=openai_api_key(),
        model=os.environ.get("FIX_MODEL", "gpt-6.1-sol"),
        reasoning_effort=os.environ.get("FIX_REASONING_EFFORT", "medium"),
        max_output_tokens=int(os.environ.get("FIX_MAX_OUTPUT_TOKENS", "4096")),
        timeout_seconds=min(
            150, float(os.environ.get("FIX_MODEL_TIMEOUT_SECONDS", "120"))
        ),
        input_price_per_million=float(os.environ["FIX_INPUT_PRICE_PER_MILLION"]),
        output_price_per_million=float(os.environ["FIX_OUTPUT_PRICE_PER_MILLION"]),
    )
    report = asyncio.run(
        evaluate(
            args.was_root,
            args.output,
            OpenAIRepairRunner(config),
            os.environ["API_KEY"],
        )
    )
    print(
        json.dumps(
            {
                "report": str(args.output / "report.json"),
                "allPassed": report["allPassed"],
                "modelCalls": report.get("modelCalls"),
            }
        )
    )
    raise SystemExit(0 if report["allPassed"] else 1)


if __name__ == "__main__":
    main()
