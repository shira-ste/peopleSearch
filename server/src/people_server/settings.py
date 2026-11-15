"""Server settings, read once from environment variables (or a .env file) and validated at startup."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path(__file__).resolve().parents[3] / "data"  # <repo>/data


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    people_csv: Path = DATA_DIR / "people-list-export.csv"  # PEOPLE_CSV: the CSV export to serve
    people_db: Path = DATA_DIR / "people.db"  # PEOPLE_DB: where the SQLite copy is built
    mcp_host: str = "127.0.0.1"  # MCP_HOST: use 0.0.0.0 inside Docker
    mcp_port: int = 8000  # MCP_PORT
    fx_enabled: bool = True  # FX_ENABLED: allow live currency conversion (needs internet)
