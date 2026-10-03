"""Bounded live Responses evaluation through the real authenticated ASGI API.

Only source HTTP is mocked. No WAS, repository push, deployment, or production
validation is claimed. Candidate Python is parsed and interpreted with a tiny
arithmetic whitelist; arbitrary model-generated code is never executed.
"""

import argparse
import ast
import asyncio
import base64
import io
import json
import os
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from iris_code_fix_agent.api import Settings, create_app
from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.configuration import load_environment, openai_api_key
from iris_code_fix_agent.runner import OpenAIRepairRunner, RunnerConfig
from iris_code_fix_agent.source import SourceFile, manifest_digest

CASES = {
    "syntax": (
        "def add(a, b)\n    return a + b\n",
        "SyntaxError: expected ':' at app.py:1",
        "Restore the missing function-definition colon, preserving addition.",
    ),
    "behavior": (
        "def inclusive_count(start, end):\n    return end - start\n",
        "inclusive_count(2, 5) returned 3, expected 4; (0, 0) returned 0, expected 1.",
        "Fix the inclusive integer interval count; preserve the public function signature.",
    ),
    "symbol": (
        "def add(a, b):\n    return a + missing_operand\n",
        "NameError: name 'missing_operand' is not defined at app.py:2; add(2, 3) must return 5.",
        "Use the supplied second argument to restore addition without adding globals.",
    ),
    "configuration": (
        "def add(a, b):\n    return a + b\n",
        "DATABASE_URL deployment variable is absent; no source change is appropriate.",
        "Supply DATABASE_URL in the deployment secret configuration.",
    ),
}


def make_fixture(case, run_id, duration=120, budget=1):
    source, observation, instruction = CASES[case]
    data = source.encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("app.py")
        member.size, member.mode, member.mtime = len(data), 0o644, 0
        archive.addfile(member, io.BytesIO(data))
    archive = buffer.getvalue()
    diagnosis = {
        "schema_version": "diagnosis-result.v3",
        "diagnosis_id": 1,
        "scope": {"service_id": 1, "deployment_id": 1},
        "backend_context": {"service_id": 1, "deployment_id": 1},
        "previous_diagnosis_id": None,
        "deployment_context": {"runtime": "python3"},
        "job_status": "succeeded",
        "remediation_execution": "not_executed",
        "error": None,
        "input_limitations": [],
        "execution": {"source": "author-created evaluation fixture"},
        "evidence": [{"id": "EV000001", "text": observation, "line_number": 2}],
        "analysis": {
            "analysis_status": "diagnosed",
            "summary": observation,
            "observations": [
                {"evidence_ids": ["EV000001"], "description": observation}
            ],
            "hypotheses": [],
            "next_checks": [],
            "missing_information": [],
            "limitations": ["Small synthetic fixture; not production diagnosis."],
            "remediation": {
                "status": "proposed",
                "reason": observation,
                "plans": [
                    {
                        "id": "R1",
                        "title": instruction,
                        "evidence_ids": ["EV000001"],
                        "changes": [
                            {
                                "kind": "configuration"
                                if case == "configuration"
                                else "code",
                                "target": "app.py",
                                "instruction": instruction,
                            }
                        ],
                    }
                ],
            },
        },
        "source_analysis": {
            "status": "analyzed",
            "reason": "Pinned complete fixture source.",
            "commit_sha": "a" * 40,
            "commit_verification": "caller_supplied",
            "requested_files": ["app.py"],
            "read_ranges": [{"path": "app.py", "start_line": 1, "end_line": 2}],
            "evidence": [],
            "findings": [],
            "limitations": [],
            "error": None,
            "archive_sha256": sha256(archive),
            "root_directory": ".",
        },
    }
    request = {
        "schemaVersion": "iris.repair-request.v1",
        "requestId": f"eval-{run_id}-{case}",
        "scope": {"serviceId": 1, "deploymentId": 1, "diagnosisId": 1},
        "diagnosisResult": diagnosis,
        "planIds": ["R1"],
        "source": {
            "repositoryId": "evaluation/owned-fixture",
            "baseCommitSha": "a" * 40,
            "rootDirectory": ".",
            "downloadUrl": f"https://s3.amazonaws.com/evaluation/{case}.tar",
            "archiveSha256": sha256(archive),
            "manifestSha256": manifest_digest({"app.py": SourceFile(data)}),
        },
        "policy": {
            "allowedPaths": ["app.py"],
            "protectedPaths": [],
            "maxChangedFiles": 1,
            "maxChangedBytes": 1024,
            "deadline": (datetime.now(UTC) + timedelta(seconds=duration)).isoformat(),
            "maxCostUsd": budget,
        },
    }
    return archive, request


