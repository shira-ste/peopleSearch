"""Integration tests: call every MCP tool through a real MCP client (in-process), as the chat backend does.

Expected numbers come from the CSV via the oracle in tests/helpers.py, not from the server's own code.
"""

import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from people_server import fx
from people_server.ingest import ingest
from people_server.server import create_server
from tests.helpers import CsvRow, age_of, annual_salary, currency_of, write_csv

pytestmark = pytest.mark.anyio
Rows = list[CsvRow]


async def call(server: MCPServer, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool and return its structured result; fail the test if the tool reported an error."""
    async with Client(server) as client:
        result = await client.call_tool(name, args or {})
    assert not result.is_error, result.content
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


async def call_error(server: MCPServer, name: str, args: dict[str, Any]) -> str:
    """Call a tool that is expected to fail and return its error message."""
    async with Client(server) as client:
        result = await client.call_tool(name, args)
    assert result.is_error
    return " ".join(getattr(block, "text", "") for block in result.content)


@pytest.fixture
def rates_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(base: str) -> fx.FxRates:
        raise httpx.ConnectError("rates API down")

    monkeypatch.setattr(fx, "get_rates", down)


# ---------- The MCP API surface ----------


async def test_server_exposes_five_tools_with_output_schemas(mcp_server: MCPServer) -> None:
    async with Client(mcp_server) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {
        "get_dataset_overview", "count_people", "aggregate", "search_people", "get_person",
    }  # fmt: skip
    assert all(t.output_schema for t in tools)
    assert all(t.input_schema.get("additionalProperties") is False for t in tools)


async def test_instructions_give_clients_the_valid_values(mcp_server: MCPServer, rows: Rows) -> None:
    """Clients get the valid values on connect, so the model needn't spend a round on get_dataset_overview."""
    async with Client(mcp_server) as client:
        instructions = client.instructions or ""
    for column in ("City", "Office", "Team", "Country"):
        for value in {r[column] for r in rows} - {""}:
            assert value in instructions


async def test_overview_lists_real_category_values(mcp_server: MCPServer, rows: Rows) -> None:
    overview = await call(mcp_server, "get_dataset_overview")
    assert overview["total_people"] == len(rows)
    assert overview["values"]["city"] == sorted({r["City"] for r in rows} - {""})
    assert set(overview["values"]["salary_type"]) == {"Yearly", "Monthly", "Hourly"}


# ---------- Input validation ----------


@pytest.mark.parametrize(
    ("args", "complaint"),
    [
        ({"filtr": {"city": "London"}}, "filtr"),  # misspelled argument
        ({"filter": {"citty": "London"}}, "citty"),  # misspelled filter field
        ({"filter": {"job_title": "Baker"}}, "job_title"),  # the real field is job_title_contains
        ({"filter": {"work_status": "retired"}}, "work_status"),  # not one of the allowed values
        ({"filter": {"min_age": 50, "max_age": 30}}, "min_age must not be greater than max_age"),
    ],
)
async def test_invalid_input_is_an_error_not_a_silent_match_all(
    mcp_server: MCPServer, args: dict[str, Any], complaint: str
) -> None:
    assert complaint in await call_error(mcp_server, "count_people", args)


async def test_search_rejects_limit_over_100(mcp_server: MCPServer) -> None:
    assert "less than or equal to 100" in await call_error(mcp_server, "search_people", {"limit": 500})


@pytest.mark.parametrize("text", ["%", "_", "a_b", "\\"])
async def test_wildcard_characters_are_matched_literally(mcp_server: MCPServer, text: str) -> None:
    assert (await call(mcp_server, "count_people", {"filter": {"name": text}}))["total"] == 0


async def test_filter_values_are_never_executed_as_sql(mcp_server: MCPServer) -> None:
    result = await call(mcp_server, "count_people", {"filter": {"name": "'; DROP TABLE people;--"}})
    assert result["total"] == 0
    assert (await call(mcp_server, "count_people"))["total"] == 107  # table still intact


# ---------- Counting ----------


@pytest.mark.parametrize("city", ["London", "New York", "Tel Aviv"])
async def test_count_by_city_matches_csv(mcp_server: MCPServer, rows: Rows, city: str) -> None:
    result = await call(mcp_server, "count_people", {"filter": {"city": city}})
    assert result["total"] == sum(r["City"] == city for r in rows)


async def test_count_filters_are_case_insensitive(mcp_server: MCPServer) -> None:
    upper = await call(mcp_server, "count_people", {"filter": {"city": "LONDON"}})
    proper = await call(mcp_server, "count_people", {"filter": {"city": "London"}})
    assert upper["total"] == proper["total"] == 55


async def test_count_hourly_employees_in_london(mcp_server: MCPServer, rows: Rows) -> None:
    result = await call(mcp_server, "count_people", {"filter": {"city": "London", "salary_type": "Hourly"}})
    assert result["total"] == sum(r["City"] == "London" and r["Salary Type"] == "Hourly" for r in rows)


async def test_count_grouped_by_team_matches_csv(mcp_server: MCPServer, rows: Rows) -> None:
    result = await call(mcp_server, "count_people", {"group_by": "team"})
    assert {g["value"]: g["count"] for g in result["groups"]} == Counter(r["Team"] or None for r in rows)


async def test_unknown_place_counts_zero(mcp_server: MCPServer) -> None:
    assert (await call(mcp_server, "count_people", {"filter": {"city": "Paris"}}))["total"] == 0


async def test_age_range_filter(mcp_server: MCPServer, rows: Rows) -> None:
    result = await call(mcp_server, "count_people", {"filter": {"min_age": 30, "max_age": 39}})
    assert result["total"] == sum(1 for r in rows if (a := age_of(r)) is not None and 30 <= a <= 39)


# ---------- Aggregates ----------


async def test_average_age_of_active_employees(mcp_server: MCPServer, rows: Rows) -> None:
    result = await call(
        mcp_server, "aggregate", {"metric": "age", "operation": "avg", "filter": {"work_status": "active"}}
    )
    ages = [a for r in rows if r["Work Status"] == "active" and (a := age_of(r)) is not None]
    [only] = result["results"]
    assert (only["value"], only["count"]) == (round(statistics.mean(ages), 2), len(ages))


async def test_salary_is_never_mixed_across_currencies(mcp_server: MCPServer, rows: Rows) -> None:
    result = await call(mcp_server, "aggregate", {"metric": "annual_salary", "operation": "avg", "group_by": "office"})
    for item in result["results"]:
        office_rows = [r for r in rows if r["Office"] == item["group"]]
        assert {currency_of(r) for r in office_rows} == {item["currency"]}  # one currency per office bucket
        expected = statistics.mean(s for r in office_rows if (s := annual_salary(r)) is not None)
        assert item["value"] == round(expected, 2)
    assert result["converted_to"] is None  # no conversion unless asked


async def test_salary_conversion_on_request(mcp_server: MCPServer, rows: Rows, fake_fx: fx.FxRates) -> None:
    result = await call(mcp_server, "aggregate", {"metric": "annual_salary", "operation": "avg", "convert_to": "USD"})
    in_usd = [s / fake_fx["rates"][currency_of(r)] for r in rows if (s := annual_salary(r)) is not None]
    assert result["results"][0]["value"] == pytest.approx(statistics.mean(in_usd), abs=0.01)
    assert result["results"][0]["currency"] == "USD"
    assert result["fx_rate_date"] == fake_fx["date"]


@pytest.mark.usefixtures("rates_down")
async def test_rates_outage_falls_back_to_original_currencies(mcp_server: MCPServer) -> None:
    result = await call(mcp_server, "aggregate", {"metric": "annual_salary", "operation": "avg", "convert_to": "USD"})
    assert "unavailable" in result["note"]
    assert {item["currency"] for item in result["results"]} == {"USD", "GBP", "ILS"}  # split, not mixed


@pytest.mark.usefixtures("rates_down")
async def test_rates_outage_explains_why_salary_sorting_fails(mcp_server: MCPServer) -> None:
    error = await call_error(mcp_server, "search_people", {"sort_by": "annual_salary", "convert_to": "USD"})
    assert "unavailable" in error
    assert "pass convert_to" not in error  # it was passed: don't tell the caller to pass it


async def test_conversion_can_be_turned_off(db_path: Path) -> None:
    server = create_server(db_path, fx_enabled=False)
    result = await call(server, "aggregate", {"metric": "annual_salary", "operation": "avg", "convert_to": "USD"})
    assert "turned off" in result["note"]
    async with Client(server) as client:
        assert "turned off" in (client.instructions or "")


async def test_people_without_a_value_are_skipped(mcp_server: MCPServer) -> None:
    result = await call(mcp_server, "aggregate", {"metric": "age", "operation": "avg"})
    assert result["results"][0]["count"] == 106  # the pending hire has no date of birth


async def test_salaries_in_currencies_without_a_rate_are_left_out(
    tmp_path: Path, rows: Rows, fake_fx: fx.FxRates
) -> None:
    custom = [{**rows[0], "Salary Currency": "EUR €"}, *rows[1:5]]
    db = tmp_path / "people.db"
    ingest(write_csv(tmp_path / "people.csv", custom), db)
    result = await call(
        create_server(db), "aggregate", {"metric": "annual_salary", "operation": "avg", "convert_to": "USD"}
    )
    assert result["results"][0]["count"] == 4
    assert "could not be converted" in result["note"]


# ---------- Search ----------


async def test_search_respects_limit_but_reports_total(mcp_server: MCPServer) -> None:
    result = await call(mcp_server, "search_people", {"filter": {"city": "London"}, "limit": 5})
    assert len(result["people"]) == 5
    assert result["total_matches"] == 55


async def test_sorting_salary_across_currencies_needs_conversion(mcp_server: MCPServer) -> None:
    assert "convert_to" in await call_error(mcp_server, "search_people", {"sort_by": "annual_salary"})


async def test_sorting_salary_within_one_currency_works(mcp_server: MCPServer) -> None:
    result = await call(
        mcp_server, "search_people", {"filter": {"city": "London"}, "sort_by": "annual_salary", "descending": True}
    )
    salaries = [p["salary"]["annual"] for p in result["people"]]
    assert salaries == sorted(salaries, reverse=True)


async def test_search_shows_original_and_converted_salary(mcp_server: MCPServer, fake_fx: fx.FxRates) -> None:
    result = await call(mcp_server, "search_people", {"filter": {"name": "Bea"}, "convert_to": "USD"})
    bea = next(p for p in result["people"] if p["name"] == "Beatrix 'Bea' Vane")
    assert bea["salary"]["currency"] == "GBP"
    assert bea["salary"]["annual_converted"] == round(bea["salary"]["annual"] / fake_fx["rates"]["GBP"], 2)


# ---------- Person lookup ----------


@pytest.mark.parametrize("written_as", ["Beatrix 'Bea' Vane", "Beatrix Vane", "Bea Vane", "Bea", "beatrix vane"])
async def test_names_match_with_or_without_the_nickname(mcp_server: MCPServer, written_as: str) -> None:
    result = await call(mcp_server, "get_person", {"name_or_email": written_as})
    assert result["person"]["name"] == "Beatrix 'Bea' Vane"


async def test_apostrophe_surnames_with_a_nickname(mcp_server: MCPServer) -> None:
    for written_as in ("Sullivan O'Shea", "Sully O'Shea"):
        result = await call(mcp_server, "get_person", {"name_or_email": written_as})
        assert result["person"]["name"] == "Sullivan 'Sully' O'Shea"


async def test_nickname_resolves_to_one_person_with_reports(mcp_server: MCPServer, rows: Rows) -> None:
    person = (await call(mcp_server, "get_person", {"name_or_email": "Bea"}))["person"]
    expected = sorted(r["Full Name"] for r in rows if r["Reports To"] == "Beatrix 'Bea' Vane")
    assert [r["name"] for r in person["direct_reports"]] == expected


async def test_manager_filter_accepts_any_name_form(mcp_server: MCPServer, rows: Rows) -> None:
    expected = sum(r["Reports To"] == "Beatrix 'Bea' Vane" for r in rows)
    for written_as in ("Bea", "Beatrix Vane", "Bea Vane"):
        result = await call(mcp_server, "count_people", {"filter": {"manager_name": written_as}})
        assert result["total"] == expected


async def test_manager_filter_still_finds_reports_of_an_unlinked_manager(tmp_path: Path, rows: Rows) -> None:
    """If a "Reports To" name matches no person, the raw name still finds their reports."""
    custom = [{**rows[0], "Reports To": "Someone Not In The Export"}, *rows[1:5]]
    db = tmp_path / "people.db"
    ingest(write_csv(tmp_path / "people.csv", custom), db)
    result = await call(create_server(db), "count_people", {"filter": {"manager_name": "Someone Not"}})
    assert result["total"] == 1


async def test_ambiguous_name_returns_candidates(mcp_server: MCPServer) -> None:
    result = await call(mcp_server, "get_person", {"name_or_email": "Alistair"})
    assert result["person"] is None
    assert {c["name"] for c in result["candidates"]} == {"Alistair Finch", "Alistair Pendergast"}


@pytest.mark.parametrize("written_as", ["alistair.finch@crumbandculture.com", "alistair.finch"])
async def test_lookup_by_full_or_partial_email(mcp_server: MCPServer, written_as: str) -> None:
    result = await call(mcp_server, "get_person", {"name_or_email": written_as})
    assert result["person"]["name"] == "Alistair Finch"


@pytest.mark.parametrize(("written_as", "message"), [("Nobody Atall", "No person matches"), ("  ", "Give a name")])
async def test_failed_lookups_are_clear_errors(mcp_server: MCPServer, written_as: str, message: str) -> None:
    assert message in await call_error(mcp_server, "get_person", {"name_or_email": written_as})
