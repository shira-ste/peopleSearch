"""Unit tests for CSV parsing and the ingestion step."""

import sqlite3
from pathlib import Path

import pytest

from ingest import parse_date, parse_float, to_row

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


def test_to_row_extracts_nickname_and_currency_code() -> None:
    row = to_row({**BLANK_ROW, "Full Name": "Beatrix 'Bea' Vane", "Salary Currency": "GBP £"})
    assert row["nickname"] == "Bea"
    assert row["salary_currency"] == "GBP"


@pytest.mark.parametrize(
    ("amount", "salary_type", "annual"),
    [("50000", "Yearly", 50000.0), ("4000", "Monthly", 48000.0), ("16", "Hourly", 33280.0), ("16", "", None)],
)
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


def test_ingest_loads_every_row_and_resolves_managers(db_path: Path, rows: list[dict[str, str]]) -> None:
    con = sqlite3.connect(db_path)
    total, with_manager_name, with_manager_id = con.execute(
        "SELECT COUNT(*), COUNT(manager_name), COUNT(manager_id) FROM people"
    ).fetchone()
    assert total == len(rows) == 107
    assert with_manager_name == with_manager_id  # every "Reports To" name matched a person
    pending = con.execute("SELECT full_name, city, annual_salary FROM people WHERE work_status = 'pending_arrival'")
    assert pending.fetchall() == [("Oryan Moshe", None, None)]
