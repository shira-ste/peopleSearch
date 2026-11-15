# Design notes

This document explains the decisions behind the project: how the data is shaped, how the MCP API is designed, and how edge cases and errors are handled.

## Goals

- **A clear, robust MCP API.** Typed tools with validated inputs and typed outputs, rather than a "send me SQL" endpoint.
- **Answers grounded in data.** Claude only answers from tool results, and the UI shows every tool call so you can verify the answer.
- **Lean code.** Three small Python files for the server, one for the chat backend, and one React component.

## The data

The source is `data/people-list-export.csv`: 107 people, 19 columns, from a bakery chain with three offices.

| Office | City | Country | People | Salary currency |
|---|---|---|---|---|
| London HQ & Flagship | London | United Kingdom | 55 | GBP |
| NYC SoHo Shop | New York | United States | 27 | USD |
| Tel Aviv Bakery | Tel Aviv | Israel | 24 | ILS |

These quirks of the data shaped the design:

- **Dates are `DD/MM/YYYY`.** They're stored as ISO dates (`YYYY-MM-DD`).
- **There is no age column.** Age is calculated from the date of birth **at query time**, so it never goes stale. Tenure works the same way.
- **Salaries come in three currencies and three pay types.** 79 people are paid yearly, 24 monthly and 3 hourly. See *Salaries* below.
- **One person hasn't started yet.** Oryan Moshe is `pending_arrival` and has almost no data, yet several people already report to them.
- **Nicknames are embedded in names,** for example `Beatrix 'Bea' Vane`. The nickname is extracted into its own column so that "Bea" finds her.
- **The CSV's "Last Name" column contains the nickname** (`'Bea' Vane`). Only the full name is stored: it's what "Reports To" uses, and searching it covers first names, surnames and nicknames in one "contains" match. First and last name aren't stored. An index on them wouldn't help either, because a "contains" search can't use one, and at 107 rows a full scan takes about 20 µs.
- **Not every apostrophe is a nickname.** Only text *between two quotes* counts, so `O'Malley` stays a surname and `Sullivan 'Sully' O'Shea` gets the nickname "Sully".
- **"Reports To" holds a name, not an ID.** It's resolved to a `manager_id` by exact full-name match; all 105 references resolve. If a name ever doesn't match, the raw name is kept and `manager_id` stays empty.

## Salaries

The goal was to keep the original pay data intact and never mix currencies by accident.

- **The original values are kept as they are:** amount, currency (`USD`, `GBP` or `ILS`) and pay type (`Yearly`, `Monthly` or `Hourly`).
- **`annual_salary` is added for comparisons,** still in the person's own currency: yearly ×1, monthly ×12, hourly ×2080 (40 hours × 52 weeks).
- **No converted amounts are stored.** Exchange rates change, so conversion happens **only when asked**: a tool receives `convert_to`, fetches live European Central Bank rates (from the free Frankfurter API, cached for an hour), and returns the original figures **plus** the converted ones, along with the rate date used.
- **Conversion is optional.** It's the only part that needs the internet, so `FX_ENABLED=false` turns it off; the tools then report original currencies and say why. Calling an outside API from an MCP tool is normal practice. MCP servers exist to give a model access to data and services it can't reach itself. Without live rates, "average salary in USD" couldn't be answered honestly, because a fixed table of rates goes stale.
- **Without `convert_to`, salary aggregates are split by currency,** so pounds are never added to shekels. Sorting people by salary across currencies is refused with a hint to pass `convert_to`.

## MCP API

The server uses the official MCP Python SDK (`MCPServer`) with the Streamable HTTP transport at `/mcp`. `create_server(db_path)` builds it, so importing the module does nothing by itself, and tests can point it at their own database. It opens the database **read-only**.

### Tools

| Tool | Inputs | Returns |
|---|---|---|
| `get_dataset_overview` | none | Total people, valid values for each category field, data notes |
| `count_people` | `filter`, `group_by?` | Total, plus per-group counts if grouped |
| `aggregate` | `metric`, `operation`, `filter`, `group_by?`, `convert_to?` | One value per group (and per currency for salary), with counts |
| `search_people` | `filter`, `sort_by`, `descending`, `limit` (1–100), `convert_to?` | Matching people and `total_matches` |
| `get_person` | `name_or_email` | The full record with manager and direct reports, or a list of candidates if ambiguous |

