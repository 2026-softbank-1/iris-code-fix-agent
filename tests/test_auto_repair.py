import base64
import io
import json
import tarfile
from unittest.mock import Mock

import httpx
import pytest
from botocore.exceptions import ClientError

from iris_code_fix_agent.auto_repair import AutoRepair
from iris_code_fix_agent.canonical import canonical_json, sha256
from iris_code_fix_agent.errors import RepairError
from iris_code_fix_agent.source import SourceFile, manifest_digest
from iris_code_fix_agent.storage import S3Artifacts


def source_archive(content):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        entry = tarfile.TarInfo("github-wrapper/app.py")
        entry.size = len(content)
        entry.mode = 0o644
        tar.addfile(entry, io.BytesIO(content))
    return output.getvalue()


class FakePublisher:
    def __init__(self):
        self.sha = "a" * 40
        self.pushes = []
        self.prepares = []
        self.branches = {}
        self.pulls = []
        self.fail_pr_response = False
        self.fail_merge_response = False
        self.merges = []

    async def head(self, *_):
        return self.sha

    async def prepare(self, repository, branch, base, changes, message, timestamp):
        assert base == self.sha
        self.prepares.append(changes)
        return chr(ord("b") + len(self.prepares) - 1) * 40

    async def publish(self, repository, branch, commit):
        assert branch.startswith("hotfix/iris/")
        if branch not in self.branches:
            self.pushes.append(commit)
            self.branches[branch] = commit
        assert self.branches[branch] == commit

    async def open_pull_request(
        self, repository, branch, base_branch, title, body, *, draft=True
    ):
        assert base_branch == "main" and branch in self.branches
        if not self.pulls:
            self.pulls.append((branch, base_branch))
            if self.fail_pr_response:
                self.fail_pr_response = False
                raise RepairError("GITHUB_PUBLICATION_FAILED", "Lost PR response")
        return "https://github.com/owner/repo/pull/1"

    async def merge_pull_request(
        self, repository, url, branch, base_branch, commit, base
    ):
        assert base_branch == "main" and self.branches[branch] == commit
        if not self.merges:
            assert self.sha == base
            self.merges.append(commit)
            self.sha = "c" * 40
            if self.fail_merge_response:
                self.fail_merge_response = False
                raise httpx.ReadTimeout("Lost merge response")
        return self.sha


