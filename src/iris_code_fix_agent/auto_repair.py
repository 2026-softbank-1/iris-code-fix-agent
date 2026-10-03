"""Durable repair candidate -> private S3 -> repair branch -> draft review PR."""

import argparse
import asyncio
import base64
import io
import json
import os
import re
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .canonical import canonical_json, sha256
from .configuration import load_environment
from .contracts import SourceSpec
from .errors import RepairError
from .publication import GitHubPublisher
from .source import (
    SourceFile,
    download_source,
    from_archive,
    inspect_trusted_snapshot,
    manifest_digest,
)
from .storage import S3Artifacts
from .store import ResultStore


class AutoRepair:
    def __init__(
        self,
        was,
        fix,
        github,
        source_client,
        storage,
        state_dir: Path,
        *,
        allowed_source_hosts,
        poll_seconds=5,
    ):
        self.was, self.fix, self.github = was, fix, github
        self.source_client, self.storage = source_client, storage
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.allowed_source_hosts = allowed_source_hosts
        self.poll_seconds = poll_seconds

    async def request(self, client, method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        if response.is_error:
            raise RepairError(
                "REMOTE_REQUEST_FAILED", "Repair dependency rejected the request", 502
            )
        body = response.json()
        return body.get("data", body)

    def save(self, state):
        path = self.state_dir / f"{state['runId']}.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(canonical_json(state))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory = os.open(self.state_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    async def wait(self, client, path, state, *, success, failed):
        while True:
            self.check_deadline(state)
            result = await self.request(client, "GET", path)
            if result["status"] in success:
                return result
            if result["status"] in failed:
                raise RepairError("DEPENDENCY_FAILED", "Diagnosis failed")
            await asyncio.sleep(self.poll_seconds)

    def check_deadline(self, state):
        if time.time() >= state["deadline"]:
            raise RepairError(
                "DEADLINE_EXCEEDED", "Automatic repair deadline expired", 408
            )

    async def run(
        self,
        run_id,
        service_id,
        deployment_id,
        *,
        max_attempts=1,
        timeout_seconds=1800,
        max_cost_usd=1,
        allowed_paths=None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", run_id):
            raise ValueError("Invalid run ID")
        if (
            max_attempts != 1
            or not 0 < timeout_seconds <= 3600
            or not 0 < max_cost_usd <= 10
        ):
            raise ValueError("Invalid automatic repair limits")
        path = self.state_dir / f"{run_id}.json"
        settings = {
            "serviceId": service_id,
            "initialDeploymentId": deployment_id,
            "maxAttempts": max_attempts,
            "maxCostUsd": max_cost_usd,
            "allowedPaths": allowed_paths or ["**"],
            "timeoutSeconds": timeout_seconds,
        }
        with ResultStore(self.state_dir / "locks").lock(run_id):
            state = (
                json.loads(path.read_text())
                if path.exists()
                else {
                    "runId": run_id,
                    "settings": settings,
                    "deadline": time.time() + timeout_seconds,
                    "status": "RUNNING",
                    "attempts": [],
                    "deploymentId": deployment_id,
                }
            )
            if state["settings"] != settings:
                raise RepairError(
                    "IDEMPOTENCY_CONFLICT", "Automatic repair run settings changed", 409
                )
            if state["status"] in {"PR_OPENED", "STOPPED"}:
                return state
            state["status"] = "RUNNING"
            state.pop("reason", None)
            self.save(state)
            try:
                async with asyncio.timeout(max(0.001, state["deadline"] - time.time())):
                    return await self.loop(state)
            except TimeoutError:
                state.update(status="STOPPED", reason="DEADLINE_EXCEEDED")
                self.save(state)
                return state
            except httpx.HTTPError:
                state.update(status="PAUSED_ERROR", reason="DEPENDENCY_UNAVAILABLE")
                self.save(state)
                return state

            except RepairError as exc:
                # External outages can resume the same persisted attempt, never a new model call.
                terminal = exc.code in {
                    "SOURCE_HEAD_CHANGED",
                    "PREIMAGE_MISMATCH",
                    "NO_CODE_PLAN",
                    "ARTIFACT_INTEGRITY_ERROR",
                    "REPEATED_CANDIDATE",
                    "NO_CANDIDATE",
                    "DIAGNOSIS_SOURCE_MISMATCH",
                    "DEADLINE_EXCEEDED",
                    "SOURCE_INTEGRITY",
                    "SOURCE_TOO_LARGE",
                    "SOURCE_UNSAFE",
                }
                state.update(
                    status="STOPPED" if terminal else "PAUSED_ERROR", reason=exc.code
                )
                self.save(state)
                return state

    async def watch_once(self, service_id, **limits):
        """Resume pending publication; skip incidents that already have a draft PR."""
        records = [
            json.loads(path.read_text()) for path in self.state_dir.glob("*.json")
        ]
        records = [row for row in records if row["settings"]["serviceId"] == service_id]
        active = [
            row for row in records if row["status"] in {"RUNNING", "PAUSED_ERROR"}
        ]
        if active:
            row = active[0]
            return await self.run(
                row["runId"],
                service_id,
                row["settings"]["initialDeploymentId"],
                **limits,
            )
        listing = await self.request(
            self.was,
            "GET",
            f"/api/v1/services/{service_id}/deployments",
            params={"size": 1},
        )
        if not listing["items"]:
            return None
        latest = listing["items"][0]
        published = {
            attempt["commitSha"]
            for row in records
            for attempt in row["attempts"]
            if "commitSha" in attempt
        }
        handled = {row["settings"]["initialDeploymentId"] for row in records}
        handled.update(
            attempt[key]
            for row in records
            for attempt in row["attempts"]
            for key in ("deploymentId", "redeploymentId")
            if key in attempt
        )
        if (
            latest["id"] in handled
            or latest["sourceSha"] in published
            or latest["status"]
            not in {
                "FAILED",
                "ROLLED_BACK",
                "MANUAL_INTERVENTION",
            }
        ):
            return None
        return await self.run(
            f"service-{service_id}-deployment-{latest['id']}",
            service_id,
            latest["id"],
            **limits,
        )

    async def loop(self, state):
        service_id = state["settings"]["serviceId"]
        for index in range(state["settings"]["maxAttempts"]):
            self.check_deadline(state)
            if len(state["attempts"]) <= index:
                state["attempts"].append(
                    {
                        "requestId": f"{state['runId']}-a{index + 1}",
                        "deploymentId": state["deploymentId"],
                        "stage": "DIAGNOSING",
                    }
                )
                self.save(state)
            attempt = state["attempts"][index]
            if attempt["stage"] == "DONE":
                continue
            deployment_path = (
                f"/api/v1/services/{service_id}/deployments/{attempt['deploymentId']}"
            )
            if "candidate" not in attempt:
                started_diagnosis = await self.request(
                    self.was, "POST", deployment_path + "/diagnose"
                )
                diagnosis = await self.wait(
                    self.was,
                    deployment_path + "/diagnosis",
                    state,
                    success={"SUCCEEDED"},
                    failed={"FAILED"},
                )
                diagnosis_id = started_diagnosis.get("id")
                if diagnosis_id is not None and diagnosis.get("id") != diagnosis_id:
                    raise RepairError(
                        "DIAGNOSIS_SOURCE_MISMATCH", "Selected diagnosis changed"
                    )
                context = await self.request(
                    self.was,
                    "GET",
                    deployment_path + "/repair-context",
                    params={"diagnosisId": diagnosis_id}
                    if diagnosis_id is not None
                    else {},
                )
                parsed = urlsplit(context["repositoryUrl"])
                repository = parsed.path.strip("/").removesuffix(".git")
                if parsed.scheme != "https" or parsed.hostname != "github.com":
                    raise RepairError(
                        "REPOSITORY_INVALID", "Only GitHub repositories are supported"
                    )
                identity = {
                    "repository": repository,
                    "branch": context["branch"],
                }
                if state.get("target", identity) != identity:
                    raise RepairError(
                        "SOURCE_HEAD_CHANGED", "Service target changed", 409
                    )
                state["target"] = identity
                source = context["source"]
                if (
                    await self.github.head(repository, context["branch"])
                    != source["commitSha"]
                ):
                    raise RepairError(
                        "SOURCE_HEAD_CHANGED",
                        "Service branch differs from failed source",
                        409,
                    )
                # Prefer hashes frozen by WAS. Without them the archive can only vouch for
                # itself, so the attempt records that the source was not pinned.
                pinned = bool(source.get("archiveSha256") and source.get("manifestSha256"))
                spec = SourceSpec(
                    repositoryId=repository,
                    baseCommitSha=source["commitSha"],
                    rootDirectory=source["rootDirectory"],
                    downloadUrl=source["downloadUrl"],
                    archiveSha256=source.get("archiveSha256") or "0" * 64,
                    manifestSha256=source.get("manifestSha256") or "0" * 64,
                )
                raw = await download_source(
                    spec, self.source_client, self.allowed_source_hosts
                )
                if pinned:
                    snapshot = from_archive(raw, spec)
                else:
                    spec = spec.model_copy(update={"archive_sha256": sha256(raw)})
                    snapshot = inspect_trusted_snapshot(raw, spec)
                    spec = spec.model_copy(
                        update={"manifest_sha256": snapshot.manifest_sha256}
                    )
                attempt["sourcePinned"] = pinned
                plans = (
                    context["diagnosisResult"]
                    .get("analysis", {})
                    .get("remediation", {})
                    .get("plans", [])
                )
                plan_ids = [
                    plan["id"]
                    for plan in plans
                    if plan.get("changes")
                    and all(change.get("kind") == "code" for change in plan["changes"])
                ]
                if not plan_ids:
                    raise RepairError(
                        "NO_CODE_PLAN", "Diagnosis has no source-code repair plan"
                    )
                payload = {
                    "requestId": attempt["requestId"],
                    "scope": {
                        "serviceId": service_id,
                        "deploymentId": attempt["deploymentId"],
                        "diagnosisId": context["diagnosisId"],
                    },
                    "diagnosisResult": context["diagnosisResult"],
                    "planIds": plan_ids,
                    "source": spec.model_dump(mode="json", by_alias=True),
                    "policy": {
                        "allowedPaths": state["settings"]["allowedPaths"],
                        "deadline": datetime.fromtimestamp(
                            state["deadline"], UTC
                        ).isoformat(),
                        "maxCostUsd": state["settings"]["maxCostUsd"],
                    },
                }
                attempt.update(stage="GENERATING", baseSha=source["commitSha"])
                self.save(state)
                candidate = await self.request(
                    self.fix,
                    "POST",
                    "/internal/repairs",
                    json=payload,
                    headers={"Idempotency-Key": attempt["requestId"]},
                )
                if candidate["status"] != "candidate_ready":
                    raise RepairError(
                        "NO_CANDIDATE", "Agent did not produce a code candidate"
                    )
                artifacts = {}
                for reference in candidate["artifacts"]:
                    if reference["name"] not in {
                        "patch.diff",
                        "changes.json",
                        "manifest.json",
                    }:
                        raise RepairError(
                            "ARTIFACT_INTEGRITY_ERROR", "Unsupported artifact"
                        )
                    response = await self.fix.get(reference["url"])
                    if (
                        response.status_code != 200
                        or sha256(response.content) != reference["sha256"]
                    ):
                        raise RepairError(
                            "ARTIFACT_INTEGRITY_ERROR", "Repair artifact changed"
                        )
                    artifacts[reference["name"]] = response.content
                changes = json.loads(artifacts["changes.json"])["files"]
                files = dict(snapshot.files)
                for change in changes:
                    files[change["path"]] = SourceFile(
                        base64.b64decode(change["contentBase64"], validate=True),
                        change["mode"],
                    )
                if manifest_digest(files) != candidate["candidateManifestSha256"]:
                    raise RepairError(
                        "ARTIFACT_INTEGRITY_ERROR", "Candidate manifest differs"
                    )
                if any(
                    row.get("candidate", {}).get("candidateManifestSha256")
                    == candidate["candidateManifestSha256"]
                    for row in state["attempts"][:index]
                ):
                    raise RepairError(
                        "REPEATED_CANDIDATE", "Repair repeats a previous candidate"
                    )
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                    for name, file in sorted(files.items()):
                        entry = tarfile.TarInfo(name)
                        entry.size, entry.mode = len(file.data), int(file.mode[-3:], 8)
                        tar.addfile(entry, io.BytesIO(file.data))
                artifacts["source.tar.gz"] = archive.getvalue()
                # Save the complete candidate before S3 or Git operations; retries use these exact bytes.
                attempt.update(
                    candidate=candidate,
                    artifacts={
                        name: base64.b64encode(data).decode()
                        for name, data in artifacts.items()
                    },
                    timestamp=datetime.now(UTC).isoformat(),
                    stage="STORING",
                )
                self.save(state)
            if "storage" not in attempt:
                attempt["storage"] = await asyncio.to_thread(
                    lambda attempt=attempt: {
                        name: self.storage.put(
                            attempt["requestId"], name, base64.b64decode(data)
                        )
                        for name, data in attempt["artifacts"].items()
                    }
                )
                attempt["stage"] = "PREPARING_COMMIT"
                self.save(state)
            target = state["target"]
            if "commitSha" not in attempt:
                changes = json.loads(
                    base64.b64decode(attempt["artifacts"]["changes.json"])
                )["files"]
                attempt["commitSha"] = await self.github.prepare(
                    target["repository"],
                    target["branch"],
                    attempt["baseSha"],
                    changes,
                    f"fix: IRIS automatic repair {attempt['requestId']}",
                    attempt["timestamp"],
                )
                attempt["stage"] = "PUSHING"
                self.save(state)
            repair_branch = f"iris/repair/{attempt['requestId']}"
            if attempt["stage"] == "PUSHING":
                await self.github.publish(
                    target["repository"], repair_branch, attempt["commitSha"]
                )
                attempt.update(stage="OPENING_PR", repairBranch=repair_branch)
                self.save(state)
            if "pullRequestUrl" not in attempt:
                attempt["pullRequestUrl"] = await self.github.open_pull_request(
                    target["repository"],
                    repair_branch,
                    target["branch"],
                    f"fix: IRIS repair candidate {attempt['requestId']}",
                    "Code repair candidate generated from the original failed deployment "
                    f"{attempt['deploymentId']} and pinned source {attempt['baseSha']}.\n\n"
                    "Validation: not run. Review and independently validate before merging. "
                    "The service branch and deployment have not been changed.",
                )
            attempt["stage"] = "PR_OPENED"
            state.update(status="PR_OPENED", pullRequestUrl=attempt["pullRequestUrl"])
            self.save(state)
            return state
        state.update(status="STOPPED", reason="ATTEMPT_LIMIT")
        self.save(state)
        return state


def warn_if_broad_github_token(token: str) -> bool:
    """A classic PAT reaches every repository its owner can; scope it per service."""
    broad = token.startswith("ghp_")
    if broad:
        print(
            "warning: GITHUB_TOKEN is a classic token; use a fine-grained token limited "
            "to the service repository (Contents and Pull requests write only)",
            file=sys.stderr,
            flush=True,
        )
    return broad


def main():
    load_environment()
    warn_if_broad_github_token(os.environ.get("GITHUB_TOKEN", ""))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--service-id", type=int, required=True)
    parser.add_argument("--deployment-id", type=int)
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Open draft repair PRs for new failed deployments of this service",
    )
    parser.add_argument("--allowed-path", action="append", required=True)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--max-cost-usd", type=float, default=1)
    args = parser.parse_args()
    if not args.watch and (not args.run_id or args.deployment_id is None):
        parser.error("--run-id and --deployment-id are required without --watch")

    async def execute():
        import boto3
        from botocore.config import Config

        storage = S3Artifacts(
            boto3.client(
                "s3",
                config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
            ),
            os.environ["FIX_S3_BUCKET"],
        )
        async with (
            httpx.AsyncClient(
                base_url=os.environ["WAS_BASE_URL"],
                headers={"Authorization": "Bearer " + os.environ["WAS_TOKEN"]},
                timeout=30,
            ) as was,
            httpx.AsyncClient(
                base_url=os.environ["FIX_BASE_URL"],
                headers={"X-API-Key": os.environ["API_KEY"]},
                timeout=180,
            ) as fix,
            httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={
                    "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                    "Accept": "application/vnd.github+json",
                },
                timeout=30,
            ) as github,
            httpx.AsyncClient(follow_redirects=False, timeout=30) as source,
        ):
            worker = AutoRepair(
                was,
                fix,
                GitHubPublisher(github),
                source,
                storage,
                Path(os.environ.get("FIX_DATA_DIR", "artifacts")) / "auto-repair",
                allowed_source_hosts=tuple(
                    os.environ["FIX_ALLOWED_SOURCE_HOSTS"].split(",")
                ),
            )
            limits = {
                "max_attempts": args.max_attempts,
                "timeout_seconds": args.timeout_seconds,
                "max_cost_usd": args.max_cost_usd,
                "allowed_paths": args.allowed_path,
            }
            while True:
                result = (
                    await worker.watch_once(args.service_id, **limits)
                    if args.watch
                    else await worker.run(
                        args.run_id, args.service_id, args.deployment_id, **limits
                    )
                )
                if result:
                    print(
                        json.dumps(
                            {
                                "runId": result["runId"],
                                "status": result["status"],
                                "reason": result.get("reason"),
                                "deploymentId": result["deploymentId"],
                                "attempts": len(result["attempts"]),
                            }
                        ),
                        flush=True,
                    )
                if not args.watch:
                    return result["status"] == "PR_OPENED"
                await asyncio.sleep(worker.poll_seconds)

    raise SystemExit(0 if asyncio.run(execute()) else 1)
