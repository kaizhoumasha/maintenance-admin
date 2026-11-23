"""WMS-owned bin codes, with string serialization at the ECS boundary."""

from typing import Annotated

from pydantic import AfterValidator, Field, StrictInt, StrictStr

BinType = StrictInt | Annotated[StrictStr, Field(min_length=1)]
EcsBinType = Annotated[BinType, AfterValidator(str)]
