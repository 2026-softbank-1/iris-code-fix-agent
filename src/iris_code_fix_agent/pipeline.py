"""One candidate attempt. Execution checks and Git publication belong to WAS."""

from datetime import UTC, datetime

from .apply import apply_proposal
from .canonical import canonical_json, sha256
from .context import build_context
from .errors import RepairError
from .source import load_source


def validate_diagnosis(request):
    diagnosis = request.diagnosis_result
    if diagnosis.get("schema_version") != "diagnosis-result.v3":
        raise RepairError("INVALID_DIAGNOSIS", "Expected original diagnosis-result.v3.")
    if diagnosis.get("job_status") != "succeeded" or not isinstance(
        diagnosis.get("analysis"), dict
    ):
        raise RepairError("DIAGNOSIS_NOT_SUCCEEDED", "Diagnosis must have succeeded.")
    for scope in (diagnosis.get("backend_context") or {}, diagnosis.get("scope") or {}):
        if not isinstance(scope, dict):
            raise RepairError("INVALID_DIAGNOSIS", "Diagnosis scope must be an object.")
        for key, expected in (
            ("service_id", request.scope.service_id),
            ("deployment_id", request.scope.deployment_id),
        ):
            if key in scope and str(scope[key]) != str(expected):
                raise RepairError(
                    "DIAGNOSIS_SCOPE_MISMATCH", "Diagnosis scope does not match."
                )
    source = diagnosis.get("source_analysis") or {}
    if not isinstance(source, dict):
        raise RepairError(
            "INVALID_DIAGNOSIS", "Diagnosis source analysis must be an object."
        )
    if any(
        source.get(key) is not None and not isinstance(source[key], str)
        for key in ("commit_sha", "archive_sha256", "root_directory")
    ):
        raise RepairError("INVALID_DIAGNOSIS", "Diagnosis source identity is invalid.")
    if (
        source.get("commit_sha")
        and source["commit_sha"].lower() != request.source.base_commit_sha
    ):
        raise RepairError(
            "DIAGNOSIS_SOURCE_MISMATCH", "Diagnosis source does not match."
        )
    if (
        source.get("archive_sha256")
        and source["archive_sha256"] != request.source.archive_sha256
    ):
        raise RepairError(
            "DIAGNOSIS_SOURCE_MISMATCH", "Diagnosis archive does not match."
        )
    if (
        source.get("root_directory")
        and source["root_directory"] != request.source.root_directory
    ):
        raise RepairError(
            "DIAGNOSIS_SOURCE_MISMATCH", "Diagnosis source root does not match."
        )
    remediation = diagnosis["analysis"].get("remediation")
    if not isinstance(remediation, dict) or not isinstance(
        remediation.get("plans"), list
    ):
        raise RepairError(
            "INVALID_DIAGNOSIS", "Diagnosis remediation plans must be an array."
        )
    plans = remediation["plans"]
    if any(
        not isinstance(plan, dict) or not isinstance(plan.get("id"), str)
        for plan in plans
    ):
        raise RepairError(
            "INVALID_DIAGNOSIS", "Diagnosis plan identifiers are invalid."
        )
    by_id = {plan.get("id"): plan for plan in plans if isinstance(plan, dict)}
    if len(by_id) != len(plans) or any(
        plan_id not in by_id for plan_id in request.plan_ids
    ):
        raise RepairError("INVALID_PLAN", "Selected plans are not in this diagnosis.")
    selected = [by_id[plan_id] for plan_id in request.plan_ids]
    if any(
        not isinstance(plan.get("changes"), list)
        or any(not isinstance(change, dict) for change in plan["changes"])
        for plan in selected
    ):
        raise RepairError("INVALID_DIAGNOSIS", "Diagnosis changes must be objects.")
    for lines in (diagnosis.get("evidence", []), source.get("evidence", [])):
        if not isinstance(lines, list) or any(
            not isinstance(line, dict) or not isinstance(line.get("id"), str)
            for line in lines
        ):
            raise RepairError(
                "INVALID_DIAGNOSIS", "Diagnosis evidence must have identifiers."
            )
    if any(
        not plan.get("changes")
        or any(change.get("kind") != "code" for change in plan["changes"])
        for plan in selected
    ):
        return "configuration_required"
    return None


def check_deadline(request):
    if request.policy.deadline <= datetime.now(UTC):
        raise RepairError("DEADLINE_EXCEEDED", "Repair deadline expired.", 408)


