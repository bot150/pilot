from typing import Literal

from pydantic import BaseModel, Field


class ChatHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=10_000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=20)


class ChatMemory(BaseModel):
    id: str
    type: str = "Memory"
    text: str
    context: str | None = None
    date: str | None = None


class ChatResponse(BaseModel):
    answer: str
    memories: list[ChatMemory] = Field(default_factory=list)
    reflected: bool = False
    retained: bool = False
    retained_text: str | None = None
    recall_warning: str | None = None
    retain_warning: str | None = None
