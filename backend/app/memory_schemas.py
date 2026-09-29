from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MemoryType = Literal["world", "experience", "observation"]
Budget = Literal["low", "mid", "high"]


class RetainRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    context: str | None = Field(default=None, max_length=500)
    document_id: str | None = Field(default=None, max_length=192)
    timestamp: datetime | None = None


class RecallRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    types: list[MemoryType] | None = None
    budget: Budget = "mid"
    max_tokens: int = Field(default=4096, ge=1, le=32_000)


class ReflectRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    budget: Budget = "low"
    max_tokens: int = Field(default=4096, ge=1, le=32_000)
    include_facts: bool = True
