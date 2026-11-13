"""Shared fixtures: a fresh database built from the real CSV, and an independent CSV-based oracle."""

import csv
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest

import server
from ingest import CSV_PATH, ingest

ANNUAL = {"Yearly": 1, "Monthly": 12, "Hourly": 2080}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("db") / "people.db"
    ingest(CSV_PATH, path)
    return path


@pytest.fixture(autouse=True)
def use_test_db(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the server at the test database so tests never touch data/people.db."""
    monkeypatch.setattr(server, "DB_PATH", db_path)
    yield


# ---------- Oracle: expected values computed straight from the CSV, without the code under test ----------


@pytest.fixture(scope="session")
def rows() -> list[dict[str, str]]:
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def age_of(row: dict[str, str]) -> int | None:
    if not row["Date of Birth"]:
        return None
    born = datetime.strptime(row["Date of Birth"], "%d/%m/%Y").date()
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def annual_salary(row: dict[str, str]) -> float | None:
    if not row["Salary Amount"] or row["Salary Type"] not in ANNUAL:
        return None
    return float(row["Salary Amount"]) * ANNUAL[row["Salary Type"]]


def currency_of(row: dict[str, str]) -> str:
    return row["Salary Currency"].split()[0] if row["Salary Currency"] else ""
