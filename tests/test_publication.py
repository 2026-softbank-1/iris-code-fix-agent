import base64
import json

import httpx
import pytest

from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.errors import RepairError
from iris_code_fix_agent.publication import GitHubPublisher


@pytest.mark.parametrize("wrong_preimage", [False, True])
async def test_prepare_checks_preimage_and_preserves_base_tree(wrong_preimage):
    before, after = b"broken\n", b"fixed\n"
    calls = []

    def handler(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body))
        if "/git/ref/" in path:
            result = {"object": {"sha": "a" * 40}}
        elif "/contents/" in path:
            result = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(before).decode(),
            }
        elif request.method == "GET":
            result = {"tree": {"sha": "original-tree"}}
        else:
            result = {"sha": "new-object"}
        return httpx.Response(200, json=result)

    change = {
        "path": "src/app.py",
        "mode": "100755",
        "operation": "update",
        "beforeSha256": sha256(b"unexpected" if wrong_preimage else before),
        "afterSha256": sha256(after),
        "contentBase64": base64.b64encode(after).decode(),
    }
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client:
        publisher = GitHubPublisher(client)
        if wrong_preimage:
            with pytest.raises(RepairError, match="Original source bytes changed"):
                await publisher.prepare(
                    "o/r", "main", "a" * 40, [change], "fix", "2026-10-03T00:00:00Z"
                )
            assert all(method == "GET" for method, _, _ in calls)
        else:
            assert (
                await publisher.prepare(
                    "o/r", "main", "a" * 40, [change], "fix", "2026-10-03T00:00:00Z"
                )
                == "new-object"
            )
            tree = next(
                body for method, path, body in calls if path.endswith("git/trees")
            )
            assert tree["base_tree"] == "original-tree"
            assert tree["tree"] == [
                {
                    "path": "src/app.py",
                    "mode": "100755",
                    "type": "blob",
                    "sha": "new-object",
                }
            ]
            commit = calls[-1][2]
            assert commit["parents"] == ["a" * 40]
            assert commit["author"]["date"] == "2026-10-03T00:00:00Z"


async def test_publish_creates_only_repair_branch_and_recovers_lost_response():
    branches = {"main": "a" * 40}
    writes = []

    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            writes.append((request.url.path, body))
            branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={"object": {"sha": body["sha"]}})
        branch = request.url.path.split("heads/", 1)[1]
        if branch not in branches:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"object": {"sha": branches[branch]}})

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client:
        publisher = GitHubPublisher(client)
        await publisher.publish("o/r", "iris/repair/attempt-1", "b" * 40)
        await publisher.publish("o/r", "iris/repair/attempt-1", "b" * 40)
        assert branches["main"] == "a" * 40
        assert writes == [
            (
                "/repos/o/r/git/refs",
                {"ref": "refs/heads/iris/repair/attempt-1", "sha": "b" * 40},
            )
        ]
        with pytest.raises(RepairError, match="Repair branch changed"):
            await publisher.publish("o/r", "iris/repair/attempt-1", "c" * 40)
        with pytest.raises(RepairError, match="repair branch"):
            await publisher.publish("o/r", "main", "b" * 40)


async def test_open_pull_request_is_draft_and_reuses_exact_head_base():
    pulls = []
    writes = []

    def handler(request):
        if request.method == "GET":
            assert request.url.params["head"] == "o:iris/repair/attempt-1"
            assert request.url.params["base"] == "main"
            return httpx.Response(200, json=pulls)
        body = json.loads(request.content)
        writes.append(body)
        assert body["draft"] is True
        pulls.append(
            {
                "head": {"ref": body["head"]},
                "base": {"ref": body["base"]},
                "html_url": "https://github.com/o/r/pull/1",
                "draft": body["draft"],
                "state": "open",
            }
        )
        return httpx.Response(201, json=pulls[0])

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client:
        publisher = GitHubPublisher(client)
        first = await publisher.open_pull_request(
            "o/r", "iris/repair/attempt-1", "main", "fix", "unverified"
        )
        second = await publisher.open_pull_request(
            "o/r", "iris/repair/attempt-1", "main", "fix", "unverified"
        )
        assert first == second == "https://github.com/o/r/pull/1"
        assert len(writes) == 1


def pull_record(**overrides):
    return {
        "head": {
            "ref": "hotfix/iris/attempt-1",
            "sha": "b" * 40,
            "repo": {"full_name": "o/r"},
        },
        "base": {"ref": "main", "repo": {"full_name": "o/r"}},
        "state": "open",
        "draft": False,
        "merged": False,
        **overrides,
    }


