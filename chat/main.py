"""Chat backend: answers questions by letting Claude call the people MCP server's tools."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal, cast

import anthropic
from anthropic.types.beta import (
    BetaMessageParam,
    BetaTextBlockParam,
    BetaToolParam,
    BetaToolResultBlockParam,
    BetaUsage,
)
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from mcp import Client
from mcp.types import CallToolResult
from pydantic import BaseModel

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORT = cast(Effort, os.environ.get("CLAUDE_EFFORT", "low"))  # lower is faster
MAX_ROUNDS = 8
# Server-side refusal fallback is only offered on some models (not Haiku), so it's sent only to those.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1"}
USE_FALLBACK = MODEL in FALLBACK_MODELS

SYSTEM = """You answer questions about a company's people records using the provided tools.
- Answer only from tool results; never guess numbers. If the tools can't answer, say so.
- Map places, teams and other categories to the valid values the data server lists below \
(e.g. "NYC" -> city "New York") and use them directly; if a place isn't listed, it isn't in the data.
- "Employees" means people with work_status 'active'; "people" or "everyone" includes all records.
- Report salaries in their original currencies. Only use convert_to when the user asks for a single \
currency or a comparison across currencies, and then mention the exchange-rate date.
- If a name matches several people, ask which one they mean.
- Politely decline questions unrelated to the people data.
- Be concise: lead with the answer, then brief supporting detail."""


def system_blocks(server_instructions: str | None) -> list[BetaTextBlockParam]:
    """Our rules plus the MCP server's own instructions (which list the valid values in the data).

    The cache breakpoint on the last block makes tools + system prompt (identical on every call) one cache
    entry that all questions share for 5 minutes after the last use.
    """
    blocks: list[BetaTextBlockParam] = [{"type": "text", "text": SYSTEM}]
    if server_instructions:
        blocks.append({"type": "text", "text": f"Notes from the data server:\n{server_instructions}"})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


client = anthropic.AsyncAnthropic()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
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
Emit = Callable[[Event], Awaitable[None]]


def tool_result_text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def log_usage(usage: BetaUsage) -> None:
    logging.info(
        "Claude call: input=%s cache_read=%s cache_write=%s output=%s",
        usage.input_tokens, usage.cache_read_input_tokens, usage.cache_creation_input_tokens, usage.output_tokens,
    )  # fmt: skip


async def run_agent(messages: list[BetaMessageParam], emit: Emit) -> None:
    """Run Claude's tool-use loop against the MCP server, reporting progress and answer text through `emit`."""
    async with Client(MCP_URL) as mcp:
        system = system_blocks(mcp.instructions)
        tools: list[BetaToolParam] = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in (await mcp.list_tools()).tools
        ]
        wrote_text = False
        for _ in range(MAX_ROUNDS):
            async with client.beta.messages.stream(
                model=MODEL,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": EFFORT},
                betas=["server-side-fallback-2026-07-01"] if USE_FALLBACK else anthropic.omit,
                fallbacks="default" if USE_FALLBACK else anthropic.omit,
                # Prompt caching, two breakpoints: system_blocks() caches tools + system prompt for every question;
                # this one caches the conversation so far, so later rounds of the tool loop reuse it too.
                cache_control={"type": "ephemeral"},
            ) as stream:
                first_chunk = True
                async for chunk in stream.text_stream:
                    if first_chunk and wrote_text:
                        await emit(TextEvent(text="\n\n"))  # separate text from an earlier round
                    first_chunk, wrote_text = False, True
                    await emit(TextEvent(text=chunk))
                response = await stream.get_final_message()
            log_usage(response.usage)
            if response.stop_reason == "refusal":
                await emit(TextEvent(text="Sorry, I can't help with that request."))
                return
            if response.stop_reason != "tool_use":
                return

            messages.append({"role": "assistant", "content": response.content})
            results: list[BetaToolResultBlockParam] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                await emit(ToolStartEvent(name=block.name, args=block.input))
                result = await mcp.call_tool(block.name, block.input)
                text, is_error = tool_result_text(result), bool(result.is_error)
                await emit(ToolResultEvent(name=block.name, args=block.input, result=text, is_error=is_error))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": results})
    await emit(TextEvent(text="I couldn't work this out within the step limit. Try rephrasing the question."))


def to_http_error(error: BaseException) -> HTTPException:
    """Map a failure to a clear message. The MCP client wraps errors in exception groups, so unwrap first."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    match error:
        case anthropic.AuthenticationError():
            return HTTPException(500, "The Claude API key is invalid. Check ANTHROPIC_API_KEY.")
        case anthropic.RateLimitError():
            logging.warning("Claude rate limit: %s", error)
            return HTTPException(429, "Claude's rate limit was reached. Please try again in a moment.")
        case anthropic.APIStatusError(status_code=status):
            return HTTPException(502, f"Claude API error ({status}). Please try again.")
        case anthropic.APIConnectionError():
            return HTTPException(502, "Could not reach the Claude API.")
        case _:  # anything else comes from the MCP connection (server down, transport errors)
            logging.exception("MCP failure", exc_info=error)
            return HTTPException(503, f"The people data service is unavailable ({MCP_URL}).")


def validate(req: ChatRequest) -> list[BetaMessageParam]:
    """Reject bad requests before any work starts. Only plain-text history is resent, not earlier tool calls."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set on the chat backend.")
    if not req.messages or req.messages[-1].role != "user" or not req.messages[-1].content.strip():
        raise HTTPException(400, "The last message must be a non-empty user question.")
    return [m.to_param() for m in req.messages]


@app.post("/api/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    """Answer in one JSON response, once everything is done."""
    messages = validate(req)
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    try:
        await run_agent(messages, collect)
    except Exception as e:
        raise to_http_error(e) from e
    return ChatResponse(
        answer="".join(e.text for e in events if isinstance(e, TextEvent)),
        tool_calls=[ToolCall(**e.model_dump(exclude={"type"})) for e in events if isinstance(e, ToolResultEvent)],
    )


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Stream progress as it happens: one JSON event per line (tool_call, tool_result, text, then done or error)."""
    messages = validate(req)
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def produce() -> None:
        # Runs as its own task, so the agent's MCP connection never has to cross a `yield`.
        try:
            await run_agent(messages, queue.put)
            await queue.put(DoneEvent())
        except Exception as e:  # the response has already started, so errors are sent as an event
            error = to_http_error(e)
            await queue.put(ErrorEvent(status=error.status_code, detail=error.detail))
        finally:
            await queue.put(None)

    async def lines() -> AsyncIterator[str]:
        task = asyncio.create_task(produce())
        try:
            while (event := await queue.get()) is not None:
                yield event.model_dump_json() + "\n"
        finally:
            task.cancel()  # the browser went away: stop working

    return StreamingResponse(lines(), media_type="application/x-ndjson")
