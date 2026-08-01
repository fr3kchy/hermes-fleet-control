from pathlib import Path
import os
from pydantic import BaseModel, Field, model_validator


class Settings(BaseModel):
    database_path: Path = Path(os.getenv("HFC_DATABASE_PATH", "data/fleet.db"))
    environment: str = os.getenv("HFC_ENVIRONMENT", "development")
    enrollment_secret: str = Field(default_factory=lambda: os.getenv("HFC_ENROLLMENT_SECRET", "change-me-local-only"), min_length=8)
    operator_token: str = Field(default_factory=lambda: os.getenv("HFC_OPERATOR_TOKEN", "change-me-operator"), min_length=8)
    hermes_binary: str = os.getenv("HFC_HERMES_BINARY", "hermes")
    task_timeout_seconds: int = 180
    enrollment_ttl_seconds: int = 600

    @model_validator(mode="after")
    def reject_default_production_secrets(self):
        if self.environment == "production":
            if self.enrollment_secret == "change-me-local-only":
                raise ValueError("HFC_ENROLLMENT_SECRET must be set in production")
            if self.operator_token == "change-me-operator":
                raise ValueError("HFC_OPERATOR_TOKEN must be set in production")
        return self
