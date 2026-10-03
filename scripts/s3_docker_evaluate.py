"""Real S3 HTTPS retrieval, paid repair, isolated Docker execution and GitHub push.

No WAS deployment is submitted. --publish creates only dedicated fixture/repair
branches. Source transport is real; the repair API runs via ASGI in process.
"""

import argparse
import asyncio
import base64
import io
import json
import os
import ssl
import subprocess
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.config import Config
from dotenv import dotenv_values
from live_evaluate import BoundedRunner, make_fixture

from iris_code_fix_agent.api import Settings, create_app
from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.configuration import load_environment, openai_api_key
from iris_code_fix_agent.contracts import RepairRequest
from iris_code_fix_agent.publication import GitHubPublisher
from iris_code_fix_agent.runner import OpenAIRepairRunner, RunnerConfig
from iris_code_fix_agent.source import SourceFile, from_archive, manifest_digest
from iris_code_fix_agent.storage import S3Artifacts

APP = """import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit


def add(a, b):
    return a + b


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        args = parse_qs(url.query)
        body = {"status": "ok"} if url.path == "/health" else {"result": add(int(args["a"][0]), int(args["b"][0]))}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""


def archive_files(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, file in sorted(files.items()):
            entry = tarfile.TarInfo(path)
            entry.size, entry.mode, entry.mtime = len(file.data), 0o644, 0
            archive.addfile(entry, io.BytesIO(file.data))
    return output.getvalue()


def fixture(case, root, image):
    app = APP.replace("def add(a, b):", "def add(a, b)") if case == "code" else APP
    copy = "COPY missing.py app.py" if case == "docker" else "COPY app.py app.py"
    dockerfile = f'FROM {image}\nWORKDIR /app\n{copy}\nRUN python -m py_compile app.py\nUSER 10001\nEXPOSE 8080\nCMD ["python", "app.py"]\n'
    return {
        f"{root}/app.py": SourceFile(app.encode()),
        f"{root}/Dockerfile": SourceFile(dockerfile.encode()),
        f"{root}/iris.json": SourceFile(
            b'{"deploy":{"healthcheckPath":"/health","healthcheckTimeout":300}}\n'
        ),
    }


def command(args, *, cwd=None, required=True):
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=180, check=False
    )
    if required and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-1500:]}")
    return result


def changes_for(files):
    return [
        {
            "path": path,
            "operation": "create",
            "mode": file.mode,
            "beforeSha256": None,
            "afterSha256": sha256(file.data),
            "contentBase64": base64.b64encode(file.data).decode(),
        }
        for path, file in files.items()
    ]


def docker_check(directory, image, name, logs):
    build = command(
        ["docker", "build", "--network=none", "-t", image, str(directory)],
        required=False,
    )
    logs.write_text(build.stdout + build.stderr)
    if build.returncode:
        return {"buildPassed": False, "exitCode": build.returncode}
    # No host credentials, host network, host mounts or outbound networking.
    command(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--hostname",
            "localhost",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=32",
            "--memory=64m",
            "--cpus=0.5",
            image,
        ]
    )
    probe = """import json, time, urllib.request
for attempt in range(100):
    try:
        assert json.load(urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=1)) == {"status":"ok"}
        for a,b in [(0,0),(2,3),(-3,4),(10,10)]:
            assert json.load(urllib.request.urlopen(f"http://127.0.0.1:8080/add?a={a}&b={b}", timeout=1)) == {"result":a+b}
        break
    except OSError:
        time.sleep(0.2)
else:
    raise RuntimeError("health timeout")
