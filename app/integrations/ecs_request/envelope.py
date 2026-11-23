"""ECS synchronous request envelope and safe validation issue translation."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.ecs_callback.envelope import issues_from


class EcsRequestEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    device_code: Annotated[str, Field(min_length=1)]
    request_type: Annotated[str, Field(min_length=1)]
    timestamp: Annotated[int, Field(ge=0, le=2**63 - 1)]
    data: dict = {}


__all__ = ["EcsRequestEnvelope", "issues_from"]
