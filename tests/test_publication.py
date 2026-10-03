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
