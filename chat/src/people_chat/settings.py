"""Chat backend settings, read once from environment variables (or a .env file) and validated at startup.

A bad value (e.g. CLAUDE_EFFORT=fast) fails immediately with a clear message, instead of every request failing.
"""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ChatSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr | None = None  # ANTHROPIC_API_KEY
    claude_model: str = "claude-haiku-5-5"  # CLAUDE_MODEL: chosen by measurement, see DESIGN.md "Speed"
    claude_effort: Effort = "low"  # CLAUDE_EFFORT: lower is faster
    mcp_url: str = "http://localhost:8000/mcp"  # MCP_URL
    max_rounds: int = Field(default=8, ge=1)  # MAX_ROUNDS: tool-use rounds before giving up
