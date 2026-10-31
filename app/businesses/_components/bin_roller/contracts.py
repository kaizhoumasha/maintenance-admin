"""The verified roller event and direction Result data, without production fields."""

from pydantic import ConfigDict, JsonValue, RootModel

from app.integrations.ecs_callback import CallbackPayload


class ScanCompletedData(CallbackPayload):
    model_config = ConfigDict(strict=True, extra="forbid")

    bin_code: str


class DirectionResult(RootModel[dict[str, JsonValue]]):
    model_config = ConfigDict(strict=True)
