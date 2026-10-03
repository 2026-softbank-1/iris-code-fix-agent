import json
import time
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from iris_code_fix_agent.errors import RepairError
from iris_code_fix_agent.github_auth import GitHubAuth
from iris_code_fix_agent.redaction import mask_text


@pytest.fixture
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    ), key.public_key()


@pytest.mark.parametrize("discover", [True, False])
async def test_app_scopes_token_signs_jwt_caches_and_refreshes(
    rsa_key, monkeypatch, discover
):
    now = time.time()
    clock = [now]
    monkeypatch.setattr("iris_code_fix_agent.github_auth.time.time", lambda: clock[0])
    calls = []
    token_count = []

    def handler(request):
        calls.append(request.url.path)
        if (
            request.url.path.endswith("/installation")
            or "/access_tokens" in request.url.path
        ):
            claims = jwt.decode(
                request.headers["Authorization"].split()[1],
                rsa_key[1],
                algorithms=["RS256"],
                options={"verify_iat": False},
            )
            assert claims["iss"] == "123"
            assert claims["iat"] == int(clock[0]) - 60
            assert claims["exp"] == int(clock[0]) + 540
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 456})
        if "/access_tokens" in request.url.path:
            assert json.loads(request.content) == {
                "repositories": ["r"],
                "permissions": {"contents": "write", "pull_requests": "write"},
            }
            token_count.append(1)
            return httpx.Response(
                201,
                json={
                    "token": f"installation-token-{len(token_count)}",
                    "expires_at": datetime.fromtimestamp(
                        clock[0] + 3600, UTC
                    ).isoformat(),
                    "permissions": {"contents": "write", "pull_requests": "write"},
                },
            )
        assert (
            request.headers["Authorization"]
            == f"Bearer installation-token-{len(token_count)}"
        )
        return httpx.Response(200, json={})

    auth = GitHubAuth(
        app_id="123",
        private_key=rsa_key[0],
        installation_id=None if discover else "456",
    )
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        auth=auth,
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get("/repos/o/r")
        await client.get("/repos/o/r/git/ref/heads/main")
        assert len(token_count) == 1
        clock[0] += 3550
        await client.get("/repos/o/r")
        assert len(token_count) == 2
        # Same repo name under another owner still needs its own scoped exchange.
        await client.get("/repos/other/r")
        assert len(token_count) == 3
        assert sum(path.endswith("/installation") for path in calls) == (
            3 if discover else 0
        )


@pytest.mark.parametrize("mode", ["token", "app"])
async def test_auth_never_sends_secrets_to_another_host(mode, rsa_key):
    calls = []
    auth = (
        GitHubAuth(token="secret")
        if mode == "token"
        else GitHubAuth(app_id="123", private_key=rsa_key[0])
    )
    async with httpx.AsyncClient(
        auth=auth, transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        for url in (
            "https://evil.test/repos/o/r",
            "http://api.github.com/repos/o/r",
            "https://api.github.com:8443/repos/o/r",
        ):
            with pytest.raises(RepairError) as exc:
                await client.get(url)
            assert exc.value.code == "GITHUB_AUTH_HOST_INVALID"
        assert calls == []


async def test_pat_and_invalid_app_token(rsa_key):
    def token_handler(request):
        assert request.headers["Authorization"] == "Bearer fake-pat"
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        auth=GitHubAuth(token="fake-pat"), transport=httpx.MockTransport(token_handler)
    ) as client:
        assert (await client.get("https://api.github.com/repos/o/r")).status_code == 200
    auth = GitHubAuth(app_id="123", private_key=rsa_key[0], installation_id="456")
    async with httpx.AsyncClient(
        auth=auth,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(201, json={"token": "secret"})
        ),
    ) as client:
        with pytest.raises(RepairError) as exc:
            await client.get("https://api.github.com/repos/o/r")
        assert exc.value.code == "GITHUB_AUTH_FAILED"


