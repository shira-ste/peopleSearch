"""Chat backend: answers questions by letting Gemini call the people MCP server's tools."""

import logging
import os
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from google import genai
from google.genai import errors, types
from mcp import Client
from mcp.types import CallToolResult
from pydantic import BaseModel

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
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

app = FastAPI()


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str

    def to_content(self) -> types.Content:
        # Gemini calls the assistant role "model".
        return types.Content(role="user" if self.role == "user" else "model", parts=[types.Part(text=self.content)])


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


async def run_agent(contents: list[types.Content], tool_calls: list[ToolCall]) -> str:
    """Run Gemini's function-calling loop against the MCP server and return the final answer text."""
    gemini = genai.Client()  # reads GEMINI_API_KEY
    async with Client(MCP_URL) as mcp:
        declarations = [
            types.FunctionDeclaration(
                name=t.name, description=t.description or "", parameters_json_schema=t.input_schema
            )
            for t in (await mcp.list_tools()).tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM,
            tools=[types.Tool(function_declarations=declarations)],
            # We run the loop ourselves so every MCP call can be recorded and shown in the UI.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        for _ in range(MAX_ROUNDS):
            response = await gemini.aio.models.generate_content(model=MODEL, contents=contents, config=config)
            if not response.function_calls:
                return response.text or "Sorry, I couldn't produce an answer to that."

            # Send the model's turn back unchanged (it carries thought signatures Gemini needs).
            if response.candidates and response.candidates[0].content:
                contents.append(response.candidates[0].content)
            parts: list[types.Part] = []
            for call in response.function_calls:
                name, args = call.name or "", call.args or {}
                result = await mcp.call_tool(name, args)
                text, is_error = tool_result_text(result), bool(result.is_error)
                tool_calls.append(ToolCall(name=name, args=args, result=text, is_error=is_error))
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=call.id, name=name, response={"error": text} if is_error else {"result": text}
                        )
                    )
                )
            contents.append(types.Content(role="user", parts=parts))
    return "I couldn't work this out within the step limit. Try rephrasing the question."


def to_http_error(error: BaseException) -> HTTPException:
    """Map a failure to a clear message. The MCP client wraps errors in exception groups, so unwrap first."""
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    match error:
        case errors.ClientError(code=429):
            return HTTPException(429, "Gemini is rate-limited right now. Please try again in a moment.")
        case errors.ClientError(code=400 | 401 | 403) if "API key" in str(error):
            return HTTPException(500, "The Gemini API key is invalid. Check GEMINI_API_KEY.")
        case errors.APIError(code=code):
            logging.error("Gemini API error: %s", error)
            return HTTPException(502, f"Gemini API error ({code}). Please try again.")
        case _:  # anything else comes from the MCP connection (server down, transport errors)
            logging.exception("MCP failure", exc_info=error)
            return HTTPException(503, f"The people data service is unavailable ({MCP_URL}).")


@app.post("/api/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        raise HTTPException(500, "GEMINI_API_KEY is not set on the chat backend.")
    if not req.messages or req.messages[-1].role != "user" or not req.messages[-1].content.strip():
        raise HTTPException(400, "The last message must be a non-empty user question.")

    # Only plain text history is resent; tool calls from earlier turns are not.
    contents = [m.to_content() for m in req.messages]
    tool_calls: list[ToolCall] = []
    try:
        answer = await run_agent(contents, tool_calls)
    except Exception as e:
        raise to_http_error(e) from e
    return ChatResponse(answer=answer, tool_calls=tool_calls)
