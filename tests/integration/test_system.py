"""The whole system end to end, without paying for Claude: the real MCP server process (over HTTP, building
its database from the CSV on startup) and the real chat backend, streaming to a client. Only Claude is
scripted. For the same flow with real Claude, see tests/e2e."""

import json
from collections.abc import Iterator
from typing import Any, cast

import anthropic
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from people_chat.app import create_app
from people_chat.settings import ChatSettings
from tests.helpers import FakeClaude, text_reply, tool_reply
from tests.integration.servers import running_mcp_server


@pytest.fixture(scope="module")
def mcp_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with running_mcp_server(tmp_path_factory.mktemp("system") / "people.db") as url:
        yield url


def chat(mcp_url: str, claude: FakeClaude, question: str) -> list[dict[str, Any]]:
    settings = ChatSettings(_env_file=None, anthropic_api_key=SecretStr("test"), mcp_url=mcp_url)
    app = create_app(settings, claude=cast(anthropic.AsyncAnthropic, claude))  # real MCP connection by URL
    with (
        TestClient(app) as client,
        client.stream("POST", "/api/chat/stream", json={"messages": [{"role": "user", "content": question}]}) as res,
    ):
        assert res.status_code == 200
        return [json.loads(line) for line in res.iter_lines() if line]


def test_question_flows_through_every_component(mcp_url: str) -> None:
    claude = FakeClaude(
        tool_reply(("get_person", {"name_or_email": "Bea Vane"})),
        text_reply("Eight people report to Bea."),
    )
    events = chat(mcp_url, claude, "Who reports to Bea?")

    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "text", "done"]
    person = json.loads(events[1]["result"])["person"]
    assert person["name"] == "Beatrix 'Bea' Vane"
    assert len(person["direct_reports"]) == 8
    # Claude received the MCP server's instructions (built from the data) and its tool list.
    system_text = claude.requests[0]["system"][-1]["text"]
    assert "Tel Aviv Bakery" in system_text
    assert {t["name"] for t in claude.requests[0]["tools"]} >= {"get_person", "count_people"}


def test_tool_errors_cross_the_http_boundary(mcp_url: str) -> None:
    claude = FakeClaude(tool_reply(("count_people", {"filtr": {}})), text_reply("Let me fix that."))
    events = chat(mcp_url, claude, "How many?")
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["is_error"] is True and "filtr" in result["result"]
    assert events[-1]["type"] == "done"