def test_environment_requires_complete_credentials_and_valid_key(
    monkeypatch, tmp_path, rsa_key
):
    for key in (
        "GITHUB_AUTH_MODE",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_FILE",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RepairError):
        GitHubAuth.from_environment()
    monkeypatch.setenv("GITHUB_TOKEN", "fake-pat")
    assert GitHubAuth.from_environment().mode == "token"
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    with pytest.raises(RepairError):
        GitHubAuth.from_environment()
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(rsa_key[0])
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_FILE", str(key_path))
    assert GitHubAuth.from_environment().mode == "app"
    key_path.write_text("invalid-secret-key")
    with pytest.raises(RepairError) as error:
        GitHubAuth.from_environment()
    assert "invalid-secret-key" not in str(error.value)


def test_fine_grained_pat_redacted():
    secret = "github_pat_" + "aB_" * 20
    assert secret not in mask_text("credential " + secret)


async def test_revoked_installation_token_refreshes_next_request_without_replaying_write(
    rsa_key,
):
    exchanges, writes = [], []

    def handler(request):
        if "/access_tokens" in request.url.path:
            exchanges.append(request)
            return httpx.Response(
                201,
                json={
                    "token": f"token-{len(exchanges)}",
                    "expires_at": datetime.fromtimestamp(
                        time.time() + 3600, UTC
                    ).isoformat(),
                    "permissions": {"contents": "write", "pull_requests": "write"},
                },
            )
        writes.append(request)
        return httpx.Response(401 if len(writes) == 1 else 200, json={})

    auth = GitHubAuth(app_id="123", private_key=rsa_key[0], installation_id="456")
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        auth=auth,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert (
            await client.post(
                "/repos/o/r/git/refs", json={"ref": "refs/heads/hotfix/iris/test"}
            )
        ).status_code == 401
        assert len(writes) == 1
        assert (
            await client.get("/repos/o/r/git/ref/heads/hotfix/iris/test")
        ).status_code == 200
        assert len(exchanges) == 2
        assert writes[1].headers["Authorization"] == "Bearer token-2"


async def test_was_auth_receives_scoped_token_caches_and_refreshes(monkeypatch):
    from iris_code_fix_agent.github_auth import WasGitHubAuth

    now = [time.time()]
    monkeypatch.setattr("iris_code_fix_agent.github_auth.time.time", lambda: now[0])
    exchanges, requests = [], []

    def was_handler(request):
        assert request.headers["Authorization"] == "Bearer was-session"
        assert request.url.path == "/api/v1/services/123/repair-github-token"
        assert json.loads(request.content) == {"repository": "o/r"}
        exchanges.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "repository": "o/r",
                    "token": f"github-secret-{len(exchanges)}",
                    "expiresAt": datetime.fromtimestamp(now[0] + 3600, UTC).isoformat(),
                },
            },
        )

    def github_handler(request):
        requests.append(request)
        assert (
            request.headers["Authorization"] == f"Bearer github-secret-{len(exchanges)}"
        )
        assert "was-session" not in request.headers.values()
        return httpx.Response(200, json={})

    async with (
        httpx.AsyncClient(
            base_url="https://was.test",
            headers={"Authorization": "Bearer was-session"},
            transport=httpx.MockTransport(was_handler),
        ) as was,
        httpx.AsyncClient(
            base_url="https://api.github.com",
            auth=WasGitHubAuth(was, 123),
            transport=httpx.MockTransport(github_handler),
        ) as github,
    ):
        await github.get("/repos/o/r")
        await github.get("/repos/o/r/git/ref/heads/main")
        assert len(exchanges) == 1
        now[0] += 3550
        await github.get("/repos/o/r")
        assert len(exchanges) == 2
        assert len(requests) == 3


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "WAS_AUTH_FAILED"),
        (403, "GITHUB_PERMISSION_DENIED"),
        (404, "WAS_SERVICE_UNAVAILABLE"),
        (409, "SOURCE_HEAD_CHANGED"),
        (503, "GITHUB_AUTH_CONFIG_INVALID"),
    ],
)
async def test_was_auth_rejects_missing_login_or_repo_access(status, code):
    from iris_code_fix_agent.github_auth import WasGitHubAuth

    calls = []
    async with (
        httpx.AsyncClient(
            base_url="https://was.test",
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json={})),
        ) as was,
        httpx.AsyncClient(
            base_url="https://api.github.com",
            auth=WasGitHubAuth(was, 123),
            transport=httpx.MockTransport(lambda request: calls.append(request)),
        ) as github,
    ):
        with pytest.raises(RepairError) as exc:
            await github.get("/repos/o/r")
        assert exc.value.code == code
        assert calls == []


