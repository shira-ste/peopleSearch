# People Search

Ask questions about a company's people records in plain English, for example *"How many people live in London?"* or *"What is the average age of employees?"*, and get answers backed by real data.

The project has two parts:

1. **An MCP server** that loads the people CSV into SQLite and exposes typed query tools over **Streamable HTTP at `/mcp`**.
2. **A chat interface**: a React web app plus a small Python backend. The backend lets Claude answer questions by calling the MCP server's tools.

```mermaid
flowchart LR
    UI["Web chat<br/>React · :5173"] -->|POST /api/chat| API["Chat backend<br/>FastAPI · :8001"]
    API <-->|tool calls| Claude["Claude API"]
    API -->|Streamable HTTP /mcp| MCP["MCP server<br/>:8000"]
    MCP --> DB[("SQLite<br/>data/people.db")]
    CSV["data/people-list-export.csv"] -.->|ingested on first start| DB
```

## Quick start

**Prerequisites:** [uv](https://docs.astral.sh/uv/) (it installs Python for you), Node.js 20+, and an [Anthropic API key](https://platform.claude.com) (separate from a claude.ai subscription).

```bash
cp .env.example .env          # then put your key in ANTHROPIC_API_KEY
```

Run each service in its own terminal:

```bash
# 1. MCP server: builds data/people.db from the CSV on first start
cd server && uv run python server.py

# 2. Chat backend
cd chat && uv run --env-file ../.env uvicorn main:app --port 8001

# 3. Web UI
cd web && npm install && npm run dev
```

Open **http://localhost:5173**, then click an example question or type your own.

Under each answer, **"Tools used"** shows exactly which MCP tools Claude called, with what arguments, and what the server returned.

## Example questions

| Question | What happens |
|---|---|
| How many people live in London? | `count_people` filtered by city |
| What is the average age of employees? | `aggregate` of age over active people |
| How many hourly employees work in London? | `count_people` filtered by city and salary type |
| Average salary by office | `aggregate` grouped by office, in each office's own currency |
| Average salary of everyone in USD | `aggregate` with live conversion to USD |
| Who reports to Bea? | `get_person`, which matches the nickname and returns direct reports |
| And in New York? | Follow-up questions work because the chat history is sent with each request |

## MCP tools

| Tool | Purpose |
|---|---|
| `get_dataset_overview` | Total count, valid values for each category field (cities, teams, …), and notes on the data |
| `count_people` | Count people matching a filter, optionally grouped by a field |
| `aggregate` | avg / min / max / sum / median of age, tenure or annual salary |
| `search_people` | List matching people, sorted, limited to 100 per call |
| `get_person` | One person's full record with their manager and direct reports |

All tools share one typed `filter` object (city, team, salary type, age range, manager, …). See [DESIGN.md](DESIGN.md) for the details.

To explore the server on its own, run `npx @modelcontextprotocol/inspector` and connect to `http://localhost:8000/mcp` (transport: Streamable HTTP).

## Project layout

```
data/      people-list-export.csv (source). people.db is generated, not committed
server/    MCP server: ingest.py (CSV → SQLite), fx.py (currency rates), server.py (tools)
chat/      Chat backend: main.py (Claude tool-use loop and POST /api/chat)
web/       React + Vite + TypeScript chat UI (src/App.tsx)
```

## Configuration

Set these in `.env`, which the chat backend reads:

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Required. Your Claude API key |
| `MCP_URL` | `http://localhost:8000/mcp` | Where the chat backend finds the MCP server |
| `CLAUDE_MODEL` | `claude-opus-5-5` | The Claude model to use |

## Data

The server loads the CSV into SQLite automatically the first time it starts.

To reload after changing the CSV:

```bash
cd server && uv run python ingest.py
```

## Code quality

Python code is fully type-annotated and checked with **ruff** (lint and format) and **mypy** in strict mode. The web app is checked with **oxlint** and the TypeScript compiler.

```bash
cd server && uv run ruff check . && uv run ruff format --check . && uv run mypy .
cd chat   && uv run ruff check . && uv run ruff format --check . && uv run mypy .
cd web    && npm run lint && npm run build
```
