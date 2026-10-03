from types import SimpleNamespace as NS

from iris_code_fix_agent.context import build_context, exposed_paths
from iris_code_fix_agent.redaction import mask_value


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
