"""MCP server exposing typed query tools over the people database (Streamable HTTP on /mcp)."""

import sqlite3
import statistics
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

import fx
from ingest import DB_PATH, ingest

AGE = ("(CAST(strftime('%Y','now') AS INT) - CAST(strftime('%Y', date_of_birth) AS INT)"
       " - (strftime('%m-%d','now') < strftime('%m-%d', date_of_birth)))")
TENURE = "((julianday('now') - julianday(start_date)) / 365.25)"
METRICS = {"age": AGE, "tenure_years": TENURE, "annual_salary": "annual_salary"}
GROUPS = {
    "city": "city", "country": "country", "office": "office", "team": "team", "gender": "gender",
    "job_title": "job_title", "work_status": "work_status", "salary_type": "salary_type",
    "salary_currency": "salary_currency", "start_year": "substr(start_date, 1, 4)",
}
GroupBy = Literal[tuple(GROUPS)]
Currency = Literal["USD", "GBP", "ILS"]

mcp = MCPServer(
    "people",
    instructions="Query a company's people records (employees of a bakery chain with offices in "
    "London, New York and Tel Aviv). Call get_dataset_overview first to learn the valid values.",
)


class PeopleFilter(BaseModel):
    """All fields optional and combined with AND. Text matches are case-insensitive."""

    name: str | None = Field(None, description="Part of the person's full name or nickname")
    city: str | None = None
    country: str | None = None
    office: str | None = None
    team: str | None = None
    gender: str | None = None
    work_status: str | None = Field(None, description="'active' or 'pending_arrival'")
    salary_type: str | None = Field(None, description="'Yearly', 'Monthly' or 'Hourly'")
    salary_currency: Currency | None = None
    job_title_contains: str | None = None
    manager_name: str | None = Field(None, description="Part of the name of the person they report to")
    min_age: int | None = None
    max_age: int | None = None


def query(sql: str, params=()) -> list[dict]:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def where(f: PeopleFilter | None) -> tuple[str, list]:
    f = f or PeopleFilter()
    clauses, params = ["1=1"], []
    for col in ("city", "country", "office", "team", "gender", "work_status", "salary_type", "salary_currency"):
        if (value := getattr(f, col)) is not None:
            clauses.append(f"{col} = ? COLLATE NOCASE")
            params.append(value)
    for col, value in (("full_name", f.name), ("job_title", f.job_title_contains), ("manager_name", f.manager_name)):
        if value:
            clauses.append(f"{col} LIKE ?")  # LIKE is case-insensitive in SQLite; nicknames are part of full_name
            params.append(f"%{value}%")
    if f.min_age is not None:
        clauses.append(f"{AGE} >= ?")
        params.append(f.min_age)
    if f.max_age is not None:
        clauses.append(f"{AGE} <= ?")
        params.append(f.max_age)
    return " AND ".join(clauses), params


def load_fx(convert_to: str | None) -> tuple[dict | None, dict]:
    """Fetch live rates if conversion was requested. Returns (rates, info to include in the result)."""
    if not convert_to:
        return None, {}
    try:
        rates = fx.get_rates(convert_to)
        return rates, {"converted_to": convert_to, "fx_rate_date": rates["date"],
                       "fx_rates_per_1_" + convert_to: rates["rates"]}
    except Exception:
        return None, {"note": "Live currency conversion is unavailable right now; amounts are in original currencies."}


def person_record(r: dict, rates: dict | None = None) -> dict:
    rec = {
        "name": r["full_name"], "job_title": r["job_title"], "team": r["team"], "office": r["office"],
        "city": r["city"], "country": r["country"], "work_status": r["work_status"], "age": r["age"],
        "start_date": r["start_date"], "reports_to": r["manager_name"], "email": r["email"],
        "salary": {"amount": r["salary_amount"], "currency": r["salary_currency"], "type": r["salary_type"],
                   "annual": r["annual_salary"]},
    }
    if rates and r["annual_salary"] is not None:
        rec["salary"]["annual_converted"] = fx.convert(r["annual_salary"], r["salary_currency"], rates)
    return rec


@mcp.tool()
def get_dataset_overview() -> dict:
    """Describe the dataset: total people, the valid values of each category field, and salary notes.
    Use it to map words like 'NYC', 'UK' or 'the bakers' onto real values before filtering."""
    fields = ["city", "country", "office", "team", "gender", "work_status", "salary_type", "salary_currency"]
    return {
        "total_people": query("SELECT COUNT(*) AS n FROM people")[0]["n"],
        "values": {c: [r[c] for r in query(f"SELECT DISTINCT {c} FROM people WHERE {c} IS NOT NULL ORDER BY 1")]
                   for c in fields},
        "notes": [
            "Salaries are stored in their original currency (USD, GBP or ILS) and pay type (Yearly, Monthly, Hourly).",
            "annual_salary = Yearly x1, Monthly x12, Hourly x2080 (40h x 52 weeks), still in the original currency.",
            "Pass convert_to to compare salaries across currencies; conversion uses live ECB rates.",
            "One person is 'pending_arrival' (not started yet) and has almost no data.",
            "Age and tenure are calculated from today's date.",
        ],
    }


