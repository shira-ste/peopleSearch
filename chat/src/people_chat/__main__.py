"""Start the chat backend: `uv run people-chat` (or `python -m people_chat`)."""

import logging
import os

import uvicorn

from people_chat.app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(),
        host=os.environ.get("CHAT_HOST", "127.0.0.1"),
        port=int(os.environ.get("CHAT_PORT", "8001")),
        log_config=None,  # keep the logging configured above
    )


if __name__ == "__main__":
    main()
