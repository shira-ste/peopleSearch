"""MCP server exposing typed query tools over the people database (Streamable HTTP on /mcp).

`create_server(db_path)` builds the server; importing this module does no I/O.
"""

import logging
import sqlite3
import statistics
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from pydantic import ConfigDict, Field

from people_server import fx
from people_server.fx import Currency
from people_server.models import (
    AggregateResult,
    AggregateRow,
    Candidate,
    CountResult,
    DirectReport,
    FxInfo,
    GroupBy,
    GroupCount,
    Metric,
    Operation,
    Overview,
    PeopleFilter,
    Person,
    PersonDetail,
    PersonLookup,
    Salary,
    SearchResult,
    SortBy,
)

logger = logging.getLogger(__name__)
Row = dict[str, Any]

AGE_SQL = (
    "(CAST(strftime('%Y','now') AS INT) - CAST(strftime('%Y', date_of_birth) AS INT)"
    " - (strftime('%m-%d','now') < strftime('%m-%d', date_of_birth)))"
)
TENURE_SQL = "((julianday('now') - julianday(start_date)) / 365.25)"
METRIC_SQL: dict[Metric, str] = {"age": AGE_SQL, "tenure_years": TENURE_SQL, "annual_salary": "annual_salary"}
GROUP_SQL: dict[GroupBy, str] = {
    "city": "city", "country": "country", "office": "office", "team": "team", "gender": "gender",
    "job_title": "job_title", "work_status": "work_status", "salary_type": "salary_type",
    "salary_currency": "salary_currency", "start_year": "substr(start_date, 1, 4)",
}  # fmt: skip
OPERATIONS: dict[Operation, Callable[[list[float]], float]] = {
    "avg": statistics.mean, "min": min, "max": max, "sum": sum, "median": statistics.median,
}  # fmt: skip
CATEGORY_FIELDS = ["city", "country", "office", "team", "gender", "work_status", "salary_type", "salary_currency"]
CONTAINS = "LIKE ? ESCAPE '\\'"  # case-insensitive "contains" match; see contains()


def contains(text: str) -> str:
    """LIKE pattern for "contains text". `%` and `_` in the text are matched literally, not as wildcards."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def data_notes(fx_enabled: bool) -> list[str]:
    conversion = (
        "Pass convert_to to compare salaries across currencies; conversion uses live ECB rates."
        if fx_enabled
        else "Currency conversion is turned off on this server; compare salaries within one currency."
    )
    return [
        "Salaries are stored in their original currency (USD, GBP or ILS) and pay type (Yearly, Monthly, Hourly).",
        "annual_salary = Yearly x1, Monthly x12, Hourly x2080 (40h x 52 weeks), still in the original currency.",
        conversion,
        "One person is 'pending_arrival' (not started yet) and has almost no data.",
        "Age and tenure are calculated from today's date.",
    ]


class PeopleDB:
    """Read-only access to the people database."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params)]
        finally:
            con.close()

    def category_values(self) -> dict[str, list[str]]:
        """The distinct values of each category field, e.g. {"city": ["London", "New York", "Tel Aviv"], ...}."""
        return {
            c: [r[c] for r in self.query(f"SELECT DISTINCT {c} FROM people WHERE {c} IS NOT NULL ORDER BY 1")]
            for c in CATEGORY_FIELDS
        }


def server_instructions(db: PeopleDB, fx_enabled: bool) -> str:
    """Sent to clients when they connect. Listing the valid values up front means the model can map words
    like "NYC" to "New York" straight away, instead of spending an extra round on get_dataset_overview."""
    values = "\n".join(f"- {field}: {', '.join(vals)}" for field, vals in db.category_values().items())
    notes = "\n".join(f"- {note}" for note in data_notes(fx_enabled))
    return (
        "Query a company's people records: a bakery chain with offices in London, New York and Tel Aviv.\n"
        f"Valid values for filters (matching is case-insensitive):\n{values}\n"
        f"Notes on the data:\n{notes}"
    )