@mcp.tool()
def count_people(filter: PeopleFilter | None = None, group_by: GroupBy | None = None) -> dict:
    """Count people matching the filter, optionally broken down by a field."""
    sql_where, params = where(filter)
    total = query(f"SELECT COUNT(*) AS n FROM people WHERE {sql_where}", params)[0]["n"]
    if not group_by:
        return {"total": total}
    rows = query(f"SELECT {GROUPS[group_by]} AS value, COUNT(*) AS count FROM people WHERE {sql_where} "
                 "GROUP BY 1 ORDER BY 2 DESC", params)
    return {"total": total, "group_by": group_by, "groups": rows}


@mcp.tool()
def aggregate(
    metric: Literal["age", "tenure_years", "annual_salary"],
    operation: Literal["avg", "min", "max", "sum", "median"],
    filter: PeopleFilter | None = None,
    group_by: GroupBy | None = None,
    convert_to: Currency | None = None,
) -> dict:
    """Compute avg/min/max/sum/median of age, tenure (years) or annual salary for people matching the filter.
    Salary results are split by original currency unless convert_to is given, in which case all salaries
    are converted at live rates and combined. People with no value for the metric are skipped."""
    rates, fx_info = load_fx(convert_to if metric == "annual_salary" else None)
    sql_where, params = where(filter)
    group_sql = GROUPS[group_by] if group_by else "NULL"
    rows = query(f"SELECT {group_sql} AS grp, salary_currency AS cur, {METRICS[metric]} AS v FROM people "
                 f"WHERE {sql_where} AND {METRICS[metric]} IS NOT NULL", params)

    split_by_currency = metric == "annual_salary" and not rates
    buckets: dict[tuple, list[float]] = {}
    for r in rows:
        value = fx.convert(r["v"], r["cur"], rates) if rates else r["v"]
        buckets.setdefault((r["grp"], r["cur"] if split_by_currency else None), []).append(value)

    op = {"avg": statistics.mean, "min": min, "max": max, "sum": sum, "median": statistics.median}[operation]
    results = []
    for (grp, cur), values in sorted(buckets.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        item = {"value": round(op(values), 2), "count": len(values)}
        if group_by:
            item[group_by] = grp
        if split_by_currency:
            item["currency"] = cur
        if rates:
            item["currency"] = convert_to
        results.append(item)
    return {"metric": metric, "operation": operation, "results": results, **fx_info}


@mcp.tool()
def search_people(
    filter: PeopleFilter | None = None,
    sort_by: Literal["name", "age", "start_date", "annual_salary"] = "name",
    descending: bool = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
    convert_to: Currency | None = None,
) -> dict:
    """List people matching the filter, with their job, location, age, manager and salary (original currency;
    plus a converted annual figure when convert_to is given). Sorting by annual_salary across different
    currencies requires convert_to."""
    rates, fx_info = load_fx(convert_to)
    sql_where, params = where(filter)
    people = [person_record(r, rates) for r in query(f"SELECT *, {AGE} AS age FROM people WHERE {sql_where}", params)]

    if sort_by == "annual_salary":
        if not rates and len({p["salary"]["currency"] for p in people if p["salary"]["currency"]}) > 1:
            raise ToolError("These people are paid in different currencies; pass convert_to to sort by salary.")
        key = lambda p: p["salary"].get("annual_converted", p["salary"]["annual"])
    else:
        key = {"name": lambda p: p["name"], "age": lambda p: p["age"], "start_date": lambda p: p["start_date"]}[sort_by]
    with_value = sorted((p for p in people if key(p) is not None), key=key, reverse=descending)
    people = with_value + [p for p in people if key(p) is None]  # missing values last

    return {"total_matches": len(people), "people": people[:limit], **fx_info}


@mcp.tool()
def get_person(name_or_email: str) -> dict:
    """Look up one person by (part of) their name, nickname or email. Returns their full record, manager and
    direct reports. If several people match, returns the candidates so you can ask which one."""
    matches = query(f"SELECT *, {AGE} AS age, {TENURE} AS tenure FROM people WHERE full_name LIKE ? OR email = ? COLLATE NOCASE",
                    (f"%{name_or_email}%", name_or_email))
    if not matches:
        raise ToolError(f"No person matches '{name_or_email}'. Try search_people with a partial name.")
    needle = name_or_email.lower()
    exact = [m for m in matches if needle in (m["full_name"].lower(), (m["email"] or "").lower(), (m["nickname"] or "").lower())]
    if len(matches) > 1 and len(exact) != 1:
        return {"candidates": [{"name": m["full_name"], "job_title": m["job_title"], "office": m["office"]}
                               for m in matches]}
    p = (exact or matches)[0]
    reports = query("SELECT full_name, job_title FROM people WHERE manager_id = ? ORDER BY full_name", (p["id"],))
    tenure = round(p["tenure"], 1) if p["tenure"] is not None else None
    return {**person_record(p), "tenure_years": tenure, "gender": p["gender"],
            "date_of_birth": p["date_of_birth"], "direct_reports": reports}


if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"Ingested {ingest()} people into {DB_PATH}")
    mcp.run("streamable-http", host="127.0.0.1", port=8000)
