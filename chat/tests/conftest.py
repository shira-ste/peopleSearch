"""Fixtures: run the chat backend against the real MCP server in-process, with a scriptable fake Claude."""

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client

import main
import server
from ingest import CSV_PATH, ingest


@pytest.fixture(autouse=True)
def in_process_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connect the backend to the real MCP server object (no network), backed by a fresh test database."""
    db = tmp_path / "people.db"
    ingest(CSV_PATH, db)
    monkeypatch.setattr(server, "DB_PATH", db)
    monkeypatch.setattr(main, "Client", lambda url: Client(server.mcp))


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with TestClient(main.app) as client:
        yield client


# ---------- Fake Claude ----------


USAGE = SimpleNamespace(input_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=5)


def text_reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)], usage=USAGE)


def tool_reply(*calls: tuple[str, dict[str, Any]]) -> SimpleNamespace:
    blocks = [SimpleNamespace(type="tool_use", id=f"call_{i}", name=name, input=args) for i, (name, args) in
              enumerate(calls)]  # fmt: skip
    return SimpleNamespace(stop_reason="tool_use", content=blocks, usage=USAGE)


class FakeStream:
    """Mimics the SDK's message stream: `async with`, `text_stream` and `get_final_message()`."""

    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def chunks() -> AsyncIterator[str]:
            for block in self.response.content:
                if block.type == "text":
                    middle = len(block.text) // 2  # deliver each text in two pieces, like a real stream
                    yield block.text[:middle]
                    yield block.text[middle:]

        return chunks()

    async def get_final_message(self) -> SimpleNamespace:
        return self.response


class FakeClaude:
    """Returns scripted responses in order and records every request it received."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> FakeStream:
        # Snapshot the messages: the backend keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(self.responses[min(len(self.requests), len(self.responses)) - 1])


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeClaude]:
    def install(*responses: SimpleNamespace) -> FakeClaude:
        fake = FakeClaude(list(responses))
        monkeypatch.setattr(main.client.beta.messages, "stream", fake.stream)
        return fake

    return install
