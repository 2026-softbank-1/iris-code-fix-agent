"""Bounded full-file exposure; secret-bearing files are never editable."""

import hashlib
import json
from pathlib import PurePosixPath

from .paths import is_protected_path, matches_path
from .redaction import mask_value, secret_ranges

MAX_CONTEXT_BYTES = 180_000


def _matches(path, patterns):
    return any(matches_path(path, pattern) for pattern in patterns)


def protected(path):
    parts = PurePosixPath(path).parts
    return is_protected_path(path) or any(
        part.casefold() in ("node_modules", ".venv") for part in parts
    )


def exposed_paths(context: dict) -> set[str]:
    return {item["path"] for item in context["files"]}


def build_context(request, snapshot) -> dict:
    diagnosis = request.diagnosis_result
    policy = request.policy
    references = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("path", "file", "file_path", "filePath") and isinstance(
                    item, str
                ):
                    references.add(item)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(diagnosis.get("source_analysis", {}))
    files, limitations, omitted, used = [], [], [], 0
    for path, source_file in sorted(
        snapshot.files.items(), key=lambda pair: (pair[0] not in references, pair[0])
    ):
        if (
            protected(path)
            or _matches(path, policy.protected_paths)
            or not _matches(path, policy.allowed_paths)
        ):
            continue
        root = request.source.root_directory.strip("/")
        if root and root != "." and not (path == root or path.startswith(root + "/")):
            continue
        data = source_file if isinstance(source_file, bytes) else source_file.data
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            omitted.append({"path": path, "reason": "binary_or_non_utf8"})
            continue
        if "\x00" in content:
            omitted.append({"path": path, "reason": "binary_or_non_utf8"})
            continue
        ranges = secret_ranges(content)
        if ranges:
            omitted.append(
                {
                    "path": path,
                    "reason": "secret_detected",
                    "redactionRanges": [{"start": a, "end": b} for a, b in ranges],
                }
            )
            continue
        if used + len(data) > MAX_CONTEXT_BYTES:
            omitted.append({"path": path, "reason": "context_budget"})
            continue
        files.append(
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "content": content,
            }
        )
        used += len(data)
    if omitted:
        limitations.append(
            "Some eligible files were omitted because they contain secrets, non-text bytes, or exceed the source context budget. Omitted files cannot be edited."
        )
    if not files:
        limitations.append(
            "No eligible original text files are exposed; existing-file edits are unavailable."
        )
    masked_diagnosis = mask_value(diagnosis)
    # Diagnosis prose is bounded separately and never substitutes for original source.
    if len(json.dumps(masked_diagnosis, ensure_ascii=False).encode("utf-8")) > 60_000:
        masked_diagnosis = {
            "schema_version": diagnosis.get("schema_version"),
            "evidence": mask_value(diagnosis.get("evidence", [])),
        }
        if (
            len(json.dumps(masked_diagnosis, ensure_ascii=False).encode("utf-8"))
            > 60_000
        ):
            masked_diagnosis = {"omitted": "Diagnosis exceeds context limit."}
        limitations.append("Diagnosis context was reduced due to its size.")
    return {
        "requestId": request.request_id,
        "baseCommitSha": request.source.base_commit_sha,
        "rootDirectory": request.source.root_directory,
        "planIds": request.plan_ids,
        "diagnosis": masked_diagnosis,
        "policy": {
            "allowedPaths": policy.allowed_paths,
            "protectedPaths": policy.protected_paths,
            "maxChangedFiles": policy.max_changed_files,
            "maxChangedBytes": policy.max_changed_bytes,
        },
        "files": files,
        "omittedFiles": omitted,
        "limitations": limitations,
    }
