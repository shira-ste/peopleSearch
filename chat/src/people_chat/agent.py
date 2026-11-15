"""The agent loop: Claude answers a question by calling the people MCP server's tools."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anthropic
from anthropic.types.beta import (
    BetaMessageParam,
    BetaTextBlockParam,
    BetaToolParam,
    BetaToolResultBlockParam,
    BetaUsage,
)
from mcp import Client
from mcp.types import CallToolResult

from people_chat.models import Event, TextEvent, ToolResultEvent, ToolStartEvent
from people_chat.settings import ChatSettings

logger = logging.getLogger(__name__)

Emit = Callable[[Event], Awaitable[None]]
Connect = Callable[[], Client]  # opens a connection to the MCP server

MAX_TOKENS = 16_000  # per Claude call; answers here are a few hundred tokens, so this is a safety ceiling
# Server-side refusal fallback is only offered on some models (not Haiku), so it's sent only to those.
# This list will go out of date as models are released; an unlisted model simply runs without the fallback.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1"}
# Tools sent in strict mode, which guarantees Claude's arguments match the schema (get_person was once
# called with no name). The tools sharing the 13-field filter are too complex for strict mode ("Schema is
# too complex"), so the MCP server validates those and Claude corrects a bad call on the next round.
STRICT_TOOLS = {"get_person", "get_dataset_overview"}
# Schema keywords strict mode rejects. Dropping them only loosens what Claude may send: the MCP server
# still enforces them.
STRICT_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength"}

SYSTEM = """You answer questions about a company's people records using the provided tools.
- Answer only from tool results; never guess numbers. If the tools can't answer, say so.
- Map places, teams and other categories to the valid values the data server lists below \
(e.g. "NYC" -> city "New York") and use them directly; if a place isn't listed, it isn't in the data.
- "Employees" means people with work_status 'active'; "people" or "everyone" includes all records.
- Report salaries in their original currencies. Only use convert_to when the user asks for a single \
currency or a comparison across currencies, and then mention the exchange-rate date. "Salary in USD" \
(or GBP/ILS) means convert to that currency; only filter by salary_currency when they ask about people \
*paid* in it.
- If a name matches several people, ask which one they mean.
- Politely decline questions unrelated to the people data.
- Be concise: lead with the answer, then brief supporting detail."""

NO_ANSWER = "Sorry, I couldn't produce an answer to that. Try rephrasing the question."
CUT_OFF = "(The answer was cut off because it got too long. Try a narrower question.)"
STEP_LIMIT = "I couldn't work this out within the step limit. Try rephrasing the question."

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None


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


def strict_schema(schema: Json) -> Json:
    """Adapt a JSON schema for strict mode: every object must say whether extra properties are allowed
    (forbid them unless the schema already decides), and unsupported keywords are dropped."""
    if isinstance(schema, dict):
        out = {key: strict_schema(value) for key, value in schema.items() if key not in STRICT_UNSUPPORTED}
        if out.get("type") == "object":
            out.setdefault("additionalProperties", False)
        return out
    if isinstance(schema, list):
        return [strict_schema(value) for value in schema]
    return schema


def to_claude_tool(name: str, description: str | None, schema: dict[str, Any]) -> BetaToolParam:
    """Convert an MCP tool to a Claude tool, in strict mode if it's listed in STRICT_TOOLS."""
    if name not in STRICT_TOOLS:
        return {"name": name, "description": description or "", "input_schema": schema}
    strict = strict_schema(schema)
    assert isinstance(strict, dict)
    input_schema: dict[str, object] = {**strict}
    return {"name": name, "description": description or "", "input_schema": input_schema, "strict": True}


def tool_result_text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def log_usage(usage: BetaUsage) -> None:
    logger.info(
        "Claude call: input=%s cache_read=%s cache_write=%s output=%s",
        usage.input_tokens, usage.cache_read_input_tokens, usage.cache_creation_input_tokens, usage.output_tokens,
    )  # fmt: skip


@dataclass
class Agent:
    claude: anthropic.AsyncAnthropic
    connect: Connect
    settings: ChatSettings

    async def run(self, messages: list[BetaMessageParam], emit: Emit) -> None:
        """Run Claude's tool-use loop against the MCP server, reporting progress and answer text through `emit`.

        Always emits some answer text, so the conversation history never ends with an empty assistant turn.
        """
        settings = self.settings
        use_fallback = settings.claude_model in FALLBACK_MODELS
        wrote_text = False

        async def say(text: str) -> None:
            nonlocal wrote_text
            await emit(TextEvent(text=f"\n\n{text}" if wrote_text else text))
            wrote_text = True

        async with self.connect() as mcp:
            system = system_blocks(mcp.instructions)
            tools = [to_claude_tool(t.name, t.description, t.input_schema) for t in (await mcp.list_tools()).tools]
            for _ in range(settings.max_rounds):
                async with self.claude.beta.messages.stream(
                    model=settings.claude_model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    tools=tools,
                    messages=messages,
                    output_config={"effort": settings.claude_effort},
                    betas=["server-side-fallback-2026-07-01"] if use_fallback else anthropic.omit,
                    fallbacks="default" if use_fallback else anthropic.omit,
                    # Prompt caching, two breakpoints: system_blocks() caches tools + system prompt for every
                    # question; this one caches the conversation so far, so later rounds reuse it too.
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

                match response.stop_reason:
                    case "tool_use":
                        pass  # run the tools below, then ask Claude again
                    case "refusal":
                        return await say("Sorry, I can't help with that request.")
                    case "max_tokens":
                        return await say(CUT_OFF)
                    case _:  # finished
                        return None if wrote_text else await say(NO_ANSWER)

                messages.append({"role": "assistant", "content": response.content})
                results: list[BetaToolResultBlockParam] = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    await emit(ToolStartEvent(name=block.name, args=block.input))
                    result = await mcp.call_tool(block.name, block.input)
                    text, is_error = tool_result_text(result), bool(result.is_error)
                    await emit(ToolResultEvent(name=block.name, args=block.input, result=text, is_error=is_error))
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error}
                    )
                messages.append({"role": "user", "content": results})
        await say(STEP_LIMIT)
