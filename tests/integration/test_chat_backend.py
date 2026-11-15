"""Integration tests for the chat backend: the real agent loop and the real MCP server (in-process), with a
scripted fake Claude."""

import json
from collections.abc import Callable, Iterator
from typing import Any, cast

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

from people_chat.agent import CUT_OFF, NO_ANSWER, STEP_LIMIT
from people_chat.app import create_app
from people_chat.settings import ChatSettings
from tests.helpers import FakeClaude, refusal_reply, text_reply, tool_reply

MakeApi = Callable[..., TestClient]


@pytest.fixture
def make_api(mcp_server: MCPServer) -> Iterator[MakeApi]:
    """Build the chat API around a fake Claude, connected to the real MCP server in-process."""
    clients: list[TestClient] = []

    def make(claude: FakeClaude, **settings: Any) -> TestClient:
        app = create_app(
            ChatSettings(_env_file=None, anthropic_api_key=SecretStr("test"), **settings),
            claude=cast(anthropic.AsyncAnthropic, claude),
            connect=lambda: Client(mcp_server),
        )
        clients.append(client := TestClient(app))
        return client

    yield make
    for client in clients:
        client.close()


def conversation(*turns: str) -> dict[str, Any]:
    """Turns alternate user / assistant and end with the user's question."""
    roles = ["user", "assistant"]
    return {"messages": [{"role": roles[i % 2], "content": text} for i, text in enumerate(turns)]}


def ask(api: TestClient, *turns: str) -> httpx2.Response:
    return api.post("/api/chat", json=conversation(*turns))


def stream_events(api: TestClient, *turns: str) -> list[dict[str, Any]]:
    with api.stream("POST", "/api/chat/stream", json=conversation(*turns)) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in res.iter_lines() if line]


def answer_text(events: list[dict[str, Any]]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "text")


# ---------- The tool loop ----------


def test_answers_by_calling_an_mcp_tool(make_api: MakeApi) -> None:
    claude = FakeClaude(tool_reply(("count_people", {"filter": {"city": "London"}})), text_reply("55 people."))
    body = ask(make_api(claude), "How many people live in London?").json()

    assert body["answer"] == "55 people."
    [call] = body["tool_calls"]
    assert call["name"] == "count_people"
    assert json.loads(call["result"])["total"] == 55  # real data from the real MCP server
    [result_block] = claude.requests[1]["messages"][-1]["content"]  # the result went back to Claude
    assert (result_block["tool_use_id"], result_block["is_error"]) == ("call_0", False)


def test_parallel_tool_calls_return_results_in_one_message(make_api: MakeApi) -> None:
    claude = FakeClaude(
        tool_reply(
            ("count_people", {"filter": {"city": "London"}}), ("count_people", {"filter": {"city": "Tel Aviv"}})
        ),
        text_reply("55 and 24."),
    )
    body = ask(make_api(claude), "London vs Tel Aviv headcount?").json()
    assert [json.loads(c["result"])["total"] for c in body["tool_calls"]] == [55, 24]
    assert [r["tool_use_id"] for r in claude.requests[1]["messages"][-1]["content"]] == ["call_0", "call_1"]


def test_tool_errors_are_sent_back_to_claude(make_api: MakeApi) -> None:
    claude = FakeClaude(tool_reply(("count_people", {"filter": {"citty": "London"}})), text_reply("Fixed it."))
    body = ask(make_api(claude), "How many in London?").json()
    assert body["tool_calls"][0]["is_error"] is True
    [result_block] = claude.requests[1]["messages"][-1]["content"]
    assert result_block["is_error"] is True and "citty" in result_block["content"]


# ---------- Every answer has text (an empty assistant turn would break the next request) ----------


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        ([text_reply("")], NO_ANSWER),  # finished without writing anything
        ([refusal_reply()], "Sorry, I can't help with that request."),
        ([text_reply("Half an ans", stop_reason="max_tokens")], f"Half an ans\n\n{CUT_OFF}"),
        ([tool_reply(("count_people", {}), preamble="Checking.")], f"Checking.\n\nChecking.\n\n{STEP_LIMIT}"),
    ],
)
def test_every_ending_produces_answer_text(make_api: MakeApi, replies: list[Any], expected: str) -> None:
    api = make_api(FakeClaude(*replies), max_rounds=2)
    assert ask(api, "Question?").json()["answer"] == expected


def test_loop_stops_after_max_rounds(make_api: MakeApi) -> None:
    claude = FakeClaude(tool_reply(("count_people", {})))  # Claude never stops calling tools
    assert STEP_LIMIT in ask(make_api(claude, max_rounds=3), "Loop forever").json()["answer"]
    assert len(claude.requests) == 3


# ---------- What is sent to Claude ----------


def test_conversation_history_is_sent_as_plain_text(make_api: MakeApi) -> None:
    claude = FakeClaude(text_reply("27."))
    ask(make_api(claude), "How many in London?", "55.", "And in NYC?")
    assert claude.requests[0]["messages"] == [
        {"role": "user", "content": "How many in London?"},
        {"role": "assistant", "content": "55."},
        {"role": "user", "content": "And in NYC?"},
    ]


def test_system_prompt_includes_the_data_servers_valid_values(make_api: MakeApi) -> None:
    claude = FakeClaude(text_reply("Hi."))
    ask(make_api(claude), "Hello")
    rules, server_notes = claude.requests[0]["system"]
    assert "cache_control" not in rules
    assert server_notes["cache_control"] == {"type": "ephemeral"}  # one breakpoint, after everything stable
    assert "New York" in server_notes["text"] and "London HQ & Flagship" in server_notes["text"]