def check_source(case, source):
    """Independent semantic checks with no eval/exec or subprocess target execution."""
    try:
        tree = ast.parse(source)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise ValueError("expected one plain function")
        function = tree.body[0]
        expected = "inclusive_count" if case == "behavior" else "add"
        names = [arg.arg for arg in function.args.args]
        expected_names = ["start", "end"] if case == "behavior" else ["a", "b"]
        if (
            function.name != expected
            or names != expected_names
            or function.decorator_list
            or function.args.defaults
            or function.args.kwonlyargs
            or function.args.vararg
            or function.args.kwarg
            or len(function.body) != 1
            or not isinstance(function.body[0], ast.Return)
        ):
            raise ValueError("unsupported function shape")

        def calculate(node, values):
            if isinstance(node, ast.Name) and node.id in values:
                return values[node.id]
            if isinstance(node, ast.Constant) and type(node.value) is int:
                return node.value
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
                left, right = (
                    calculate(node.left, values),
                    calculate(node.right, values),
                )
                return left + right if isinstance(node.op, ast.Add) else left - right
            raise ValueError("unsupported or unresolved expression")

        pairs = [(0, 0), (2, 5), (-3, 4), (10, 10)]
        for left, right in pairs:
            actual = calculate(function.body[0].value, dict(zip(names, (left, right))))
            expected_value = right - left + 1 if case == "behavior" else left + right
            if actual != expected_value:
                return {
                    "passed": False,
                    "checker": "AST arithmetic whitelist",
                    "reason": "behavior mismatch",
                    "actual": actual,
                    "expected": expected_value,
                }
        return {
            "passed": True,
            "checker": "AST arithmetic whitelist",
            "assertions": len(pairs),
        }
    except (SyntaxError, ValueError, UnicodeError):
        return {
            "passed": False,
            "checker": "AST arithmetic whitelist",
            "reason": "syntax, symbol, or supported-shape check failed",
        }


class BoundedRunner:
    def __init__(self, delegate, call_limit, total_budget):
        self.delegate, self.call_limit, self.total_budget = (
            delegate,
            call_limit,
            total_budget,
        )
        self.calls, self.reserved = 0, 0.0

    async def propose(self, context, max_cost_usd):
        if (
            self.calls >= self.call_limit
            or self.reserved + max_cost_usd > self.total_budget + 1e-9
        ):
            raise RuntimeError("Evaluation call or reserved budget exhausted")
        self.calls += 1
        self.reserved += max_cost_usd
        return await self.delegate.propose(context, max_cost_usd)


