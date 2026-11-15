"""Shared fixtures. Every test runs against a database built from the CSV in a temporary folder, so tests
never touch data/people.db."""

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from people_server import fx
from people_server.ingest import ingest
from people_server.server import create_server
from tests.helpers import CSV_PATH, CsvRow, read_csv

FAKE_RATES: fx.FxRates = {"date": "2026-01-01", "rates": {"USD": 1.0, "GBP": 0.5, "ILS": 4.0}}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def rows() -> list[CsvRow]:
    """The CSV as plain dicts: the oracle that expected values are computed from."""
    return read_csv()


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("db") / "people.db"
    ingest(CSV_PATH, path)
    return path


@pytest.fixture
def mcp_server(db_path: Path) -> MCPServer:
    return create_server(db_path)


@pytest.fixture
def fake_fx(monkeypatch: pytest.MonkeyPatch) -> fx.FxRates:
    monkeypatch.setattr(fx, "get_rates", lambda base: FAKE_RATES)
    return FAKE_RATES
