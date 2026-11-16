"""Start the MCP server: `uv run people-server [--csv file.csv]` (or `python -m people_server`)."""

import argparse
import logging
from pathlib import Path

from people_server.ingest import ensure_database
from people_server.server import create_server
from people_server.settings import ServerSettings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = ServerSettings()
    parser = argparse.ArgumentParser(description="Serve a people CSV over MCP (Streamable HTTP at /mcp).")
    parser.add_argument("--csv", type=Path, default=settings.people_csv, help="CSV file to serve (default: PEOPLE_CSV)")
    csv_path: Path = parser.parse_args().csv
    ensure_database(csv_path, settings.people_db)  # builds or refreshes the database if needed
    server = create_server(settings.people_db, fx_enabled=settings.fx_enabled)
    server.run("streamable-http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
