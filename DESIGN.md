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
- **"Reports To" holds a name, not an ID.** It's resolved to a `manager_id` by exact full-name match; all 105 references resolve. If a name ever doesn't match, the raw name is kept and `manager_id` stays empty.

## Salaries

The goal was to keep the original pay data intact and never mix currencies by accident.

- **The original values are kept as they are:** amount, currency (`USD`, `GBP` or `ILS`) and pay type (`Yearly`, `Monthly` or `Hourly`).
- **`annual_salary` is added for comparisons,** still in the person's own currency: yearly ×1, monthly ×12, hourly ×2080 (40 hours × 52 weeks).
- **No converted amounts are stored.** Exchange rates change, so conversion happens **only when asked**: a tool receives `convert_to`, fetches live European Central Bank rates (from the free Frankfurter API, cached for an hour), and returns the original figures **plus** the converted ones, along with the rate date used.
- **Without `convert_to`, salary aggregates are split by currency,** so pounds are never added to shekels. Sorting people by salary across currencies is refused with a hint to pass `convert_to`.

## MCP API

The server uses the official MCP Python SDK (`MCPServer`) with the Streamable HTTP transport at `/mcp`. It opens the database **read-only**.

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
| `name` | Part of the full name, which includes the nickname |
| `city`, `country`, `office`, `team`, `gender`, `work_status`, `salary_type`, `salary_currency` | Exact |
| `job_title_contains`, `manager_name` | Part of the text |
| `min_age`, `max_age` | Age range |

### Why typed tools, not SQL

A tool such as `run_sql` could answer anything, but it would push query-writing onto the model and make the API hard to validate. Typed tools give:

- **Validation for free.** Pydantic rejects bad input (an unknown `group_by`, `limit` over 100, …) with a clear message.
- **No SQL injection.** Column names come only from fixed lists in the code, and every value is passed as a bound parameter.
- **Typed outputs.** Every tool declares an output schema, so clients know exactly what comes back.

### How Claude maps vague words to data

There's no alias table in the code. When a question says "NYC" or "the UK", Claude calls `get_dataset_overview`, sees the real values (`New York`, `United Kingdom`), and filters with those. If a place isn't in the data, such as "Paris", Claude can say so instead of reporting "0 people".

## Chat backend

`chat/main.py` is a single FastAPI endpoint, `POST /api/chat`. For each request it:

1. Connects to the MCP server and lists its tools.
2. Gives the tools to Claude along with the conversation.
3. Runs the tool-use loop. Each time Claude asks for a tool, the backend calls it on the MCP server and returns the result. The loop is capped at 8 rounds.
4. Returns the answer **and** the list of tool calls, so the UI can show them.

Other behavior:

- **History:** the UI sends the plain-text history each time, so follow-up questions like *"and in New York?"* work. Tool results from earlier turns aren't resent.
- **Model:** `claude-opus-5-5`, configurable with `CLAUDE_MODEL`, with server-side refusal fallback enabled. The backend runs the tool loop itself so it can record every MCP call for the UI.
- **System prompt:** Claude must:
  - answer only from tool results;
  - check the overview when a name is unclear;
  - treat "employees" as active people;
  - report salaries in their original currency unless asked otherwise;
  - ask which person is meant when a name matches several people;
  - decline questions that aren't about the people data.

## Edge cases

| Situation | Behavior |
|---|---|
| "Employees" vs "people" | "Employees" means `active`. "People" or "everyone" includes the pending hire |
| The pending hire has no age or salary | Rows with no value are skipped in averages and other aggregates, so they don't distort them |
| A name matches several people ("Alistair") | `get_person` returns the candidates, and Claude asks which one |
| A nickname is used ("Bea") | Counts as an exact match, so it resolves straight to Beatrix 'Bea' Vane |
| A place not in the data ("Paris") | Count is 0, and the overview shows Claude it isn't one of the locations |
| Salaries across currencies | Split by currency, unless `convert_to` is given |
| A large result | `limit` is capped at 100, and `total_matches` always gives the full count |
| A bad date or amount in the CSV | Stored as empty instead of stopping the import |
| An unrelated question | Claude politely declines |

## Error handling

| Failure | What the user sees |
|---|---|
| Invalid tool arguments | The error goes back to Claude, which corrects the call and continues |
| A tool can't answer (no such person, salary sort without conversion) | A clear `ToolError` message that Claude uses to recover |
| Currency API is down | Results in original currencies, with a note that conversion is unavailable |
| MCP server is down | HTTP 503: "The people data service is unavailable" |
| API key missing or invalid | HTTP 500 with a message naming `ANTHROPIC_API_KEY` |
| Claude rate-limited | The SDK retries automatically, then HTTP 429 with a short message |
| Claude unavailable | HTTP 502 with a short message |
| Claude declines the request | "Sorry, I can't help with that request." |
| 8 tool rounds without an answer | "I couldn't work this out within the step limit. Try rephrasing." |
| Empty question | The Send button is disabled, and the backend also rejects it with 400 |

In the UI, errors appear inline as a red message bubble, and the conversation stays usable.

## Not done yet

- Streaming answers token by token. Answers currently arrive all at once, with a "Thinking…" indicator while waiting.
- Docker / one-command startup.
