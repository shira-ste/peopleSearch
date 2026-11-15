"""End-to-end tests: real Claude + the real MCP server, checking the facts in each answer.

These call the paid Claude API (a few cents per run), so they only run when asked:

    RUN_E2E=1 uv run pytest -m e2e                                         # in-process backend + MCP server
    RUN_E2E=1 E2E_BASE_URL=http://localhost:8080 uv run pytest -m e2e      # a running deployment (e.g. Docker)

The expected values are facts about data/people-list-export.csv (verified against the CSV by the
integration tests). Language models phrase answers differently each time, so the checks look for the key
fact or tool behaviour, not exact wording.
"""

import os
import re
import statistics
from collections.abc import Callable, Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.server.mcpserver import MCPServer

from people_chat import agent
from people_chat.app import create_app
from tests.helpers import age_of, read_csv

BASE_URL = os.environ.get("E2E_BASE_URL")  # set to test a deployed stack instead of an in-process one
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("RUN_E2E"), reason="set RUN_E2E=1 to call the real Claude API"),
]
in_process_only = pytest.mark.skipif(BASE_URL is not None, reason="inspects the backend's internals")
Answer = dict[str, Any]


@pytest.fixture
def api(mcp_server: MCPServer) -> Iterator[TestClient | httpx2.Client]:
    if BASE_URL:
        with httpx2.Client(base_url=BASE_URL, timeout=120) as client:
            yield client
    else:  # real Claude (key from .env), real MCP server in-process
        with TestClient(create_app(connect=lambda: Client(mcp_server))) as client:
            yield client


def ask(api: TestClient | httpx2.Client, *turns: str) -> Answer:
    roles = ["user", "assistant"]
    res = api.post("/api/chat", json={"messages": [{"role": roles[i % 2], "content": t} for i, t in enumerate(turns)]})
    assert res.status_code == 200, res.text
    answer: Answer = res.json()
    return answer


WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "eight": "8"}


def numbers(text: str) -> set[str]:
    """Numbers mentioned in an answer, including small ones written as words ("three")."""
    found = set(re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")))
    return found | {digit for word, digit in WORDS.items() if re.search(rf"\b{word}\b", text, re.I)}


def expected_average_age_of_active_employees() -> float:
    """Ages change over time, so compute today's value from the CSV instead of hard-coding it."""
    return statistics.mean(a for r in read_csv() if r["Work Status"] == "active" and (a := age_of(r)) is not None)


def used_tool(answer: Answer, name: str, check: Callable[[dict[str, Any]], bool] = lambda args: True) -> bool:
    return any(c["name"] == name and check(c["args"]) for c in answer["tool_calls"])


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How many people live in London?", "55"),
        ("How many people work in Tel Aviv?", "24"),
        ("How many people are in the NYC office?", "27"),
        ("How many hourly employees work in London?", "3"),
        ("How many people are on the Bread team?", "13"),
    ],
)
def test_counts(api: TestClient | httpx2.Client, question: str, expected: str) -> None:
    assert expected in numbers(ask(api, question)["answer"])


def test_average_age_of_employees(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "What is the average age of employees?")
    expected = expected_average_age_of_active_employees()
    assert numbers(answer["answer"]) & {f"{expected:.1f}", f"{expected:.2f}"}
    assert used_tool(answer, "aggregate", lambda a: a.get("filter", {}).get("work_status") == "active")


def test_salary_stays_in_original_currencies(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "What is the average salary in each office?")
    assert used_tool(answer, "aggregate", lambda a: not a.get("convert_to"))
    assert {"47148", "76240.74", "76241", "215100"} & numbers(answer["answer"])


def test_salary_converted_when_asked(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "What is the average salary of everyone in USD?")
    assert used_tool(answer, "aggregate", lambda a: a.get("convert_to") == "USD")


def test_people_paid_in_a_currency_are_filtered_not_converted(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "What's the average salary of the people who are paid in USD?")
    assert used_tool(
        answer, "aggregate", lambda a: a.get("filter", {}).get("salary_currency") == "USD" and not a.get("convert_to")
    )
    assert {"76240.74", "76241", "76240.7"} & numbers(answer["answer"])


def test_nickname_and_direct_reports(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "Who reports to Bea?")["answer"]
    for name in ("Alistair Pendergast", "Dror Avraham", "Yael Greenberg"):
        assert name in answer


def test_ambiguous_name_asks_which_one(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "Tell me about Alistair")["answer"]
    assert "Alistair Finch" in answer and "Alistair Pendergast" in answer


def test_place_not_in_data(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "How many people work in Paris?")["answer"].lower()
    assert re.search(r"\b(0|zero|no|none)\b", answer)


def test_aliases_are_mapped_without_an_extra_lookup_round(api: TestClient | httpx2.Client) -> None:
    """The valid values come with the MCP server's instructions, so "NYC" needs no get_dataset_overview call."""
    answer = ask(api, "How many people are in the NYC office?")
    assert "27" in numbers(answer["answer"])
    assert not used_tool(answer, "get_dataset_overview")


def test_follow_up_question_uses_history(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "How many people live in London?", "55 people live in London.", "And in New York?")
    assert "27" in numbers(answer["answer"])


def test_unrelated_question_is_declined_without_tools(api: TestClient | httpx2.Client) -> None:
    answer = ask(api, "What's the weather in London tomorrow?")
    assert answer["tool_calls"] == []


@in_process_only
def test_prompt_cache_is_reused_across_rounds(api: TestClient | httpx2.Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call resends ~4K tokens of tools + system prompt; after the first round they must come from cache."""
    usages: list[Any] = []
    monkeypatch.setattr(agent, "log_usage", usages.append)  # record each Claude call's token usage
    ask(api, "How many people live in London, and how many of them are paid hourly?")

    assert len(usages) >= 2, "expected at least one tool round"
    first, *later = usages
    assert first.cache_creation_input_tokens + first.cache_read_input_tokens > 3000  # prefix written (or already warm)
    assert all(u.cache_read_input_tokens > 3000 for u in later)  # later rounds read it back


@in_process_only
def test_prompt_cache_is_shared_across_questions(
    api: TestClient | httpx2.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new question must reuse the cached tools + system prompt, not write them again."""
    usages: list[Any] = []
    monkeypatch.setattr(agent, "log_usage", usages.append)  # record each Claude call's token usage
    ask(api, "How many people work in Tel Aviv?")
    first_call_of_second_question = len(usages)
    ask(api, "How many people are on the Pastry team?")
    assert usages[first_call_of_second_question].cache_read_input_tokens > 3000
