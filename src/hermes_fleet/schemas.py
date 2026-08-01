from typing import Any
from pydantic import BaseModel, Field


class EnrollmentTokenRequest(BaseModel):
    node_name: str = Field(min_length=1, max_length=128)


class EnrollRequest(BaseModel):
    token: str
    node_name: str = Field(min_length=1, max_length=128)
    capabilities: dict[str, Any]


class HeartbeatRequest(BaseModel):
    status: str = Field(pattern="^(online|degraded|offline)$")
    metrics: dict[str, Any] = {}
    capabilities: dict[str, Any] = {}


class TaskRequest(BaseModel):
    node_id: str
    profile: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=20000)
