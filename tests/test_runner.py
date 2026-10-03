import json

import httpx
import pytest

from iris_code_fix_agent.runner import OpenAIRepairRunner, RunnerConfig, RunnerError

PROPOSAL = {
    "status": "no_change",
    "summary": "No supported change",
    "edits": [],
    "limitations": [],
    "checksRequired": [],
}


async def test_complete_input_metadata_is_bounded_before_call():
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        runner = OpenAIRepairRunner(
            RunnerConfig(
                api_key="test", input_price_per_million=1, output_price_per_million=2
            ),
            client,
        )
        with pytest.raises(RunnerError) as error:
            await runner.propose({"metadata": "x" * 262_144}, 100)
        assert error.value.code == "MODEL_INPUT_TOO_LARGE"
        assert calls == []


@pytest.mark.asyncio
async def test_structured_request_and_usage():
    def handler(request):
        body = json.loads(request.content)
        assert (
            body["model"] == "gpt-6.1-sol"
            and body["tools"] == []
            and body["store"] is False
        )
        assert body["text"]["format"]["schema"]["required"] == list(
            body["text"]["format"]["schema"]["properties"]
        )
        return httpx.Response(
            200,
            headers={"x-request-id": "req-test"},
            json={
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": json.dumps(PROPOSAL)}
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAIRepairRunner(
            RunnerConfig(
                api_key="test", input_price_per_million=1, output_price_per_million=2
            ),
            client,
        ).propose({}, 1)
    assert result.usage["costUsd"] == 0.00014
    assert result.provider_request_id == "req-test"


@pytest.mark.asyncio
async def test_budget_before_call_and_configuration():
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        runner = OpenAIRepairRunner(
            RunnerConfig(
                api_key="test", input_price_per_million=1, output_price_per_million=2
            ),
            client,
        )
        with pytest.raises(RunnerError, match="reservation"):
            await runner.propose({}, 0.00001)
        with pytest.raises(RunnerError, match="price rates"):
            await OpenAIRepairRunner(RunnerConfig(api_key="test"), client).propose(
                {}, 1
            )
    assert not calls


@pytest.mark.asyncio
async def test_timeout_unknown_no_retry():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("source-secret-sample")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RunnerError) as exc:
            await OpenAIRepairRunner(
                RunnerConfig(
                    api_key="test",
                    input_price_per_million=1,
                    output_price_per_million=2,
                ),
                client,
            ).propose({}, 1)
    assert exc.value.code == "MODEL_CALL_UNKNOWN"
    assert exc.value.usage["reservedCostUsd"] > 0 and exc.value.usage["costUsd"] is None
    assert "source-secret-sample" not in str(exc.value) and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,code",
    [
        ({"status": "incomplete", "output": []}, "MODEL_INCOMPLETE"),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": "sensitive sample"}],
                    }
                ],
            },
            "MODEL_REFUSED",
        ),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "sensitive invalid text"}
                        ],
                    }
                ],
            },
            "MODEL_INVALID_OUTPUT",
        ),
    ],
)
async def test_non_candidate_response_is_sanitized_and_accounted(body, code):
    body["usage"] = {"input_tokens": 12, "output_tokens": 3}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(RunnerError) as exc:
            await OpenAIRepairRunner(
                RunnerConfig(
                    api_key="test",
                    input_price_per_million=1,
                    output_price_per_million=2,
                ),
                client,
            ).propose({}, 1)
    assert exc.value.code == code and exc.value.usage["inputTokens"] == 12
    assert "sensitive" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"reasoning_effort": "none"},
        {"model": "not a model!"},
        {"model": ""},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 0},
        {"max_output_tokens": 16385},
        {"max_output_tokens": 1.5},
    ],
)
async def test_invalid_configuration_rejected_before_call(overrides):
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    ) as client:
        config = RunnerConfig(
            api_key="test",
            input_price_per_million=1,
            output_price_per_million=2,
            **overrides,
        )
        with pytest.raises(RunnerError) as exc:
            await OpenAIRepairRunner(config, client).propose({}, 1)
    assert exc.value.code == "MODEL_CONFIGURATION_REQUIRED" and not calls


@pytest.mark.asyncio
async def test_exact_sol61_payload_and_no_redirects():
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6.1-sol"
        assert body["reasoning"] == {"effort": "xhigh"}
        assert body["max_output_tokens"] == 1234
        assert body["tools"] == [] and body["store"] is False
        assert body["text"]["format"]["type"] == "json_schema"
        return httpx.Response(307, headers={"location": "https://unexpected.example"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(RunnerError):
            await OpenAIRepairRunner(
                RunnerConfig(
                    api_key="test",
                    reasoning_effort="xhigh",
                    max_output_tokens=1234,
                    input_price_per_million=1,
                    output_price_per_million=2,
                ),
                client,
            ).propose({}, 1)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_nonfinite_actual_usage_rejected_safely():
    body = {
        "status": "completed",
        "usage": {"input_tokens": 10**400, "output_tokens": 1},
        "output": [],
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(RunnerError) as exc:
            await OpenAIRepairRunner(
                RunnerConfig(
                    api_key="test",
                    input_price_per_million=1,
                    output_price_per_million=2,
                ),
                client,
            ).propose({}, 1)
    assert exc.value.code == "MODEL_CALL_UNKNOWN"


@pytest.mark.asyncio
async def test_model_name_is_configurable():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RunnerError):
            await OpenAIRepairRunner(
                RunnerConfig(
                    api_key="test",
                    model="another-model-1.0",
                    input_price_per_million=1,
                    output_price_per_million=2,
                ),
                client,
            ).propose({}, 1)
    assert seen == ["another-model-1.0"]


def test_default_output_budget_leaves_room_for_reasoning():
    assert RunnerConfig().max_output_tokens >= 8192
