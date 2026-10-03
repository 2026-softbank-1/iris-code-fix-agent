"""Publish sealed candidates only on repair branches with draft pull requests."""

import base64
import re
from urllib.parse import quote

from .canonical import sha256
from .contracts import safe_path
from .errors import RepairError
from .paths import is_protected_path, validate_file_tree


class GitHubPublisher:
    def __init__(self, client):
        self.client = client

    async def api(self, method, repository, path, **kwargs):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise RepairError("REPOSITORY_INVALID", "Invalid GitHub repository")
        response = await self.client.request(
            method, f"/repos/{repository}/{path}", **kwargs
        )
        if response.is_error:
            raise RepairError("GITHUB_PUBLICATION_FAILED", "GitHub request failed", 502)
        return response.json()

    async def head(self, repository, branch):
        reference = await self.api(
            "GET", repository, f"git/ref/heads/{quote(branch, safe='')}"
        )
        return reference["object"]["sha"]

    async def prepare(self, repository, branch, base_sha, changes, message, timestamp):
        if await self.head(repository, branch) != base_sha:
            raise RepairError("SOURCE_HEAD_CHANGED", "Service branch changed", 409)
        if not changes or len(changes) > 5:
            raise RepairError("INVALID_CANDIDATE", "Invalid change count")
        paths = [safe_path(change["path"]) for change in changes]
        if len(set(paths)) != len(paths) or any(
            is_protected_path(path) for path in paths
        ):
            raise RepairError("PATH_FORBIDDEN", "Invalid publication paths")
        validate_file_tree(dict.fromkeys(paths))
        commit = await self.api("GET", repository, f"git/commits/{base_sha}")
        tree = []
        # Check every preimage before creating any blob.
        for change in changes:
            if change["operation"] not in {"update", "create"} or change[
                "mode"
            ] not in {"100644", "100755"}:
                raise RepairError("INVALID_CANDIDATE", "Invalid edit operation or mode")
            payload = base64.b64decode(change["contentBase64"], validate=True)
            if sha256(payload) != change["afterSha256"]:
                raise RepairError("ARTIFACT_INTEGRITY_ERROR", "Candidate bytes changed")
            path = quote(change["path"], safe="/")
            response = await self.client.get(
                f"/repos/{repository}/contents/{path}", params={"ref": base_sha}
            )
            if change["operation"] == "create":
                if response.status_code != 404:
                    raise RepairError(
                        "PREIMAGE_MISMATCH", "Create target exists or is inaccessible"
                    )
            else:
                if response.status_code != 200:
                    raise RepairError("PREIMAGE_MISMATCH", "Update target unavailable")
                before = response.json()
                if before.get("type") != "file" or before.get("encoding") != "base64":
                    raise RepairError(
                        "PREIMAGE_MISMATCH", "Update target is not a regular file"
                    )
                if (
                    sha256(base64.b64decode(before["content"]))
                    != change["beforeSha256"]
                ):
                    raise RepairError(
                        "PREIMAGE_MISMATCH", "Original source bytes changed"
                    )
        for change in changes:
            blob = await self.api(
                "POST",
                repository,
                "git/blobs",
                json={"content": change["contentBase64"], "encoding": "base64"},
            )
            tree.append(
                {
                    "path": change["path"],
                    "mode": change["mode"],
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
        new_tree = await self.api(
            "POST",
            repository,
            "git/trees",
            json={"base_tree": commit["tree"]["sha"], "tree": tree},
        )
        created = await self.api(
            "POST",
            repository,
            "git/commits",
            json={
                "message": message,
                "tree": new_tree["sha"],
                "parents": [base_sha],
                "author": {
                    "name": "IRIS Repair",
                    "email": "repair@iris.invalid",
                    "date": timestamp,
                },
                "committer": {
                    "name": "IRIS Repair",
                    "email": "repair@iris.invalid",
                    "date": timestamp,
                },
            },
        )
        return created["sha"]

    async def publish(self, repository, branch, commit_sha):
        if not re.fullmatch(r"iris/repair/[A-Za-z0-9][A-Za-z0-9_-]{0,127}", branch):
            raise RepairError("PATH_FORBIDDEN", "Publication requires a repair branch")
        response = await self.client.get(
            f"/repos/{repository}/git/ref/heads/{quote(branch, safe='')}"
        )
        if response.status_code == 200:
            if response.json().get("object", {}).get("sha") == commit_sha:
                return
            raise RepairError("SOURCE_HEAD_CHANGED", "Repair branch changed", 409)
        if response.status_code != 404:
            raise RepairError(
                "GITHUB_PUBLICATION_FAILED", "GitHub branch lookup failed", 502
            )
        # Create only a new repair ref. Never update the original service branch.
        await self.api(
            "POST",
            repository,
            "git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
        )

    async def open_pull_request(self, repository, branch, base_branch, title, body):
        if (
            not re.fullmatch(r"iris/repair/[A-Za-z0-9][A-Za-z0-9_-]{0,127}", branch)
            or branch == base_branch
        ):
            raise RepairError(
                "PATH_FORBIDDEN", "Pull request requires a separate repair branch"
            )
        owner = repository.split("/", 1)[0]
        existing = await self.api(
            "GET",
            repository,
            "pulls",
            params={
                "state": "open",
                "head": f"{owner}:{branch}",
                "base": base_branch,
            },
        )
        for pull in existing:
            if (
                pull.get("head", {}).get("ref") == branch
                and pull.get("base", {}).get("ref") == base_branch
            ):
                return pull["html_url"]
        pull = await self.api(
            "POST",
            repository,
            "pulls",
            json={
                "head": branch,
                "base": base_branch,
                "title": title,
                "body": body,
                "draft": True,
            },
        )
        return pull["html_url"]
