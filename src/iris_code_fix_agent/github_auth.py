"""GitHub PAT or repository-scoped, automatically refreshed App authentication."""

import argparse
import asyncio
import json
import os
import re
import time
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

import httpx
import jwt

from .configuration import load_environment
from .errors import RepairError

GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def validate_repository(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(
        part in {".", ".."} for part in repository.split("/")
    ):
        raise RepairError("REPOSITORY_INVALID", "Invalid GitHub repository")
    return repository


class GitHubAuth(httpx.Auth):
    """Keep credentials in memory and only send them to GitHub's HTTPS API."""

    def __init__(
        self, *, token=None, app_id=None, private_key=None, installation_id=None
    ):
        self.token = token
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.mode = "app" if app_id else "token"
        if not (app_id and private_key) and not (token and not app_id):
            raise RepairError(
                "GITHUB_AUTH_CONFIG_INVALID", "GitHub credentials missing"
            )
        self._tokens = {}
        self._lock = asyncio.Lock()

    @classmethod
    def from_environment(cls):
        mode = os.environ.get("GITHUB_AUTH_MODE", "").strip()
        app_id = os.environ.get("GITHUB_APP_ID", "").strip()
        key_file = os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE", "").strip()
        installation = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()
        mode = mode or ("app" if app_id or key_file or installation else "token")
        if mode == "token":
            token = os.environ.get("GITHUB_TOKEN", "").strip()
            if not token:
                raise RepairError("GITHUB_AUTH_CONFIG_INVALID", "Set GITHUB_TOKEN")
            return cls(token=token)
        if mode != "app" or not app_id or not key_file:
            raise RepairError(
                "GITHUB_AUTH_CONFIG_INVALID",
                "Set GITHUB_AUTH_MODE=app, GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_FILE",
            )
        if installation and (not installation.isdecimal() or int(installation) < 1):
            raise RepairError("GITHUB_AUTH_CONFIG_INVALID", "Invalid installation ID")
        try:
            key = Path(key_file).read_text()
            # Validate key at startup without exposing it through diagnostics.
            jwt.encode({"iss": app_id}, key, algorithm="RS256")
        except (OSError, ValueError, jwt.PyJWTError):
            raise RepairError(
                "GITHUB_AUTH_CONFIG_INVALID", "Cannot load GitHub App RSA private key"
            ) from None
        return cls(app_id=app_id, private_key=key, installation_id=installation or None)

    def _jwt(self):
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.app_id},
            self.private_key,
            algorithm="RS256",
        )

    def _app_request(self, method, path, **kwargs):
        return httpx.Request(
            method,
            "https://api.github.com" + path,
            headers={**GITHUB_HEADERS, "Authorization": "Bearer " + self._jwt()},
            **kwargs,
        )

    async def async_auth_flow(self, request):
        if (
            request.url.scheme != "https"
            or request.url.host != "api.github.com"
            or request.url.port not in {None, 443}
        ):
            raise RepairError("GITHUB_AUTH_HOST_INVALID", "Untrusted GitHub API host")
        if self.token:
            request.headers["Authorization"] = "Bearer " + self.token
            yield request
            return
        match = re.match(r"^/repos/([^/]+/[^/]+)(?:/|$)", request.url.path)
        if not match:
            raise RepairError("REPOSITORY_INVALID", "App requests require a repository")
        repository = validate_repository(match[1])
        async with self._lock:
            cached = self._tokens.get(repository)
            if not cached or cached[1] <= time.time() + 60:
                installation_id = self.installation_id
                if installation_id is None:
                    response = yield self._app_request(
                        "GET", f"/repos/{repository}/installation"
                    )
                    await response.aread()
                    if response.is_error:
                        raise RepairError(
                            "GITHUB_AUTH_FAILED",
                            "GitHub App installation unavailable",
                            502,
                        )
                    try:
                        installation_id = response.json()["id"]
                        if not isinstance(installation_id, int) or installation_id < 1:
                            raise ValueError
                    except (KeyError, ValueError, TypeError):
                        raise RepairError(
                            "GITHUB_AUTH_FAILED",
                            "Invalid GitHub installation response",
                            502,
                        ) from None
                response = yield self._app_request(
                    "POST",
                    f"/app/installations/{installation_id}/access_tokens",
                    json={
                        "repositories": [repository.split("/")[1]],
                        "permissions": {"contents": "write", "pull_requests": "write"},
                    },
                )
                await response.aread()
                if response.is_error:
                    raise RepairError(
                        "GITHUB_AUTH_FAILED",
                        "GitHub installation token request failed",
                        502,
                    )
                try:
                    data = response.json()
                    expires = datetime.fromisoformat(data["expires_at"]).timestamp()
                    if (
                        not data["token"]
                        or expires <= time.time() + 60
                        or any(
                            data.get("permissions", {}).get(permission) != "write"
                            for permission in ("contents", "pull_requests")
                        )
                    ):
                        raise ValueError
                    cached = (data["token"], expires)
                except (KeyError, ValueError, TypeError):
                    raise RepairError(
                        "GITHUB_AUTH_FAILED",
                        "Invalid GitHub installation token or permissions",
                        502,
                    ) from None
                self._tokens[repository] = cached
        request.headers["Authorization"] = "Bearer " + cached[0]
        response = yield request
        if response.status_code == 401:
            # A revoked token is refreshed on the next explicit coordinator retry.
            # Never replay a write whose outcome might be uncertain.
            async with self._lock:
                if self._tokens.get(repository) == cached:
                    self._tokens.pop(repository, None)