async def evaluate(
    output, runner, settings, *, cases=None, duration=120, budget=1, total_budget=3
):
    cases = list(cases or CASES)
    if (
        duration <= 0
        or duration > 180
        or budget <= 0
        or budget > 1
        or total_budget <= 0
        or total_budget > 3
    ):
        raise ValueError(
            "duration <=180 seconds, call budget <=$1 and total budget <=$3 required"
        )
    if len([case for case in cases if case != "configuration"]) > 3:
        raise ValueError("At most three paid attempts")
    output.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    fixtures = {case: make_fixture(case, run_id, duration, budget) for case in cases}
    archives = {
        request["source"]["downloadUrl"]: archive
        for archive, request in fixtures.values()
    }
    source_downloads = []

    def download(request):
        source_downloads.append(1)
        return httpx.Response(200, content=archives[str(request.url)])

    bounded = BoundedRunner(runner, 3, total_budget)
    report = {
        "runId": run_id,
        "transport": "real authenticated ASGI API; mocked source HTTPS; real model unless runner explicitly injected",
        "validationOwner": "standalone evaluator, not WAS",
        "cases": [],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(download)
    ) as source_client:
        app = create_app(settings, bounded, source_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://evaluation"
        ) as client:
            report["health"] = (await client.get("/healthz")).json()
            for case, (_, request) in fixtures.items():
                entry = {
                    "case": case,
                    "baseline": None
                    if case == "configuration"
                    else check_source(case, CASES[case][0]),
                }
                headers = {
                    "X-API-Key": settings.api_key,
                    "Idempotency-Key": request["requestId"],
                }
                response = await client.post(
                    "/internal/repairs", json=request, headers=headers
                )
                result = response.json()
                entry.update(
                    httpStatus=response.status_code, result=result, candidateCheck=None
                )
                if result.get("status") == "candidate_ready":
                    artifact = next(
                        item
                        for item in result["artifacts"]
                        if item["name"] == "changes.json"
                    )
                    fetched = await client.get(artifact["url"], headers=headers)
                    if (
                        fetched.status_code != 200
                        or sha256(fetched.content) != artifact["sha256"]
                    ):
                        raise ValueError("Artifact integrity failed")
                    changes = fetched.json()["files"]
                    if len(changes) != 1 or changes[0]["path"] != "app.py":
                        raise ValueError("Unexpected fixture changes")
                    candidate = base64.b64decode(
                        changes[0]["contentBase64"], validate=True
                    )
                    entry["candidateCheck"] = check_source(case, candidate)
                receipt = (
                    await client.get(
                        f"/internal/repairs/{request['requestId']}", headers=headers
                    )
                ).json()
                entry["receiptStatus"] = receipt.get("status")
                if not result.get("usage") and receipt.get("result"):
                    entry["failureUsage"] = receipt["result"].get("usage")
                entry["passed"] = (
                    result.get("status") == "configuration_required"
                    if case == "configuration"
                    else bool(
                        entry["candidateCheck"] and entry["candidateCheck"]["passed"]
                    )
                )
                report["cases"].append(entry)
                usages = [
                    item["result"].get("usage") or item.get("failureUsage")
                    for item in report["cases"]
                ]
                report.update(
                    knownCostUsd=sum(
                        usage.get("costUsd") or 0 for usage in usages if usage
                    ),
                    costMayBeUnknown=any(
                        item["receiptStatus"] == "UNKNOWN_OUTCOME"
                        for item in report["cases"]
                    ),
                    modelCalls=bounded.calls,
                    reservedBudgetUsd=bounded.reserved,
                    sourceDownloads=len(source_downloads),
                )
                (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
                if receipt.get("status") == "UNKNOWN_OUTCOME":
                    break  # Never retry or continue after uncertain provider billing.
    report["allPassed"] = len(report["cases"]) == len(cases) and all(
        item["passed"] for item in report["cases"]
    )
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--per-call-budget", type=float, default=1)
    parser.add_argument("--total-budget", type=float, default=3)
    args = parser.parse_args()
    config = RunnerConfig(
        api_key=openai_api_key(),
        model=os.environ.get("FIX_MODEL", "gpt-6.1-sol"),
        reasoning_effort=os.environ.get("FIX_REASONING_EFFORT", "medium"),
        max_output_tokens=int(os.environ.get("FIX_MAX_OUTPUT_TOKENS", "4096")),
        timeout_seconds=min(
            args.duration, float(os.environ.get("FIX_MODEL_TIMEOUT_SECONDS", "120"))
        ),
        input_price_per_million=float(os.environ["FIX_INPUT_PRICE_PER_MILLION"]),
        output_price_per_million=float(os.environ["FIX_OUTPUT_PRICE_PER_MILLION"]),
        cached_input_price_per_million=float(
            os.environ["FIX_CACHED_INPUT_PRICE_PER_MILLION"]
        )
        if os.environ.get("FIX_CACHED_INPUT_PRICE_PER_MILLION")
        else None,
    )
    settings = Settings(
        os.environ["API_KEY"],
        args.output / "receipts",
        ("s3.amazonaws.com",),
        max_concurrent=1,
        max_cost_usd=1,
    )
    report = asyncio.run(
        evaluate(
            args.output,
            OpenAIRepairRunner(config),
            settings,
            duration=args.duration,
            budget=args.per_call_budget,
            total_budget=args.total_budget,
        )
    )
    print(
        json.dumps(
            {
                "report": str(args.output / "report.json"),
                "modelCalls": report["modelCalls"],
                "allPassed": report["allPassed"],
            }
        )
    )
    raise SystemExit(0 if report["allPassed"] else 1)


if __name__ == "__main__":
    main()
