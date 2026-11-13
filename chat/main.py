"""Chat backend: answers questions by letting Claude call the people MCP server's tools."""

import logging
import os
from typing import Any, Literal

import anthropic
from anthropic.types.beta import BetaMessageParam, BetaToolParam, BetaToolResultBlockParam
from fastapi import FastAPI, HTTPException
from mcp import Client
from mcp.types import CallToolResult
from pydantic import BaseModel

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
MAX_ROUNDS = 8

SYSTEM = """You answer questions about a company's people records using the provided tools.
- Answer only from tool results; never guess numbers. If the tools can't answer, say so.
- If a place, team or other category in the question doesn't obviously match the data, call \
get_dataset_overview first to find the right value (e.g. "NYC" -> city "New York").
- "Employees" means people with work_status 'active'; "people" or "everyone" includes all records.
- Report salaries in their original currencies. Only use convert_to when the user asks for a single \
currency or a comparison across currencies, and then mention the exchange-rate date.
- If a name matches several people, ask which one they mean.
- Politely decline questions unrelated to the people data.
- Be concise: lead with the answer, then brief supporting detail."""

client = anthropic.AsyncAnthropic()
app = FastAPI()


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


def tool_result_text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


async def run_agent(messages: list[BetaMessageParam], tool_calls: list[ToolCall]) -> str:
    """Run Claude's tool-use loop against the MCP server and return the final answer text."""
    async with Client(MCP_URL) as mcp:
        tools: list[BetaToolParam] = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in (await mcp.list_tools()).tools
        ]
        for _ in range(MAX_ROUNDS):
            response = await client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                tools=tools,
                messages=messages,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if response.stop_reason == "refusal":
                return "Sorry, I can't help with that request."
            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text")

            messages.append({"role": "assistant", "content": response.content})
            results: list[BetaToolResultBlockParam] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = await mcp.call_tool(block.name, block.input)
                text, is_error = tool_result_text(result), bool(result.is_error)
                tool_calls.append(ToolCall(name=block.name, args=block.input, result=text, is_error=is_error))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": results})
    return "I couldn't work this out within the step limit. Try rephrasing the question."


def to_http_error(error: BaseException) -> HTTPException:
    """Map a failure to a clear message. The MCP client wraps errors in exception groups, so unwrap first."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    match error:
        case anthropic.AuthenticationError():
            return HTTPException(500, "The Claude API key is invalid. Check ANTHROPIC_API_KEY.")
        case anthropic.RateLimitError():
            return HTTPException(429, "Claude is rate-limited right now. Please try again in a moment.")
        case anthropic.APIStatusError(status_code=status):
            return HTTPException(502, f"Claude API error ({status}). Please try again.")
        case anthropic.APIConnectionError():
            return HTTPException(502, "Could not reach the Claude API.")
        case _:  # anything else comes from the MCP connection (server down, transport errors)
            logging.exception("MCP failure", exc_info=error)
            return HTTPException(503, f"The people data service is unavailable ({MCP_URL}).")


@app.post("/api/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set on the chat backend.")
    if not req.messages or req.messages[-1].role != "user" or not req.messages[-1].content.strip():
        raise HTTPException(400, "The last message must be a non-empty user question.")

    # Only plain text history is resent; tool calls from earlier turns are not.
    messages = [m.to_param() for m in req.messages]
    tool_calls: list[ToolCall] = []
    try:
        answer = await run_agent(messages, tool_calls)
    except Exception as e:
        raise to_http_error(e) from e
    return ChatResponse(answer=answer, tool_calls=tool_calls)
