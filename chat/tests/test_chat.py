"""Functional tests for the chat backend: the real tool loop and the real MCP server, with a scripted fake Claude."""

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

import main
from tests.conftest import FakeClaude, text_reply, tool_reply

FakeFactory = Callable[..., FakeClaude]


def ask(api: TestClient, *turns: str) -> httpx2.Response:
    """Send a conversation; turns alternate user / assistant and end with the user's question."""
    roles = ["user", "assistant"]
    messages = [{"role": roles[i % 2], "content": text} for i, text in enumerate(turns)]
    return api.post("/api/chat", json={"messages": messages})


# ---------- The tool loop ----------


def test_answers_by_calling_an_mcp_tool(api: TestClient, fake_claude: FakeFactory) -> None:
    fake = fake_claude(tool_reply(("count_people", {"filter": {"city": "London"}})), text_reply("55 people."))
    res = ask(api, "How many people live in London?")

    assert res.status_code == 200
    body = res.json()
    assert body["answer"] == "55 people."
    [call] = body["tool_calls"]
    assert call["name"] == "count_people"
    assert json.loads(call["result"])["total"] == 55  # real data from the real MCP server
    # The tool result went back to Claude, linked to the right tool_use id.
    [result_block] = fake.requests[1]["messages"][-1]["content"]
    assert result_block["tool_use_id"] == "call_0"
    assert result_block["is_error"] is False


def test_parallel_tool_calls_return_results_in_one_message(api: TestClient, fake_claude: FakeFactory) -> None:
    fake = fake_claude(
        tool_reply(
            ("count_people", {"filter": {"city": "London"}}), ("count_people", {"filter": {"city": "Tel Aviv"}})
        ),
        text_reply("55 and 24."),
    )
    body = ask(api, "London vs Tel Aviv headcount?").json()
    assert [json.loads(c["result"])["total"] for c in body["tool_calls"]] == [55, 24]
    results = fake.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["call_0", "call_1"]


def test_tool_errors_are_sent_back_to_claude(api: TestClient, fake_claude: FakeFactory) -> None:
    fake = fake_claude(tool_reply(("get_person", {"name_or_email": "Nobody Atall"})), text_reply("No such person."))
    body = ask(api, "Who is Nobody Atall?").json()

    assert body["tool_calls"][0]["is_error"] is True
    [result_block] = fake.requests[1]["messages"][-1]["content"]
    assert result_block["is_error"] is True
    assert "No person matches" in result_block["content"]


def test_invalid_tool_arguments_are_reported_not_crashed(api: TestClient, fake_claude: FakeFactory) -> None:
    fake_claude(tool_reply(("search_people", {"limit": 500})), text_reply("Let me use a smaller limit."))
    body = ask(api, "List everyone").json()
    assert body["tool_calls"][0]["is_error"] is True
    assert "100" in body["tool_calls"][0]["result"]


def test_loop_stops_after_max_rounds(api: TestClient, fake_claude: FakeFactory) -> None:
    fake = fake_claude(tool_reply(("count_people", {})))  # Claude never stops calling tools
    body = ask(api, "Loop forever").json()
    assert "step limit" in body["answer"]
    assert len(fake.requests) == main.MAX_ROUNDS


def test_refusal_gets_a_polite_answer(api: TestClient, fake_claude: FakeFactory) -> None:
    fake_claude(SimpleRefusal())
    assert ask(api, "Something disallowed").json()["answer"] == "Sorry, I can't help with that request."


class SimpleRefusal:
    stop_reason = "refusal"
    content: list[Any] = []
    usage = SimpleNamespace(input_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=0)


def test_conversation_history_is_sent_as_plain_text(api: TestClient, fake_claude: FakeFactory) -> None:
    fake = fake_claude(text_reply("27."))
    ask(api, "How many in London?", "55.", "And in NYC?")
    assert fake.requests[0]["messages"] == [
        {"role": "user", "content": "How many in London?"},
        {"role": "assistant", "content": "55."},
        {"role": "user", "content": "And in NYC?"},
    ]
    assert {t["name"] for t in fake.requests[0]["tools"]} >= {"count_people", "get_person"}


def test_every_claude_call_enables_prompt_caching(api: TestClient, fake_claude: FakeFactory) -> None:
    fake = fake_claude(tool_reply(("count_people", {})), text_reply("107."))
    ask(api, "How many people?")
    assert len(fake.requests) == 2
    for request in fake.requests:
        assert request["cache_control"] == {"type": "ephemeral"}  # caches the growing conversation
        assert request["system"][-1]["cache_control"] == {"type": "ephemeral"}  # caches tools + system prompt