class WasGitHubAuth(httpx.Auth):
    """Receive repository-scoped tokens from the owner's existing WAS session."""

    mode = "was"

    def __init__(self, was, service_id):
        if service_id < 1:
            raise RepairError("GITHUB_AUTH_CONFIG_INVALID", "Invalid WAS service ID")
        if was.base_url.scheme != "https" and not (
            was.base_url.scheme == "http"
            and was.base_url.host in {"localhost", "127.0.0.1", "::1"}
        ):
            raise RepairError(
                "GITHUB_AUTH_HOST_INVALID", "WAS token exchange requires HTTPS"
            )
        self.was = was
        self.service_id = service_id
        self._tokens = {}
        self._lock = asyncio.Lock()

    async def async_auth_flow(self, request):
        if (
            request.url.scheme != "https"
            or request.url.host != "api.github.com"
            or request.url.port not in {None, 443}
        ):
            raise RepairError("GITHUB_AUTH_HOST_INVALID", "Untrusted GitHub API host")
        match = re.match(r"^/repos/([^/]+/[^/]+)(?:/|$)", request.url.path)
        if not match:
            raise RepairError(
                "REPOSITORY_INVALID", "WAS authentication requires a repository"
            )
        repository = validate_repository(match[1])
        async with self._lock:
            cached = self._tokens.get(repository)
            if not cached or cached[1] <= time.time() + 60:
                response = await self.was.post(
                    f"/api/v1/services/{self.service_id}/repair-github-token",
                    json={"repository": repository},
                    follow_redirects=False,
                )
                if response.is_error:
                    codes = {
                        401: "WAS_AUTH_FAILED",
                        403: "GITHUB_PERMISSION_DENIED",
                        404: "WAS_SERVICE_UNAVAILABLE",
                        409: "SOURCE_HEAD_CHANGED",
                        503: "GITHUB_AUTH_CONFIG_INVALID",
                    }
                    raise RepairError(
                        codes.get(response.status_code, "GITHUB_AUTH_FAILED"),
                        "WAS could not authorize GitHub access",
                        response.status_code,
                    )
                try:
                    body = response.json()
                    if body.get("success") is False:
                        raise ValueError
                    data = body["data"]
                    expires_at = datetime.fromisoformat(data["expiresAt"])
                    value = data["token"]
                    if (
                        data["repository"].lower() != repository.lower()
                        or not isinstance(value, str)
                        or not value
                        or expires_at.tzinfo is None
                        or expires_at.timestamp() <= time.time() + 60
                    ):
                        raise ValueError
                    cached = (value, expires_at.timestamp())
                except (KeyError, ValueError, TypeError, AttributeError):
                    raise RepairError(
                        "GITHUB_AUTH_FAILED", "Invalid WAS GitHub token response", 502
                    ) from None
                self._tokens[repository] = cached
        request.headers["Authorization"] = "Bearer " + cached[0]
        response = yield request
        if response.status_code == 401:
            async with self._lock:
                if self._tokens.get(repository) == cached:
                    self._tokens.pop(repository, None)


def main():
    """Check configured GitHub access without creating refs, commits or PRs."""
    load_environment()
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument(
        "--service-id", type=int, help="WAS service ID (required in default was mode)"
    )
    args = parser.parse_args()
    mode = os.environ.get("GITHUB_AUTH_MODE", "was").strip() or "was"
    if mode == "was" and (not args.service_id or args.service_id < 1):
        parser.error("--service-id is required in was authentication mode")

    async def check():
        from .publication import GitHubPublisher

        async with AsyncExitStack() as stack:
            if mode == "was":
                if not os.environ.get("WAS_BASE_URL") or not os.environ.get(
                    "WAS_TOKEN"
                ):
                    raise RepairError(
                        "GITHUB_AUTH_CONFIG_INVALID", "Set WAS_BASE_URL and WAS_TOKEN"
                    )
                was = await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=os.environ["WAS_BASE_URL"],
                        headers={"Authorization": "Bearer " + os.environ["WAS_TOKEN"]},
                        timeout=30,
                    )
                )
                auth = WasGitHubAuth(was, args.service_id)
            else:
                auth = GitHubAuth.from_environment()
            repository = validate_repository(args.repository)
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url="https://api.github.com",
                    auth=auth,
                    headers=GITHUB_HEADERS,
                    timeout=30,
                )
            )
            publisher = GitHubPublisher(client)
            repo = await publisher.api("GET", repository, "")
            if repo.get("permissions", {}).get("push") is False:
                raise RepairError(
                    "GITHUB_PERMISSION_DENIED", "Repository write access required", 403
                )
            await publisher.head(repository, repo["default_branch"])
            await publisher.api("GET", repository, "pulls", params={"per_page": 1})
            print(
                json.dumps(
                    {
                        "repository": repository,
                        "authMode": auth.mode,
                        "defaultBranch": repo["default_branch"],
                        "status": "AUTHENTICATED",
                    }
                )
            )

    try:
        asyncio.run(check())
    except (RepairError, httpx.HTTPError) as exc:
        parser.exit(
            1,
            (exc.code if isinstance(exc, RepairError) else "GITHUB_UNAVAILABLE") + "\n",
        )
