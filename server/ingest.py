"""Load people-list-export.csv into a SQLite database. Run directly to rebuild."""

import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_PATH = DATA_DIR / "people-list-export.csv"
DB_PATH = DATA_DIR / "people.db"

ANNUAL_FACTOR = {"Yearly": 1, "Monthly": 12, "Hourly": 2080}  # 2080 = 40h x 52 weeks

SCHEMA = """
CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    full_name TEXT, first_name TEXT, last_name TEXT, nickname TEXT,
    work_status TEXT, job_title TEXT, email TEXT, team TEXT,
    office TEXT, city TEXT, country TEXT, gender TEXT, contract_type TEXT,
    start_date TEXT, date_of_birth TEXT,
    salary_amount REAL, salary_currency TEXT, salary_type TEXT, annual_salary REAL,
    manager_name TEXT, manager_id INTEGER REFERENCES people(id)
);
"""


def clean(value):
    value = (value or "").strip()
    return value or None


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except (AttributeError, ValueError):
        return None


def parse_float(value):
    try:
        return float(value.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def to_row(r):
    full_name = clean(r["Full Name"]) or " ".join(filter(None, [clean(r["First Name"]), clean(r["Last Name"])]))
    nickname = re.search(r"'([^']+)'", full_name or "")
    amount = parse_float(r["Salary Amount"])
    salary_type = clean(r["Salary Type"])
    currency = clean(r["Salary Currency"])
    return {
        "full_name": full_name,
        "first_name": clean(r["First Name"]),
        "last_name": clean(r["Last Name"]),
        "nickname": nickname.group(1) if nickname else None,
        "work_status": clean(r["Work Status"]),
        "job_title": clean(r["Job"]),
        "email": clean(r["Work Email"]),
        "team": clean(r["Team"]),
        "office": clean(r["Office"]),
        "city": clean(r["City"]),
        "country": clean(r["Country"]),
        "gender": clean(r["Gender"]),
        "contract_type": clean(r["Contract Type"]),
        "start_date": parse_date(r["Start Date"]),
        "date_of_birth": parse_date(r["Date of Birth"]),
        "salary_amount": amount,
        "salary_currency": currency.split()[0] if currency else None,  # "GBP £" -> "GBP"
        "salary_type": salary_type,
        "annual_salary": amount * ANNUAL_FACTOR[salary_type] if amount and salary_type in ANNUAL_FACTOR else None,
        "manager_name": clean(r["Reports To"]),
    }


def ingest(csv_path=CSV_PATH, db_path=DB_PATH):
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = [to_row(r) for r in csv.DictReader(f)]

    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    cols = list(rows[0])
    con.executemany(
        f"INSERT INTO people ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", rows
    )
    con.execute(
        "UPDATE people SET manager_id = (SELECT m.id FROM people m WHERE m.full_name = people.manager_name)"
    )
    con.commit()
    con.close()
    return len(rows)


if __name__ == "__main__":
    print(f"Ingested {ingest()} people into {DB_PATH}")
