"""MCP server exposing typed query tools over the people database (Streamable HTTP on /mcp)."""

import os
import sqlite3
import statistics
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

import fx
from fx import Currency
from ingest import DB_PATH, ingest

Row = dict[str, Any]
Metric = Literal["age", "tenure_years", "annual_salary"]
Operation = Literal["avg", "min", "max", "sum", "median"]
GroupBy = Literal[
    "city", "country", "office", "team", "gender", "job_title",
    "work_status", "salary_type", "salary_currency", "start_year",
]  # fmt: skip
SortBy = Literal["name", "age", "start_date", "annual_salary"]

AGE_SQL = (
    "(CAST(strftime('%Y','now') AS INT) - CAST(strftime('%Y', date_of_birth) AS INT)"
    " - (strftime('%m-%d','now') < strftime('%m-%d', date_of_birth)))"
)
TENURE_SQL = "((julianday('now') - julianday(start_date)) / 365.25)"
# People (and Claude) write names in different forms. For "Beatrix 'Bea' Vane" also match:
# "Beatrix Vane" (without the nickname) and "Bea Vane" (nickname + surname).
PLAIN_NAME_SQL = "CASE WHEN nickname IS NULL THEN full_name ELSE replace(full_name, '''' || nickname || ''' ', '') END"
NICK_NAME_SQL = (
    "CASE WHEN nickname IS NULL THEN full_name ELSE nickname || substr(full_name, instr(full_name, ''' ') + 1) END"
)
# Part of the name, in any of those forms. Takes the same LIKE pattern three times.
NAME_LIKE_SQL = f"(full_name LIKE ? OR {PLAIN_NAME_SQL} LIKE ? OR {NICK_NAME_SQL} LIKE ?)"
METRIC_SQL: dict[Metric, str] = {"age": AGE_SQL, "tenure_years": TENURE_SQL, "annual_salary": "annual_salary"}
GROUP_SQL: dict[GroupBy, str] = {
    "city": "city", "country": "country", "office": "office", "team": "team", "gender": "gender",
    "job_title": "job_title", "work_status": "work_status", "salary_type": "salary_type",
    "salary_currency": "salary_currency", "start_year": "substr(start_date, 1, 4)",
}  # fmt: skip
OPERATIONS: dict[Operation, Callable[[list[float]], float]] = {
    "avg": statistics.mean, "min": min, "max": max, "sum": sum, "median": statistics.median,
}  # fmt: skip


def query(sql: str, params: Sequence[Any] = ()) -> list[Row]:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


DATA_NOTES = [
    "Salaries are stored in their original currency (USD, GBP or ILS) and pay type (Yearly, Monthly, Hourly).",
    "annual_salary = Yearly x1, Monthly x12, Hourly x2080 (40h x 52 weeks), still in the original currency.",
    "Pass convert_to to compare salaries across currencies; conversion uses live ECB rates.",
    "One person is 'pending_arrival' (not started yet) and has almost no data.",
    "Age and tenure are calculated from today's date.",
]
CATEGORY_FIELDS = ["city", "country", "office", "team", "gender", "work_status", "salary_type", "salary_currency"]


def category_values() -> dict[str, list[str]]:
    """The distinct values of each category field, e.g. {"city": ["London", "New York", "Tel Aviv"], ...}."""
    return {
        c: [r[c] for r in query(f"SELECT DISTINCT {c} FROM people WHERE {c} IS NOT NULL ORDER BY 1")]
        for c in CATEGORY_FIELDS
    }


def server_instructions() -> str:
    """Sent to clients when they connect. Listing the valid values up front means the model can map words
    like "NYC" to "New York" straight away, instead of spending an extra round on get_dataset_overview."""
    values = "\n".join(f"- {field}: {', '.join(vals)}" for field, vals in category_values().items())
    notes = "\n".join(f"- {note}" for note in DATA_NOTES)
    return (
        "Query a company's people records: a bakery chain with offices in London, New York and Tel Aviv.\n"
        f"Valid values for filters (matching is case-insensitive):\n{values}\n"
        f"Notes on the data:\n{notes}"
    )


# The instructions are built from the data, so the database must exist before the server object is created.
if not DB_PATH.exists():
    print(f"Ingested {ingest()} people into {DB_PATH}")

mcp = MCPServer("people", instructions=server_instructions())


# ---------- Inputs ----------


