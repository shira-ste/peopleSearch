"""Live currency conversion, used only when a tool is asked to convert."""

import time

import httpx

_cache: dict[str, tuple[float, dict]] = {}
TTL_SECONDS = 3600


def get_rates(base: str) -> dict:
    """Return {"date": ..., "rates": {currency: units per 1 `base`}} from the ECB via Frankfurter."""
    cached = _cache.get(base)
    if cached and time.time() - cached[0] < TTL_SECONDS:
        return cached[1]
    resp = httpx.get("https://api.frankfurter.dev/v1/latest", params={"from": base, "to": "USD,GBP,ILS"}, timeout=5, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    result = {"date": data["date"], "rates": {**data["rates"], base: 1.0}}
    _cache[base] = (time.time(), result)
    return result


def convert(amount: float, currency: str, fx: dict) -> float:
    return round(amount / fx["rates"][currency], 2)
