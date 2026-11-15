# One image recipe for both Python services. PACKAGE picks which: people-server or people-chat.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

ARG PACKAGE
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

# Dependencies first (cached between code changes), installed exactly as pinned in uv.lock.
COPY pyproject.toml uv.lock .python-version ./
COPY server/pyproject.toml server/
COPY chat/pyproject.toml chat/
RUN mkdir -p server/src/people_server chat/src/people_chat \
    && uv sync --frozen --no-dev --no-install-workspace --package "$PACKAGE"

# Then the code.
COPY server/src server/src
COPY chat/src chat/src
RUN uv sync --frozen --no-dev --package "$PACKAGE"

ENV PATH="/app/.venv/bin:$PATH"
