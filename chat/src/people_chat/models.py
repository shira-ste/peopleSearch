"""Request, response and streamed-event models of the chat API."""

from typing import Any, Literal

from anthropic.types.beta import BetaMessageParam
from pydantic import BaseModel


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str

    def to_param(self) -> BetaMessageParam:
        if self.role == "user":
            return {"role": "user", "content": self.content}
        return {"role": "assistant", "content": self.content}


class ChatRequest(BaseModel):
    messages: list[Message]


class ToolCall(BaseModel):
    name: str
    args: dict[str, Any]
    result: str
    is_error: bool


class ChatResponse(BaseModel):
    answer: str
    tool_calls: list[ToolCall]


# ---------- Events: what the agent reports while it works (streamed to the UI one per line) ----------


class TextEvent(BaseModel):
    type: Literal["text"] = "text"
    text: str  # the next piece of the answer


class ToolStartEvent(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    name: str
    args: dict[str, Any]


class ToolResultEvent(ToolCall):
    type: Literal["tool_result"] = "tool_result"


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    status: int
    detail: str


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"


Event = TextEvent | ToolStartEvent | ToolResultEvent | ErrorEvent | DoneEvent
