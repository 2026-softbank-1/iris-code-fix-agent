"""Publish sealed candidates on hotfix branches and merge through GitHub PRs."""

import base64
import re
from urllib.parse import quote, urlsplit

from .canonical import sha256
from .contracts import safe_path
from .errors import RepairError
from .github_auth import validate_repository
from .paths import is_protected_path, validate_file_tree


class GitHubPublisher:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def check_auth(response):
        if response.status_code == 401:
            raise RepairError("GITHUB_AUTH_FAILED", "GitHub authentication failed", 401)
        if response.status_code == 403:
            raise RepairError(
                "GITHUB_PERMISSION_DENIED", "GitHub access denied or rate limited", 403
            )

    async def api(self, method, repository, path, **kwargs):
        validate_repository(repository)
        response = await self.client.request(
            method, f"/repos/{repository}" + (f"/{path}" if path else ""), **kwargs
        )
        self.check_auth(response)
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
            self.check_auth(response)
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
        validate_repository(repository)
        if not re.fullmatch(
            r"(?:hotfix/iris|iris/repair)/[A-Za-z0-9][A-Za-z0-9_-]{0,127}", branch
        ):
            raise RepairError("PATH_FORBIDDEN", "Publication requires a repair branch")
        response = await self.client.get(
            f"/repos/{repository}/git/ref/heads/{quote(branch, safe='')}"
        )
        self.check_auth(response)
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

    async def open_pull_request(
        self, repository, branch, base_branch, title, body, *, draft=True
    ):
        if (
            not re.fullmatch(
                r"(?:hotfix/iris|iris/repair)/[A-Za-z0-9][A-Za-z0-9_-]{0,127}", branch
            )
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
                "state": "all",
                "head": f"{owner}:{branch}",
                "base": base_branch,
            },
        )
        for pull in existing:
            if (
                pull.get("head", {}).get("ref") == branch
                and pull.get("base", {}).get("ref") == base_branch
            ):
                if pull.get("state") == "closed" and not pull.get("merged_at"):
                    raise RepairError(
                        "PULL_REQUEST_CLOSED", "Repair pull request was closed", 409
                    )
                if bool(pull.get("draft", False)) != draft and not pull.get(
                    "merged_at"
                ):
                    raise RepairError(
                        "PULL_REQUEST_MODE_CHANGED",
                        "Repair pull request mode changed",
                        409,
                    )
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
                "draft": draft,
            },
        )
        return pull["html_url"]

    async def merge_pull_request(
        self, repository, pull_url, branch, base_branch, commit_sha, base_sha
    ):
        """Reconcile merged PRs before checking base; pin the head in the merge API."""
        validate_repository(repository)
        parsed = urlsplit(pull_url)
        match = re.fullmatch(
            rf"/{re.escape(repository)}/pull/([1-9][0-9]*)", parsed.path
        )
        if parsed.scheme != "https" or parsed.netloc != "github.com" or not match:
            raise RepairError("REPOSITORY_INVALID", "Invalid repair pull request URL")
        if base_branch != "main" or not re.fullmatch(
            r"(?:hotfix/iris|iris/repair)/[A-Za-z0-9][A-Za-z0-9_-]{0,127}", branch
        ):
            raise RepairError(
                "PATH_FORBIDDEN", "Merge requires a hotfix branch into main"
            )
        number = match[1]
        pull = await self.api("GET", repository, f"pulls/{number}")
        if (
            pull.get("head", {}).get("ref") != branch
            or pull.get("head", {}).get("sha") != commit_sha
            or pull.get("head", {}).get("repo", {}).get("full_name", "").lower()
            != repository.lower()
            or pull.get("base", {}).get("ref") != base_branch
            or pull.get("base", {}).get("repo", {}).get("full_name", "").lower()
            != repository.lower()
        ):
            raise RepairError("SOURCE_HEAD_CHANGED", "Repair pull request changed", 409)
        if pull.get("merged"):
            if not pull.get("merge_commit_sha"):
                raise RepairError(
                    "GITHUB_PUBLICATION_FAILED", "Merge SHA unavailable", 502
                )
            return pull["merge_commit_sha"]
        if pull.get("state") != "open":
            raise RepairError(
                "PULL_REQUEST_CLOSED", "Repair pull request was closed", 409
            )
        if pull.get("draft"):
            raise RepairError("MERGE_BLOCKED", "Repair pull request is a draft", 409)
        if await self.head(repository, base_branch) != base_sha:
            raise RepairError(
                "SOURCE_HEAD_CHANGED", "Main branch changed before merge", 409
            )
        response = await self.client.put(
            f"/repos/{repository}/pulls/{number}/merge",
            json={"sha": commit_sha, "merge_method": "merge"},
        )
        if response.status_code == 409:
            raise RepairError(
                "SOURCE_HEAD_CHANGED", "Repair head changed before merge", 409
            )
        if response.status_code in {405, 422}:
            raise RepairError(
                "MERGE_BLOCKED",
                "GitHub checks, protection or conflicts block merge",
                409,
            )
        self.check_auth(response)
        if response.is_error:
            raise RepairError("GITHUB_PUBLICATION_FAILED", "GitHub merge failed", 502)
        result = response.json()
        if not result.get("merged") or not result.get("sha"):
            raise RepairError("MERGE_BLOCKED", "GitHub did not merge the repair", 409)
        return result["sha"]
