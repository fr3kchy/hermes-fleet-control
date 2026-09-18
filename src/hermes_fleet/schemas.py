from typing import Any, Literal
import json
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


_SENSITIVE_KEYS = {"secret", "password", "token", "api_key", "cookie", "authorization"}


def _contains_sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).lower() in _SENSITIVE_KEYS or _contains_sensitive(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_sensitive(item) for item in value)
    return False


class EnrollmentTokenRequest(BaseModel):
    node_name: str = Field(min_length=1, max_length=128)


class EnrollRequest(BaseModel):
    token: str
    node_name: str = Field(min_length=1, max_length=128)
    capabilities: dict[str, Any]


class HeartbeatRequest(BaseModel):
    status: str = Field(pattern="^(online|degraded|offline)$")
    metrics: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class TaskRequest(BaseModel):
    node_id: str
    profile: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=20000)


class TaskRequirements(BaseModel):
    os: str | None = None
    architecture: str | None = None
    tools: list[str] = Field(default_factory=list, max_length=64)
    skills: list[str] = Field(default_factory=list, max_length=64)
    mcp: list[str] = Field(default_factory=list, max_length=64)
    browser: bool = False
    gui: bool = False
    gpu: bool = False
    min_vram_gb: float = Field(default=0, ge=0, le=1024)
    repository: str | None = Field(default=None, max_length=1024)
    credential_domain: str | None = Field(default=None, max_length=128)
    data_classification: str = Field(default="internal", max_length=32)


class FleetTaskCreate(BaseModel):
    protocol_version: Literal["2.0"] = "2.0"
    task_id: UUID | None = None
    event_id: UUID | None = None
    origin_node_id: str = Field(default="coordinator", max_length=128)
    origin_profile: str = Field(default="chief-of-staff", min_length=1, max_length=64)
    origin_session_id: str | None = Field(default=None, max_length=256)
    destination_node_id: str | None = Field(default=None, max_length=128)
    profile: str = Field(default="default", min_length=1, max_length=64)
    parent_fleet_task_id: UUID | None = None
    delegation_depth: int = Field(default=0, ge=0, le=32)
    objective: str = Field(min_length=1, max_length=12000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=32)
    requirements: TaskRequirements = Field(default_factory=TaskRequirements)
    risk_class: int = Field(default=1, ge=0, le=6)
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = None
    sequence: int = Field(default=1, ge=1)
    idempotency_key: str | None = Field(default=None, max_length=256)
    workspace: str = Field(default="scratch", max_length=1024)
    goal: bool = False
    max_retries: int | None = Field(default=None, ge=1, le=20)
    model: str | None = Field(default=None, max_length=128)
    provider: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def provider_requires_model(self):
        if self.provider and not self.model:
            raise ValueError("provider requires model")
        if len(json.dumps(self.payload, ensure_ascii=False)) > 32768:
            raise ValueError("payload exceeds the 32 KiB protocol bound")
        if _contains_sensitive(self.payload):
            raise ValueError("payload contains a credential-like field; pass an operator-owned reference instead")
        return self


class AssignmentAck(BaseModel):
    protocol_version: Literal["2.0"] = "2.0"
    event_id: UUID
    timestamp: str
    sequence: int = Field(ge=1)
    disposition: Literal["accepted", "rejected", "duplicate"]
    idempotency_key: str
    remote_board_id: str | None = Field(default=None, max_length=256)
    remote_card_id: str | None = Field(default=None, max_length=256)
    reason_code: str | None = Field(default=None, max_length=64)
    detail: str | None = Field(default=None, max_length=2000)


class NodeLifecycleEvent(BaseModel):
    protocol_version: Literal["2.0"] = "2.0"
    event_id: UUID
    fleet_task_id: UUID
    idempotency_key: str
    timestamp: str
    sequence: int = Field(ge=1)
    event_type: Literal["created", "ready", "running", "blocked", "review", "approval_required", "done", "failed", "cancelled", "unknown"]
    remote_board_id: str | None = Field(default=None, max_length=256)
    remote_card_id: str | None = Field(default=None, max_length=256)
    result_summary: str | None = Field(default=None, max_length=8000)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    artifacts: list[dict[str, Any]] = Field(default_factory=list, max_length=32)
    failure_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def bounded_redacted_evidence(self):
        bounded = {"summary": self.result_summary, "evidence": self.evidence, "artifacts": self.artifacts}
        if len(json.dumps(bounded, ensure_ascii=False)) > 32768:
            raise ValueError("event evidence exceeds the 32 KiB protocol bound")
        if _contains_sensitive(bounded):
            raise ValueError("event evidence contains a credential-like field")
        return self


class CancelRequest(BaseModel):
    reason: str = Field(default="operator request", max_length=1000)


class ReconcileRequest(BaseModel):
    remote_status: str | None = Field(default=None, max_length=32)
    remote_sequence: int | None = Field(default=None, ge=0)
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=32)


class NodePolicy(BaseModel):
    trust_zone: Literal["untrusted", "trusted", "owner"] = "trusted"
    data_classification: Literal["public", "internal", "confidential", "restricted"] = "internal"
    credential_domains: list[str] = Field(default_factory=list, max_length=64)
    max_risk: int = Field(default=1, ge=0, le=6)
    max_concurrency: int = Field(default=2, ge=1, le=64)
    profile_concurrency: dict[str, int] = Field(default_factory=dict)
    exclusive_gpu: bool = True
    delegation_depth: int = Field(default=3, ge=0, le=10)


class BenchmarkSample(BaseModel):
    node_id: str
    name: str = Field(min_length=1, max_length=128)
    value: float
    unit: str = Field(min_length=1, max_length=32)
    opt_in: bool = False