async def run_attempt(request, input_digest: str, runner, client, allowed_hosts=()):
    check_deadline(request)
    status = validate_diagnosis(request)
    result = {
        "schemaVersion": "iris.repair-result.v1",
        "requestId": request.request_id,
        "inputDigest": input_digest,
        "baseCommitSha": request.source.base_commit_sha,
        "status": status or "candidate_ready",
        "validation": {"status": "not_run", "owner": "was"},
        "repositoryPushAuthorized": False,
        "deploymentAuthorized": False,
        "changedFiles": [],
        "artifacts": [],
        "candidateDigest": None,
        "candidateManifestSha256": None,
        "summary": "Selected plan requires configuration or an operational change."
        if status
        else "",
        "limitations": [],
        "checksRequired": [],
        "usage": None,
    }
    if status:
        return result, {}
    snapshot = await load_source(request.source, client, allowed_hosts=allowed_hosts)
    context = build_context(request, snapshot)
    check_deadline(request)
    if not context["files"]:
        result.update(
            status="needs_more_evidence",
            summary="No eligible source context is available.",
        )
        result["limitations"] = context.get("limitations", [])
        return result, {}
    supplied_diagnosis = context.get("diagnosis") or {}
    supplied_plans = (
        supplied_diagnosis.get("analysis", {}).get("remediation", {}).get("plans", [])
    )
    if not set(request.plan_ids) <= {plan["id"] for plan in supplied_plans}:
        result.update(
            status="needs_more_evidence",
            summary="Selected diagnosis plans exceed the model context budget.",
            limitations=context.get("limitations", []),
        )
        return result, {}
    response = await runner.propose(context, max_cost_usd=request.policy.max_cost_usd)
    try:
        return _finish_candidate(request, snapshot, context, response, result)
    except RepairError as exc:
        exc.usage = response.usage
        raise


def _finish_candidate(request, snapshot, context, response, result):
    check_deadline(request)
    proposal = response.proposal
    result.update(
        status=proposal.status,
        summary=proposal.summary,
        limitations=list(
            dict.fromkeys(context.get("limitations", []) + proposal.limitations)
        ),
        checksRequired=proposal.checks_required,
        usage=response.usage,
        model=response.model,
        providerRequestId=response.provider_request_id,
    )
    if proposal.status != "candidate_ready":
        if proposal.edits:
            raise RepairError(
                "INVALID_PROPOSAL", "A non-candidate result cannot contain edits."
            )
        return result, {}
    exposed = {file["path"] for file in context["files"]}
    evidence = {line.get("id") for line in request.diagnosis_result.get("evidence", [])}
    evidence.update(
        line.get("id")
        for line in (request.diagnosis_result.get("source_analysis") or {}).get(
            "evidence", []
        )
    )
    for edit in proposal.edits:
        if edit.operation == "update" and edit.path not in exposed:
            raise RepairError(
                "UNEXPOSED_EDIT", "Edited source was not provided to the model."
            )
        if not edit.plan_ids or not set(edit.plan_ids) <= set(request.plan_ids):
            raise RepairError(
                "INVALID_PROPOSAL_REFERENCE", "Edit plan references do not match."
            )
        if not edit.evidence_ids or not set(edit.evidence_ids) <= evidence:
            raise RepairError(
                "INVALID_PROPOSAL_REFERENCE", "Edit evidence references do not match."
            )
    candidate = apply_proposal(
        snapshot, proposal, request.policy, request.source.root_directory
    )
    changes = canonical_json(
        {"schemaVersion": "iris.repair-changes.v1", "files": candidate.changes}
    )
    manifest = canonical_json(
        {
            "schemaVersion": "iris.repair-manifest.v1",
            "baseCommitSha": request.source.base_commit_sha,
            "baseManifestSha256": snapshot.manifest_sha256,
            "candidateManifestSha256": candidate.manifest_sha256,
            "candidateDigest": candidate.digest,
            "files": [
                {key: value for key, value in change.items() if key != "contentBase64"}
                for change in candidate.changes
            ],
        }
    )
    artifacts = {
        "patch.diff": candidate.patch.encode("utf-8"),
        "changes.json": changes,
        "manifest.json": manifest,
    }
    result.update(
        candidateDigest=candidate.digest,
        candidateManifestSha256=candidate.manifest_sha256,
        changedFiles=[
            {key: value for key, value in change.items() if key != "contentBase64"}
            for change in candidate.changes
        ],
        artifacts=[
            {
                "name": name,
                "sha256": sha256(data),
                "byteLength": len(data),
                "url": f"/internal/repairs/{request.request_id}/artifacts/{name}",
            }
            for name, data in artifacts.items()
        ],
    )
    return result, artifacts