class PeopleFilter(BaseModel):
    """All fields are optional and combined with AND. Text matches are case-insensitive."""

    name: str | None = Field(default=None, description="Part of the person's full name or nickname")
    city: str | None = None
    country: str | None = None
    office: str | None = None
    team: str | None = None
    gender: str | None = None
    work_status: str | None = Field(default=None, description="'active' or 'pending_arrival'")
    salary_type: str | None = Field(default=None, description="'Yearly', 'Monthly' or 'Hourly'")
    salary_currency: Currency | None = None
    job_title_contains: str | None = None
    manager_name: str | None = Field(default=None, description="Part of the name of the person they report to")
    min_age: int | None = None
    max_age: int | None = None


# ---------- Outputs ----------


class FxInfo(BaseModel):
    converted_to: Currency | None = None
    fx_rate_date: str | None = None
    fx_rates: dict[str, float] | None = Field(default=None, description="Units per 1 of converted_to")
    note: str | None = None


class Salary(BaseModel):
    amount: float | None
    currency: str | None
    type: str | None
    annual: float | None = Field(description="Annualized, in the original currency")
    annual_converted: float | None = Field(default=None, description="Annualized, in convert_to currency")


class Person(BaseModel):
    name: str
    job_title: str | None
    team: str | None
    office: str | None
    city: str | None
    country: str | None
    work_status: str | None
    age: int | None
    start_date: str | None
    reports_to: str | None
    email: str | None
    salary: Salary


class DirectReport(BaseModel):
    name: str
    job_title: str | None


class PersonDetail(Person):
    gender: str | None
    date_of_birth: str | None
    tenure_years: float | None
    direct_reports: list[DirectReport]


class Candidate(BaseModel):
    name: str
    job_title: str | None
    office: str | None


class PersonLookup(BaseModel):
    person: PersonDetail | None = None
    candidates: list[Candidate] | None = Field(default=None, description="Set when several people match")


class Overview(BaseModel):
    total_people: int
    values: dict[str, list[str]]
    notes: list[str]


class GroupCount(BaseModel):
    value: str | None
    count: int


class CountResult(BaseModel):
    total: int
    group_by: GroupBy | None = None
    groups: list[GroupCount] | None = None


class AggregateRow(BaseModel):
    value: float
    count: int
    group: str | None = None
    currency: str | None = None


class AggregateResult(FxInfo):
    metric: Metric
    operation: Operation
    group_by: GroupBy | None
    results: list[AggregateRow]


class SearchResult(FxInfo):
    total_matches: int
    people: list[Person]


# ---------- Helpers ----------


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
    # LIKE is case-insensitive in SQLite.
    if f.name:
        clauses.append(NAME_LIKE_SQL)
        params += [f"%{f.name}%"] * 3
    if f.job_title_contains:
        clauses.append("job_title LIKE ?")
        params.append(f"%{f.job_title_contains}%")
    if f.manager_name:  # match the manager's own record, so "Beatrix Vane" finds Bea's reports too
        clauses.append(f"manager_id IN (SELECT id FROM people WHERE {NAME_LIKE_SQL})")
        params += [f"%{f.manager_name}%"] * 3
    if f.min_age is not None:
        clauses.append(f"{AGE_SQL} >= ?")
        params.append(f.min_age)
    if f.max_age is not None:
        clauses.append(f"{AGE_SQL} <= ?")
        params.append(f.max_age)
    return " AND ".join(clauses), params


def load_fx(convert_to: Currency | None) -> tuple[fx.FxRates | None, FxInfo]:
    """Fetch live rates only if conversion was requested; fall back to original currencies if the API is down."""
    if not convert_to:
        return None, FxInfo()
    try:
        rates = fx.get_rates(convert_to)
    except Exception:
        return None, FxInfo(note="Live currency conversion is unavailable; amounts are in original currencies.")
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


# ---------- Tools ----------


@mcp.tool()
def get_dataset_overview() -> Overview:
    """Describe the dataset: total people, the valid values of each category field, and salary notes.
    Use it to map words like 'NYC', 'UK' or 'the bakers' onto real values before filtering."""
    return Overview(
        total_people=query("SELECT COUNT(*) AS n FROM people")[0]["n"],
        values=category_values(),
        notes=DATA_NOTES,
    )