def test_every_claude_call_enables_prompt_caching(make_api: MakeApi) -> None:
    claude = FakeClaude(tool_reply(("count_people", {})), text_reply("107."))
    ask(make_api(claude), "How many people?")
    assert len(claude.requests) == 2
    for request in claude.requests:
        assert request["cache_control"] == {"type": "ephemeral"}  # caches the growing conversation
        assert request["system"][-1]["cache_control"] == {"type": "ephemeral"}  # caches tools + system prompt


def test_cached_prefix_is_identical_across_rounds(make_api: MakeApi) -> None:
    """The cache only hits if tools and system prompt are byte-identical on every call."""
    claude = FakeClaude(tool_reply(("count_people", {})), tool_reply(("get_dataset_overview", {})), text_reply("Ok."))
    ask(make_api(claude), "Overview please")
    first, *rest = claude.requests
    for later in rest:
        assert json.dumps(later["tools"]) == json.dumps(first["tools"])
        assert later["system"] == first["system"]


def test_simple_tools_use_strict_mode(make_api: MakeApi) -> None:
    claude = FakeClaude(text_reply("Hi."))
    ask(make_api(claude), "Hello")
    tools = {t["name"]: t for t in claude.requests[0]["tools"]}
    assert tools["get_person"]["strict"] is True
    assert tools["get_person"]["input_schema"]["required"] == ["name_or_email"]
    assert "strict" not in tools["search_people"]  # too complex for strict mode; the server validates it


def test_model_and_effort_come_from_settings(make_api: MakeApi) -> None:
    claude = FakeClaude(text_reply("Hi."))
    ask(make_api(claude, claude_model="claude-opus-5-5", claude_effort="medium"), "Hello")
    request = claude.requests[0]
    assert (request["model"], request["output_config"]) == ("claude-opus-5-5", {"effort": "medium"})
    assert request["fallbacks"] == "default"  # refusal fallback is sent to models that support it


# ---------- Streaming endpoint ----------


def test_stream_reports_progress_in_order(make_api: MakeApi) -> None:
    claude = FakeClaude(tool_reply(("count_people", {"filter": {"city": "London"}})), text_reply("55 live there."))
    events = stream_events(make_api(claude), "How many people live in London?")

    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "text", "done"]
    assert events[0] == {"type": "tool_call", "name": "count_people", "args": {"filter": {"city": "London"}}}
    assert json.loads(events[1]["result"])["total"] == 55
    assert answer_text(events) == "55 live there."


def test_stream_separates_text_from_different_rounds(make_api: MakeApi) -> None:
    claude = FakeClaude(tool_reply(("count_people", {}), preamble="Let me check."), text_reply("107 people."))
    assert answer_text(stream_events(make_api(claude), "How many people?")) == "Let me check.\n\n107 people."


def test_stream_reports_failures_as_an_error_event(make_api: MakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    api = create_app(
        ChatSettings(_env_file=None, anthropic_api_key=SecretStr("test")),
        claude=cast(anthropic.AsyncAnthropic, FakeClaude(text_reply("unused"))),
        connect=lambda: Client("http://127.0.0.1:9/mcp"),  # nothing listens here: MCP server down
    )
    with TestClient(api) as client:
        events = stream_events(client, "How many people live in London?")
    assert events[-1]["type"] == "error"
    assert (events[-1]["status"], "unavailable" in events[-1]["detail"]) == (503, True)


def test_json_and_stream_endpoints_agree(make_api: MakeApi) -> None:
    replies = (tool_reply(("count_people", {"filter": {"city": "Tel Aviv"}})), text_reply("24."))
    body = ask(make_api(FakeClaude(*replies)), "Tel Aviv?").json()
    events = stream_events(make_api(FakeClaude(*replies)), "Tel Aviv?")
    assert body["answer"] == answer_text(events)
    assert body["tool_calls"] == [
        {k: v for k, v in e.items() if k != "type"} for e in events if e["type"] == "tool_result"
    ]


# ---------- Input validation ----------


def test_missing_api_key(mcp_server: MCPServer) -> None:
    app = create_app(ChatSettings(_env_file=None, anthropic_api_key=None))
    with TestClient(app) as client:
        res = ask(client, "Hi")
    assert res.status_code == 500 and "ANTHROPIC_API_KEY" in res.json()["detail"]


@pytest.mark.parametrize(
    "turns",
    [
        ("",),  # empty question
        ("   ",),  # blank question
        ("Hi", "Hello!"),  # last message from the assistant
        ("Hi", "", "And now?"),  # an empty earlier turn (Claude rejects empty messages)
    ],
)
def test_bad_conversations_are_rejected(make_api: MakeApi, turns: tuple[str, ...]) -> None:
    api = make_api(FakeClaude(text_reply("unused")))
    assert ask(api, *turns).status_code == 400
    assert api.post("/api/chat/stream", json=conversation(*turns)).status_code == 400


def test_unknown_role_is_rejected(make_api: MakeApi) -> None:
    res = make_api(FakeClaude()).post("/api/chat", json={"messages": [{"role": "system", "content": "hi"}]})
    assert res.status_code == 422


def test_health(make_api: MakeApi) -> None:
    assert make_api(FakeClaude()).get("/health").json() == {"status": "ok"}
