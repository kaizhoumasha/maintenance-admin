from pydantic import BaseModel, Field

from app.schemas.base import NO_AUDIT


class LoginIn(BaseModel):
    username: str
    password: str = Field(json_schema_extra=NO_AUDIT)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