@pytest.mark.parametrize(
    "case",
    ["wrong_repository", "expired", "no_timezone", "missing_token", "failed_envelope"],
)
async def test_was_auth_rejects_invalid_token_response(case):
    from iris_code_fix_agent.github_auth import WasGitHubAuth

    data = {
        "repository": "o/r",
        "token": "ghs_secret",
        "expiresAt": datetime.fromtimestamp(time.time() + 3600, UTC).isoformat(),
    }
    if case == "wrong_repository":
        data["repository"] = "other/r"
    elif case == "expired":
        data["expiresAt"] = "2020-01-01T00:00:00Z"
    elif case == "no_timezone":
        data["expiresAt"] = "2030-01-01T00:00:00"
    elif case == "missing_token":
        data.pop("token")
    async with (
        httpx.AsyncClient(
            base_url="https://was.test",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"success": case != "failed_envelope", "data": data}
                )
            ),
        ) as was,
        httpx.AsyncClient(
            base_url="https://api.github.com",
            auth=WasGitHubAuth(was, 123),
            transport=httpx.MockTransport(
                lambda _: pytest.fail("invalid token reached GitHub")
            ),
        ) as github,
    ):
        with pytest.raises(RepairError) as exc:
            await github.get("/repos/o/r")
        assert exc.value.code == "GITHUB_AUTH_FAILED"
        assert "ghs_secret" not in str(exc.value)


async def test_was_auth_requires_https_and_never_leaks_token_to_other_host():
    from iris_code_fix_agent.github_auth import WasGitHubAuth

    async with httpx.AsyncClient(base_url="http://remote.test") as was:
        with pytest.raises(RepairError):
            WasGitHubAuth(was, 123)
    exchanges = []
    async with (
        httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(lambda request: exchanges.append(request)),
        ) as was,
        httpx.AsyncClient(auth=WasGitHubAuth(was, 123)) as github,
    ):
        with pytest.raises(RepairError):
            await github.get("https://other.test/repos/o/r")
        assert exchanges == []


async def test_was_auth_can_publish_hotfix_and_merge_through_github():
    from iris_code_fix_agent.github_auth import WasGitHubAuth
    from iris_code_fix_agent.publication import GitHubPublisher

    base, candidate, merged = "a" * 40, "b" * 40, "c" * 40
    branches, pulls, writes, exchanges = {"main": base}, [], [], []
    branch = "hotfix/iris/was-integration"

    def was_handler(request):
        exchanges.append(request)
        assert request.headers["Authorization"] == "Bearer was-session"
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": "o/r",
                    "token": "ghs_scoped",
                    "expiresAt": datetime.fromtimestamp(
                        time.time() + 3600, UTC
                    ).isoformat(),
                }
            },
        )

    def github_handler(request):
        assert request.headers["Authorization"] == "Bearer ghs_scoped"
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if request.method in {"POST", "PUT", "PATCH"}:
            writes.append((request.method, path))
        if "/git/ref/heads/" in path:
            ref = path.split("heads/", 1)[1]
            return (
                httpx.Response(200, json={"object": {"sha": branches[ref]}})
                if ref in branches
                else httpx.Response(404, json={})
            )
        if path.endswith("git/refs"):
            branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={})
        if path.endswith("/merge"):
            assert body == {"sha": candidate, "merge_method": "merge"}
            branches["main"] = merged
            pulls[0].update(merged=True, merge_commit_sha=merged, state="closed")
            return httpx.Response(200, json={"merged": True, "sha": merged})
        if path.endswith("pulls/1"):
            return httpx.Response(200, json=pulls[0])
        if request.method == "GET":
            return httpx.Response(200, json=pulls)
        assert body["draft"] is False
        pulls.append(
            {
                "head": {"ref": branch, "sha": candidate, "repo": {"full_name": "o/r"}},
                "base": {"ref": "main", "repo": {"full_name": "o/r"}},
                "html_url": "https://github.com/o/r/pull/1",
                "state": "open",
                "draft": False,
            }
        )
        return httpx.Response(201, json=pulls[0])

    async with (
        httpx.AsyncClient(
            base_url="https://was.test",
            headers={"Authorization": "Bearer was-session"},
            transport=httpx.MockTransport(was_handler),
        ) as was,
        httpx.AsyncClient(
            base_url="https://api.github.com",
            auth=WasGitHubAuth(was, 123),
            transport=httpx.MockTransport(github_handler),
        ) as github,
    ):
        publisher = GitHubPublisher(github)
        assert await publisher.head("o/r", "main") == base
        await publisher.publish("o/r", branch, candidate)
        url = await publisher.open_pull_request(
            "o/r", branch, "main", "fix", "body", draft=False
        )
        assert (
            await publisher.merge_pull_request(
                "o/r", url, branch, "main", candidate, base
            )
            == merged
        )
        assert (
            await publisher.merge_pull_request(
                "o/r", url, branch, "main", candidate, base
            )
            == merged
        )
        assert len(exchanges) == 1
        assert writes == [
            ("POST", "/repos/o/r/git/refs"),
            ("POST", "/repos/o/r/pulls"),
            ("PUT", "/repos/o/r/pulls/1/merge"),
        ]
