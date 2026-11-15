"""Unit tests for CSV parsing and loading."""

import os
import sqlite3
from pathlib import Path

import pytest

from people_server.ingest import ensure_database, ingest, name_forms, parse_date, parse_float, split_nickname, to_row
from tests.helpers import CsvRow, read_csv, write_csv

BLANK_ROW = {
    "Full Name": "", "Work Status": "", "Start Date": "", "Job": "", "Work Email": "", "Team": "",
    "Reports To": "", "Office": "", "Salary Amount": "", "Salary Currency": "", "Salary Type": "",
    "Tenure": "", "Country": "", "City": "", "First Name": "", "Last Name": "", "Date of Birth": "",
    "Gender": "", "Contract Type": "",
}  # fmt: skip


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("03/06/2023", "2023-06-03"), ("31/12/1999", "1999-12-31"), ("2023-06-03", None), ("", None), (None, None)],
)
def test_parse_date_reads_day_first(raw: str | None, expected: str | None) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [("88000", 88000.0), ("13.5", 13.5), ("88,000", 88000.0), ("", None)])
def test_parse_float(raw: str, expected: float | None) -> None:
    assert parse_float(raw) == expected


# ---------- Names ----------


@pytest.mark.parametrize(
    ("full_name", "nickname", "plain"),
    [
        ("Beatrix 'Bea' Vane", "Bea", "Beatrix Vane"),
        ("Sullivan 'Sully' O'Shea", "Sully", "Sullivan O'Shea"),
        ("Beatrix Vane 'Bea'", "Bea", "Beatrix Vane"),  # nickname last
        # Apostrophes inside surnames are not nicknames:
        ("Finnian O'Malley", None, "Finnian O'Malley"),
        ("D'Arcy O'Hara", None, "D'Arcy O'Hara"),
        ("Liam O'Neill-O'Brien", None, "Liam O'Neill-O'Brien"),
    ],
)
def test_split_nickname(full_name: str, nickname: str | None, plain: str) -> None:
    assert split_nickname(full_name) == (nickname, plain)


@pytest.mark.parametrize(
    ("full_name", "forms"),
    [
        ("Beatrix 'Bea' Vane", ["Beatrix 'Bea' Vane", "Beatrix Vane", "Bea Vane"]),
        ("Sullivan 'Sully' O'Shea", ["Sullivan 'Sully' O'Shea", "Sullivan O'Shea", "Sully O'Shea"]),
        ("Beatrix Vane 'Bea'", ["Beatrix Vane 'Bea'", "Beatrix Vane", "Bea Vane"]),
        ("Alistair Finch", ["Alistair Finch"]),
    ],
)
def test_name_forms(full_name: str, forms: list[str]) -> None:
    assert name_forms(full_name) == forms


# ---------- Rows ----------


def test_to_row_extracts_nickname_and_currency_code() -> None:
    row = to_row({**BLANK_ROW, "Full Name": "Beatrix 'Bea' Vane", "Salary Currency": "GBP £"})
    assert row["nickname"] == "Bea"
    assert row["name_forms"] == "Beatrix 'Bea' Vane\nBeatrix Vane\nBea Vane"
    assert row["salary_currency"] == "GBP"


@pytest.mark.parametrize(
    ("amount", "salary_type", "annual"),
    [("50000", "Yearly", 50000.0), ("4000", "Monthly", 48000.0), ("16", "Hourly", 33280.0), ("16", "", None),
     ("0", "Yearly", 0.0)],  # a salary of 0 is a value, not a missing one
)  # fmt: skip
def test_to_row_annualizes_salary_in_original_currency(amount: str, salary_type: str, annual: float | None) -> None:
    row = to_row({**BLANK_ROW, "Salary Amount": amount, "Salary Type": salary_type, "Salary Currency": "ILS ₪"})
    assert row["annual_salary"] == annual
    assert row["salary_amount"] == float(amount)  # the original amount is kept as-is
    assert row["salary_currency"] == "ILS"


def test_to_row_turns_empty_values_into_none() -> None:
    row = to_row({**BLANK_ROW, "First Name": "Oryan", "Last Name": "Moshe"})
    assert row["full_name"] == "Oryan Moshe"  # falls back to first + last name
    assert row["city"] is None
    assert row["date_of_birth"] is None
    assert row["annual_salary"] is None
    assert to_row(BLANK_ROW)["full_name"] is None  # no name at all is None, not ""


# ---------- Loading ----------


def test_ingest_loads_every_row_and_resolves_managers(db_path: Path, rows: list[CsvRow]) -> None:
    con = sqlite3.connect(db_path)
    total, with_manager_name, with_manager_id = con.execute(
        "SELECT COUNT(*), COUNT(manager_name), COUNT(manager_id) FROM people"
    ).fetchone()
    assert total == len(rows) == 107
    assert with_manager_name == with_manager_id  # every "Reports To" name matched a person
    pending = con.execute("SELECT full_name, city, annual_salary FROM people WHERE work_status = 'pending_arrival'")
    assert pending.fetchall() == [("Oryan Moshe", None, None)]


def test_ingest_rejects_an_empty_csv(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text(",".join(BLANK_ROW) + "\n")
    with pytest.raises(ValueError, match="no data rows"):
        ingest(empty, tmp_path / "people.db")


def test_failed_rebuild_keeps_the_previous_database(tmp_path: Path, rows: list[CsvRow]) -> None:
    db = tmp_path / "people.db"
    ingest(write_csv(tmp_path / "good.csv", rows), db)
    broken = tmp_path / "broken.csv"
    broken.write_text("Full Name\nSomeone\n")  # missing columns: to_row raises KeyError
    with pytest.raises(KeyError):
        ingest(broken, db)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM people").fetchone() == (107,)
    assert not (tmp_path / "people.db.tmp").exists()


def test_ensure_database_rebuilds_only_when_the_csv_changes(tmp_path: Path) -> None:
    rows = read_csv()
    csv_path, db = write_csv(tmp_path / "people.csv", rows), tmp_path / "people.db"

    ensure_database(csv_path, db)  # missing: built
    built_at = db.stat().st_mtime_ns
    ensure_database(csv_path, db)  # unchanged: kept
    assert db.stat().st_mtime_ns == built_at

    write_csv(csv_path, rows[:10])
    later = db.stat().st_mtime + 10
    os.utime(csv_path, (later, later))  # the CSV is now newer than the database
    ensure_database(csv_path, db)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM people").fetchone() == (10,)
