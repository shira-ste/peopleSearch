"""Functional tests: call every MCP tool through a real MCP client (in-process), as the chat backend does.

Expected numbers come from the CSV via the oracle helpers in conftest.py, not from the server's own code.
"""

import statistics
from collections import Counter
from typing import Any

import pytest
from mcp import Client

import fx
import server
from tests.conftest import age_of, annual_salary, currency_of

pytestmark = pytest.mark.anyio
Rows = list[dict[str, str]]
FAKE_RATES: fx.FxRates = {"date": "2026-01-01", "rates": {"USD": 1.0, "GBP": 0.5, "ILS": 4.0}}


async def call(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool and return its structured result; fail the test if the tool reported an error."""
    async with Client(server.mcp) as client:
        result = await client.call_tool(name, args or {})
    assert not result.is_error, result.content
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


async def call_error(name: str, args: dict[str, Any]) -> str:
    """Call a tool that is expected to fail and return its error message."""
    async with Client(server.mcp) as client:
        result = await client.call_tool(name, args)
    assert result.is_error
    return " ".join(getattr(block, "text", "") for block in result.content)


@pytest.fixture
def fake_fx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fx, "get_rates", lambda base: FAKE_RATES)


# ---------- The MCP API surface ----------


async def test_server_exposes_five_tools_with_output_schemas() -> None:
    async with Client(server.mcp) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {
        "get_dataset_overview", "count_people", "aggregate", "search_people", "get_person",
    }  # fmt: skip
    assert all(t.output_schema for t in tools)


async def test_overview_lists_real_category_values(rows: Rows) -> None:
    overview = await call("get_dataset_overview")
    assert overview["total_people"] == len(rows)
    assert overview["values"]["city"] == sorted({r["City"] for r in rows} - {""})
    assert set(overview["values"]["salary_type"]) == {"Yearly", "Monthly", "Hourly"}


# ---------- Counting ----------


@pytest.mark.parametrize("city", ["London", "New York", "Tel Aviv"])
async def test_count_by_city_matches_csv(rows: Rows, city: str) -> None:
    result = await call("count_people", {"filter": {"city": city}})
    assert result["total"] == sum(r["City"] == city for r in rows)


async def test_count_filters_are_case_insensitive() -> None:
    upper = await call("count_people", {"filter": {"city": "LONDON"}})
    proper = await call("count_people", {"filter": {"city": "London"}})
    assert upper["total"] == proper["total"] == 55


async def test_count_hourly_employees_in_london(rows: Rows) -> None:
    result = await call("count_people", {"filter": {"city": "London", "salary_type": "Hourly"}})
    assert result["total"] == sum(r["City"] == "London" and r["Salary Type"] == "Hourly" for r in rows) == 3


async def test_count_grouped_by_team_matches_csv(rows: Rows) -> None:
    result = await call("count_people", {"group_by": "team"})
    assert {g["value"]: g["count"] for g in result["groups"]} == Counter(r["Team"] or None for r in rows)


async def test_unknown_place_counts_zero() -> None:
    assert (await call("count_people", {"filter": {"city": "Paris"}}))["total"] == 0


async def test_filter_values_are_never_executed_as_sql() -> None:
    result = await call("count_people", {"filter": {"name": "'; DROP TABLE people;--"}})
    assert result["total"] == 0
    assert (await call("count_people"))["total"] == 107  # table still intact


async def test_age_range_filter(rows: Rows) -> None:
    result = await call("count_people", {"filter": {"min_age": 30, "max_age": 39}})
    assert result["total"] == sum(1 for r in rows if (a := age_of(r)) is not None and 30 <= a <= 39)


# ---------- Aggregates ----------


async def test_average_age_of_active_employees(rows: Rows) -> None:
    result = await call("aggregate", {"metric": "age", "operation": "avg", "filter": {"work_status": "active"}})
    ages = [a for r in rows if r["Work Status"] == "active" and (a := age_of(r)) is not None]
    assert result["results"] == [{"value": round(statistics.mean(ages), 2), "count": len(ages), "group": None,
                                  "currency": None}]  # fmt: skip


async def test_salary_is_never_mixed_across_currencies(rows: Rows) -> None:
    result = await call("aggregate", {"metric": "annual_salary", "operation": "avg", "group_by": "office"})
    for item in result["results"]:
        office_rows = [r for r in rows if r["Office"] == item["group"]]
        assert {currency_of(r) for r in office_rows} == {item["currency"]}  # one currency per office bucket
        expected = statistics.mean(s for r in office_rows if (s := annual_salary(r)) is not None)
        assert item["value"] == round(expected, 2)
    assert result["converted_to"] is None  # no conversion unless asked


async def test_salary_conversion_on_request(rows: Rows, fake_fx: None) -> None:
    result = await call("aggregate", {"metric": "annual_salary", "operation": "avg", "convert_to": "USD"})
    in_usd = [s / FAKE_RATES["rates"][currency_of(r)] for r in rows if (s := annual_salary(r)) is not None]
    assert result["results"][0]["value"] == pytest.approx(statistics.mean(in_usd), abs=0.01)
    assert result["results"][0]["currency"] == "USD"
    assert result["fx_rate_date"] == "2026-01-01"


async def test_fx_outage_falls_back_to_original_currencies(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(base: str) -> fx.FxRates:
        raise ConnectionError("rates API down")

    monkeypatch.setattr(fx, "get_rates", down)
    result = await call("aggregate", {"metric": "annual_salary", "operation": "avg", "convert_to": "USD"})
    assert "unavailable" in result["note"]
    assert {item["currency"] for item in result["results"]} == {"USD", "GBP", "ILS"}  # split, not mixed


async def test_people_without_a_value_are_skipped() -> None:
    result = await call("aggregate", {"metric": "age", "operation": "avg"})
    assert result["results"][0]["count"] == 106  # the pending hire has no date of birth


# ---------- Search ----------


async def test_search_respects_limit_but_reports_total() -> None:
    result = await call("search_people", {"filter": {"city": "London"}, "limit": 5})
    assert len(result["people"]) == 5
    assert result["total_matches"] == 55


async def test_search_rejects_limit_over_100() -> None:
    assert "less than or equal to 100" in await call_error("search_people", {"limit": 500})


async def test_sorting_salary_across_currencies_needs_conversion() -> None:
    assert "convert_to" in await call_error("search_people", {"sort_by": "annual_salary"})


async def test_sorting_salary_within_one_currency_works() -> None:
    result = await call("search_people", {"filter": {"city": "London"}, "sort_by": "annual_salary", "descending": True})
    salaries = [p["salary"]["annual"] for p in result["people"]]
    assert salaries == sorted(salaries, reverse=True)


async def test_search_shows_original_and_converted_salary(fake_fx: None) -> None:
    result = await call("search_people", {"filter": {"name": "Bea"}, "convert_to": "USD"})
    bea = next(p for p in result["people"] if p["name"] == "Beatrix 'Bea' Vane")
    assert bea["salary"]["currency"] == "GBP"
    assert bea["salary"]["annual_converted"] == round(bea["salary"]["annual"] / 0.5, 2)


# ---------- Person lookup ----------


async def test_nickname_resolves_to_one_person_with_reports(rows: Rows) -> None:
    result = await call("get_person", {"name_or_email": "Bea"})
    person = result["person"]
    assert person["name"] == "Beatrix 'Bea' Vane"
    expected = sorted(r["Full Name"] for r in rows if r["Reports To"] == "Beatrix 'Bea' Vane")
    assert [r["name"] for r in person["direct_reports"]] == expected


async def test_ambiguous_name_returns_candidates() -> None:
    result = await call("get_person", {"name_or_email": "Alistair"})
    assert result["person"] is None
    assert {c["name"] for c in result["candidates"]} == {"Alistair Finch", "Alistair Pendergast"}


async def test_lookup_by_email() -> None:
    result = await call("get_person", {"name_or_email": "alistair.finch@crumbandculture.com"})
    assert result["person"]["name"] == "Alistair Finch"


async def test_unknown_person_is_a_clear_error() -> None:
    assert "No person matches" in await call_error("get_person", {"name_or_email": "Nobody Atall"})
