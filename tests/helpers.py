"""Shared test helpers: an oracle that computes expected values straight from the CSV (independently of the
code under test), a CSV writer for custom datasets, and a scriptable fake Claude."""

import csv
from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from people_server.settings import ServerSettings

CSV_PATH = ServerSettings().people_csv
ANNUAL = {"Yearly": 1, "Monthly": 12, "Hourly": 2080}
CsvRow = dict[str, str]


# ---------- Oracle ----------


def read_csv(path: Path = CSV_PATH) -> list[CsvRow]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[CsvRow]) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def age_of(row: CsvRow) -> int | None:
    if not row["Date of Birth"]:
        return None
    born = datetime.strptime(row["Date of Birth"], "%d/%m/%Y").date()
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def annual_salary(row: CsvRow) -> float | None:
    if not row["Salary Amount"] or row["Salary Type"] not in ANNUAL:
        return None
    return float(row["Salary Amount"]) * ANNUAL[row["Salary Type"]]


def currency_of(row: CsvRow) -> str:
    return row["Salary Currency"].split()[0] if row["Salary Currency"] else ""


# ---------- Fake Claude ----------

USAGE = SimpleNamespace(input_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=5)


def text_reply(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    content = [SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(stop_reason=stop_reason, content=content, usage=USAGE)


def tool_reply(*calls: tuple[str, dict[str, Any]], preamble: str = "") -> SimpleNamespace:
    blocks = [SimpleNamespace(type="tool_use", id=f"call_{i}", name=name, input=args) for i, (name, args) in
              enumerate(calls)]  # fmt: skip
    if preamble:
        blocks.insert(0, SimpleNamespace(type="text", text=preamble))
    return SimpleNamespace(stop_reason="tool_use", content=blocks, usage=USAGE)


def refusal_reply() -> SimpleNamespace:
    return SimpleNamespace(stop_reason="refusal", content=[], usage=USAGE)


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
    """Stands in for `anthropic.AsyncAnthropic`: returns scripted responses in order (repeating the last one)
    and records every request it received."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=self)  # so the agent's `claude.beta.messages.stream` lands here

    def stream(self, **kwargs: Any) -> FakeStream:
        # Snapshot the messages: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return FakeStream(self.responses[min(len(self.requests), len(self.responses)) - 1])
