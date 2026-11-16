"""Load a people CSV export into a SQLite database.

The server calls `ensure_database` on startup, so the database is (re)built on demand: when it's missing,
or when it was built from a CSV with different contents. To rebuild by hand: `uv run people-ingest file.csv`.
"""

import argparse
import contextlib
import csv
import hashlib
import io
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

ANNUAL_FACTOR = {"Yearly": 1, "Monthly": 12, "Hourly": 2080}  # 2080 = 40h x 52 weeks

# A nickname is a quoted word standing on its own: "Beatrix 'Bea' Vane". The quotes must be at word edges,
# so apostrophes inside surnames (O'Shea, D'Arcy O'Hara) are not mistaken for one.
NICKNAME = re.compile(r"(?:^|\s)'([^']+)'(?=\s|$)")

SCHEMA = """
CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    full_name TEXT, nickname TEXT,
    name_forms TEXT,  -- every way the name may be written, one per line (see name_forms())
    work_status TEXT, job_title TEXT, email TEXT, team TEXT,
    office TEXT, city TEXT, country TEXT, gender TEXT, contract_type TEXT,
    start_date TEXT, date_of_birth TEXT,
    salary_amount REAL, salary_currency TEXT, salary_type TEXT, annual_salary REAL,
    manager_name TEXT, manager_id INTEGER REFERENCES people(id)
);
CREATE INDEX people_manager ON people(manager_id);
-- Which CSV the database was built from: a rebuild is needed when the CSV's contents change.
CREATE TABLE source (csv_path TEXT NOT NULL, sha256 TEXT NOT NULL);
"""


class PersonRow(TypedDict):
    full_name: str | None
    nickname: str | None
    name_forms: str | None
    work_status: str | None
    job_title: str | None
    email: str | None
    team: str | None
    office: str | None
    city: str | None
    country: str | None
    gender: str | None
    contract_type: str | None
    start_date: str | None
    date_of_birth: str | None
    salary_amount: float | None
    salary_currency: str | None
    salary_type: str | None
    annual_salary: float | None
    manager_name: str | None


def clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def parse_date(value: str | None) -> str | None:
    """The export writes dates day-first (03/06/2023 is 3 June 2023). Stored as ISO dates."""
    try:
        return datetime.strptime((value or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def parse_float(value: str | None) -> float | None:
    try:
        return float((value or "").replace(",", ""))
    except ValueError:
        return None


def split_nickname(full_name: str) -> tuple[str | None, str]:
    """ "Beatrix 'Bea' Vane" -> ("Bea", "Beatrix Vane"). Names without a nickname come back unchanged."""
    match = NICKNAME.search(full_name)
    if not match:
        return None, full_name
    without = full_name[: match.start()] + full_name[match.end() :]
    return match.group(1), " ".join(without.split())


def name_forms(full_name: str) -> list[str]:
    """Every way a person's name may be written, so a search for any of them finds them.

    "Beatrix 'Bea' Vane" -> ["Beatrix 'Bea' Vane", "Beatrix Vane", "Bea Vane"]
    """
    nickname, plain = split_nickname(full_name)
    forms = [full_name, plain]
    if nickname:
        surname = plain.split(" ", 1)[1] if " " in plain else ""
        forms.append(f"{nickname} {surname}".strip())
    return list(dict.fromkeys(forms))  # drop duplicates, keep order


def to_row(r: dict[str, str]) -> PersonRow:
    # Only the full name is stored: it's what "Reports To" uses, and the CSV's "Last Name" column contains
    # nicknames ("'Alley' Vance"). First + last name are only a fallback if "Full Name" is ever empty.
    full_name = clean(r["Full Name"]) or clean(" ".join(filter(None, [clean(r["First Name"]), clean(r["Last Name"])])))
    nickname = split_nickname(full_name)[0] if full_name else None
    amount = parse_float(r["Salary Amount"])
    salary_type = clean(r["Salary Type"])
    currency = clean(r["Salary Currency"])
    annual = amount * ANNUAL_FACTOR[salary_type] if amount is not None and salary_type in ANNUAL_FACTOR else None
    return {
        "full_name": full_name,
        "nickname": nickname,
        "name_forms": "\n".join(name_forms(full_name)) if full_name else None,
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
        "annual_salary": annual,
        "manager_name": clean(r["Reports To"]),
    }


def ingest(csv_path: Path, db_path: Path) -> int:
    """Build the database from the CSV and return the number of people loaded.

    The new database is written next to the old one and swapped in at the end, so a running server never
    sees a missing or half-built file, and a failed import leaves the previous database in place.
    """
    data = csv_path.read_bytes()
    rows = [to_row(r) for r in csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))]
    if not rows:
        raise ValueError(f"{csv_path} has no data rows")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)
    try:
        with contextlib.closing(sqlite3.connect(tmp_path)) as con, con:  # closes, and commits or rolls back
            con.executescript(SCHEMA)
            cols = list(rows[0])
            placeholders = ", ".join(f":{c}" for c in cols)
            con.executemany(f"INSERT INTO people ({', '.join(cols)}) VALUES ({placeholders})", rows)
            con.execute(
                "UPDATE people SET manager_id = (SELECT m.id FROM people m WHERE m.full_name = people.manager_name)"
            )
            con.execute("INSERT INTO source VALUES (?, ?)", (str(csv_path), hashlib.sha256(data).hexdigest()))
        os.replace(tmp_path, db_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return len(rows)


def built_from(db_path: Path) -> str | None:
    """The SHA-256 of the CSV the database was built from, or None if there's no usable database."""
    if not db_path.exists():
        return None
    try:
        with contextlib.closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as con:
            row = con.execute("SELECT sha256 FROM source").fetchone()
    except sqlite3.Error:  # not a database, or built before the source table existed
        return None
    return str(row[0]) if row else None


def ensure_database(csv_path: Path, db_path: Path) -> None:
    """Build the database unless it was built from a CSV with exactly these contents.

    Comparing contents, not modification times, means switching to another file (even an older one) or
    editing the CSV in place always triggers a rebuild, and touching it without changes doesn't.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(f"People CSV not found: {csv_path} (set PEOPLE_CSV or pass --csv)")
    if built_from(db_path) == hashlib.sha256(csv_path.read_bytes()).hexdigest():
        return
    count = ingest(csv_path, db_path)
    logger.info("Loaded %d people from %s into %s", count, csv_path, db_path)


def main() -> None:
    from people_server.settings import ServerSettings

    settings = ServerSettings()
    parser = argparse.ArgumentParser(description="Load a people CSV export into SQLite.")
    parser.add_argument("csv", type=Path, nargs="?", default=settings.people_csv, help="CSV file to load")
    parser.add_argument("--db", type=Path, default=settings.people_db, help="SQLite file to write")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.info("Loaded %d people from %s into %s", ingest(args.csv, args.db), args.csv, args.db)


if __name__ == "__main__":
    main()
