"""Start the MCP server: `uv run people-server` (or `python -m people_server`)."""

import logging

from people_server.ingest import ensure_database
from people_server.server import create_server
from people_server.settings import ServerSettings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = ServerSettings()
    ensure_database(settings.people_csv, settings.people_db)  # builds or refreshes the database if needed
    server = create_server(settings.people_db, fx_enabled=settings.fx_enabled)
    server.run("streamable-http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