@pytest.mark.parametrize(
    "draft_pr,resume_merge", [(True, False), (False, False), (False, True)]
)
@pytest.mark.parametrize("auto_deploy", [True, False])
@pytest.mark.parametrize("resume_storage", [True, False])
@pytest.mark.parametrize("resume_pr", [True, False])
async def test_hotfix_pr_merge_and_resume_without_redeployment(
    tmp_path, auto_deploy, resume_storage, resume_pr, draft_pr, resume_merge
):
    publisher = FakePublisher()
    publisher.fail_pr_response = resume_pr
    publisher.fail_merge_response = resume_merge
    storage_client = Mock()
    storage_client.put_object.return_value = {"VersionId": "v1"}
    content = b"value = 0\n"
    artifacts = {}
    generation_calls = []
    manual_calls = []

    def was_handler(request):
        path = request.url.path
        if path.endswith("/diagnose"):
            return httpx.Response(202, json={"data": {"id": 1, "status": "RUNNING"}})
        if path.endswith("/diagnosis"):
            return httpx.Response(200, json={"data": {"id": 1, "status": "SUCCEEDED"}})
        if path.endswith("/repair-context"):
            assert request.url.params["diagnosisId"] == "1"
            deployment = int(path.split("/")[-2])
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repositoryUrl": "https://github.com/owner/repo",
                        "branch": "main",
                        "autoDeploy": auto_deploy,
                        "diagnosisId": deployment,
                        "source": {
                            "commitSha": publisher.sha,
                            "rootDirectory": ".",
                            "downloadUrl": "https://bucket.s3.amazonaws.com/source",
                        },
                        "diagnosisResult": {
                            "schema_version": "diagnosis-result.v3",
                            "job_status": "succeeded",
                            "analysis": {
                                "remediation": {
                                    "plans": [
                                        {"id": "p1", "changes": [{"kind": "code"}]}
                                    ]
                                }
                            },
                        },
                    }
                },
            )
        current = 1
        deployment = {
            "id": current,
            "sourceSha": publisher.sha,
            "status": "FAILED",
        }
        if request.method == "POST":
            manual_calls.append(json.loads(request.content))
            assert manual_calls[-1]["sourceSha"] == publisher.sha
            return httpx.Response(201, json={"data": deployment})
        if path.endswith("/deployments"):
            return httpx.Response(200, json={"data": {"items": [deployment]}})
        return httpx.Response(200, json={"data": deployment})

    def fix_handler(request):
        nonlocal content
        if request.method == "GET":
            return httpx.Response(
                200, content=artifacts[request.url.path.split("/")[-1]]
            )
        payload = json.loads(request.content)
        generation_calls.append(payload)
        updated = f"value = {len(generation_calls)}\n".encode()
        manifest = manifest_digest({"app.py": SourceFile(updated)})
        changes = [
            {
                "path": "app.py",
                "operation": "update",
                "mode": "100644",
                "beforeSha256": sha256(content),
                "afterSha256": sha256(updated),
                "contentBase64": base64.b64encode(updated).decode(),
            }
        ]
        artifacts.update(
            {
                "changes.json": canonical_json({"files": changes}),
                "patch.diff": b"diff",
                "manifest.json": canonical_json({"candidateManifestSha256": manifest}),
            }
        )
        content = updated
        return httpx.Response(
            200,
            json={
                "status": "candidate_ready",
                "candidateManifestSha256": manifest,
                "candidateDigest": manifest,
                "artifacts": [
                    {
                        "name": name,
                        "sha256": sha256(data),
                        "url": f"/internal/repairs/{payload['requestId']}/artifacts/{name}",
                    }
                    for name, data in artifacts.items()
                ],
            },
        )

    async with (
        httpx.AsyncClient(
            base_url="https://was.test", transport=httpx.MockTransport(was_handler)
        ) as was,
        httpx.AsyncClient(
            base_url="https://fix.test", transport=httpx.MockTransport(fix_handler)
        ) as fix,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=source_archive(content))
            )
        ) as source,
    ):
        worker = AutoRepair(
            was,
            fix,
            publisher,
            source,
            S3Artifacts(storage_client, "repair-bucket"),
            tmp_path,
            allowed_source_hosts=("bucket.s3.amazonaws.com",),
            poll_seconds=0,
        )
        if resume_storage:
            storage_client.put_object.side_effect = ClientError(
                {"Error": {"Code": "ServiceUnavailable"}}, "PutObject"
            )
            interrupted = await worker.run(
                "recursive", 1, 1, allowed_paths=["app.py"], draft_pr=draft_pr
            )
            assert interrupted["status"] == "PAUSED_ERROR"
            assert len(generation_calls) == 1 and not publisher.pushes
            storage_client.put_object.side_effect = None
            storage_client.reset_mock()
        result = await worker.run(
            "recursive", 1, 1, allowed_paths=["app.py"], draft_pr=draft_pr
        )
        if resume_pr:
            assert result["status"] == "PAUSED_ERROR"
            assert result["attempts"][0]["stage"] == "OPENING_PR"
            result = await worker.run(
                "recursive", 1, 1, allowed_paths=["app.py"], draft_pr=draft_pr
            )
        if resume_merge:
            assert result["status"] == "PAUSED_ERROR"
            assert result["attempts"][0]["stage"] == "MERGING"
            result = await worker.run(
                "recursive", 1, 1, allowed_paths=["app.py"], draft_pr=draft_pr
            )
        assert result["status"] == ("PR_OPENED" if draft_pr else "MERGED")
        assert result["pullRequestUrl"] == "https://github.com/owner/repo/pull/1"
        # The fixture WAS context carries no frozen hashes, so the journal says so.
        assert result["attempts"][0]["sourcePinned"] is False
        assert len(publisher.pushes) == len(generation_calls) == 1
        assert publisher.sha == ("a" if draft_pr else "c") * 40
        assert len(publisher.merges) == (0 if draft_pr else 1)
        if not draft_pr:
            assert result["mergeCommitSha"] == "c" * 40
        assert publisher.branches == {"hotfix/iris/recursive-a1": "b" * 40}
        assert publisher.pulls == [("hotfix/iris/recursive-a1", "main")]
        assert manual_calls == []
        assert storage_client.put_object.call_count == 4
        stored = storage_client.put_object.call_args_list[-1].kwargs
        with tarfile.open(fileobj=io.BytesIO(stored["Body"])) as tar:
            assert tar.extractfile("app.py").read() == b"value = 1\n"
        assert "downloadUrl" not in (tmp_path / "recursive.json").read_text()
        assert (
            await worker.run(
                "recursive", 1, 1, allowed_paths=["app.py"], draft_pr=draft_pr
            )
            == result
        )
        assert len(publisher.pushes) == len(publisher.pulls) == 1
        assert (
            await worker.watch_once(1, allowed_paths=["app.py"], draft_pr=draft_pr)
            is None
        )
        assert len(generation_calls) == 1