@mcp.tool()
def count_people(filter: PeopleFilter | None = None, group_by: GroupBy | None = None) -> CountResult:
    """Count people matching the filter, optionally broken down by a field."""
    sql_where, params = where(filter)
    total = query(f"SELECT COUNT(*) AS n FROM people WHERE {sql_where}", params)[0]["n"]
    if not group_by:
        return CountResult(total=total)
    rows = query(
        f"SELECT {GROUP_SQL[group_by]} AS value, COUNT(*) AS count FROM people WHERE {sql_where} "
        "GROUP BY 1 ORDER BY 2 DESC",
        params,
    )
    return CountResult(total=total, group_by=group_by, groups=[GroupCount(**r) for r in rows])


@mcp.tool()
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
    rates, fx_info = load_fx(convert_to if metric == "annual_salary" else None)
    sql_where, params = where(filter)
    metric_sql = METRIC_SQL[metric]
    group_sql = GROUP_SQL[group_by] if group_by else "NULL"
    rows = query(
        f"SELECT {group_sql} AS grp, salary_currency AS cur, {metric_sql} AS v FROM people "
        f"WHERE {sql_where} AND {metric_sql} IS NOT NULL",
        params,
    )

    split_by_currency = metric == "annual_salary" and rates is None
    buckets: dict[tuple[str | None, str | None], list[float]] = {}
    for r in rows:
        value = fx.convert(r["v"], r["cur"], rates) if rates else float(r["v"])
        currency = r["cur"] if split_by_currency else fx_info.converted_to
        buckets.setdefault((r["grp"], currency), []).append(value)

    results = [
        AggregateRow(value=round(OPERATIONS[operation](values), 2), count=len(values), group=grp, currency=cur)
        for (grp, cur), values in sorted(buckets.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))
    ]
    return AggregateResult(
        metric=metric, operation=operation, group_by=group_by, results=results, **fx_info.model_dump()
    )


@mcp.tool()
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
    rates, fx_info = load_fx(convert_to)
    sql_where, params = where(filter)
    people = [to_person(r, rates) for r in query(f"SELECT *, {AGE_SQL} AS age FROM people WHERE {sql_where}", params)]

    if sort_by == "annual_salary" and rates is None and len({p.salary.currency for p in people} - {None}) > 1:
        raise ToolError("These people are paid in different currencies; pass convert_to to sort by salary.")

    def key(p: Person) -> Any:  # noqa: ANN401 - str, int or float depending on sort_by
        if sort_by == "annual_salary":
            return p.salary.annual_converted if rates else p.salary.annual
        return {"name": p.name, "age": p.age, "start_date": p.start_date}[sort_by]

    with_value = sorted((p for p in people if key(p) is not None), key=key, reverse=descending)
    people = with_value + [p for p in people if key(p) is None]  # missing values always last
    return SearchResult(total_matches=len(people), people=people[:limit], **fx_info.model_dump())


@mcp.tool()
def get_person(name_or_email: str) -> PersonLookup:
    """Look up one person by (part of) their name, nickname or email. Returns their full record, manager and
    direct reports. If several people match, returns the candidates so you can ask which one."""
    matches = query(
        f"SELECT *, {AGE_SQL} AS age, {TENURE_SQL} AS tenure, {PLAIN_NAME_SQL} AS plain_name, "
        f"{NICK_NAME_SQL} AS nick_name FROM people "
        f"WHERE {NAME_LIKE_SQL} OR email = ? COLLATE NOCASE",
        (*[f"%{name_or_email}%"] * 3, name_or_email),
    )
    if not matches:
        raise ToolError(f"No person matches '{name_or_email}'. Try search_people with a partial name.")

    needle = name_or_email.lower()
    exact = [
        m
        for m in matches
        if needle in {str(m[c]).lower() for c in ("full_name", "plain_name", "nick_name", "email", "nickname")}
    ]
    if len(matches) > 1 and len(exact) != 1:
        return PersonLookup(
            candidates=[Candidate(name=m["full_name"], job_title=m["job_title"], office=m["office"]) for m in matches]
        )

    p = (exact or matches)[0]
    reports = query("SELECT full_name AS name, job_title FROM people WHERE manager_id = ? ORDER BY 1", (p["id"],))
    return PersonLookup(
        person=PersonDetail(
            **to_person(p).model_dump(),
            gender=p["gender"],
            date_of_birth=p["date_of_birth"],
            tenure_years=round(p["tenure"], 1) if p["tenure"] is not None else None,
            direct_reports=[DirectReport(**r) for r in reports],
        )
    )


if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=int(os.environ.get("MCP_PORT", "8000")))
