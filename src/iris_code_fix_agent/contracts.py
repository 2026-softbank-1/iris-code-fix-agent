import re
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

HASH = r"^[0-9a-f]{64}$"


def safe_path(path: str, *, root: bool = False) -> str:
    if root and path == ".":
        return path
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(p in ("", ".", "..") for p in path.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    ):
        raise ValueError("Expected a normalized repository-relative path")
    if re.match(r"^[A-Za-z]:", path):
        raise ValueError("Drive paths are forbidden")
    return path


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )


class Scope(Contract):
    service_id: int = Field(gt=0)
    deployment_id: int = Field(gt=0)
    diagnosis_id: int = Field(gt=0)


class SourceSpec(Contract):
    repository_id: str = Field(min_length=1, max_length=256)
    base_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    root_directory: str
    download_url: HttpUrl
    archive_sha256: str = Field(pattern=HASH)
    manifest_sha256: str = Field(pattern=HASH)

    @field_validator("root_directory")
    @classmethod
    def root(cls, value: str) -> str:
        return safe_path(value, root=True)

    @field_validator("download_url")
    @classmethod
    def https(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https" or value.username or value.password:
            raise ValueError("Source URL must use HTTPS without credentials")
        return value


class RepairPolicy(Contract):
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    protected_paths: list[str] = Field(default_factory=list, max_length=100)
    max_changed_files: int = Field(default=5, gt=0, le=5)
    max_changed_bytes: int = Field(default=65536, gt=0, le=65536)
    deadline: datetime
    max_cost_usd: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("deadline")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Deadline must include a timezone")
        return value

    @field_validator("allowed_paths", "protected_paths")
    @classmethod
    def patterns(cls, values: list[str]) -> list[str]:
        for value in values:
            safe_path(value)
        return values


class RepairRequest(Contract):
    schema_version: Literal["iris.repair-request.v1"] = "iris.repair-request.v1"
    request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    scope: Scope
    diagnosis_result: dict[str, Any]
    plan_ids: list[str] = Field(min_length=1, max_length=20)
    source: SourceSpec
    policy: RepairPolicy

    @field_validator("plan_ids")
    @classmethod
    def unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(
            not v or len(v) > 128 for v in values
        ):
            raise ValueError("Plan IDs must be nonempty and unique")
        return values


class TextEdit(Contract):
    path: str
    operation: Literal["update", "create"]
    before_sha256: str | None = Field(pattern=HASH)
    old_text: str | None
    new_text: str
    evidence_ids: list[str]
    plan_ids: list[str]

    @field_validator("path")
    @classmethod
    def path_valid(cls, value: str) -> str:
        return safe_path(value)

    @model_validator(mode="after")
    def preimage(self):
        if self.operation == "update" and (
            self.before_sha256 is None or not self.old_text
        ):
            raise ValueError("Updates require a hash and nonempty old text")
        if self.operation == "create" and (
            self.before_sha256 is not None or self.old_text is not None
        ):
            raise ValueError("Creates must have null preimages")
        return self


class ModelProposal(Contract):
    status: Literal[
        "candidate_ready", "needs_more_evidence", "configuration_required", "no_change"
    ]
    summary: str
    edits: list[TextEdit]
    limitations: list[str]
    checks_required: list[str]

    @model_validator(mode="after")
    def status_edits(self):
        if (self.status == "candidate_ready") != bool(self.edits):
            raise ValueError(
                "Only candidate_ready may contain edits and it requires edits"
            )
        return self
