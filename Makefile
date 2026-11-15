# Common tasks. Run `make help` to list them.
.PHONY: help install dev run-mcp run-chat run-web check lint test test-e2e up down docker-test

help:  ## list the tasks
	@grep -E "^[a-z-]+:.*## " Makefile | sed "s/:.*## /\t/"

install:  ## install Python and web dependencies
	uv sync
	cd web && npm ci

dev:  ## run all three services locally (Ctrl+C stops them all)
	$(MAKE) -j3 run-mcp run-chat run-web
run-mcp:
	uv run people-server
run-chat:
	uv run people-chat
run-web:
	cd web && npm run dev

check: lint test  ## lint, type-check and run all tests that need no API key

lint:  ## ruff (lint + format), mypy (types), oxlint + tsc (web)
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	cd web && npm run lint && npx tsc -b

test:  ## unit + integration tests (Python) and UI tests
	uv run pytest
	cd web && npm test

test-e2e:  ## real-Claude end-to-end tests (needs ANTHROPIC_API_KEY; costs a few cents)
	RUN_E2E=1 uv run pytest -m e2e

up:  ## start everything in Docker: http://localhost:8080
	docker compose up --build -d --wait

down:  ## stop the Docker stack
	docker compose down

docker-test: up  ## run the real-Claude end-to-end tests against the Docker stack
	RUN_E2E=1 E2E_BASE_URL=http://localhost:8080 uv run pytest -m e2e
