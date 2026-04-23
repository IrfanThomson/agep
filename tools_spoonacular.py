"""Spoonacular grocery-API adapter for AGEP.

Optional drop-in replacement for ``tools._price_menu_plan_simulated``. Enable
with ``AGEP_GROCERY=spoonacular`` and ``SPOONACULAR_API_KEY=...``.

Two endpoints are used:

* ``GET /food/ingredients/search?query=<name>`` → resolves a free-text
  ingredient name to a Spoonacular numeric ID.
* ``GET /food/ingredients/{id}/information?amount=<n>&unit=<unit>`` → returns
  ``estimatedCost: {value, unit}`` where ``value`` is in **US cents**.

The free tier is 150 requests/day. We minimize calls with two layers of
caching:

* In-process ``_NAME_TO_ID`` cache — a search per unique ingredient name
  costs one request, regardless of how many times the name appears.
* In-process ``_PRICE_CACHE`` keyed by ``(id, qty, unit)`` — identical
  pricing requests within a session are served from memory.

If the network call or the schema lookup fails for an ingredient, that
ingredient is reported as ``unknown`` (mirroring the simulated backend's
contract) so the agent loop continues gracefully.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_BASE = "https://api.spoonacular.com"
_NAME_TO_ID: dict[str, int | None] = {}
_PRICE_CACHE: dict[tuple[int, float, str], float | None] = {}


def _api_key() -> str:
    key = os.getenv("SPOONACULAR_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "AGEP_GROCERY=spoonacular but SPOONACULAR_API_KEY is not set. "
            "Get a free key at https://spoonacular.com/food-api/console"
        )
    return key


def _http_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Issue a GET against the Spoonacular API.

    Imported lazily so the simulated default has zero network dependency.
    """
    try:
        import requests  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "AGEP_GROCERY=spoonacular requires 'requests'. "
            "Install with: pip install requests"
        ) from e

    params = {**params, "apiKey": _api_key()}
    resp = requests.get(f"{_BASE}{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _resolve_id(name: str) -> int | None:
    """Search Spoonacular for an ingredient name, return its numeric ID."""
    key = name.strip().lower()
    if key in _NAME_TO_ID:
        return _NAME_TO_ID[key]
    try:
        data = _http_get("/food/ingredients/search", {"query": key, "number": 1})
        results = data.get("results") or []
        ing_id = int(results[0]["id"]) if results else None
    except Exception as e:
        logger.warning("Spoonacular search failed for %r: %s", name, e)
        ing_id = None
    _NAME_TO_ID[key] = ing_id
    return ing_id


def _price_one(name: str, qty: float, unit: str) -> float | None:
    """Return USD cost for one ingredient line, or ``None`` if unknown."""
    ing_id = _resolve_id(name)
    if ing_id is None:
        return None
    cache_key = (ing_id, qty, unit.strip().lower())
    if cache_key in _PRICE_CACHE:
        return _PRICE_CACHE[cache_key]
    try:
        data = _http_get(
            f"/food/ingredients/{ing_id}/information",
            {"amount": qty, "unit": unit},
        )
        ec = data.get("estimatedCost") or {}
        cents = ec.get("value")
        usd = round(float(cents) / 100.0, 2) if cents is not None else None
    except Exception as e:
        logger.warning("Spoonacular pricing failed for %r: %s", name, e)
        usd = None
    _PRICE_CACHE[cache_key] = usd
    return usd


def price_menu_plan_spoonacular(plan_json: str) -> dict[str, Any]:
    """Live Spoonacular pricing for a MenuPlan.

    Mirrors the contract of ``tools._price_menu_plan_simulated`` so the
    Executor agent does not need to know which backend is active.
    """
    try:
        plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    except json.JSONDecodeError as e:
        return {"error": f"plan_json is not valid JSON: {e}"}

    recipes = plan.get("recipes", []) if isinstance(plan, dict) else []
    unknown: list[str] = []
    grand_total = 0.0

    for recipe in recipes:
        for ing in recipe.get("ingredients", []):
            name = ing.get("name", "")
            qty = float(ing.get("quantity", 0) or 0)
            unit = ing.get("unit", "")
            cost = _price_one(name, qty, unit)
            ing["estimated_cost_usd"] = cost
            if cost is None:
                unknown.append(name or "?")
            else:
                grand_total += cost

    plan["total_cost_usd"] = round(grand_total, 2)
    return {
        "grounded_plan": plan,
        "unknown_ingredients": unknown,
        "source": "spoonacular",
    }
