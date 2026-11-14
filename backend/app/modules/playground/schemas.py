from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookRequest(StrictModel):
    command_id: UUID
    expected_revision: str = Field(pattern=r"^(0|[1-9][0-9]*:[1-9][0-9]*)$", max_length=50)
    queue: bool = False


class VersionRequest(StrictModel):
    expected_version: int = Field(ge=1)


class InviteRequest(StrictModel):
    command_id: UUID


class TokenRequest(StrictModel):
    token: str = Field(min_length=20, max_length=180)


class PrepareRequest(StrictModel):
    event_id: int = Field(ge=1)
    booth_id: int = Field(ge=1)
