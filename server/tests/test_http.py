"""End-to-end test: start the real server process and talk to it over Streamable HTTP on /mcp."""

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from mcp import Client

SERVER_DIR = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=SERVER_DIR,
        env={"MCP_PORT": str(port), "PATH": ""},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(60):
            try:
                httpx.get(url, timeout=0.5)
                break
            except httpx.TransportError:
                time.sleep(0.25)
        else:
            pytest.fail("MCP server did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.anyio
async def test_mcp_over_streamable_http(server_url: str) -> None:
    async with Client(server_url) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("count_people", {"filter": {"city": "Tel Aviv"}})
    assert "count_people" in names
    assert result.structured_content == {"total": 24, "group_by": None, "groups": None}
