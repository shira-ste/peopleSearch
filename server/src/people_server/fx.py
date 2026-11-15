"""Live currency conversion, used only when a tool is asked to convert.

Rates are the European Central Bank's daily reference rates, served by the free Frankfurter API
(no key needed). Conversion is optional: with FX_ENABLED=false the tools report original currencies only.
"""

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
    """Fetch today's rates, cached for an hour. Raises httpx.HTTPError, KeyError or ValueError on failure."""
    cached = _cache.get(base)
    if cached and time.monotonic() - cached[0] < TTL_SECONDS:  # monotonic: unaffected by clock changes
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
    _cache[base] = (time.monotonic(), rates)
    return rates


def convert(amount: float, currency: str | None, fx: FxRates) -> float | None:
    """Convert to the rates' base currency, or None if the currency is missing or has no rate."""
    rate = fx["rates"].get(currency or "")
    return round(amount / rate, 2) if rate else None
