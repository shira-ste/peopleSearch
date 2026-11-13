"""Live currency conversion, used only when a tool is asked to convert."""

import time
from typing import Literal, TypedDict

import httpx

Currency = Literal["USD", "GBP", "ILS"]
TTL_SECONDS = 3600


class FxRates(TypedDict):
    date: str
    rates: dict[str, float]  # units of each currency per 1 unit of the base currency


_cache: dict[str, tuple[float, FxRates]] = {}


def get_rates(base: Currency) -> FxRates:
    """Fetch today's ECB rates from Frankfurter (no API key), cached for an hour."""
    cached = _cache.get(base)
    if cached and time.time() - cached[0] < TTL_SECONDS:
        return cached[1]
    resp = httpx.get(
        "https://api.frankfurter.dev/v1/latest",
        params={"from": base, "to": "USD,GBP,ILS"},
        timeout=5,
        follow_redirects=True,
    )
    resp.raise_for_status()
    data = resp.json()
    rates: FxRates = {"date": str(data["date"]), "rates": {**data["rates"], base: 1.0}}
    _cache[base] = (time.time(), rates)
    return rates


def convert(amount: float, currency: str, fx: FxRates) -> float:
    return round(amount / fx["rates"][currency], 2)