- `metric` is one of `age`, `tenure_years`, `annual_salary`.
- `operation` is one of `avg`, `min`, `max`, `sum`, `median`.
- `group_by` is one of `city`, `country`, `office`, `team`, `gender`, `job_title`, `work_status`, `salary_type`, `salary_currency`, `start_year`.

### The shared filter

All fields are optional and combined with AND. Matching is case-insensitive.

| Field | Match |
|---|---|
| `name` | Part of the name, written with the nickname, without it, or as nickname + surname |
| `city`, `country`, `office`, `team`, `gender` | Exact (the valid values come with the server's instructions) |
| `work_status`, `salary_type`, `salary_currency` | One of a fixed list, e.g. `active` / `pending_arrival` |
| `job_title_contains`, `manager_name` | Part of the text |
| `min_age`, `max_age` | Age range (`min_age` must not exceed `max_age`) |

`%` and `_` in search text are matched literally, not as SQL wildcards.

**Unknown names are errors.** By default pydantic, and so the MCP SDK, *ignores* fields it doesn't know. A misspelled `{"filter": {"citty": "London"}}`, or `job_title` instead of `job_title_contains`, would silently drop the filter and answer about everyone. The filter forbids unknown fields, and the server does the same for every tool's top-level arguments, so a typo comes back as an error Claude can fix.

### Why typed tools, not SQL

A tool such as `run_sql` could answer anything, but it would push query-writing onto the model and make the API hard to validate. Typed tools give:

- **Validation for free.** Pydantic rejects bad input (an unknown `group_by`, a misspelled field, `limit` over 100, …) with a clear message.
- **No SQL injection.** Column names come only from fixed lists in the code, and every value is passed as a bound parameter.
- **Typed outputs.** Every tool declares an output schema, so clients know exactly what comes back.

### How Claude maps vague words to data

There's no alias table in the code. Instead, the MCP server sends its **instructions** when a client connects: an MCP feature for telling clients how to use a server. They list the valid values of every category field (cities, offices, teams, …) and short notes on the data, built from the database at startup. The chat backend adds them to Claude's system prompt.

So when a question says "NYC" or "the UK", Claude maps it straight to `New York` or `United Kingdom` and filters with that. If a place isn't listed, such as "Paris", Claude can say so without calling any tool.

This used to be done by calling `get_dataset_overview` first. That cost a whole extra Claude round trip, about 1 s, because the real tool could only be chosen after the overview came back. The tool still exists for clients that want the data on demand.

The instructions are built when the server starts, together with loading a changed CSV.

### Why the system prompt has no table schema

Claude never sees the database or writes SQL. It only calls the tools, so what it needs is the *tools'* interface, and it gets that from MCP:

- **Each tool's input schema,** generated from the pydantic models, with field descriptions and allowed values.
- **The server's instructions,** with the valid values of each field and notes on the data.

The table schema would describe columns Claude can't query directly, like `name_forms` or `manager_id`. It would add tokens to every call, and could tempt the model to reason about SQL instead of using the tools. If the tools change, their schemas change with them, so the prompt can't go out of date.

## Chat backend

The chat backend (`people_chat`: the loop in `agent.py`, the endpoints in `app.py`) runs one agent loop and offers it through two endpoints. For each question it:

1. Connects to the MCP server and lists its tools.
2. Gives the tools to Claude along with the conversation.
3. Runs the tool-use loop. Each time Claude asks for a tool, the backend calls it on the MCP server and returns the result. The loop is capped at 8 rounds.
4. Reports what happens as **events**: `tool_call` when Claude picks a tool, `tool_result` when the MCP server answers, `text` for each piece of the answer as Claude writes it, then `done` (or `error`).

The two endpoints:

| Endpoint | Returns | Used by |
|---|---|---|
| `POST /api/chat/stream` | The events as they happen, one JSON object per line (`application/x-ndjson`) | The web UI |
| `POST /api/chat` | One JSON response `{answer, tool_calls}` once everything is done | Tests and scripts |

Bad requests (empty question, missing API key) are rejected with a normal HTTP error before streaming starts. A failure after streaming has started can't change the HTTP status any more, so it's sent as an `error` event instead, and the UI shows it the same way.

Other behavior:

- **History:** the UI sends the plain-text history each time, so follow-up questions like *"and in New York?"* work. Tool results from earlier turns aren't resent.
- **Model:** `claude-haiku-5-5` at low effort, configurable with `CLAUDE_MODEL` and `CLAUDE_EFFORT`. See *Speed* below for why. On models that support it (Opus, Sonnet), server-side refusal fallback is enabled.
- **System prompt:** Claude must:
  - answer only from tool results;
  - map places and categories using the valid values from the data server;
  - treat "employees" as active people;
  - report salaries in their original currency unless asked otherwise;
  - ask which person is meant when a name matches several people;
  - decline questions that aren't about the people data.

## Speed

Measuring a few questions showed that **about 99% of the time is spent waiting for Claude**. The MCP server and SQLite take 10–40 ms. Every answer needs at least two Claude calls in a row: one to choose a tool, one to write the answer from the result.

Three changes followed from that:

**1. A faster model.** Median time per question, on the same questions:

| Model | Median | Price per million tokens (in / out) |
|---|---|---|
| Opus 5.5, medium effort (previous default) | 4.0 s | $4 / $20 |
| Opus 5.5, low effort | 3.5 s | $4 / $20 |
| Sonnet 5.5, low effort | 3.2 s | $2 / $10 |
| **Haiku 5.5, low effort (default)** | **2.5 s** | **$0.10 / $0.50** |

These are simple lookups, so a small model is a good fit. Haiku passed the full end-to-end suite twice before becoming the default. Opus "fast mode" wasn't available on this account.

**2. Streaming.** The UI used to show nothing until the whole answer was ready. It now shows each tool lookup as it happens and the answer as it's written. Measured on "Who reports to Bea?":

| | Before (Opus, no streaming) | After (Haiku, streaming) |
|---|---|---|
| First sign of progress | 3.9 s | 1.1 s ("Looking up get_person…") |
| Answer starts appearing | 3.9 s | 1.8 s |
| Answer complete | 3.9 s | 2.4 s |

**3. No extra lookup round.** Each Claude call costs about 1 s, mostly fixed overhead, so the number of calls matters more than their size. Two calls is the minimum for a question that needs data: one to request a tool, and one to answer from its result, because Claude can't run tools itself. A third call happened whenever Claude first had to look up the valid values (`get_dataset_overview`) before it could choose the real tool. Sending those values with the MCP server's instructions removed that round:

| | Calls per question | Median |
|---|---|---|
| Before | 2.3 | 2.73 s |
| After | 1.8 (a question about a place that isn't in the data, like "Paris", now needs only 1) | 2.29 s |

Turning off Claude's thinking was also measured. Each call got about 0.25 s faster, but Claude made more extra lookups, so it was slower overall, and it was not adopted.

## Caching

Measurements showed where the time and money go, which decided what to cache:

| Part | Measured | Cached? |
|---|---|---|
| Tool definitions + system prompt, resent on every Claude call | ≈4,200 tokens per call, 2–4 calls per question | **Yes**, with Claude prompt caching |
| MCP connection + tool listing | ≈8 ms per question | No: nothing to gain |
| A SQLite query (107 rows) | ≈20 ms | No: a cache would add stale-data risk for no gain |
| Exchange rates | One web call | Yes, in memory for an hour (`people_server/fx.py`) |
| Whole answers | — | No: answers depend on the conversation so far |

**Prompt caching** uses two cache markers on every Claude call:

1. **On the system prompt.** Tools and system prompt are identical on every call, so they become one cache entry that every question shares.
2. **At the end of the conversation** (`cache_control` on the request). The growing conversation is cached, so each round of the tool loop reuses the previous round.

A cached read costs 5% of the normal input price. Entries last 5 minutes after their last use. The first version had only the second marker, so every new question re-wrote the 4,200-token prefix; adding the first marker raised the input-cost saving from 45% to **93%** on the end-to-end test suite.

The cache only works if the prefix is byte-identical between calls. So the system prompt contains nothing that changes, such as dates, and the tools come back in a fixed order. `test_cached_prefix_is_identical_across_rounds` guards this.

The backend logs each call's cache usage (`cache_read` / `cache_write`). The opt-in end-to-end tests check against the real API that the cache is hit both within a question and across questions.

## Edge cases

| Situation | Behavior |
|---|---|
| "Employees" vs "people" | "Employees" means `active`. "People" or "everyone" includes the pending hire |
| The pending hire has no age or salary | Rows with no value are skipped in averages and other aggregates, so they don't distort them |
| A name matches several people ("Alistair") | `get_person` returns the candidates, and Claude asks which one |
| A nickname is used ("Bea") | Counts as an exact match, so it resolves straight to Beatrix 'Bea' Vane |
| A name written without its nickname ("Beatrix Vane") or as nickname + surname ("Bea Vane") | Also matches. Names are compared in all three forms, in `get_person`, the `name` filter and the manager filter. Claude naturally writes names this way, so before this fix such lookups failed. |
| A place not in the data ("Paris") | Claude sees from the server's instructions that it isn't a location, and says so |
| A misspelled argument or filter field (`filtr`, `citty`) | An error naming the field, never a silent "everyone" |
| `%` or `_` in a name | Matched literally |
| An empty name in `get_person` | "Give a name or email to look up." |
| Part of an email ("alistair.finch") | Matches |
| A "Reports To" name that matches no person | Still found by the manager filter through the raw name |
| Apostrophes in surnames ("D'Arcy O'Hara") | Not mistaken for a nickname: only a quoted word on its own counts |
| Salaries across currencies | Split by currency, unless `convert_to` is given |
| A large result | `limit` is capped at 100, and `total_matches` always gives the full count |
| A bad date or amount in the CSV | Stored as empty instead of stopping the import |
| An empty CSV, or a failure while loading | An error; the previous database stays in place (the new one is built in a temporary file and swapped in) |
| "Salary in USD" vs "people paid in USD" | The first converts everyone's salary; the second filters to people paid in dollars |
| An unrelated question | Claude politely declines |

## Error handling

| Failure | What the user sees |
|---|---|
| Invalid tool arguments | Prevented where possible: tools with simple schemas (`get_person`, `get_dataset_overview`) use the API's **strict mode**, which guarantees the arguments match the schema. `get_person` was once called with no name at all. The tools that share the 13-field filter are too complex for strict mode, so for those the MCP server rejects bad arguments, the error goes back to Claude, and Claude corrects the call. |
| A tool can't answer (no such person, salary sort without conversion) | A clear `ToolError` message that Claude uses to recover |
| Currency API is down | Results in original currencies, with a note that conversion is unavailable. Sorting by salary across currencies says conversion is unavailable, rather than asking for `convert_to` again |
| MCP server is down | HTTP 503: "The people data service is unavailable" |
| A bug in the chat backend | HTTP 500: "Something went wrong on the chat backend", logged with the full error, rather than blaming the data service |
| A setting has a wrong value (e.g. `CLAUDE_EFFORT=fast`) | The service refuses to start and names the setting |
| API key missing or invalid | HTTP 500 with a message naming `ANTHROPIC_API_KEY` |
| Claude rate-limited | The SDK retries automatically, then HTTP 429 with a short message |
| Claude unavailable | HTTP 502 with a short message |
| Claude declines the request | "Sorry, I can't help with that request." |
| Claude finishes without writing anything | A fallback message. Every answer has text, because an empty assistant message in the history would make Claude reject every later request |
| The answer hits the length limit | The partial answer plus a note that it was cut off |
| 8 tool rounds without an answer | "I couldn't work this out within the step limit. Try rephrasing." |
| Empty question, or an empty earlier message | The Send button is disabled, and the backend also rejects it with 400 |
| The connection drops mid-answer | The UI says the answer was interrupted, rather than showing the partial text as finished, and leaves it out of the history |

In the UI, errors appear inline as a red message bubble, and the conversation stays usable.

## Engineering practices

- **One uv workspace.** A root `pyproject.toml` and a single `uv.lock` hold both Python packages (`people_server`, `people_chat`) and the shared dev tools, so there's one environment, one install (`uv sync`) and one place for ruff, mypy and pytest settings. Each package still declares its own runtime dependencies, so each Docker image installs only what its service needs.
- **No work at import time.** The server is built by `create_server(db_path)` and the API by `create_app(settings, …)`. Entry points (`__main__.py`) load settings, set up logging and start things. This keeps tests independent of the real data and makes the dependencies explicit.
- **Settings validated at startup** with pydantic-settings.
- **Logging** through per-module loggers, configured once at each entry point.
- **Tests in one tree:** `tests/unit`, `tests/integration` (including a whole-system test across processes) and `tests/e2e` (real Claude; can target the Docker stack). Shared helpers live in `tests/helpers.py`, not in `conftest.py`.
- **CI** on every push: lint, types, all Python and UI tests, and the Docker build.
