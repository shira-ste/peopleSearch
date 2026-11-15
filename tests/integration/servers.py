"""Start the real MCP server as a separate process, for tests that talk to it over HTTP."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from tests.helpers import CSV_PATH


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextmanager
def running_mcp_server(db_path: Path) -> Iterator[str]:
    """Run `python -m people_server` on a free port with its own database; yield its /mcp URL."""
    port = free_port()
    env = {**os.environ, "PEOPLE_CSV": str(CSV_PATH), "PEOPLE_DB": str(db_path), "MCP_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "people_server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(80):
            if proc.poll() is not None:
                raise RuntimeError(f"MCP server exited: {proc.stderr.read().decode() if proc.stderr else ''}")
            try:
                httpx.get(url, timeout=0.5)
                break
            except httpx.TransportError:
                time.sleep(0.25)
        else:
            raise RuntimeError("MCP server did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)
