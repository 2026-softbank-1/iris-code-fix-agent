"""Create pinned offline fixtures. No network or model calls are performed."""

import argparse
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.source import SourceFile, manifest_digest


def make_fixture(download_url: str) -> tuple[bytes, dict, dict]:
    source = (Path(__file__).parent / "source" / "app.py").read_bytes()
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        member = tarfile.TarInfo("app.py")
        member.size, member.mode, member.mtime = len(source), 0o644, 0
        archive.addfile(member, io.BytesIO(source))
    archive_bytes = archive_buffer.getvalue()
    diagnosis = {
        "schema_version": "diagnosis-result.v3",
        "diagnosis_id": "example-diagnosis",
        "scope": {"service_id": 1, "deployment_id": 1},
        "previous_diagnosis_id": None,
        "deployment_context": {},
        "job_status": "succeeded",
        "remediation_execution": "not_executed",
        "error": None,
        "input_limitations": [],
        "execution": {},
        "evidence": [
            {"id": "L000001", "text": "SyntaxError: expected ':'", "line_number": 1}
        ],
        "analysis": {
            "analysis_status": "diagnosed",
            "summary": "Function definition lacks a colon.",
            "observations": [],
            "hypotheses": [],
            "next_checks": [],
            "missing_information": [],
            "limitations": ["Synthetic offline fixture."],
            "remediation": {
                "status": "proposed",
                "reason": "Repair syntax.",
                "plans": [
                    {
                        "id": "P1",
                        "title": "Add missing colon",
                        "evidence_ids": ["L000001"],
                        "changes": [
                            {
                                "kind": "code",
                                "target": "app.py",
                                "instruction": "Add missing colon.",
                            }
                        ],
                    }
                ],
            },
        },
        "source_analysis": {
            "status": "analyzed",
            "reason": "Fixture source available.",
            "commit_sha": "a" * 40,
            "commit_verification": "caller_supplied",
            "requested_files": [],
            "read_ranges": [],
            "evidence": [],
            "findings": [],
            "limitations": [],
            "error": None,
            "archive_sha256": sha256(archive_bytes),
            "root_directory": ".",
        },
    }
    request = {
        "schemaVersion": "iris.repair-request.v1",
        "requestId": "example-repair-001",
        "scope": {"serviceId": 1, "deploymentId": 1, "diagnosisId": 1},
        "diagnosisResult": diagnosis,
        "planIds": ["P1"],
        "source": {
            "repositoryId": "example/repository",
            "baseCommitSha": "a" * 40,
            "rootDirectory": ".",
            "downloadUrl": download_url,
            "archiveSha256": sha256(archive_bytes),
            "manifestSha256": manifest_digest({"app.py": SourceFile(source)}),
        },
        "policy": {
            "allowedPaths": ["app.py"],
            "protectedPaths": [],
            "maxChangedFiles": 1,
            "maxChangedBytes": 1024,
            "deadline": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "maxCostUsd": 1,
        },
    }
    proposal = {
        "status": "candidate_ready",
        "summary": "Add the missing function colon.",
        "edits": [
            {
                "path": "app.py",
                "operation": "update",
                "beforeSha256": sha256(source),
                "oldText": "def add(a, b)\n",
                "newText": "def add(a, b):\n",
                "evidenceIds": ["L000001"],
                "planIds": ["P1"],
            }
        ],
        "limitations": ["Synthetic example; no production verification performed."],
        "checksRequired": ["WAS should compile the candidate in an isolated runner."],
    }
    return archive_bytes, request, proposal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--output", type=Path, default=Path("examples/generated"))
    args = parser.parse_args()
    archive, request, proposal = make_fixture(args.download_url)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "source.tar").write_bytes(archive)
    for name, value in [("request.json", request), ("fake-proposal.json", proposal)]:
        (args.output / name).write_text(json.dumps(value, indent=2) + "\n")
    print(
        f"Created fixtures in {args.output}; upload source.tar before using the request."
    )


if __name__ == "__main__":
    main()
