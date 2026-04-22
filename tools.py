"""Simulated external APIs and loop-control tools for AGEP.

Five functions are exposed to the agents:

1. `search_grocery_price`    (Executor)
2. `get_nutrition_info`      (Executor)
3. `check_allergy_conflict`  (Verifier)
4. `approve_plan`            (Verifier)       — escalates the loop on success
5. `flag_constraint_conflict` (Critic)        — escalates on impossible input

The price and nutrition databases are small, deliberate dicts. Fillers like
`almond flour` and `cashew cream` are marked with `allergens=["nuts"]` so the
hallucination-prevention loop has real gotchas to catch.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

_GROCERY_PRICES_USD: dict[str, dict[str, float]] = {
    # Proteins
    "salmon":            {"per_unit": 12.00, "unit": "lb"},
    "cod":               {"per_unit":  7.50, "unit": "lb"},
    "shrimp":            {"per_unit": 10.00, "unit": "lb"},
    "chicken breast":    {"per_unit":  5.00, "unit": "lb"},
    "ground beef":       {"per_unit":  6.00, "unit": "lb"},
    "tofu":              {"per_unit":  3.00, "unit": "lb"},
    "tempeh":            {"per_unit":  4.50, "unit": "lb"},
    "seitan":            {"per_unit":  6.00, "unit": "lb"},
    # Grains / legumes
    "quinoa":            {"per_unit":  4.00, "unit": "lb"},
    "brown rice":        {"per_unit":  2.00, "unit": "lb"},
    "white rice":        {"per_unit":  1.50, "unit": "lb"},
    "couscous":          {"per_unit":  3.00, "unit": "lb"},
    "farro":             {"per_unit":  4.50, "unit": "lb"},
    "pasta":             {"per_unit":  1.80, "unit": "lb"},
    "rolled oats":       {"per_unit":  1.50, "unit": "lb"},
    "lentils":           {"per_unit":  2.50, "unit": "lb"},
    "chickpeas":         {"per_unit":  2.00, "unit": "lb"},
    "black beans":       {"per_unit":  2.00, "unit": "lb"},
    "white beans":       {"per_unit":  2.20, "unit": "lb"},
    "kidney beans":      {"per_unit":  2.10, "unit": "lb"},
    # Oils & condiments
    "olive oil":         {"per_unit":  0.50, "unit": "tbsp"},
    "coconut oil":       {"per_unit":  0.60, "unit": "tbsp"},
    "sesame oil":        {"per_unit":  0.70, "unit": "tbsp"},
    "soy sauce":         {"per_unit":  0.10, "unit": "tbsp"},
    "miso paste":        {"per_unit":  0.50, "unit": "tbsp"},
    "vinegar":           {"per_unit":  0.15, "unit": "tbsp"},
    "honey":             {"per_unit":  0.40, "unit": "tbsp"},
    "maple syrup":       {"per_unit":  0.80, "unit": "tbsp"},
    # Aromatics
    "garlic":            {"per_unit":  0.25, "unit": "clove"},
    "lemon":             {"per_unit":  0.75, "unit": "count"},
    "lime":              {"per_unit":  0.50, "unit": "count"},
    "ginger":            {"per_unit":  0.25, "unit": "tbsp"},
    "onion":             {"per_unit":  0.75, "unit": "count"},
    "shallot":           {"per_unit":  0.80, "unit": "count"},
    "scallion":          {"per_unit":  0.25, "unit": "count"},
    # Vegetables
    "asparagus":         {"per_unit":  4.00, "unit": "lb"},
    "broccoli":          {"per_unit":  2.50, "unit": "lb"},
    "cauliflower":       {"per_unit":  2.80, "unit": "lb"},
    "spinach":           {"per_unit":  3.00, "unit": "lb"},
    "kale":              {"per_unit":  2.50, "unit": "lb"},
    "arugula":           {"per_unit":  4.00, "unit": "lb"},
    "mixed greens":      {"per_unit":  4.00, "unit": "lb"},
    "bell pepper":       {"per_unit":  1.50, "unit": "count"},
    "tomato":            {"per_unit":  1.25, "unit": "count"},
    "cherry tomatoes":   {"per_unit":  3.50, "unit": "lb"},
    "cucumber":          {"per_unit":  1.00, "unit": "count"},
    "zucchini":          {"per_unit":  1.50, "unit": "count"},
    "mushroom":          {"per_unit":  3.00, "unit": "lb"},
    "carrot":            {"per_unit":  1.20, "unit": "lb"},
    "celery":            {"per_unit":  1.80, "unit": "bunch"},
    "potato":            {"per_unit":  1.00, "unit": "lb"},
    "sweet potato":      {"per_unit":  1.50, "unit": "lb"},
    "corn":              {"per_unit":  0.80, "unit": "count"},
    "avocado":           {"per_unit":  1.75, "unit": "count"},
    "green beans":       {"per_unit":  2.50, "unit": "lb"},
    "eggplant":          {"per_unit":  2.00, "unit": "lb"},
    "radish":            {"per_unit":  2.00, "unit": "bunch"},
    # Herbs & spices (cheap, treat allergens as none)
    "basil":             {"per_unit":  2.00, "unit": "bunch"},
    "parsley":           {"per_unit":  1.50, "unit": "bunch"},
    "cilantro":          {"per_unit":  1.50, "unit": "bunch"},
    "dill":              {"per_unit":  1.80, "unit": "bunch"},
    "rosemary":          {"per_unit":  2.00, "unit": "bunch"},
    "thyme":             {"per_unit":  2.00, "unit": "bunch"},
    "oregano":           {"per_unit":  2.00, "unit": "bunch"},
    "cumin":             {"per_unit":  0.20, "unit": "tsp"},
    "smoked paprika":    {"per_unit":  0.25, "unit": "tsp"},
    "turmeric":          {"per_unit":  0.20, "unit": "tsp"},
    "black pepper":      {"per_unit":  0.15, "unit": "tsp"},
    "sea salt":          {"per_unit":  0.05, "unit": "tsp"},
    "bay leaf":          {"per_unit":  0.30, "unit": "count"},
    "chili flakes":      {"per_unit":  0.20, "unit": "tsp"},
    # Dairy
    "butter":            {"per_unit":  5.00, "unit": "lb"},
    "milk":              {"per_unit":  4.00, "unit": "gallon"},
    "yogurt":            {"per_unit":  5.00, "unit": "quart"},
    "eggs":              {"per_unit":  0.40, "unit": "count"},
    "parmesan":          {"per_unit": 18.00, "unit": "lb"},
    "feta":              {"per_unit": 10.00, "unit": "lb"},
    "cheddar":           {"per_unit":  8.00, "unit": "lb"},
    # Nuts (flagged for the nut-allergy loop)
    "pine nuts":         {"per_unit": 22.00, "unit": "lb"},
    "almond flour":      {"per_unit": 12.00, "unit": "lb"},
    "cashew cream":      {"per_unit":  9.00, "unit": "lb"},
    "walnuts":           {"per_unit": 14.00, "unit": "lb"},
    "almonds":           {"per_unit": 11.00, "unit": "lb"},
    # Pantry staples
    "nutritional yeast": {"per_unit": 10.00, "unit": "lb"},
    "coconut milk":      {"per_unit":  3.50, "unit": "can"},
    "chickpea flour":    {"per_unit":  4.00, "unit": "lb"},
    "vegetable broth":   {"per_unit":  0.20, "unit": "cup"},
    "tomato paste":      {"per_unit":  0.50, "unit": "tbsp"},
    "dijon mustard":     {"per_unit":  0.30, "unit": "tbsp"},
    "tahini":            {"per_unit":  0.60, "unit": "tbsp"},
    "capers":            {"per_unit":  0.40, "unit": "tbsp"},
    "olives":            {"per_unit":  6.00, "unit": "lb"},
}

_NUTRITION_DB: dict[str, dict[str, Any]] = {
    # Proteins
    "salmon":            {"calories": 208, "protein_g": 20, "is_vegan": False, "allergens": ["fish"]},
    "cod":               {"calories": 105, "protein_g": 23, "is_vegan": False, "allergens": ["fish"]},
    "shrimp":            {"calories":  99, "protein_g": 24, "is_vegan": False, "allergens": ["shellfish"]},
    "chicken breast":    {"calories": 165, "protein_g": 31, "is_vegan": False, "allergens": ["meat"]},
    "ground beef":       {"calories": 250, "protein_g": 26, "is_vegan": False, "allergens": ["meat"]},
    "tofu":               {"calories":  76, "protein_g":  8, "is_vegan": True,  "allergens": ["soy"]},
    "tempeh":             {"calories": 193, "protein_g": 19, "is_vegan": True,  "allergens": ["soy"]},
    "seitan":             {"calories": 370, "protein_g": 75, "is_vegan": True,  "allergens": ["gluten"]},
    # Grains / legumes
    "quinoa":            {"calories": 120, "protein_g":  4, "is_vegan": True,  "allergens": []},
    "brown rice":        {"calories": 112, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "white rice":        {"calories": 130, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "couscous":          {"calories": 112, "protein_g":  4, "is_vegan": True,  "allergens": ["gluten"]},
    "farro":             {"calories": 170, "protein_g":  6, "is_vegan": True,  "allergens": ["gluten"]},
    "pasta":             {"calories": 131, "protein_g":  5, "is_vegan": True,  "allergens": ["gluten"]},
    "rolled oats":       {"calories": 389, "protein_g": 17, "is_vegan": True,  "allergens": []},
    "lentils":           {"calories": 116, "protein_g":  9, "is_vegan": True,  "allergens": []},
    "chickpeas":         {"calories": 164, "protein_g":  9, "is_vegan": True,  "allergens": []},
    "black beans":       {"calories": 132, "protein_g":  9, "is_vegan": True,  "allergens": []},
    "white beans":       {"calories": 139, "protein_g":  9, "is_vegan": True,  "allergens": []},
    "kidney beans":      {"calories": 127, "protein_g":  9, "is_vegan": True,  "allergens": []},
    # Oils & condiments
    "olive oil":         {"calories": 119, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "coconut oil":       {"calories": 117, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "sesame oil":        {"calories": 120, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "soy sauce":         {"calories":  10, "protein_g":  2, "is_vegan": True,  "allergens": ["soy", "gluten"]},
    "miso paste":        {"calories":  33, "protein_g":  2, "is_vegan": True,  "allergens": ["soy"]},
    "vinegar":           {"calories":   3, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "honey":             {"calories":  64, "protein_g":  0, "is_vegan": False, "allergens": []},
    "maple syrup":       {"calories":  52, "protein_g":  0, "is_vegan": True,  "allergens": []},
    # Aromatics
    "garlic":            {"calories":   4, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "lemon":             {"calories":  17, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "lime":              {"calories":  20, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "ginger":            {"calories":   2, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "onion":             {"calories":  40, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "shallot":           {"calories":  72, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "scallion":          {"calories":  32, "protein_g":  2, "is_vegan": True,  "allergens": []},
    # Vegetables
    "asparagus":         {"calories":  20, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "broccoli":          {"calories":  34, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "cauliflower":       {"calories":  25, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "spinach":           {"calories":  23, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "kale":              {"calories":  35, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "arugula":           {"calories":  25, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "mixed greens":      {"calories":  20, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "bell pepper":       {"calories":  31, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "tomato":            {"calories":  18, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "cherry tomatoes":   {"calories":  27, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "cucumber":          {"calories":  16, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "zucchini":          {"calories":  17, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "mushroom":          {"calories":  22, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "carrot":            {"calories":  41, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "celery":            {"calories":  16, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "potato":            {"calories":  77, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "sweet potato":      {"calories":  86, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "corn":              {"calories":  96, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "avocado":           {"calories": 160, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "green beans":       {"calories":  31, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "eggplant":          {"calories":  25, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "radish":            {"calories":  16, "protein_g":  1, "is_vegan": True,  "allergens": []},
    # Herbs & spices
    "basil":             {"calories":  22, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "parsley":           {"calories":  36, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "cilantro":          {"calories":  23, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "dill":              {"calories":  43, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "rosemary":          {"calories": 131, "protein_g":  3, "is_vegan": True,  "allergens": []},
    "thyme":             {"calories": 101, "protein_g":  6, "is_vegan": True,  "allergens": []},
    "oregano":           {"calories": 265, "protein_g":  9, "is_vegan": True,  "allergens": []},
    "cumin":             {"calories": 375, "protein_g": 18, "is_vegan": True,  "allergens": []},
    "smoked paprika":    {"calories": 282, "protein_g": 14, "is_vegan": True,  "allergens": []},
    "turmeric":          {"calories": 354, "protein_g":  8, "is_vegan": True,  "allergens": []},
    "black pepper":      {"calories": 251, "protein_g": 10, "is_vegan": True,  "allergens": []},
    "sea salt":          {"calories":   0, "protein_g":  0, "is_vegan": True,  "allergens": []},
    "bay leaf":          {"calories": 313, "protein_g":  8, "is_vegan": True,  "allergens": []},
    "chili flakes":      {"calories": 282, "protein_g": 12, "is_vegan": True,  "allergens": []},
    # Dairy
    "butter":            {"calories": 717, "protein_g":  1, "is_vegan": False, "allergens": ["dairy"]},
    "milk":              {"calories":  42, "protein_g":  3, "is_vegan": False, "allergens": ["dairy"]},
    "yogurt":            {"calories":  59, "protein_g": 10, "is_vegan": False, "allergens": ["dairy"]},
    "eggs":              {"calories":  72, "protein_g":  6, "is_vegan": False, "allergens": ["egg"]},
    "parmesan":          {"calories": 431, "protein_g": 38, "is_vegan": False, "allergens": ["dairy"]},
    "feta":              {"calories": 264, "protein_g": 14, "is_vegan": False, "allergens": ["dairy"]},
    "cheddar":           {"calories": 403, "protein_g": 25, "is_vegan": False, "allergens": ["dairy"]},
    # Nuts (flagged for the nut-allergy loop)
    "pine nuts":         {"calories": 673, "protein_g": 14, "is_vegan": True,  "allergens": ["nuts"]},
    "almond flour":      {"calories": 640, "protein_g": 21, "is_vegan": True,  "allergens": ["nuts"]},
    "cashew cream":      {"calories": 350, "protein_g":  8, "is_vegan": True,  "allergens": ["nuts"]},
    "walnuts":           {"calories": 654, "protein_g": 15, "is_vegan": True,  "allergens": ["nuts"]},
    "almonds":           {"calories": 579, "protein_g": 21, "is_vegan": True,  "allergens": ["nuts"]},
    # Pantry staples
    "nutritional yeast": {"calories": 290, "protein_g": 50, "is_vegan": True,  "allergens": []},
    "coconut milk":      {"calories": 445, "protein_g":  5, "is_vegan": True,  "allergens": []},
    "chickpea flour":    {"calories": 356, "protein_g": 22, "is_vegan": True,  "allergens": []},
    "vegetable broth":   {"calories":  11, "protein_g":  1, "is_vegan": True,  "allergens": []},
    "tomato paste":      {"calories":  82, "protein_g":  4, "is_vegan": True,  "allergens": []},
    "dijon mustard":     {"calories":  60, "protein_g":  4, "is_vegan": True,  "allergens": []},
    "tahini":            {"calories": 595, "protein_g": 17, "is_vegan": True,  "allergens": []},
    "capers":            {"calories":  23, "protein_g":  2, "is_vegan": True,  "allergens": []},
    "olives":            {"calories": 115, "protein_g":  1, "is_vegan": True,  "allergens": []},
}

_RESTRICTION_BLOCKS: dict[str, list[str]] = {
    "vegan":            ["dairy", "egg", "meat", "fish"],
    "vegetarian":       ["meat", "fish"],
    "nut-allergy":      ["nuts"],
    "gluten-free":      ["gluten"],
    "dairy-free":       ["dairy"],
    "soy-allergy":      ["soy"],
    "shellfish-allergy":["shellfish"],
    "egg-allergy":      ["egg"],
    "fish-allergy":     ["fish"],
}

# Restrictions that must be satisfied by ALL dishes on the menu.
# Allergies — cross-contamination risk — fall into this bucket.
_GLOBAL_EXCLUSIONS: set[str] = {
    "nut-allergy",
    "gluten-free",
    "dairy-free",
    "soy-allergy",
    "shellfish-allergy",
    "egg-allergy",
    "fish-allergy",
}

# Restrictions that only require AT LEAST ONE compliant dish. Dietary
# preferences — vegan/vegetarian — fall into this bucket: a single dedicated
# dish on the menu is enough to serve the guest holding that preference.
_PER_GUEST_PREFERENCES: set[str] = {"vegan", "vegetarian"}


def _lookup_key(ingredient: str) -> str:
    """Case-insensitive, trimmed key for the in-memory databases."""
    return ingredient.strip().lower()


# Conversion factors from the given unit to the canonical unit family.
# Each entry maps alias → (canonical, factor) where canonical_qty = qty * factor.
_UNIT_CONVERSIONS: dict[str, tuple[str, float]] = {
    # Weight — canonical: lb
    "lb":   ("lb", 1.0),
    "lbs":  ("lb", 1.0),
    "pound":("lb", 1.0),
    "pounds":("lb", 1.0),
    "oz":   ("lb", 1.0 / 16.0),
    "ounce":("lb", 1.0 / 16.0),
    "ounces":("lb", 1.0 / 16.0),
    "g":    ("lb", 1.0 / 453.592),
    "gram": ("lb", 1.0 / 453.592),
    "grams":("lb", 1.0 / 453.592),
    "kg":   ("lb", 2.20462),
    "kilogram":("lb", 2.20462),
    "kilograms":("lb", 2.20462),
    # Volume — canonical: tbsp
    "tbsp": ("tbsp", 1.0),
    "tablespoon": ("tbsp", 1.0),
    "tablespoons": ("tbsp", 1.0),
    "tsp":  ("tsp", 1.0),
    "teaspoon": ("tsp", 1.0),
    "teaspoons": ("tsp", 1.0),
    "cup":  ("cup", 1.0),
    "cups": ("cup", 1.0),
    "ml":   ("tbsp", 1.0 / 14.787),
    "milliliter":("tbsp", 1.0 / 14.787),
    "l":    ("tbsp", 1000.0 / 14.787),
    "liter":("tbsp", 1000.0 / 14.787),
    "gallon":("gallon", 1.0),
    "quart":("quart", 1.0),
    "can":  ("can", 1.0),
    # Countables
    "count":("count", 1.0),
    "whole":("count", 1.0),
    "each": ("count", 1.0),
    "clove":("clove", 1.0),
    "cloves":("clove", 1.0),
    "bunch":("bunch", 1.0),
    "bunches":("bunch", 1.0),
}

# When the canonical unit on record is compatible with the unit the caller
# used, convert. cup ↔ tbsp uses 16 tbsp per cup.
_CROSS_UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    ("cup", "tbsp"): 16.0,
    ("tbsp", "cup"): 1.0 / 16.0,
    ("cup", "tsp"): 48.0,
    ("tsp", "cup"): 1.0 / 48.0,
    ("tbsp", "tsp"): 3.0,
    ("tsp", "tbsp"): 1.0 / 3.0,
}


def _convert_quantity(qty: float, from_unit: str, to_unit: str) -> float | None:
    """Convert ``qty`` from ``from_unit`` to ``to_unit``. Return None if incompatible."""
    from_key = from_unit.strip().lower()
    to_key = to_unit.strip().lower()
    from_canon = _UNIT_CONVERSIONS.get(from_key)
    if from_canon is None:
        return None
    canon_unit, canon_factor = from_canon
    canon_qty = qty * canon_factor
    if canon_unit == to_key:
        return canon_qty
    # Try cross-unit conversion within the same family.
    cross = _CROSS_UNIT_CONVERSIONS.get((canon_unit, to_key))
    if cross is not None:
        return canon_qty * cross
    return None


def list_known_ingredients() -> list[str]:
    """Return every ingredient name the Executor can price and the Verifier can audit.

    The Architect should only pick ingredients from this list — anything else
    will be flagged as unknown by the Verifier and force a re-plan.
    """
    return sorted(_NUTRITION_DB.keys())


def pantry_with_units() -> list[str]:
    """Return the pantry list as ``"name (unit)"`` strings so the Architect
    knows which unit each ingredient is priced in."""
    return [
        f"{name} ({row['unit']})"
        for name, row in sorted(_GROCERY_PRICES_USD.items())
    ]


def search_grocery_price(item: str, quantity: float, unit: str) -> dict[str, Any]:
    """Look up the grocery price for an ingredient.

    Simulates a grocery-API call. Returns estimated cost for the given
    quantity/unit, or `estimated_cost_usd=None` if the ingredient is unknown
    (so the agent flags the gap instead of hallucinating a price).

    Args:
        item: Ingredient name, e.g. "Salmon" or "quinoa".
        quantity: Numeric amount.
        unit: Unit of measure; should match the ingredient's canonical unit
              but the function returns a rough fallback conversion if not.

    Returns:
        {"item": str, "quantity": float, "unit": str,
         "estimated_cost_usd": float | None, "source": "simulated"}
    """
    row = _GROCERY_PRICES_USD.get(_lookup_key(item))
    if row is None:
        return {
            "item": item,
            "quantity": quantity,
            "unit": unit,
            "estimated_cost_usd": None,
            "source": "simulated",
            "note": "Unknown ingredient; price could not be determined.",
        }
    canonical_unit = row["unit"]
    canonical_qty = _convert_quantity(float(quantity), unit, canonical_unit)
    if canonical_qty is None:
        return {
            "item": item,
            "quantity": quantity,
            "unit": unit,
            "canonical_unit": canonical_unit,
            "unit_price_usd": row["per_unit"],
            "estimated_cost_usd": None,
            "source": "simulated",
            "note": (
                f"Unit '{unit}' is not convertible to canonical '{canonical_unit}'. "
                f"Re-request using {canonical_unit}."
            ),
        }
    cost = round(row["per_unit"] * canonical_qty, 2)
    return {
        "item": item,
        "quantity": quantity,
        "unit": unit,
        "canonical_unit": canonical_unit,
        "canonical_quantity": round(canonical_qty, 4),
        "unit_price_usd": row["per_unit"],
        "estimated_cost_usd": cost,
        "source": "simulated",
    }


def get_nutrition_info(ingredient: str) -> dict[str, Any]:
    """Return nutrition + allergen facts for an ingredient.

    Args:
        ingredient: Ingredient name.

    Returns:
        {"ingredient": str, "calories": int, "protein_g": float,
         "is_vegan": bool, "allergens": list[str], "known": bool}
    """
    row = _NUTRITION_DB.get(_lookup_key(ingredient))
    if row is None:
        return {
            "ingredient": ingredient,
            "known": False,
            "note": "Unknown ingredient; treat as unverified.",
        }
    return {
        "ingredient": ingredient,
        "known": True,
        "calories": row["calories"],
        "protein_g": row["protein_g"],
        "is_vegan": row["is_vegan"],
        "allergens": list(row["allergens"]),
    }


def check_allergy_conflict(
    ingredient: str, restrictions: list[str]
) -> dict[str, Any]:
    """Check whether an ingredient violates any of the given dietary restrictions.

    This is the ground-truth used by the Verifier agent. Cross-references an
    ingredient's allergen/vegan profile against the user's restrictions.

    Args:
        ingredient: The ingredient to audit.
        restrictions: List of dietary tags, e.g. ["vegan", "nut-allergy"].

    Returns:
        {"ingredient": str, "safe": bool, "reason": str,
         "violated_restrictions": list[str]}
    """
    info = get_nutrition_info(ingredient)
    if not info.get("known"):
        return {
            "ingredient": ingredient,
            "safe": False,
            "reason": "Unknown ingredient — cannot verify safety.",
            "violated_restrictions": [],
        }

    violated: list[str] = []
    for tag in restrictions:
        tag_key = tag.strip().lower()
        blocked_attrs = _RESTRICTION_BLOCKS.get(tag_key, [])
        allergen_hit = any(a in info["allergens"] for a in blocked_attrs)
        vegan_hit = tag_key == "vegan" and not info["is_vegan"]
        if allergen_hit or vegan_hit:
            violated.append(tag)

    if violated:
        return {
            "ingredient": ingredient,
            "safe": False,
            "reason": (
                f"'{ingredient}' violates {violated} "
                f"(allergens={info['allergens']}, is_vegan={info['is_vegan']})."
            ),
            "violated_restrictions": violated,
        }
    return {
        "ingredient": ingredient,
        "safe": True,
        "reason": "No conflicts detected.",
        "violated_restrictions": [],
    }


def price_menu_plan(plan_json: str) -> dict[str, Any]:
    """Price every ingredient in a menu plan in a single call.

    The Executor should prefer this batch tool over many `search_grocery_price`
    calls — one invocation fills in the entire grounded plan.

    Args:
        plan_json: JSON string of a MenuPlan (recipes[], total_cost_usd, notes).

    Returns:
        {"grounded_plan": <MenuPlan-shaped dict with all estimated_cost_usd
                            populated and total_cost_usd recomputed>,
         "unknown_ingredients": [list of ingredient names not in the DB],
         "source": "simulated"}
    """
    import json

    try:
        plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    except json.JSONDecodeError as e:
        return {"error": f"plan_json is not valid JSON: {e}"}

    recipes = plan.get("recipes", []) if isinstance(plan, dict) else []
    unknown: list[str] = []
    grand_total = 0.0

    for recipe in recipes:
        for ing in recipe.get("ingredients", []):
            priced = search_grocery_price(
                item=ing.get("name", ""),
                quantity=float(ing.get("quantity", 0) or 0),
                unit=ing.get("unit", ""),
            )
            cost = priced.get("estimated_cost_usd")
            ing["estimated_cost_usd"] = cost
            if cost is None:
                unknown.append(ing.get("name", "?"))
            else:
                grand_total += cost

    plan["total_cost_usd"] = round(grand_total, 2)
    return {
        "grounded_plan": plan,
        "unknown_ingredients": unknown,
        "source": "simulated",
    }


def audit_menu_plan(
    plan_json: str, restrictions: list[str]
) -> dict[str, Any]:
    """Audit a menu plan with correct whole-menu semantics.

    Distinguishes two restriction kinds:

    * **Global exclusions** (allergies like ``nut-allergy``, ``gluten-free``):
      NO dish may contain the allergen, since cross-contamination would affect
      the allergic guest regardless of which dish they ate.
    * **Per-guest preferences** (dietary choices like ``vegan``, ``vegetarian``):
      AT LEAST ONE dish on the menu must be fully compliant, so the guest has
      something to eat. Other dishes may be non-compliant.

    Args:
        plan_json: JSON string of a MenuPlan.
        restrictions: List of dietary tags.

    Returns:
        {"status": "approved" | "rejected",
         "violations": ["..."],
         "audited_count": int}
    """
    import json

    try:
        plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    except json.JSONDecodeError as e:
        return {
            "status": "rejected",
            "violations": [f"plan_json is not valid JSON: {e}"],
            "audited_count": 0,
        }

    recipes = plan.get("recipes", []) if isinstance(plan, dict) else []
    violations: list[str] = []
    audited = 0

    tags = [r.strip().lower() for r in restrictions]
    global_excl = [t for t in tags if t in _GLOBAL_EXCLUSIONS]
    per_guest = [t for t in tags if t in _PER_GUEST_PREFERENCES]

    # Global exclusions — any dish containing the allergen is a violation.
    for recipe in recipes:
        recipe_name = recipe.get("name", "<unnamed>")
        for ing in recipe.get("ingredients", []):
            audited += 1
            name = ing.get("name", "")
            info = get_nutrition_info(name)
            if not info.get("known"):
                violations.append(
                    f"'{name}' in '{recipe_name}': unknown ingredient — "
                    "cannot verify safety."
                )
                continue
            for tag in global_excl:
                blocked = _RESTRICTION_BLOCKS.get(tag, [])
                if any(a in info["allergens"] for a in blocked):
                    violations.append(
                        f"'{name}' in '{recipe_name}' contains "
                        f"{info['allergens']} — violates '{tag}' (affects every guest)."
                    )

    # Per-guest preferences — need at least one recipe that's fully compliant.
    for tag in per_guest:
        blocked = _RESTRICTION_BLOCKS.get(tag, [])
        has_compliant = False
        for recipe in recipes:
            compliant = True
            for ing in recipe.get("ingredients", []):
                info = get_nutrition_info(ing.get("name", ""))
                if not info.get("known"):
                    compliant = False
                    break
                if tag == "vegan" and not info["is_vegan"]:
                    compliant = False
                    break
                if any(a in info["allergens"] for a in blocked):
                    compliant = False
                    break
            if compliant:
                has_compliant = True
                break
        if not has_compliant:
            violations.append(
                f"No recipe is fully compliant with '{tag}'. "
                f"Add at least one dedicated '{tag}' dish."
            )

    return {
        "status": "approved" if not violations else "rejected",
        "violations": violations,
        "audited_count": audited,
    }


def approve_plan(tool_context: ToolContext) -> dict[str, Any]:
    """Approve the current plan and terminate the Plan-Act-Reflect loop.

    ONLY the Verifier should call this, and only when both the Critic's
    verdict and the Verifier's own audit have cleared.

    Returns:
        {"approved": True}
    """
    tool_context.state["plan_approved"] = True
    tool_context.actions.escalate = True
    return {"approved": True}


def flag_constraint_conflict(
    reason: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Abort the loop because the user's constraints are mathematically impossible.

    The Critic calls this when even the cheapest realistic plan cannot satisfy
    the budget, or when dietary restrictions leave no viable recipes.

    Args:
        reason: Human-readable explanation of the conflict.

    Returns:
        {"aborted": True, "reason": str}
    """
    tool_context.state["constraint_conflict"] = True
    tool_context.state["conflict_reason"] = reason
    tool_context.actions.escalate = True
    return {"aborted": True, "reason": reason}