def test_cached_prefix_is_identical_across_rounds(api: TestClient, fake_claude: FakeFactory) -> None:
    """The cache only hits if tools and system prompt are byte-identical on every call."""
    fake = fake_claude(tool_reply(("count_people", {})), tool_reply(("get_dataset_overview", {})), text_reply("Done."))
    ask(api, "Overview please")
    first, *rest = fake.requests
    for later in rest:
        assert json.dumps(later["tools"], sort_keys=False) == json.dumps(first["tools"], sort_keys=False)
        assert later["system"] == first["system"]


# ---------- Streaming endpoint ----------


def stream_events(api: TestClient, *turns: str) -> list[dict[str, Any]]:
    roles = ["user", "assistant"]
    messages = [{"role": roles[i % 2], "content": text} for i, text in enumerate(turns)]
    with api.stream("POST", "/api/chat/stream", json={"messages": messages}) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in res.iter_lines() if line]


def test_stream_reports_progress_in_order(api: TestClient, fake_claude: FakeFactory) -> None:
    fake_claude(tool_reply(("count_people", {"filter": {"city": "London"}})), text_reply("55 people live in London."))
    events = stream_events(api, "How many people live in London?")

    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "text", "done"]
    assert events[0] == {"type": "tool_call", "name": "count_people", "args": {"filter": {"city": "London"}}}
    assert json.loads(events[1]["result"])["total"] == 55
    assert "".join(e["text"] for e in events if e["type"] == "text") == "55 people live in London."


def test_stream_separates_text_from_different_rounds(api: TestClient, fake_claude: FakeFactory) -> None:
    preamble = tool_reply(("count_people", {}))
    preamble.content.insert(0, SimpleNamespace(type="text", text="Let me check."))
    fake_claude(preamble, text_reply("107 people."))
    events = stream_events(api, "How many people?")
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Let me check.\n\n107 people."


def test_stream_reports_failures_as_an_error_event(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp import Client

    monkeypatch.setattr(main, "Client", lambda url: Client("http://127.0.0.1:9/mcp"))  # MCP server down
    events = stream_events(api, "How many people live in London?")
    assert events[-1]["type"] == "error"
    assert events[-1]["status"] == 503
    assert "unavailable" in events[-1]["detail"]


def test_stream_rejects_bad_requests_before_streaming(api: TestClient) -> None:
    res = api.post("/api/chat/stream", json={"messages": [{"role": "user", "content": " "}]})
    assert res.status_code == 400


def test_json_and_stream_endpoints_agree(api: TestClient, fake_claude: FakeFactory) -> None:
    fake_claude(tool_reply(("count_people", {"filter": {"city": "Tel Aviv"}})), text_reply("24."))
    body = ask(api, "Tel Aviv?").json()
    fake_claude(tool_reply(("count_people", {"filter": {"city": "Tel Aviv"}})), text_reply("24."))
    events = stream_events(api, "Tel Aviv?")
    assert body["answer"] == "".join(e["text"] for e in events if e["type"] == "text")
    assert body["tool_calls"] == [
        {k: v for k, v in e.items() if k != "type"} for e in events if e["type"] == "tool_result"
    ]


# ---------- Input validation ----------


def test_missing_api_key(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    res = ask(api, "Hi")
    assert res.status_code == 500
    assert "ANTHROPIC_API_KEY" in res.json()["detail"]


@pytest.mark.parametrize("question", ["", "   "])
def test_empty_question_is_rejected(api: TestClient, question: str) -> None:
    assert ask(api, question).status_code == 400


def test_last_message_must_come_from_the_user(api: TestClient) -> None:
    assert ask(api, "Hi", "Hello!").status_code == 400


def test_unknown_role_is_rejected(api: TestClient) -> None:
    res = api.post("/api/chat", json={"messages": [{"role": "system", "content": "hi"}]})
    assert res.status_code == 422


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
        (ConnectionError("refused"), 503, "data service is unavailable"),
    ],
)
def test_errors_map_to_clear_messages(error: Exception, status: int, message: str) -> None:
    # The MCP client wraps errors in exception groups; the mapping must see through them.
    for raised in (error, ExceptionGroup("wrapped", [error])):
        http_error = main.to_http_error(raised)
        assert http_error.status_code == status
        assert message in http_error.detail


def test_mcp_server_down_returns_503(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp import Client

    monkeypatch.setattr(main, "Client", lambda url: Client("http://127.0.0.1:9/mcp"))  # nothing listens here
    res = ask(api, "How many people live in London?")
    assert res.status_code == 503
    assert "unavailable" in res.json()["detail"]
