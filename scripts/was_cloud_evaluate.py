"""Exercise existing WAS build/deploy/redeploy APIs using owned fixture services."""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

TERMINAL = {"SUCCEEDED", "FAILED", "ROLLED_BACK", "MANUAL_INTERVENTION", "REMOVED"}


async def evaluate(root, base_url):
    fixture = json.loads((root / "report.json").read_text())
    token = json.loads((root / "was-token.json").read_text())["accessToken"]
    report_path = root / "cloud-report.json"
    report = (
        json.loads(report_path.read_text())
        if report_path.exists()
        else {
            "runId": fixture["runId"],
            "cases": [],
            "allPassed": False,
            "target": "WAS API -> S3 snapshot -> CodeBuild -> ECR -> GitOps -> EKS",
            "automaticRepairRedeployTested": False,
        }
    )

    def save():
        report_path.write_text(json.dumps(report, indent=2) + "\n")

    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": "Bearer " + token}, timeout=60
    ) as client:

        async def api(method, path, **kwargs):
            response = await client.request(method, "/api/v1" + path, **kwargs)
            if response.is_error:
                raise RuntimeError(
                    f"{method} {path} HTTP {response.status_code}: {response.text[:500]}"
                )
            return response.json()["data"]

        async def wait(service, deployment, case, phase):
            deadline = time.monotonic() + 1200
            previous = None
            while time.monotonic() < deadline:
                detail = await api(
                    "GET", f"/services/{service}/deployments/{deployment}"
                )
                state = detail["status"]
                case[phase] = detail
                save()
                if state != previous:
                    print(
                        json.dumps(
                            {
                                "case": case["case"],
                                "phase": phase,
                                "deployment": deployment,
                                "status": state,
                            }
                        ),
                        flush=True,
                    )
                    previous = state
                if state in TERMINAL:
                    return detail
                await asyncio.sleep(10)
            raise TimeoutError("Deployment polling deadline expired")

        if "projectId" not in report:
            project = await api(
                "POST",
                "/projects",
                json={
                    "name": "code-fix-e2e-" + fixture["runId"],
                    "description": "Owned code and Docker failure fixtures for repair/redeployment verification",
                },
            )
            report["projectId"] = project["id"]
            save()
        for original in fixture["cases"]:
            if any(
                row["case"] == original["case"] and "serviceId" in row
                for row in report["cases"]
            ):
                continue
            report["cases"] = [
                row for row in report["cases"] if row["case"] != original["case"]
            ]
            original = {**original, **original.get("cloudPublication", {})}
            case = {"case": original["case"], "passed": False}
            report["cases"].append(case)
            save()
            service = await api(
                "POST",
                f"/projects/{report['projectId']}/services",
                json={
                    "repositoryUrl": "https://github.com/"
                    + original.get("repository", "2026-softbank-1/iris-code-fix-agent"),
                    "name": f"fix-e2e-{fixture['runId'][:6]}-{original['case']}",
                    "branch": original["fixtureBranch"],
                    "rootDirectory": f"examples/e2e/{fixture['runId']}/{original['case']}",
                    "isAutoDeploy": False,
                    "targetIds": [1],
                },
            )
            case["serviceId"] = service["id"]
            save()
            await api(
                "PATCH",
                f"/services/{service['id']}",
                json={
                    "builder": "dockerfile",
                    "dockerfilePath": "Dockerfile",
                    "port": 8080,
                },
            )

        async def run_case(case):
            original = next(
                row for row in fixture["cases"] if row["case"] == case["case"]
            )
            original = {**original, **original.get("cloudPublication", {})}
            service = case["serviceId"]
            path = f"/services/{service}/deployments"
            if "baselineId" not in case:
                started = await api(
                    "POST",
                    path,
                    json={"triggerType": "MANUAL"},
                    headers={
                        "Idempotency-Key": f"e2e-{fixture['runId']}-{case['case']}-broken"
                    },
                )
                case["baselineId"] = started["id"]
                save()
            baseline = await wait(service, case["baselineId"], case, "baseline")
            if (
                baseline["status"] != "FAILED"
                or baseline.get("failureCode") != "BUILD_FAILED"
            ):
                raise ValueError("Expected user fixture build failure")
            await api(
                "PATCH",
                f"/services/{service}",
                json={"sourceBranch": original["repairBranch"]},
            )
            if "fixedId" not in case:
                started = await api(
                    "POST",
                    path,
                    json={
                        "triggerType": "MANUAL",
                        "sourceSha": original["pushedCommit"],
                    },
                    headers={
                        "Idempotency-Key": f"e2e-{fixture['runId']}-{case['case']}-fixed"
                    },
                )
                case["fixedId"] = started["id"]
                save()
            fixed = await wait(service, case["fixedId"], case, "fixed")
            if fixed["sourceSha"] != original["pushedCommit"]:
                case.setdefault("preliminaryDeployments", []).append(fixed)
                started = await api(
                    "POST",
                    path,
                    json={
                        "triggerType": "MANUAL",
                        "sourceSha": original["pushedCommit"],
                    },
                    headers={
                        "Idempotency-Key": f"e2e-{fixture['runId']}-{case['case']}-{original['pushedCommit'][:8]}"
                    },
                )
                case["fixedId"] = started["id"]
                save()
                fixed = await wait(service, case["fixedId"], case, "fixed")
            if fixed["status"] != "SUCCEEDED":
                raise ValueError("Corrected deployment failed")
            domains = await api("GET", f"/services/{service}/domains")
            case["domains"] = domains
            save()
            if "redeploymentId" not in case:
                started = await api(
                    "POST",
                    path,
                    json={
                        "triggerType": "REDEPLOY",
                        "sourceDeploymentId": case["fixedId"],
                    },
                    headers={
                        "Idempotency-Key": f"e2e-{fixture['runId']}-{case['case']}-redeploy"
                    },
                )
                case["redeploymentId"] = started["id"]
                save()
            redeployed = await wait(
                service, case["redeploymentId"], case, "redeployment"
            )
            checks = []
            async with httpx.AsyncClient(timeout=15) as public:
                for domain in domains:
                    if not domain.get("url"):
                        continue
                    url = domain["url"]
                    health = await public.get(url + "/health")
                    assert health.status_code == 200 and health.json() == {
                        "status": "ok"
                    }
                    for a, b in [(0, 0), (2, 3), (-3, 4), (10, 10)]:
                        result = await public.get(url + "/add", params={"a": a, "b": b})
                        assert result.status_code == 200 and result.json() == {
                            "result": a + b
                        }
                    checks.append({"url": url, "passed": True, "assertions": 5})
            case["publicChecks"] = checks
            case["passed"] = (
                redeployed["status"] == "SUCCEEDED"
                and redeployed["sourceSha"] == original["pushedCommit"]
                and bool(checks)
            )
            save()

        outcomes = await asyncio.gather(
            *(run_case(case) for case in report["cases"]), return_exceptions=True
        )
        for case, outcome in zip(report["cases"], outcomes):
            if isinstance(outcome, Exception):
                case["error"] = str(outcome)
        report["allPassed"] = all(case["passed"] for case in report["cases"])
        save()
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-url", default="https://api.likelion.uk")
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.root, args.base_url))
    print(
        json.dumps(
            {
                "allPassed": report["allPassed"],
                "report": str(args.root / "cloud-report.json"),
            }
        )
    )
    raise SystemExit(0 if report["allPassed"] else 1)


if __name__ == "__main__":
    main()
