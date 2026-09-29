from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.chat_schemas import ChatMemory


class IncidentCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    service: str = Field(min_length=2, max_length=100)


class IncidentResolve(BaseModel):
    outcome: str = Field(min_length=3, max_length=10_000)
    evidence: str = Field(default="", max_length=10_000)


class IncidentInvestigationRequest(BaseModel):
    evidence: str = Field(default="", max_length=10_000)


class IncidentRead(BaseModel):
    id: int
    title: str
    service: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class IncidentInvestigationResponse(BaseModel):
    incident: IncidentRead
    recalled_memories: list[ChatMemory] = Field(default_factory=list)
    learned_pattern: str | None = None
    recommendation: str


class IncidentResolveResponse(BaseModel):
    success: bool
    incident: IncidentRead
    retained: bool
    outcome: str
