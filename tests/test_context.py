from types import SimpleNamespace as NS

import pytest

from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.context import build_context, exposed_paths
from iris_code_fix_agent.contracts import ModelProposal, RepairRequest
from iris_code_fix_agent.errors import RepairError
from iris_code_fix_agent.pipeline import _finish_candidate
from iris_code_fix_agent.redaction import mask_value
from iris_code_fix_agent.runner import RunnerResponse
from iris_code_fix_agent.source import SourceFile


def test_secret_file_omission_and_relevance():
    request = NS(
        request_id="repair-1",
        diagnosis_result={
            "source_analysis": {"findings": [{"path": "src/z.py"}]},
            "evidence": [{"message": "api_key=abcdefghijk123456"}],
        },
        plan_ids=["R1"],
        source=NS(root_directory=".", base_commit_sha="a" * 40),
        policy=NS(
            allowed_paths=["**"],
            protected_paths=[],
            max_changed_files=5,
            max_changed_bytes=65536,
        ),
    )
    snapshot = NS(
        files={
            "src/a.py": NS(data=b'password="abcdefghijk123"'),
            "src/z.py": NS(data=b"print(1)\n"),
            ".env": NS(data=b"SECRET=hi"),
            ".github/workflow.yml": NS(data=b"name: protected"),
        }
    )
    context = build_context(request, snapshot)
    assert exposed_paths(context) == {"src/z.py"}
    assert context["omittedFiles"][0]["reason"] == "secret_detected"
    assert "abcdefghijk123456" not in str(context)
    assert context["files"][0]["content"] == "print(1)\n"


def test_mask_nested_download_url():
    assert (
        mask_value({"downloadUrl": "https://private?signature=abc"})["downloadUrl"]
        == "[REDACTED]"
    )


def test_overlapping_secrets_mask_union_without_suffix_leak():
    from iris_code_fix_agent.redaction import mask_text, secret_ranges

    text = "authorization=ghp_abcdefghijklmnopqrstuvwxyz0123456789 tail"
    spans = secret_ranges(text)
    assert len(spans) == 1
    assert mask_text(text) == "[REDACTED] tail"


def test_plan_targets_survive_the_context_budget(monkeypatch):
    monkeypatch.setattr("iris_code_fix_agent.context.MAX_CONTEXT_BYTES", 30)
    request = NS(
        request_id="repair-1",
        diagnosis_result={
            "analysis": {
                "remediation": {
                    "plans": [
                        {
                            "id": "R1",
                            "changes": [{"kind": "code", "target": "./z.py:12"}],
                        },
                        {"id": "R2", "changes": [{"kind": "code", "target": "a.py"}]},
                    ]
                }
            }
        },
        plan_ids=["R1"],
        source=NS(root_directory=".", base_commit_sha="a" * 40),
        policy=NS(
            allowed_paths=["**"],
            protected_paths=[],
            max_changed_files=5,
            max_changed_bytes=65536,
        ),
    )
    snapshot = NS(
        files={
            "a.py": NS(data=b"a = 1\n" * 4),
            "z.py": NS(data=b"z = 1\n" * 4),
        }
    )
    context = build_context(request, snapshot)
    # Only the selected plan's target fits; the unselected plan's file is dropped.
    assert exposed_paths(context) == {"z.py"}


@pytest.fixture
def environment_context(syntax_repair_fixture):
    _, payload, _ = syntax_repair_fixture
    request = RepairRequest.model_validate(payload)
    request.policy.allowed_paths = ["app.py"]
    snapshot = NS(
        files={
            "app.py": SourceFile(b"print(1)\n", "100644"),
            "package.json": SourceFile(b'{"scripts":{"build":"tsc"}}', "100644"),
            "Dockerfile": SourceFile(b"FROM node:22\n", "100644"),
            "unrelated.txt": SourceFile(b"private context", "100644"),
        }
    )
    return request, snapshot


def test_environment_references_do_not_grant_write_access(environment_context):
    request, snapshot = environment_context
    context = build_context(request, snapshot)
    assert exposed_paths(context) == {"app.py"}
    assert {item["path"] for item in context["referenceFiles"]} == {
        "package.json",
        "Dockerfile",
    }
    assert "private context" not in str(context)
    for item in context["referenceFiles"]:
        assert item["sha256"] == sha256(snapshot.files[item["path"]].data)


def test_environment_references_keep_secret_and_protection_boundary(
    environment_context,
):
    request, snapshot = environment_context
    request.policy.protected_paths = ["Dockerfile"]
    snapshot.files["package.json"] = SourceFile(
        b'{"password":"abcdefghijk123456"}', "100644"
    )
    snapshot.files[".env.example"] = SourceFile(b"PORT=8000\n", "100644")
    snapshot.files[".github/package.json"] = SourceFile(b"{}", "100644")
    context = build_context(request, snapshot)
    assert context["referenceFiles"] == []
    assert "abcdefghijk123456" not in str(context)
    assert context["omittedFiles"] == [
        {
            "path": "package.json",
            "reason": "secret_detected",
            "redactionRanges": context["omittedFiles"][0]["redactionRanges"],
        }
    ]


def test_reference_budget_does_not_starve_target(environment_context, monkeypatch):
    request, snapshot = environment_context
    monkeypatch.setattr("iris_code_fix_agent.context.MAX_CONTEXT_BYTES", 30)
    monkeypatch.setattr("iris_code_fix_agent.context.MAX_REFERENCE_BYTES", 8)
    context = build_context(request, snapshot)
    assert exposed_paths(context) == {"app.py"}
    assert context["referenceFiles"] == []
    assert {item["reason"] for item in context["omittedFiles"]} == {"context_budget"}


def test_environment_references_stay_inside_service_root(environment_context):
    request, snapshot = environment_context
    request.source.root_directory = "services/api"
    request.policy.allowed_paths = ["services/api/app.py"]
    snapshot.files["services/api/app.py"] = snapshot.files["app.py"]
    snapshot.files["services/api/package.json"] = snapshot.files["package.json"]
    context = build_context(request, snapshot)
    assert exposed_paths(context) == {"services/api/app.py"}
    assert [item["path"] for item in context["referenceFiles"]] == [
        "services/api/package.json"
    ]


@pytest.mark.parametrize("operation", ["update", "create"])
def test_model_cannot_promote_reference_file_to_edit(environment_context, operation):
    request, snapshot = environment_context
    context = build_context(request, snapshot)
    original = snapshot.files["package.json"].data
    proposal = ModelProposal(
        status="candidate_ready",
        summary="Unauthorized manifest change",
        edits=[
            {
                "path": "package.json",
                "operation": operation,
                "beforeSha256": sha256(original) if operation == "update" else None,
                "oldText": original.decode() if operation == "update" else None,
                "newText": "{}",
                "evidenceIds": ["L000001"],
                "planIds": request.plan_ids,
            }
        ],
        limitations=[],
        checksRequired=[],
    )
    with pytest.raises(RepairError) as exc:
        _finish_candidate(
            request,
            snapshot,
            context,
            RunnerResponse(proposal, {}, "fake"),
            {},
        )
    assert exc.value.code == (
        "UNEXPOSED_EDIT" if operation == "update" else "PATH_FORBIDDEN"
    )


def test_allowed_environment_file_is_editable_not_duplicated(environment_context):
    request, snapshot = environment_context
    request.policy.allowed_paths.append("package.json")
    context = build_context(request, snapshot)
    assert exposed_paths(context) == {"app.py", "package.json"}
    assert [item["path"] for item in context["referenceFiles"]] == ["Dockerfile"]
