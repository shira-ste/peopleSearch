"""Unit tests for the chat backend's pure functions: tool conversion, error mapping and settings."""

from typing import Any

import anthropic
import httpx2
import pytest
from mcp import MCPError
from pydantic import ValidationError

from people_chat.agent import Json, strict_schema, to_claude_tool
from people_chat.app import to_http_error
from people_chat.settings import ChatSettings

# ---------- Tool conversion ----------


def test_strict_tools_forbid_extra_properties_and_drop_unsupported_keywords() -> None:
    schema = {
        "type": "object",
        "properties": {"name_or_email": {"type": "string", "minLength": 1}, "limit": {"type": "integer", "maximum": 9}},
        "required": ["name_or_email"],
    }
    tool = to_claude_tool("get_person", "Look up a person", schema)
    assert tool.get("strict") is True
    converted: dict[str, Any] = dict(tool["input_schema"])
    assert converted["additionalProperties"] is False
    assert converted["properties"] == {"name_or_email": {"type": "string"}, "limit": {"type": "integer"}}


def test_strict_schema_keeps_an_existing_additional_properties_rule() -> None:
    schema: Json = {"type": "object", "additionalProperties": {"type": "integer"}}
    assert strict_schema(schema) == schema  # a dict[str, int] parameter stays a dict of ints


def test_only_listed_tools_are_strict() -> None:
    tool = to_claude_tool("search_people", "", {"type": "object", "$defs": {}, "properties": {}})
    assert "strict" not in tool


# ---------- Error mapping ----------

REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def api_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    return cls("error", response=httpx2.Response(status, request=REQUEST), body=None)


@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (api_error(anthropic.AuthenticationError, 401), 500, "API key is invalid"),
        (api_error(anthropic.RateLimitError, 429), 429, "rate limit"),
        (api_error(anthropic.InternalServerError, 529), 502, "Claude API error (529)"),
        (anthropic.APIConnectionError(request=REQUEST), 502, "Could not reach"),
        (httpx2.ConnectError("refused"), 503, "data service is unavailable"),
        (MCPError(code=-32603, message="boom"), 503, "data service is unavailable"),
        (KeyError("bug"), 500, "Something went wrong on the chat backend"),  # our bug: don't blame the data service
    ],
)
def test_errors_map_to_clear_messages(error: Exception, status: int, message: str) -> None:
    # The MCP client wraps errors in exception groups; the mapping must see through them.
    for raised in (error, ExceptionGroup("wrapped", [error])):
        http_error = to_http_error(raised, "http://mcp")
        assert http_error.status_code == status
        assert message in http_error.detail


# ---------- Settings ----------


def test_settings_reject_an_unknown_effort_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_EFFORT", "fast")
    with pytest.raises(ValidationError, match="claude_effort"):
        ChatSettings(_env_file=None)


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CLAUDE_MODEL", "CLAUDE_EFFORT", "MCP_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = ChatSettings(_env_file=None)
    assert (settings.claude_model, settings.claude_effort) == ("claude-haiku-5-5", "low")
    assert settings.anthropic_api_key is None