print("health and 4 arithmetic assertions passed")
"""
    try:
        response = command(
            ["docker", "exec", name, "python", "-c", probe], required=False
        )
        inspected = json.loads(command(["docker", "inspect", name]).stdout)[0]
        return {
            "buildPassed": True,
            "runtimePassed": response.returncode == 0,
            "assertions": 5,
            "containerId": inspected["Id"],
            "imageId": inspected["Image"],
            "probe": response.stdout.strip(),
            "stderr": response.stderr[-1000:],
        }
    finally:
        command(["docker", "rm", "-f", name], required=False)


def materialize(files, root, directory):
    directory.mkdir(parents=True, exist_ok=True)
    for path, file in files.items():
        relative = Path(path).relative_to(root)
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file.data)


async def evaluate(args):
    args.output.mkdir(parents=True, exist_ok=False)
    run = uuid.uuid4().hex[:12]
    report = {
        "runId": run,
        "storageBackend": "S3-compatible" if args.endpoint else "AWS S3",
        "repairTransport": "ASGI API; real HTTPS source and model",
        "deploymentTarget": "isolated local Docker",
        "wasRedeploymentTested": False,
        "cases": [],
        "allPassed": False,
    }

    def save():
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    save()
    options = {
        "region_name": args.region,
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 2},
        ),
    }
    if args.endpoint:
        options.update(endpoint_url=args.endpoint)
    if args.ca:
        options["verify"] = str(args.ca.resolve())
    if args.s3_env:
        env = dotenv_values(args.s3_env, interpolate=False)
        options.update(
            aws_access_key_id=env["MINIO_ROOT_USER"],
            aws_secret_access_key=env["MINIO_ROOT_PASSWORD"],
        )
    s3 = boto3.client("s3", **options)
    s3.head_bucket(Bucket=args.bucket)  # Bucket provisioning stays outside evaluation.
    storage = S3Artifacts(s3, args.bucket, f"evaluations/{run}")
    ca = ssl.create_default_context(cafile=str(args.ca.resolve())) if args.ca else True
    token = os.environ.get("GITHUB_TOKEN")
    if args.publish and not token:
        token = command(["gh", "auth", "token"]).stdout.strip()
    base_image = json.loads(command(["docker", "image", "inspect", args.image]).stdout)[
        0
    ]["RepoDigests"][0]
    report["baseImage"] = base_image
    config = RunnerConfig(
        api_key=openai_api_key(),
        input_price_per_million=float(os.environ["FIX_INPUT_PRICE_PER_MILLION"]),
        output_price_per_million=float(os.environ["FIX_OUTPUT_PRICE_PER_MILLION"]),
        cached_input_price_per_million=float(
            os.environ["FIX_CACHED_INPUT_PRICE_PER_MILLION"]
        )
        if os.environ.get("FIX_CACHED_INPUT_PRICE_PER_MILLION")
        else None,
    )
    runner = BoundedRunner(OpenAIRepairRunner(config), 2, 2)
    async with (
        httpx.AsyncClient(verify=ca, follow_redirects=False) as source,
        httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as github,
    ):
        publisher = GitHubPublisher(github)
        original_head = (
            await publisher.head(args.repository, args.base_branch)
            if args.publish
            else "a" * 40
        )
        for case in ("code", "docker"):
            root = f"examples/e2e/{run}/{case}"
            files = fixture(case, root, base_image)
            directory = args.output / case
            materialize(files, root, directory / "baseline")
            entry = {"case": case, "passed": False}
            report["cases"].append(entry)
            entry["baseline"] = await asyncio.to_thread(
                docker_check,
                directory / "baseline",
                f"iris-eval:{run}-{case}-broken",
                f"iris-eval-{run}-{case}-broken",
                directory / "baseline.log",
            )
            if entry["baseline"]["buildPassed"]:
                raise ValueError("Broken baseline unexpectedly built")
            base_sha = original_head
            fixture_branch = f"iris/evaluation/{run}-{case}"
            if args.publish:
                base_sha = await publisher.prepare(
                    args.repository,
                    args.base_branch,
                    original_head,
                    changes_for(files),
                    f"test: broken {case} fixture {run}",
                    datetime.now(UTC).isoformat(),
                )
                await publisher.api(
                    "POST",
                    args.repository,
                    "git/refs",
                    json={"ref": f"refs/heads/{fixture_branch}", "sha": base_sha},
                )
                entry["fixtureBranch"] = fixture_branch
            archive = archive_files(files)
            reference = storage.put(f"{run}-{case}-original", "source.tar.gz", archive)
            assert storage.get(reference) == archive
            url = storage.download_url(reference)
            _, request = make_fixture("syntax", run, duration=180)
            request.update(requestId=f"s3-{run}-{case}")
            request["source"].update(
                repositoryId=args.repository,
                baseCommitSha=base_sha,
                rootDirectory=root,
                downloadUrl=url,
                archiveSha256=sha256(archive),
                manifestSha256=manifest_digest(files),
            )
            request["policy"].update(
                allowedPaths=[f"{root}/app.py", f"{root}/Dockerfile"],
                maxChangedFiles=2,
                maxChangedBytes=8192,
            )
            diagnosis = request["diagnosisResult"]
            observation = (
                "SyntaxError: expected ':' at app.py function add; Docker RUN python -m py_compile app.py failed"
                if case == "code"
                else "Docker COPY missing.py app.py failed: missing.py does not exist; app.py is the existing HTTP server"
            )
            target = f"{root}/app.py" if case == "code" else f"{root}/Dockerfile"
            diagnosis["source_analysis"].update(
                commit_sha=base_sha,
                root_directory=root,
                archive_sha256=sha256(archive),
                requested_files=list(files),
                read_ranges=[],
            )
            diagnosis["evidence"][0]["text"] = observation
            diagnosis["analysis"]["summary"] = observation
            diagnosis["analysis"]["observations"] = [
                {"description": observation, "evidence_ids": ["EV000001"]}
            ]
            plan = diagnosis["analysis"]["remediation"]["plans"][0]
            plan.update(title="Repair the observed fixture build failure")
            plan["changes"] = [
                {
                    "kind": "code",
                    "target": target,
                    "instruction": "Restore the missing colon in add without changing HTTP behavior"
                    if case == "code"
                    else "Correct COPY to use existing app.py; preserve compile validation, nonroot USER, command and port",
                }
            ]
            settings = Settings(
                os.environ["API_KEY"],
                directory / "receipts",
                (urlsplit(url).hostname,),
                max_concurrent=1,
            )
            app = create_app(settings, runner, source_client=source)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://fix"
            ) as api:
                headers = {
                    "X-API-Key": settings.api_key,
                    "Idempotency-Key": request["requestId"],
                }
                response = await api.post(
                    "/internal/repairs", json=request, headers=headers, timeout=180
                )
                result = response.json()
                entry.update(
                    repairHttpStatus=response.status_code,
                    repairStatus=result.get("status"),
                    usage=result.get("usage"),
                )
                save()
                if (
                    response.status_code != 200
                    or result.get("status") != "candidate_ready"
                ):
                    raise ValueError(
                        f"Repair failed; see receipt (HTTP {response.status_code})"
                    )
                calls_before = runner.calls
                replay = await api.post(
                    "/internal/repairs", json=request, headers=headers
                )
                assert replay.json() == result and runner.calls == calls_before
                entry["idempotentReplay"] = True
                refs = {}
                artifacts = {}
                for artifact in result["artifacts"]:
                    fetched = await api.get(artifact["url"], headers=headers)
                    assert (
                        fetched.status_code == 200
                        and sha256(fetched.content) == artifact["sha256"]
                        and len(fetched.content) == artifact["byteLength"]
                    )
                    refs[artifact["name"]] = storage.put(
                        request["requestId"], artifact["name"], fetched.content
                    )
                    artifacts[artifact["name"]] = storage.get(refs[artifact["name"]])
            changes = json.loads(artifacts["changes.json"])["files"]
            candidate = dict(files)
            for change in changes:
                candidate[change["path"]] = SourceFile(
                    base64.b64decode(change["contentBase64"], validate=True),
                    change["mode"],
                )
            assert manifest_digest(candidate) == result["candidateManifestSha256"]
            # Restrict this execution harness to the exact authored fixture repair.
            expected = fixture("clean", root, base_image)
            if candidate != expected:
                raise ValueError(
                    "Candidate differs from narrowly authorized fixture; source saved in receipts"
                )
            materialize(candidate, root, directory / "candidate")
            entry["candidate"] = await asyncio.to_thread(
                docker_check,
                directory / "candidate",
                f"iris-eval:{run}-{case}-candidate",
                f"iris-eval-{run}-{case}-candidate",
                directory / "candidate.log",
            )
            save()
            if not entry["candidate"].get("runtimePassed"):
                raise ValueError("Candidate runtime failed")
            refs["source.tar.gz"] = storage.put(
                request["requestId"], "source.tar.gz", archive_files(candidate)
            )
            entry["storedArtifacts"] = refs
            if args.publish:
                commit = await publisher.prepare(
                    args.repository,
                    fixture_branch,
                    base_sha,
                    changes,
                    f"fix: verified {case} fixture {run}",
                    datetime.now(UTC).isoformat(),
                )
                branch = f"iris/repair/s3-{run}-{case}"
                await publisher.publish(args.repository, branch, commit)
                await publisher.publish(args.repository, branch, commit)
                assert await publisher.head(args.repository, branch) == commit
                # Re-read pushed files from GitHub, then build the retrieved source.
                for path, file in candidate.items():
                    remote = await publisher.api(
                        "GET",
                        args.repository,
                        "contents/" + path,
                        params={"ref": commit},
                    )
                    assert base64.b64decode(remote["content"]) == file.data
                entry.update(
                    repairBranch=branch,
                    pushedCommit=commit,
                    githubRetrievalVerified=True,
                )
            retrieved = storage.get(refs["source.tar.gz"])
            spec = RepairRequest.model_validate(request).source.model_copy(
                update={
                    "archive_sha256": sha256(retrieved),
                    "manifest_sha256": result["candidateManifestSha256"],
                }
            )
            retrieved_files = from_archive(retrieved, spec).files
            materialize(retrieved_files, root, directory / "redeployed")
            entry["redeployment"] = await asyncio.to_thread(
                docker_check,
                directory / "redeployed",
                f"iris-eval:{run}-{case}-redeployed",
                f"iris-eval-{run}-{case}-redeployed",
                directory / "redeployment.log",
            )
            entry["passed"] = entry["redeployment"].get("runtimePassed", False)
            save()
        if args.publish:
            assert (
                await publisher.head(args.repository, args.base_branch) == original_head
            )
            report["serviceBranchUnchanged"] = True
    report.update(
        allPassed=all(row["passed"] for row in report["cases"]),
        modelCalls=runner.calls,
        knownCostUsd=sum(
            row.get("usage", {}).get("costUsd", 0) for row in report["cases"]
        ),
    )
    save()
    return report


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--s3-env", type=Path)
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--image", default="python:3.11-alpine")
    parser.add_argument("--repository", default="2026-softbank-1/iris-code-fix-agent")
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(evaluate(args))
    print(
        json.dumps(
            {
                "allPassed": report["allPassed"],
                "modelCalls": report["modelCalls"],
                "report": str(args.output / "report.json"),
            }
        )
    )
    raise SystemExit(0 if report["allPassed"] else 1)


if __name__ == "__main__":
    main()
