"""The real MCP server process, over Streamable HTTP on /mcp: startup builds the database on demand."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp import Client

from tests.integration.servers import running_mcp_server


@pytest.fixture(scope="module")
def server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    db = tmp_path_factory.mktemp("http") / "people.db"  # doesn't exist yet: the server must build it
    with running_mcp_server(db) as url:
        assert db.exists()
        yield url


@pytest.mark.anyio
async def test_mcp_over_streamable_http(server_url: str) -> None:
    async with Client(server_url) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("count_people", {"filter": {"city": "Tel Aviv"}})
        instructions = client.instructions or ""
    assert names == {"get_dataset_overview", "count_people", "aggregate", "search_people", "get_person"}
    assert result.structured_content == {"total": 24, "group_by": None, "groups": None}
    assert "Tel Aviv Bakery" in instructions


def test_database_location_is_configurable(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "folder" / "people.db"
    with running_mcp_server(db):
        assert db.exists()