def where(f: PeopleFilter | None) -> tuple[str, list[Any]]:
    """Build a parameterized WHERE clause. Column names come from code; values are always bound parameters."""
    f = f or PeopleFilter()
    clauses: list[str] = ["1=1"]
    params: list[Any] = []
    exact = {
        "city": f.city, "country": f.country, "office": f.office, "team": f.team, "gender": f.gender,
        "work_status": f.work_status, "salary_type": f.salary_type, "salary_currency": f.salary_currency,
    }  # fmt: skip
    for col, value in exact.items():
        if value is not None:
            clauses.append(f"{col} = ? COLLATE NOCASE")
            params.append(value)
    if f.name:  # name_forms holds the name with, without and as nickname + surname
        clauses.append(f"name_forms {CONTAINS}")
        params.append(contains(f.name))
    if f.job_title_contains:
        clauses.append(f"job_title {CONTAINS}")
        params.append(contains(f.job_title_contains))
    if f.manager_name:
        # Match the manager's own record (so any form of their name works), and fall back to the raw
        # "Reports To" text for anyone whose manager couldn't be linked to a person when loading.
        clauses.append(
            f"(manager_id IN (SELECT id FROM people WHERE name_forms {CONTAINS}) OR manager_name {CONTAINS})"
        )
        params += [contains(f.manager_name)] * 2
    if f.min_age is not None:
        clauses.append(f"{AGE_SQL} >= ?")
        params.append(f.min_age)
    if f.max_age is not None:
        clauses.append(f"{AGE_SQL} <= ?")
        params.append(f.max_age)
    return " AND ".join(clauses), params


def load_fx(convert_to: Currency | None, enabled: bool) -> tuple[fx.FxRates | None, FxInfo]:
    """Fetch live rates only if conversion was requested; fall back to original currencies if unavailable."""
    if not convert_to:
        return None, FxInfo()
    if not enabled:
        return None, FxInfo(
            note="Currency conversion is turned off on this server; amounts are in original currencies."
        )
    try:
        rates = fx.get_rates(convert_to)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("Exchange rates unavailable: %s", e)
        return None, FxInfo(
            note="Live currency conversion is unavailable right now; amounts are in original currencies."
        )
    return rates, FxInfo(converted_to=convert_to, fx_rate_date=rates["date"], fx_rates=rates["rates"])


def to_person(r: Row, rates: fx.FxRates | None = None) -> Person:
    annual = r["annual_salary"]
    converted = fx.convert(annual, r["salary_currency"], rates) if rates and annual is not None else None
    return Person(
        name=r["full_name"],
        job_title=r["job_title"],
        team=r["team"],
        office=r["office"],
        city=r["city"],
        country=r["country"],
        work_status=r["work_status"],
        age=r["age"],
        start_date=r["start_date"],
        reports_to=r["manager_name"],
        email=r["email"],
        salary=Salary(
            amount=r["salary_amount"],
            currency=r["salary_currency"],
            type=r["salary_type"],
            annual=annual,
            annual_converted=converted,
        ),
    )


def reject_unknown_arguments(tool: Tool) -> Tool:
    """The SDK ignores argument names a tool doesn't have, so a typo ("filtr") silently drops the filter
    and the tool answers about everyone. Forbid extra arguments so the caller gets an error to fix instead.
    This also makes the published input schema say additionalProperties: false."""
    base = tool.fn_metadata.arg_model
    tool.fn_metadata.arg_model = type(base.__name__, (base,), {"model_config": ConfigDict(extra="forbid")})
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)
    return tool


