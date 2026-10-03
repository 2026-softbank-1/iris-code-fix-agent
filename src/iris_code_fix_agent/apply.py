import base64
import difflib
from dataclasses import dataclass
from itertools import pairwise

from .canonical import canonical_json, sha256
from .contracts import ModelProposal, RepairPolicy, safe_path
from .errors import RepairError
from .paths import is_protected_path, matches_path, validate_file_tree
from .redaction import secret_ranges
from .source import SourceFile, SourceSnapshot, manifest_digest


@dataclass(frozen=True)
class AppliedCandidate:
    files: dict[str, SourceFile]
    changes: list[dict]
    patch: str
    manifest_sha256: str
    digest: str


def _matches(path: str, pattern: str) -> bool:
    return matches_path(path, pattern)


def _permitted(path: str, policy: RepairPolicy, root: str) -> bool:
    if is_protected_path(path):
        return False
    return (
        (root == "." or path.startswith(root + "/"))
        and any(_matches(path, p) for p in policy.allowed_paths)
        and not any(_matches(path, p) for p in policy.protected_paths)
    )


def _text(data: bytes) -> str:
    if b"\x00" in data:
        raise RepairError("BINARY_UNSUPPORTED", "Binary edits are unsupported")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise RepairError(
            "BINARY_UNSUPPORTED", "Only UTF-8 text edits are supported"
        ) from None


def _diff(path: str, before: bytes | None, after: bytes) -> str:
    old = [] if before is None else _text(before).splitlines(keepends=True)
    new = _text(after).splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(
            old,
            new,
            fromfile="/dev/null" if before is None else "a/" + path,
            tofile="b/" + path,
        )
    )
    output = []
    for line in lines:
        output.append(line)
        if not line.endswith("\n"):
            output.append("\n\\ No newline at end of file\n")
    return "".join(output)


def apply_proposal(
    snapshot: SourceSnapshot,
    proposal: ModelProposal,
    policy: RepairPolicy,
    root_directory: str,
) -> AppliedCandidate:
    safe_path(root_directory, root=True)
    grouped = {}
    for edit in proposal.edits:
        if secret_ranges(edit.new_text):
            raise RepairError(
                "SECRET_CHANGE_FORBIDDEN", "An edit introduces secret-bearing text"
            )
        if not _permitted(edit.path, policy, root_directory):
            raise RepairError("PATH_FORBIDDEN", "An edit targets a forbidden path")
        grouped.setdefault(edit.path, []).append(edit)
    if len(grouped) > policy.max_changed_files:
        raise RepairError("CHANGE_LIMIT", "Too many changed files")
    files = dict(snapshot.files)
    changes = []
    patches = []
    changed_bytes = 0
    for path, edits in sorted(grouped.items()):
        original = snapshot.files.get(path)
        if original is None:
            if len(edits) != 1 or edits[0].operation != "create":
                raise RepairError(
                    "PREIMAGE_MISMATCH", "Create requires a missing file and one edit"
                )
            data = edits[0].new_text.encode("utf-8")
            mode = "100644"
        else:
            text = _text(original.data)
            intervals = []
            for edit in edits:
                if edit.operation != "update" or edit.before_sha256 != sha256(
                    original.data
                ):
                    raise RepairError(
                        "PREIMAGE_MISMATCH",
                        "Edit preimage does not match original source",
                    )
                old = edit.old_text
                assert old is not None
                start = text.find(old)
                if start < 0 or text.find(old, start + 1) >= 0:
                    raise RepairError(
                        "AMBIGUOUS_EDIT", "Old text must occur exactly once"
                    )
                intervals.append((start, start + len(old), edit.new_text))
            intervals.sort()
            if any(a[1] > b[0] for a, b in pairwise(intervals)):
                raise RepairError(
                    "OVERLAPPING_EDITS", "Edits overlap in the original source"
                )
            for start, end, replacement in reversed(intervals):
                text = text[:start] + replacement + text[end:]
            data = text.encode("utf-8")
            mode = original.mode
        if secret_ranges(_text(data)):
            raise RepairError(
                "SECRET_CHANGE_FORBIDDEN", "Candidate file contains secret-bearing text"
            )
        if original is not None and original.data == data:
            raise RepairError("NO_CHANGE", "An edit does not change source bytes")
        changed_bytes += len(data) + (len(original.data) if original else 0)
        if changed_bytes > policy.max_changed_bytes:
            raise RepairError("CHANGE_LIMIT", "Changed content exceeds byte limit")
        files[path] = SourceFile(data, mode)
        changes.append(
            {
                "path": path,
                "operation": "update" if original else "create",
                "beforeSha256": sha256(original.data) if original else None,
                "afterSha256": sha256(data),
                "mode": mode,
                "contentBase64": base64.b64encode(data).decode("ascii"),
            }
        )
        patches.append(_diff(path, original.data if original else None, data))
    try:
        validate_file_tree(files)
    except ValueError:
        raise RepairError(
            "PATH_COLLISION", "Candidate contains conflicting file paths"
        ) from None
    manifest = manifest_digest(files)
    patch = "".join(patches)
    artifact = {"changes": changes, "patch": patch, "manifestSha256": manifest}
    return AppliedCandidate(
        files, changes, patch, manifest, sha256(canonical_json(artifact))
    )
