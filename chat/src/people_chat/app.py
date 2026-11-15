"""The chat HTTP API: POST /api/chat/stream (streamed events) and POST /api/chat (one JSON response)."""

import asyncio
import logging
from collections.abc import AsyncIterator

import anthropic
import httpx2
from anthropic.types.beta import BetaMessageParam
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from mcp import Client, MCPError

from people_chat.agent import Agent, Connect
from people_chat.models import (
    ChatRequest,
    ChatResponse,
    DoneEvent,
    ErrorEvent,
    Event,
    TextEvent,
    ToolCall,
    ToolResultEvent,
)
from people_chat.settings import ChatSettings

logger = logging.getLogger(__name__)


def to_http_error(error: BaseException, mcp_url: str) -> HTTPException:
    """Map a failure to a clear message. The MCP client wraps errors in exception groups, so unwrap first."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    match error:
        case anthropic.AuthenticationError():
            return HTTPException(500, "The Claude API key is invalid. Check ANTHROPIC_API_KEY.")
        case anthropic.RateLimitError():
            logger.warning("Claude rate limit: %s", error)
            return HTTPException(429, "Claude's rate limit was reached. Please try again in a moment.")
        case anthropic.APIStatusError(status_code=status):
            logger.error("Claude API error: %s", error)
            return HTTPException(502, f"Claude API error ({status}). Please try again.")
        case anthropic.APIConnectionError():
            return HTTPException(502, "Could not reach the Claude API.")
        case httpx2.TransportError() | MCPError() | ConnectionError():  # the MCP server is down or failing
            logger.warning("MCP server unavailable: %r", error)
            return HTTPException(503, f"The people data service is unavailable ({mcp_url}).")
        case _:  # a bug on our side: say so rather than blaming the data service
            logger.exception("Unexpected error", exc_info=error)
            return HTTPException(500, "Something went wrong on the chat backend. Please try again.")


def create_app(
    settings: ChatSettings | None = None,
    *,
    claude: anthropic.AsyncAnthropic | None = None,
    connect: Connect | None = None,
) -> FastAPI:
    """Build the API. Tests pass their own `claude` and `connect` (MCP connection) to avoid the network."""
    settings = settings or ChatSettings()
    api_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    agent = Agent(
        claude=claude or anthropic.AsyncAnthropic(api_key=api_key),
        connect=connect or (lambda: Client(settings.mcp_url)),
        settings=settings,
    )
    app = FastAPI(title="People Search chat")

    def validate(req: ChatRequest) -> list[BetaMessageParam]:
        """Reject bad requests before any work starts. Only plain-text history is resent, not earlier tool calls."""
        if claude is None and settings.anthropic_api_key is None:
            raise HTTPException(500, "ANTHROPIC_API_KEY is not set on the chat backend.")
        if not req.messages or req.messages[-1].role != "user" or not req.messages[-1].content.strip():
            raise HTTPException(400, "The last message must be a non-empty user question.")
        if any(not m.content.strip() for m in req.messages):
            raise HTTPException(400, "Messages must not be empty.")
        return [m.to_param() for m in req.messages]

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> ChatResponse:
        """Answer in one JSON response, once everything is done."""
        messages = validate(req)
        events: list[Event] = []

        async def collect(event: Event) -> None:
            events.append(event)

        try:
            await agent.run(messages, collect)
        except Exception as e:
            raise to_http_error(e, settings.mcp_url) from e
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
                await agent.run(messages, queue.put)
                await queue.put(DoneEvent())
            except Exception as e:  # the response has already started, so errors are sent as an event
                error = to_http_error(e, settings.mcp_url)
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
