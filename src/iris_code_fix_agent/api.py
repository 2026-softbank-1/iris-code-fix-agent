"""Authenticated candidate API; WAS retains execution and publication ownership."""

import asyncio
import hmac
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, Request
from fastapi import Path as RoutePath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from .canonical import semantic_digest, sha256
from .configuration import load_environment, openai_api_key
from .contracts import RepairRequest
from .errors import RepairError
from .pipeline import run_attempt
from .runner import OpenAIRepairRunner, RunnerConfig, RunnerError
from .store import ResultStore

RequestId = Annotated[str, RoutePath(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]


@dataclass(frozen=True)
class Settings:
    api_key: str
    data_dir: Path = Path("artifacts")
    allowed_source_hosts: tuple[str, ...] = ("s3.amazonaws.com",)
    max_concurrent: int = 2
    max_cost_usd: float = 1
    max_body_bytes: int = 1_048_576

    def __post_init__(self):
        if (
            not self.api_key.isascii()
            or len(self.api_key) < 32
            or any(c.isspace() for c in self.api_key)
        ):
            raise ValueError(
                "API_KEY must be at least 32 non-whitespace ASCII characters."
            )
        if (
            self.max_concurrent < 1
            or self.max_body_bytes < 1
            or not math.isfinite(self.max_cost_usd)
            or self.max_cost_usd <= 0
            or not self.allowed_source_hosts
        ):
            raise ValueError("Invalid service limits or source host allowlist.")

    @classmethod
    def from_env(cls):
        load_environment()
        return cls(
            api_key=os.environ.get("API_KEY", ""),
            data_dir=Path(os.environ.get("FIX_DATA_DIR", "artifacts")),
            allowed_source_hosts=tuple(
                host.strip().lower()
                for host in os.environ.get(
                    "FIX_ALLOWED_SOURCE_HOSTS", "s3.amazonaws.com"
                ).split(",")
                if host.strip()
            ),
            max_concurrent=int(os.environ.get("FIX_MAX_CONCURRENT", "2")),
            max_cost_usd=float(os.environ.get("FIX_MAX_COST_USD", "1")),
        )


class RequestGate:
    def __init__(self, app, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/healthz":
            return await self.app(scope, receive, send)
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if not hmac.compare_digest(
            headers.get(b"x-api-key", b""), self.settings.api_key.encode("ascii")
        ):
            return await JSONResponse(
                {"error": {"code": "UNAUTHORIZED"}}, status_code=401
            )(scope, receive, send)
        if scope["method"] == "POST":
            if (
                headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
                != b"application/json"
            ):
                return await JSONResponse(
                    {"error": {"code": "JSON_REQUIRED"}}, status_code=415
                )(scope, receive, send)
            body = bytearray()
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > self.settings.max_body_bytes:
                            return await JSONResponse(
                                {"error": {"code": "INPUT_TOO_LARGE"}}, status_code=413
                            )(scope, receive, send)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await JSONResponse(
                    {"error": {"code": "BODY_TIMEOUT"}}, status_code=408
                )(scope, receive, send)
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            return await self.app(scope, buffered_receive, send)
        return await self.app(scope, receive, send)


def create_app(
    settings: Settings | None = None, runner=None, source_client=None, store=None
):
    settings = settings or Settings.from_env()
    store = store or ResultStore(settings.data_dir)
    if runner is None:

        def rate(name):
            value = os.environ.get(name)
            return float(value) if value and value.strip() else None

        runner = OpenAIRepairRunner(
            RunnerConfig(
                api_key=openai_api_key(),
                model=os.environ.get("FIX_MODEL", "gpt-6.1-sol"),
                reasoning_effort=os.environ.get("FIX_REASONING_EFFORT", "medium"),
                max_output_tokens=int(os.environ.get("FIX_MAX_OUTPUT_TOKENS", "4096")),
                timeout_seconds=float(
                    os.environ.get("FIX_MODEL_TIMEOUT_SECONDS", "120")
                ),
                input_price_per_million=rate("FIX_INPUT_PRICE_PER_MILLION"),
                output_price_per_million=rate("FIX_OUTPUT_PRICE_PER_MILLION"),
                cached_input_price_per_million=rate(
                    "FIX_CACHED_INPUT_PRICE_PER_MILLION"
                ),
            )
        )
    app = FastAPI(title="Iris Code Fix Agent", version="0.1.0")
    app.add_middleware(RequestGate, settings=settings)
    semaphore = asyncio.Semaphore(settings.max_concurrent)
    app.state.store = store

    @app.exception_handler(RepairError)
    async def repair_error(_request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=exc.http_status,
        )

    @app.exception_handler(RequestValidationError)
    async def input_error(_request, exc):
        # Default FastAPI errors echo rejected input, including signed URLs and source.
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "fields": [
                        {"location": list(error["loc"]), "type": error["type"]}
                        for error in exc.errors()
                    ],
                }
            },
            status_code=422,
        )

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/internal/repairs")
    async def repair(
        payload: RepairRequest, idempotency_key: Annotated[str | None, Header()] = None
    ):
        if idempotency_key != payload.request_id:
            raise RepairError(
                "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must match requestId."
            )
        if payload.policy.max_cost_usd > settings.max_cost_usd:
            raise RepairError(
                "COST_LIMIT_EXCEEDED", "Request exceeds the service cost cap."
            )
        input_digest = semantic_digest(payload)
        with store.lock(payload.request_id):
            if store.get(payload.request_id) is not None:
                existing = store.begin(payload.request_id, input_digest)
                if existing["status"] == "SUCCEEDED":
                    return existing["result"]
                return JSONResponse(existing, status_code=409)
            if semaphore.locked():
                raise RepairError("BUSY", "Concurrent repair limit reached.", 429)
            await semaphore.acquire()
            try:
                store.begin(payload.request_id, input_digest)
                remaining = (
                    payload.policy.deadline - datetime.now(UTC)
                ).total_seconds()
                if remaining <= 0:
                    raise RepairError(
                        "DEADLINE_EXCEEDED", "Repair deadline expired.", 408
                    )
                async with asyncio.timeout(remaining):
                    if source_client is not None:
                        result, artifacts = await run_attempt(
                            payload,
                            input_digest,
                            runner,
                            source_client,
                            settings.allowed_source_hosts,
                        )
                    else:
                        async with httpx.AsyncClient(
                            follow_redirects=False, timeout=30
                        ) as client:
                            result, artifacts = await run_attempt(
                                payload,
                                input_digest,
                                runner,
                                client,
                                settings.allowed_source_hosts,
                            )
                store.write_artifacts(payload.request_id, artifacts)
                store.finish(payload.request_id, "SUCCEEDED", result=result)
                return result
            except RepairError as exc:
                usage = getattr(exc, "usage", None)
                store.finish(
                    payload.request_id,
                    "FAILED",
                    result={"usage": usage},
                    error_code=exc.code,
                )
                raise
            except TimeoutError:
                # Cancellation may have reached a paid provider call; never replay blindly.
                store.finish(
                    payload.request_id,
                    "UNKNOWN_OUTCOME",
                    error_code="MODEL_CALL_UNKNOWN",
                )
                return JSONResponse(
                    {"error": {"code": "MODEL_CALL_UNKNOWN"}}, status_code=504
                )
            except RunnerError as exc:
                status = (
                    "UNKNOWN_OUTCOME" if exc.code == "MODEL_CALL_UNKNOWN" else "FAILED"
                )
                store.finish(
                    payload.request_id,
                    status,
                    result={"usage": exc.usage},
                    error_code=exc.code,
                )
                return JSONResponse(
                    {"error": {"code": exc.code, "message": exc.message}},
                    status_code=502,
                )
            except asyncio.CancelledError:
                store.finish(
                    payload.request_id,
                    "UNKNOWN_OUTCOME",
                    error_code="MODEL_CALL_UNKNOWN",
                )
                raise
            except Exception:  # noqa: BLE001 -- keep uncertain receipts on unexpected failure
                store.finish(
                    payload.request_id, "UNKNOWN_OUTCOME", error_code="INTERNAL_ERROR"
                )
                return JSONResponse(
                    {"error": {"code": "INTERNAL_ERROR"}}, status_code=500
                )
            finally:
                semaphore.release()

    @app.get("/internal/repairs/{request_id}")
    async def get_result(request_id: RequestId):
        result = store.recover(request_id)
        if result is None:
            raise RepairError("REPAIR_NOT_FOUND", "Request not found.", 404)
        return result

    @app.get("/internal/repairs/{request_id}/artifacts/{name}")
    async def get_artifact(request_id: RequestId, name: str, request: Request):
        path = store.artifact(request_id, name)
        record = store.get(request_id)
        expected = next(
            (
                artifact["sha256"]
                for artifact in record["result"].get("artifacts", [])
                if artifact["name"] == name
            ),
            None,
        )
        if expected is None or sha256(path.read_bytes()) != expected:
            raise RepairError(
                "ARTIFACT_INTEGRITY_ERROR",
                "Stored artifact failed integrity verification.",
                500,
            )
        return FileResponse(path, media_type="application/octet-stream", filename=name)

    return app


def main():
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        description="Run the authenticated Iris repair candidate API."
    )
    parser.add_argument(
        "--env-file", type=Path, default=Path(os.environ.get("FIX_ENV_FILE", ".env"))
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    arguments = parser.parse_args()
    load_environment(arguments.env_file)
    uvicorn.run(
        "iris_code_fix_agent.api:create_app",
        factory=True,
        host=arguments.host or os.environ.get("FIX_HOST", "0.0.0.0"),
        port=arguments.port
        if arguments.port is not None
        else int(os.environ.get("FIX_PORT", "8000")),
    )