def test_s3_checksum_private_upload_and_failure():
    client = Mock()
    client.put_object.return_value = {"VersionId": "version"}
    storage = S3Artifacts(client, "repair-bucket")
    first = storage.put("run", "patch.diff", b"content")
    assert first == storage.put("run", "patch.diff", b"content")
    args = client.put_object.call_args.kwargs
    assert "ACL" not in args and args["Metadata"]["sha256"] == sha256(b"content")
    assert (
        args["ChecksumSHA256"]
        == base64.b64encode(bytes.fromhex(sha256(b"content"))).decode()
    )
    client.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied"}}, "PutObject"
    )
    with pytest.raises(RepairError, match="upload failed"):
        storage.put("run", "patch.diff", b"content")


def test_classic_github_token_is_flagged(capsys):
    from iris_code_fix_agent.auto_repair import warn_if_broad_github_token

    assert warn_if_broad_github_token("ghp_" + "a" * 36)
    assert "fine-grained" in capsys.readouterr().err
    assert not warn_if_broad_github_token("github_pat_" + "a" * 40)
    assert capsys.readouterr().err == ""


async def test_legacy_journal_cannot_gain_automatic_merge_on_resume(tmp_path):
    state = {
        "runId": "old-run",
        "status": "PR_OPENED",
        "attempts": [],
        "deploymentId": 1,
        "deadline": 0,
        "settings": {
            "serviceId": 1,
            "initialDeploymentId": 1,
            "maxAttempts": 1,
            "maxCostUsd": 1,
            "allowedPaths": ["app.py"],
            "timeoutSeconds": 1800,
        },
    }
    (tmp_path / "old-run.json").write_bytes(canonical_json(state))
    worker = AutoRepair(None, None, None, None, None, tmp_path, allowed_source_hosts=())
    with pytest.raises(RepairError) as error:
        await worker.run("old-run", 1, 1, allowed_paths=["app.py"])
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    result = await worker.run("old-run", 1, 1, allowed_paths=["app.py"], draft_pr=True)
    assert result["status"] == "PR_OPENED"
    assert result["settings"]["draftPr"] is True


async def test_automatic_merge_requires_service_main_before_generation(tmp_path):
    def handler(request):
        if request.url.path.endswith("/diagnose"):
            return httpx.Response(202, json={"id": 1})
        if request.url.path.endswith("/diagnosis"):
            return httpx.Response(200, json={"id": 1, "status": "SUCCEEDED"})
        assert request.url.path.endswith("/repair-context")
        return httpx.Response(
            200, json={"repositoryUrl": "https://github.com/o/r", "branch": "develop"}
        )

    async with httpx.AsyncClient(
        base_url="https://was.test", transport=httpx.MockTransport(handler)
    ) as was:
        worker = AutoRepair(
            was,
            None,
            None,
            None,
            None,
            tmp_path,
            allowed_source_hosts=(),
            poll_seconds=0,
        )
        result = await worker.run("wrong-branch", 1, 1)
        assert result["status"] == "STOPPED"
        assert result["reason"] == "TARGET_BRANCH_INVALID"
        assert "candidate" not in result["attempts"][0]