def create_server(db_path: Path, *, fx_enabled: bool = True) -> MCPServer:
    """Build the MCP server for an existing database."""
    db = PeopleDB(db_path)

    def get_dataset_overview() -> Overview:
        """Describe the dataset: total people, the valid values of each category field, and salary notes.
        Use it to map words like 'NYC', 'UK' or 'the bakers' onto real values before filtering."""
        return Overview(
            total_people=db.query("SELECT COUNT(*) AS n FROM people")[0]["n"],
            values=db.category_values(),
            notes=data_notes(fx_enabled),
        )

    def count_people(filter: PeopleFilter | None = None, group_by: GroupBy | None = None) -> CountResult:
        """Count people matching the filter, optionally broken down by a field."""
        sql_where, params = where(filter)
        total = db.query(f"SELECT COUNT(*) AS n FROM people WHERE {sql_where}", params)[0]["n"]
        if not group_by:
            return CountResult(total=total)
        rows = db.query(
            f"SELECT {GROUP_SQL[group_by]} AS value, COUNT(*) AS count FROM people WHERE {sql_where} "
            "GROUP BY 1 ORDER BY 2 DESC",
            params,
        )
        return CountResult(total=total, group_by=group_by, groups=[GroupCount(**r) for r in rows])

    def aggregate(
        metric: Metric,
        operation: Operation,
        filter: PeopleFilter | None = None,
        group_by: GroupBy | None = None,
        convert_to: Currency | None = None,
    ) -> AggregateResult:
        """Compute avg/min/max/sum/median of age, tenure (years) or annual salary for people matching the filter.
        Salary results are split by original currency unless convert_to is given, in which case all salaries
        are converted at live rates and combined. People with no value for the metric are skipped."""
        rates, fx_info = load_fx(convert_to if metric == "annual_salary" else None, fx_enabled)
        sql_where, params = where(filter)
        metric_sql = METRIC_SQL[metric]
        group_sql = GROUP_SQL[group_by] if group_by else "NULL"
        rows = db.query(
            f"SELECT {group_sql} AS grp, salary_currency AS cur, {metric_sql} AS v FROM people "
            f"WHERE {sql_where} AND {metric_sql} IS NOT NULL",
            params,
        )

        split_by_currency = metric == "annual_salary" and rates is None
        buckets: dict[tuple[str | None, str | None], list[float]] = {}
        unconvertible = 0
        for r in rows:
            value = fx.convert(r["v"], r["cur"], rates) if rates else float(r["v"])
            if value is None:  # a currency with no exchange rate: leave it out rather than mix currencies
                unconvertible += 1
                continue
            currency = r["cur"] if split_by_currency else fx_info.converted_to
            buckets.setdefault((r["grp"], currency), []).append(value)
        if unconvertible:
            fx_info.note = f"{unconvertible} salaries in other currencies could not be converted and were left out."

        results = [
            AggregateRow(value=round(OPERATIONS[operation](values), 2), count=len(values), group=grp, currency=cur)
            for (grp, cur), values in sorted(buckets.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))
        ]
        return AggregateResult(
            metric=metric, operation=operation, group_by=group_by, results=results, **fx_info.model_dump()
        )

    def search_people(
        filter: PeopleFilter | None = None,
        sort_by: SortBy = "name",
        descending: bool = False,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        convert_to: Currency | None = None,
    ) -> SearchResult:
        """List people matching the filter, with their job, location, age, manager and salary (original currency;
        plus a converted annual figure when convert_to is given). Sorting by annual_salary across different
        currencies requires convert_to."""
        rates, fx_info = load_fx(convert_to, fx_enabled)
        sql_where, params = where(filter)
        rows = db.query(f"SELECT *, {AGE_SQL} AS age FROM people WHERE {sql_where}", params)
        people = [to_person(r, rates) for r in rows]

        if sort_by == "annual_salary" and rates is None and len({p.salary.currency for p in people} - {None}) > 1:
            if convert_to:  # conversion was asked for but isn't possible right now
                raise ToolError(f"{fx_info.note} Sorting by salary across currencies isn't possible without it.")
            raise ToolError("These people are paid in different currencies; pass convert_to to sort by salary.")

        def key(p: Person) -> Any:  # noqa: ANN401 - str, int or float depending on sort_by
            if sort_by == "annual_salary":
                return p.salary.annual_converted if rates else p.salary.annual
            return {"name": p.name, "age": p.age, "start_date": p.start_date}[sort_by]

        with_value = sorted((p for p in people if key(p) is not None), key=key, reverse=descending)
        people = with_value + [p for p in people if key(p) is None]  # missing values always last
        return SearchResult(total_matches=len(people), people=people[:limit], **fx_info.model_dump())

    def get_person(name_or_email: str) -> PersonLookup:
        """Look up one person by (part of) their name, nickname or email. Returns their full record, manager and
        direct reports. If several people match, returns the candidates so you can ask which one."""
        needle = name_or_email.strip()
        if not needle:
            raise ToolError("Give a name or email to look up.")
        matches = db.query(
            f"SELECT *, {AGE_SQL} AS age, {TENURE_SQL} AS tenure FROM people "
            f"WHERE name_forms {CONTAINS} OR email {CONTAINS}",
            (contains(needle), contains(needle)),
        )
        if not matches:
            raise ToolError(f"No person matches '{needle}'. Try search_people with a partial name.")

        def is_exact(m: Row) -> bool:
            names = [*m["name_forms"].split("\n"), m["email"], m["nickname"]]
            return needle.lower() in {n.lower() for n in names if n}

        exact = [m for m in matches if is_exact(m)]
        if len(matches) > 1 and len(exact) != 1:
            return PersonLookup(
                candidates=[
                    Candidate(name=m["full_name"], job_title=m["job_title"], office=m["office"]) for m in matches
                ]
            )

        p = (exact or matches)[0]
        reports = db.query(
            "SELECT full_name AS name, job_title FROM people WHERE manager_id = ? ORDER BY 1", (p["id"],)
        )
        return PersonLookup(
            person=PersonDetail(
                **to_person(p).model_dump(),
                gender=p["gender"],
                date_of_birth=p["date_of_birth"],
                tenure_years=round(p["tenure"], 1) if p["tenure"] is not None else None,
                direct_reports=[DirectReport(**r) for r in reports],
            )
        )

    tools: list[Callable[..., Any]] = [get_dataset_overview, count_people, aggregate, search_people, get_person]
    return MCPServer(
        "people",
        instructions=server_instructions(db, fx_enabled),
        tools=[reject_unknown_arguments(Tool.from_function(fn)) for fn in tools],
    )
