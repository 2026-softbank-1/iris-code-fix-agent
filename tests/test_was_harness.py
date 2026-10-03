"""Cross-repo live harness tests; provider calls are always replaced offline."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from iris_code_fix_agent.contracts import ModelProposal
from iris_code_fix_agent.runner import RunnerResponse

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "was_evaluate", SCRIPTS / "was_evaluate.py"
)
harness = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
spec.loader.exec_module(harness)


def test_full_diagnosis_preserves_realistic_original_contract():
    archive, raw = harness.full_diagnosis("syntax", "offline")
    assert archive and raw["schema_version"] == "diagnosis-result.v3"
    assert raw["analysis"]["observations"][0]["id"] == "O1"
    assert raw["analysis"]["hypotheses"][0]["id"] == "H1"
    plan = raw["analysis"]["remediation"]["plans"][0]
    assert plan["id"] == "R1" and plan["hypothesis_ids"] == ["H1"]
    assert plan["changes"][0]["kind"] == "code"
    assert plan["changes"][0]["snippet_kind"] == "template"
    assert raw["evidence"][0]["source_id"] == "fixture-build"
    assert raw["evidence"][0]["id"] == "EV000001"


def test_configuration_diagnosis_has_no_code_plan():
    _, raw = harness.full_diagnosis("configuration", "offline")
    assert (
        raw["analysis"]["remediation"]["plans"][0]["changes"][0]["kind"]
        == "configuration"
    )


async def test_database_must_be_explicit_postgres(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="isolated migrated PostgreSQL"):
        await harness.evaluate(tmp_path, tmp_path / "output", None, "x" * 32)


@pytest.mark.skipif(
    not (os.environ.get("WAS_EVALUATION_ROOT") and os.environ.get("TEST_DATABASE_URL")),
    reason="Requires cross-repo WAS environment and isolated migrated PostgreSQL",
)
async def test_real_was_routes_db_auth_and_clients_with_offline_provider(tmp_path):
    class OfflineRunner:
        calls = 0

        async def propose(self, context, max_cost_usd):
            self.calls += 1
            assert context["diagnosis"]["analysis"]["observations"][0]["id"] == "O1"
            source = context["files"][0]
            return RunnerResponse(
                ModelProposal.model_validate(
                    {
                        "status": "candidate_ready",
                        "summary": "Restore colon.",
                        "edits": [
                            {
                                "path": "app.py",
                                "operation": "update",
                                "beforeSha256": source["sha256"],
                                "oldText": "def add(a, b)\n",
                                "newText": "def add(a, b):\n",
                                "evidenceIds": ["EV000001"],
                                "planIds": ["R1"],
                            }
                        ],
                        "limitations": [],
                        "checksRequired": [],
                    }
                ),
                {"costUsd": 0, "inputTokens": 0, "outputTokens": 0},
                "offline-evaluation",
            )

    runner = OfflineRunner()
    report = await harness.evaluate(
        Path(os.environ["WAS_EVALUATION_ROOT"]), tmp_path, runner, "x" * 32
    )
    assert report["allPassed"], report
    assert runner.calls == report["modelCalls"] == 1
    assert all(case["sql"]["crossContractDigestMatches"] for case in report["cases"])
    assert all(case["replay"]["sameRepairId"] for case in report["cases"])
    assert "downloadUrl" not in (tmp_path / "report.json").read_text()
