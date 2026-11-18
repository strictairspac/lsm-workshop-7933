from pydantic import BaseModel, Field


class ArchiveQuery(BaseModel):
    start_key: str | None = Field(default=None, max_length=64)
    end_key: str | None = Field(default=None, max_length=64)
    read_quota: int | None = Field(default=None, ge=0)
