from datetime import UTC, datetime

import pytest

from iris_code_fix_agent.apply import apply_proposal
from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.contracts import ModelProposal, RepairPolicy, TextEdit
from iris_code_fix_agent.errors import RepairError
from iris_code_fix_agent.source import SourceFile, SourceSnapshot, manifest_digest


def setup(data=b"a\r\nb\r\n"):
    files = {
        "src/main.py": SourceFile(data, "100755"),
        "other.txt": SourceFile(b"keep"),
    }
    return SourceSnapshot(files, manifest_digest(files)), RepairPolicy(
        allowed_paths=["src/**"], deadline=datetime.now(UTC), max_cost_usd=1
    )


def proposal(snapshot, old="a", new="x", path="src/main.py"):
    return ModelProposal(
        status="candidate_ready",
        summary="fix",
        edits=[
            TextEdit(
                path=path,
                operation="update",
                before_sha256=sha256(snapshot.files["src/main.py"].data),
                old_text=old,
                new_text=new,
                evidence_ids=["EV1"],
                plan_ids=["R1"],
            )
        ],
        limitations=[],
        checks_required=[],
    )


def test_preserves_bytes_and_mode_and_source():
    snapshot, policy = setup()
    result = apply_proposal(snapshot, proposal(snapshot), policy, ".")
    assert result.files["src/main.py"].data == b"x\r\nb\r\n"
    assert result.files["src/main.py"].mode == "100755"
    assert snapshot.files["src/main.py"].data == b"a\r\nb\r\n"
    assert result.files["other.txt"] == snapshot.files["other.txt"]


@pytest.mark.parametrize(
    "old,code", [("missing", "AMBIGUOUS_EDIT"), ("a", "AMBIGUOUS_EDIT")]
)
def test_unique_occurrence(old, code):
    snapshot, policy = setup(b"aaa")
    with pytest.raises(RepairError, match="exactly once"):
        apply_proposal(snapshot, proposal(snapshot, old), policy, ".")


def test_hash_and_root_boundary():
    snapshot, policy = setup()
    p = proposal(snapshot)
    p.edits[0].before_sha256 = "0" * 64
    with pytest.raises(RepairError) as error:
        apply_proposal(snapshot, p, policy, ".")
    assert error.value.code == "PREIMAGE_MISMATCH"
    with pytest.raises(RepairError) as error:
        apply_proposal(snapshot, proposal(snapshot), policy, "app")
    assert error.value.code == "PATH_FORBIDDEN"


def test_original_offsets_and_no_newline():
    snapshot, policy = setup(b"alpha beta")
    p = proposal(snapshot, "alpha", "A")
    p.edits.append(
        TextEdit(
            path="src/main.py",
            operation="update",
            before_sha256=sha256(b"alpha beta"),
            old_text="beta",
            new_text="B",
            evidence_ids=["EV1"],
            plan_ids=["R1"],
        )
    )
    result = apply_proposal(snapshot, p, policy, ".")
    assert result.files["src/main.py"].data == b"A B"
    assert result.patch.count("\\ No newline at end of file") == 2


def test_overlap_rejected():
    snapshot, policy = setup(b"alpha beta")
    p = proposal(snapshot, "alpha beta", "A")
    p.edits.append(
        TextEdit(
            path="src/main.py",
            operation="update",
            before_sha256=sha256(b"alpha beta"),
            old_text="beta",
            new_text="B",
            evidence_ids=[],
            plan_ids=[],
        )
    )
    with pytest.raises(RepairError) as error:
        apply_proposal(snapshot, p, policy, ".")
    assert error.value.code == "OVERLAPPING_EDITS"


def test_patch_applies_with_git_and_preserves_no_newline(tmp_path):
    import subprocess

    snapshot, policy = setup(b"alpha beta")
    result = apply_proposal(snapshot, proposal(snapshot, "alpha", "A"), policy, ".")
    target = tmp_path / "src" / "main.py"
    target.parent.mkdir()
    target.write_bytes(b"alpha beta")
    subprocess.run(
        ["git", "apply", "--check", "-"],
        input=result.patch.encode(),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "apply", "-"],
        input=result.patch.encode(),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert target.read_bytes() == b"A beta"


@pytest.mark.parametrize("existing,path", [("src/a", "src/a/b"), ("src/a/b", "src/a")])
def test_candidate_tree_collisions(existing, path):
    files = {existing: SourceFile(b"original")}
    snapshot = SourceSnapshot(files, manifest_digest(files))
    _, policy = setup()
    p = ModelProposal(
        status="candidate_ready",
        summary="create",
        edits=[
            TextEdit(
                path=path,
                operation="create",
                before_sha256=None,
                old_text=None,
                new_text="new",
                evidence_ids=["EV1"],
                plan_ids=["R1"],
            )
        ],
        limitations=[],
        checks_required=[],
    )
    with pytest.raises(RepairError) as error:
        apply_proposal(snapshot, p, policy, ".")
    assert error.value.code == "PATH_COLLISION"
    assert snapshot.files == files


@pytest.mark.parametrize("operation", ["create", "update"])
def test_secret_output_rejected(operation):
    snapshot, policy = setup()
    p = proposal(snapshot, new='api_key="sk-abcdefghijklmnop0123456789"')
    if operation == "create":
        p.edits[0] = TextEdit(
            path="src/new.py",
            operation="create",
            before_sha256=None,
            old_text=None,
            new_text=p.edits[0].new_text,
            evidence_ids=["EV1"],
            plan_ids=["R1"],
        )
    with pytest.raises(RepairError) as error:
        apply_proposal(snapshot, p, policy, ".")
    assert error.value.code == "SECRET_CHANGE_FORBIDDEN"


@pytest.mark.parametrize(
    "path",
    [
        "SRC/.SSH/config",
        "src/.AWS/config",
        "src/.npmrc",
        "src/.pypirc",
        "src/CREDENTIALS",
        "src/id_ed25519",
    ],
)
def test_shared_protected_paths(path):
    from iris_code_fix_agent.paths import is_protected_path

    assert is_protected_path(path)


def test_secret_formed_by_original_and_replacement_rejected():
    snapshot, policy = setup(b'api_key="sk-short"')
    p = proposal(snapshot, old="short", new="abcdefghijklmnop0123456789")
    with pytest.raises(RepairError) as error:
        apply_proposal(snapshot, p, policy, ".")
    assert error.value.code == "SECRET_CHANGE_FORBIDDEN"
