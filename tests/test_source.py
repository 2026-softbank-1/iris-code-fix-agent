import io
import tarfile

import pytest

from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.contracts import SourceSpec
from iris_code_fix_agent.errors import RepairError
from iris_code_fix_agent.source import SourceFile, from_archive, manifest_digest


def archive(entries):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            tar.addfile(info, io.BytesIO(data) if info.size else None)
    return out.getvalue()


def spec(data, files):
    return SourceSpec(
        repository_id="repo",
        base_commit_sha="a" * 40,
        root_directory=".",
        download_url="https://bucket.s3.amazonaws.com/source?secret=never-log",
        archive_sha256=sha256(data),
        manifest_sha256=manifest_digest(files),
    )


def test_wrapper_and_manifest():
    data = archive([("repo-sha/src/a.py", b"hello", tarfile.REGTYPE)])
    files = {"src/a.py": SourceFile(b"hello")}
    assert from_archive(data, spec(data, files)).files == files


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("a\\b", tarfile.REGTYPE),
        ("symlink", tarfile.SYMTYPE),
        ("fifo", tarfile.FIFOTYPE),
    ],
)
def test_unsafe_entries(name, kind):
    data = archive([(name, b"x", kind)])
    with pytest.raises(RepairError) as error:
        from_archive(data, spec(data, {"x": SourceFile(b"x")}))
    assert error.value.code == "SOURCE_UNSAFE"


def test_duplicate_and_digest():
    data = archive([("a", b"x", tarfile.REGTYPE), ("a", b"y", tarfile.REGTYPE)])
    with pytest.raises(RepairError):
        from_archive(data, spec(data, {"a": SourceFile(b"x")}))
    data = archive([("a", b"x", tarfile.REGTYPE)])
    with pytest.raises(RepairError) as error:
        from_archive(data, spec(data, {"a": SourceFile(b"y")}))
    assert error.value.code == "SOURCE_INTEGRITY"


@pytest.mark.parametrize(
    "entries",
    [
        [("a", b"x", tarfile.REGTYPE), ("a/b", b"y", tarfile.REGTYPE)],
        [("a", b"x", tarfile.REGTYPE), ("a/b", b"", tarfile.DIRTYPE)],
    ],
)
def test_file_descendant_collisions(entries):
    data = archive(entries)
    with pytest.raises(RepairError) as error:
        from_archive(data, spec(data, {"a": SourceFile(b"x")}))
    assert error.value.code == "SOURCE_UNSAFE"


def test_file_count_limit_is_reported_as_too_large(monkeypatch):
    monkeypatch.setattr("iris_code_fix_agent.source.MAX_FILES", 3)
    data = archive([(f"f{i}", b"x", tarfile.REGTYPE) for i in range(4)])
    with pytest.raises(RepairError) as error:
        from_archive(data, spec(data, {"f0": SourceFile(b"x")}))
    assert error.value.code == "SOURCE_TOO_LARGE"


def test_single_file_limit_is_reported_as_too_large(monkeypatch):
    monkeypatch.setattr("iris_code_fix_agent.source.MAX_FILE", 4)
    data = archive([("big", b"12345", tarfile.REGTYPE)])
    with pytest.raises(RepairError) as error:
        from_archive(data, spec(data, {"big": SourceFile(b"12345")}))
    assert error.value.code == "SOURCE_TOO_LARGE"
