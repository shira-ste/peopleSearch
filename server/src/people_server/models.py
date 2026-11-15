"""Typed inputs and outputs of the MCP tools. Pydantic validates inputs and generates the JSON schemas."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from people_server.fx import Currency

Metric = Literal["age", "tenure_years", "annual_salary"]
Operation = Literal["avg", "min", "max", "sum", "median"]
GroupBy = Literal[
    "city", "country", "office", "team", "gender", "job_title",
    "work_status", "salary_type", "salary_currency", "start_year",
]  # fmt: skip
SortBy = Literal["name", "age", "start_date", "annual_salary"]
WorkStatus = Literal["active", "pending_arrival"]
SalaryType = Literal["Yearly", "Monthly", "Hourly"]


# ---------- Inputs ----------


class PeopleFilter(BaseModel):
    """All fields are optional and combined with AND. Text matches are case-insensitive."""

    # A misspelled field ("citty") is an error, not silently ignored (which would match everyone).
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description="Part of the person's name, with or without nickname")
    city: str | None = None
    country: str | None = None
    office: str | None = None
    team: str | None = None
    gender: str | None = None
    work_status: WorkStatus | None = None
    salary_type: SalaryType | None = None
    salary_currency: Currency | None = None
    job_title_contains: str | None = None
    manager_name: str | None = Field(default=None, description="Part of the name of the person they report to")
    min_age: int | None = Field(default=None, ge=0)
    max_age: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_age_range(self) -> Self:
        if self.min_age is not None and self.max_age is not None and self.min_age > self.max_age:
            raise ValueError("min_age must not be greater than max_age")
        return self


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