@pytest.mark.parametrize("lost_response", [False, True])
async def test_hotfix_merge_pins_head_and_reconciles_lost_response(lost_response):
    pull = pull_record()
    main = "a" * 40
    writes = []

    def handler(request):
        nonlocal main
        if request.method == "PUT":
            body = json.loads(request.content)
            assert body == {"sha": "b" * 40, "merge_method": "merge"}
            writes.append(body)
            main = "c" * 40
            pull.update(merged=True, state="closed", merge_commit_sha=main)
            if lost_response:
                raise httpx.ReadTimeout("Lost response")
            return httpx.Response(200, json={"merged": True, "sha": main})
        if "/git/ref/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": main}})
        return httpx.Response(200, json=pull)

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client:
        publisher = GitHubPublisher(client)
        args = (
            "o/r",
            "https://github.com/o/r/pull/1",
            "hotfix/iris/attempt-1",
            "main",
            "b" * 40,
            "a" * 40,
        )
        if lost_response:
            with pytest.raises(httpx.ReadTimeout):
                await publisher.merge_pull_request(*args)
        else:
            assert await publisher.merge_pull_request(*args) == "c" * 40
        assert await publisher.merge_pull_request(*args) == "c" * 40
        assert len(writes) == 1


@pytest.mark.parametrize(
    "case,expected",
    [
        ("head", "SOURCE_HEAD_CHANGED"),
        ("main", "SOURCE_HEAD_CHANGED"),
        ("fork", "SOURCE_HEAD_CHANGED"),
        ("closed", "PULL_REQUEST_CLOSED"),
        ("draft", "MERGE_BLOCKED"),
        ("protected", "MERGE_BLOCKED"),
        ("conflict", "MERGE_BLOCKED"),
        ("head_race", "SOURCE_HEAD_CHANGED"),
    ],
)
async def test_merge_refuses_changed_or_blocked_pr(case, expected):
    pull = pull_record()
    writes = []
    if case == "head":
        pull["head"]["sha"] = "x" * 40
    if case == "fork":
        pull["head"]["repo"]["full_name"] = "fork/r"
    if case == "closed":
        pull["state"] = "closed"
    if case == "draft":
        pull["draft"] = True

    def handler(request):
        if request.method == "PUT":
            writes.append(request)
            return httpx.Response(
                {"protected": 405, "conflict": 422, "head_race": 409}[case], json={}
            )
        if "/git/ref/" in request.url.path:
            return httpx.Response(
                200, json={"object": {"sha": ("x" if case == "main" else "a") * 40}}
            )
        return httpx.Response(200, json=pull)

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RepairError) as error:
            await GitHubPublisher(client).merge_pull_request(
                "o/r",
                "https://github.com/o/r/pull/1",
                "hotfix/iris/attempt-1",
                "main",
                "b" * 40,
                "a" * 40,
            )
        assert error.value.code == expected
        assert len(writes) == int(case in {"protected", "conflict", "head_race"})


async def test_open_hotfix_pr_is_ready_and_recovers_already_merged_pr():
    pulls, writes = [], []

    def handler(request):
        if request.method == "GET":
            assert request.url.params["state"] == "all"
            return httpx.Response(200, json=pulls)
        body = json.loads(request.content)
        assert body["draft"] is False
        writes.append(body)
        pulls.append({**pull_record(), "html_url": "https://github.com/o/r/pull/1"})
        return httpx.Response(201, json=pulls[0])

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client:
        publisher = GitHubPublisher(client)
        args = ("o/r", "hotfix/iris/attempt-1", "main", "fix", "body")
        assert (
            await publisher.open_pull_request(*args, draft=False)
            == "https://github.com/o/r/pull/1"
        )
        pulls[0].update(state="closed", merged_at="2026-10-03T00:00:00Z")
        assert (
            await publisher.open_pull_request(*args, draft=False)
            == "https://github.com/o/r/pull/1"
        )
        assert len(writes) == 1


@pytest.mark.parametrize(
    "status,code", [(401, "GITHUB_AUTH_FAILED"), (403, "GITHUB_PERMISSION_DENIED")]
)
@pytest.mark.parametrize("operation", ["head", "preimage", "publish", "merge"])
async def test_auth_failures_are_not_misreported_as_changed_source(
    status, code, operation
):
    def handler(request):
        if operation in {"preimage", "merge"}:
            if "/git/ref/" in request.url.path:
                return httpx.Response(200, json={"object": {"sha": "a" * 40}})
            if "/git/commits/" in request.url.path:
                return httpx.Response(200, json={"tree": {"sha": "original-tree"}})
            if request.url.path.endswith("pulls/1"):
                return httpx.Response(200, json=pull_record())
        return httpx.Response(
            status, json={"message": "do not expose provider details"}
        )

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as client:
        publisher = GitHubPublisher(client)
        with pytest.raises(RepairError) as error:
            if operation == "head":
                await publisher.head("o/r", "main")
            elif operation == "publish":
                await publisher.publish("o/r", "hotfix/iris/attempt-1", "b" * 40)
            elif operation == "merge":
                await publisher.merge_pull_request(
                    "o/r",
                    "https://github.com/o/r/pull/1",
                    "hotfix/iris/attempt-1",
                    "main",
                    "b" * 40,
                    "a" * 40,
                )
            else:
                await publisher.prepare(
                    "o/r",
                    "main",
                    "a" * 40,
                    [
                        {
                            "path": "app.py",
                            "mode": "100644",
                            "operation": "update",
                            "beforeSha256": sha256(b"before"),
                            "afterSha256": sha256(b"after"),
                            "contentBase64": base64.b64encode(b"after").decode(),
                        }
                    ],
                    "fix",
                    "2026-10-03T00:00:00Z",
                )
        assert error.value.code == code
        assert "provider details" not in str(error.value)
