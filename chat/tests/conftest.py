"""Fixtures: run the chat backend against the real MCP server in-process, with a scriptable fake Claude."""

from collections.abc import Callable, Iterator
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


def text_reply(text: str) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def tool_reply(*calls: tuple[str, dict[str, Any]]) -> SimpleNamespace:
    blocks = [SimpleNamespace(type="tool_use", id=f"call_{i}", name=name, input=args) for i, (name, args) in
              enumerate(calls)]  # fmt: skip
    return SimpleNamespace(stop_reason="tool_use", content=blocks)


class FakeClaude:
    """Returns scripted responses in order and records every request it received."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        # Snapshot the messages: the backend keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses[min(len(self.requests), len(self.responses)) - 1]


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeClaude]:
    def install(*responses: SimpleNamespace) -> FakeClaude:
        fake = FakeClaude(list(responses))
        monkeypatch.setattr(main.client.beta.messages, "create", fake.create)
        return fake

    return install
