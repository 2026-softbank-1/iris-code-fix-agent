"""Offline verification of live evaluation safeguards; never calls OpenAI."""

import importlib.util
from pathlib import Path

import pytest

from iris_code_fix_agent.api import Settings
from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.contracts import ModelProposal, RepairRequest
from iris_code_fix_agent.runner import RunnerResponse
from iris_code_fix_agent.source import from_archive

spec = importlib.util.spec_from_file_location(
    "live_evaluate", Path(__file__).resolve().parents[1] / "scripts/live_evaluate.py"
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class OfflineFixtureRunner:
    calls = 0

    async def propose(self, context, max_cost_usd):
        self.calls += 1
        assert "downloadUrl" not in str(context)
        source = context["files"][0]
        replacements = {
            "syntax": ("def add(a, b)\n", "def add(a, b):\n"),
            "behavior": ("return end - start", "return end - start + 1"),
            "symbol": ("missing_operand", "b"),
        }
        case = context["requestId"].split("-")[-1]
        old, new = replacements[case]
        return RunnerResponse(
            ModelProposal.model_validate(
                {
                    "status": "candidate_ready",
                    "summary": "Offline fixture repair",
                    "edits": [
                        {
                            "path": "app.py",
                            "operation": "update",
                            "beforeSha256": source["sha256"],
                            "oldText": old,
                            "newText": new,
                            "evidenceIds": ["EV000001"],
                            "planIds": ["R1"],
                        }
                    ],
                    "limitations": [],
                    "checksRequired": [],
                }
            ),
            {"inputTokens": 10, "outputTokens": 10, "costUsd": 0.001},
            "offline-test",
        )


@pytest.mark.parametrize("case", ["syntax", "behavior", "symbol"])
def test_fixture_has_real_failing_baseline_and_pinned_archive(case):
    archive, request = harness.make_fixture(case, "test")
    snapshot = from_archive(archive, RepairRequest.model_validate(request).source)
    assert sha256(archive) == request["source"]["archiveSha256"]
    assert not harness.check_source(case, snapshot.files["app.py"].data)["passed"]


def test_candidate_checker_rejects_execution_and_test_weakening():
    assert not harness.check_source(
        "symbol", "def add(a, b):\n    return __import__('os').system('echo unsafe')\n"
    )["passed"]
    assert not harness.check_source(
        "behavior", "def inclusive_count(start, end):\n    return 1\n"
    )["passed"]
    assert harness.check_source(
        "behavior", "def inclusive_count(start, end):\n    return end - start + 1\n"
    )["passed"]


async def test_api_evaluation_checks_candidates_and_config_bypasses_model(tmp_path):
    runner = OfflineFixtureRunner()
    report = await harness.evaluate(
        tmp_path, runner, Settings("x" * 32, tmp_path / "receipts")
    )
    assert report["allPassed"]
    assert report["modelCalls"] == runner.calls == 3
    assert report["sourceDownloads"] == 3
    assert report["reservedBudgetUsd"] == 3
    assert all(case["result"]["inputDigest"] for case in report["cases"])
    text = (tmp_path / "report.json").read_text()
    assert "downloadUrl" not in text and "s3.amazonaws.com" not in text
    assert report["cases"][-1]["result"]["usage"] is None


async def test_total_budget_prevents_extra_paid_call():
    runner = OfflineFixtureRunner()
    bounded = harness.BoundedRunner(runner, 3, 1)
    bounded.calls, bounded.reserved = 1, 1
    with pytest.raises(RuntimeError, match="exhausted"):
        await bounded.propose({}, 1)
    assert runner.calls == 0


async def test_overlarge_budget_rejected_before_call(tmp_path):
    runner = OfflineFixtureRunner()
    with pytest.raises(ValueError):
        await harness.evaluate(tmp_path, runner, Settings("x" * 32), total_budget=4)
    assert runner.calls == 0


async def test_unknown_provider_call_stops_remaining_cases(tmp_path):
    from iris_code_fix_agent.runner import RunnerError

    class UnknownRunner:
        calls = 0

        async def propose(self, context, max_cost_usd):
            self.calls += 1
            raise RunnerError(
                "MODEL_CALL_UNKNOWN", "Unknown provider outcome", {"costUsd": None}
            )

    runner = UnknownRunner()
    report = await harness.evaluate(
        tmp_path, runner, Settings("x" * 32, tmp_path / "receipts")
    )
    assert runner.calls == report["modelCalls"] == 1
    assert len(report["cases"]) == 1
    assert report["costMayBeUnknown"]
    assert not report["allPassed"]
    assert report["cases"][0]["receiptStatus"] == "UNKNOWN_OUTCOME"
