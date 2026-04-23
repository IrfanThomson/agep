"""AGEP — Autonomous Gourmet Event Planner CLI.

Entry point. Reads CLI args, runs a preflight sanity check, runs the
five-agent safety loop, runs the post-loop Chef and (optional) image
generator, then renders a printable booklet. Never requires an API key
when using the default Claude Code backend.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import sys
from typing import Any, Optional

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types
from pydantic import ValidationError

from state import CookingScript, EventConstraints, MenuPlan

APP_NAME = "AGEP"
USER_ID = "local-user"
SESSION_ID = "agep-session"
CHEF_SESSION_ID = "agep-chef-session"
MIN_BUDGET_PER_GUEST_USD = 8.0


class ConstraintConflictError(Exception):
    """Raised when the user's constraints are mathematically impossible."""


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, EventConstraints] = {
    "happy": EventConstraints(
        guests=6,
        budget_usd=200.0,
        required_ingredients=["Salmon", "Quinoa"],
        dietary_restrictions=["vegan", "nut-allergy"],
    ),
    "budget_crunch": EventConstraints(
        guests=6,
        budget_usd=60.0,
        required_ingredients=["Salmon", "Quinoa"],
        dietary_restrictions=["vegan", "nut-allergy"],
    ),
    "impossible": EventConstraints(
        guests=20,
        budget_usd=10.0,
        required_ingredients=[],
        dietary_restrictions=[],
    ),
    "hidden_gluten": EventConstraints(
        guests=6,
        budget_usd=150.0,
        required_ingredients=["rolled oats"],
        dietary_restrictions=["gluten-free"],
    ),
    # v3 additions — exercise the new Critic tools.
    "weeknight": EventConstraints(
        guests=4,
        budget_usd=80.0,
        required_ingredients=["chicken breast"],
        dietary_restrictions=[],
        kitchen_equipment=["stovetop", "sheet pan", "mixing bowl"],
        max_prep_minutes=45,
    ),
    "macro_floor": EventConstraints(
        guests=4,
        budget_usd=120.0,
        required_ingredients=["salmon"],
        dietary_restrictions=["nut-allergy"],
        calorie_floor_per_guest=700,
        protein_floor_per_guest_g=40,
    ),
}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight_check(constraints: EventConstraints) -> None:
    """Raise :class:`ConstraintConflictError` if the request is obviously impossible.

    This runs BEFORE any LLM call so we never waste tokens on hopeless
    scenarios. Adjust ``MIN_BUDGET_PER_GUEST_USD`` if your grocery market is
    unusually cheap or expensive.
    """
    if constraints.guests < 1:
        raise ConstraintConflictError(
            f"guests must be >= 1, got {constraints.guests}"
        )
    if constraints.budget_usd <= 0:
        raise ConstraintConflictError(
            f"budget_usd must be > 0, got {constraints.budget_usd}"
        )
    per_guest = constraints.budget_usd / constraints.guests
    if per_guest < MIN_BUDGET_PER_GUEST_USD:
        raise ConstraintConflictError(
            f"Budget of ${constraints.budget_usd:.2f} for {constraints.guests} "
            f"guests = ${per_guest:.2f}/guest, below the floor of "
            f"${MIN_BUDGET_PER_GUEST_USD:.2f}/guest. Constraints are "
            "mathematically infeasible."
        )


# ---------------------------------------------------------------------------
# Main loop driver
# ---------------------------------------------------------------------------


async def run_scenario(constraints: EventConstraints) -> dict[str, Any]:
    """Run AGEP end-to-end and return the final session state.

    Raises :class:`ConstraintConflictError` if preflight or mid-loop escalation
    detects mathematical infeasibility. Raises :class:`RuntimeError` if the
    loop exhausts ``MAX_ITERATIONS`` without approval.
    """
    preflight_check(constraints)

    # Import lazily so preflight failures don't pay the import cost (and so
    # AGEP_MODEL/AGEP_LLM env vars set by CLI are seen by get_model).
    from agents import build_loop

    loop_agent = build_loop()
    runner = InMemoryRunner(agent=loop_agent, app_name=APP_NAME)

    initial_state = {
        "user_constraints": constraints.model_dump(),
        "current_plan": {},
        "grounded_plan": {},
        "critique": {},
        "verification": {},
        "saboteur_report": {},
        "plan_approved": False,
        "constraint_conflict": False,
        "conflict_reason": "",
    }

    await _create_session(runner, SESSION_ID, initial_state)

    new_message = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                text=(
                    "Plan a menu that satisfies the constraints in "
                    "session state (see user_constraints)."
                )
            )
        ],
    )

    log_state: dict[str, Any] = {"iteration": 0, "last_author": None}
    async for event in runner.run_async(
        user_id=USER_ID, session_id=SESSION_ID, new_message=new_message
    ):
        _log_event(event, log_state)

    final_state = await _final_state(runner, SESSION_ID)
    final_state["_iterations"] = log_state["iteration"]

    if final_state.get("constraint_conflict"):
        raise ConstraintConflictError(
            final_state.get("conflict_reason") or "constraints infeasible"
        )
    if not final_state.get("plan_approved"):
        raise RuntimeError(
            "AGEP did not converge on an approved plan within the iteration "
            "budget. Inspect the event log above for rejection reasons."
        )
    return final_state


async def run_chef(grounded_plan: Any) -> dict[str, Any]:
    """Run the Chef agent on the approved grounded_plan.

    Uses its own runner so a malformed Chef output cannot retroactively
    invalidate or modify the approved menu.
    """
    from agents import build_chef

    chef = build_chef()
    runner = InMemoryRunner(agent=chef, app_name=APP_NAME)
    await _create_session(
        runner,
        CHEF_SESSION_ID,
        {"grounded_plan": grounded_plan, "cooking_script": {}},
    )
    new_message = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part(
                text=(
                    "Write step-by-step cooking instructions for every "
                    "recipe in session state grounded_plan."
                )
            )
        ],
    )
    async for event in runner.run_async(
        user_id=USER_ID, session_id=CHEF_SESSION_ID, new_message=new_message
    ):
        _log_chef_event(event)

    state = await _final_state(runner, CHEF_SESSION_ID)
    return state.get("cooking_script") or {}


async def _create_session(
    runner: InMemoryRunner, session_id: str, initial_state: dict[str, Any]
) -> None:
    result = runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
        state=initial_state,
    )
    if inspect.isawaitable(result):
        await result


async def _final_state(runner: InMemoryRunner, session_id: str) -> dict[str, Any]:
    result = runner.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id
    )
    session = await result if inspect.isawaitable(result) else result
    return dict(session.state) if session else {}


def _log_event(event: Any, log_state: dict[str, Any]) -> None:
    """Render one ADK event as a human-readable line.

    The underlying event stream is dense (truncated JSON blobs with every
    tool call and response). This formatter turns it into a scannable trace:
    per-iteration headers, one line per agent action, key fields pulled out
    of tool payloads.
    """
    author = getattr(event, "author", None) or "?"
    content = getattr(event, "content", None)
    if not content or not getattr(content, "parts", None):
        return

    # A fresh architect turn after the saboteur → new iteration.
    if author == "architect" and log_state["last_author"] in (None, "saboteur"):
        log_state["iteration"] += 1
        print(f"\nIteration {log_state['iteration']}")
    log_state["last_author"] = author

    for part in content.parts:
        if getattr(part, "text", None) and part.text.strip():
            line = _summarize_agent_text(author, part.text)
            if line:
                print(_fmt(author, "→", line))
        fc = getattr(part, "function_call", None)
        if fc and fc.name:
            print(_fmt(author, "→", _summarize_tool_call(fc.name, fc.args or {})))
        fr = getattr(part, "function_response", None)
        if fr and fr.name:
            print(_fmt(author, "←", _summarize_tool_response(fr.name, fr.response or {})))


def _log_chef_event(event: Any) -> None:
    author = getattr(event, "author", None) or "?"
    content = getattr(event, "content", None)
    if not content or not getattr(content, "parts", None):
        return
    for part in content.parts:
        if getattr(part, "text", None) and part.text.strip():
            payload = _parse_agent_json(part.text)
            if isinstance(payload, dict) and "recipes" in payload:
                n = len(payload.get("recipes") or [])
                print(_fmt(author, "→", f"wrote instructions for {n} recipe{'s' if n != 1 else ''}"))
            else:
                print(_fmt(author, "→", _short(" ".join(part.text.split()), 120)))


def _fmt(author: str, arrow: str, body: str) -> str:
    return f"  {author:<10s} {arrow} {body}"


def _summarize_agent_text(author: str, text: str) -> str | None:
    """Turn an agent's final text response into a one-line summary."""
    clean = text.strip()
    payload = _parse_agent_json(clean)

    if author == "architect" and isinstance(payload, dict) and "recipes" in payload:
        recipes = payload.get("recipes") or []
        return f"drafted {len(recipes)} recipe{'s' if len(recipes) != 1 else ''}"

    if author == "executor" and isinstance(payload, dict) and "recipes" in payload:
        total = payload.get("total_cost_usd")
        if isinstance(total, (int, float)):
            return f"returned priced plan (${float(total):.2f})"
        return "returned priced plan"

    if author == "critic" and isinstance(payload, dict) and "status" in payload:
        status = str(payload.get("status", "")).upper()
        reason = _short(str(payload.get("reason", "")), 90)
        deltas = payload.get("delta_instructions") or []
        tail = f" · {len(deltas)} delta-instruction{'s' if len(deltas) != 1 else ''}" if deltas else ""
        return f"{status} · \"{reason}\"{tail}"

    if author == "verifier" and isinstance(payload, dict) and "status" in payload:
        status = str(payload.get("status", "")).upper()
        violations = payload.get("violations") or []
        if not violations:
            return f"{status} · 0 violations"
        preview = _short(violations[0], 80)
        more = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
        return f"{status} · {len(violations)} violation{'s' if len(violations) != 1 else ''}{more}"

    if author == "saboteur" and isinstance(payload, dict) and "status" in payload:
        status_raw = str(payload.get("status", "")).strip().lower()
        if status_raw == "loophole_found":
            attack = _short(str(payload.get("attack", "")), 90)
            return f"LOOPHOLE · \"{attack}\""
        notes = _short(str(payload.get("notes", "")), 90)
        return f"CLEAR · \"{notes}\"" if notes else "CLEAR · no loophole found"

    # Fallback for the saboteur when JSON parsing fails (e.g. invalid escapes
    # in the notes field): look for the status strings directly in the text.
    if author == "saboteur":
        if "no_loophole_found" in clean:
            return "CLEAR · no loophole found"
        if "loophole_found" in clean:
            return "LOOPHOLE · (see raw JSON below)"

    # Not a known agent shape; show a short preview.
    compact = " ".join(clean.split())
    return _short(compact, 120)


def _summarize_tool_call(name: str, args: dict[str, Any]) -> str:
    if name == "price_menu_plan":
        plan = _parse_agent_json(args.get("plan_json", "") or "") or {}
        recipes = plan.get("recipes") or [] if isinstance(plan, dict) else []
        ingredient_count = sum(len(r.get("ingredients") or []) for r in recipes)
        return f"price_menu_plan({len(recipes)} recipes, {ingredient_count} ingredients)"

    if name == "audit_menu_plan":
        plan = _parse_agent_json(args.get("plan_json", "") or "") or {}
        recipes = plan.get("recipes") or [] if isinstance(plan, dict) else []
        ingredient_count = sum(len(r.get("ingredients") or []) for r in recipes)
        restrictions = args.get("restrictions") or []
        rlist = ", ".join(restrictions) if isinstance(restrictions, list) else str(restrictions)
        return f"audit_menu_plan({ingredient_count} ingredients, [{rlist}])"

    if name == "validate_nutrition_macros":
        guests = args.get("guests", "?")
        cf = args.get("calorie_floor_per_guest", 0)
        pf = args.get("protein_floor_per_guest_g", 0)
        return f"validate_nutrition_macros(guests={guests}, kcal_floor={cf}, protein_floor={pf}g)"

    if name == "check_equipment":
        avail = args.get("available_equipment") or []
        rlist = ", ".join(avail) if isinstance(avail, list) else str(avail)
        return f"check_equipment(available=[{rlist}])"

    if name == "validate_prep_time":
        return f"validate_prep_time(max={args.get('max_prep_minutes', 0)} min)"

    if name == "approve_plan":
        return "approve_plan — loop exits"

    if name == "flag_constraint_conflict":
        reason = _short(str(args.get("reason", "")), 100)
        return f"flag_constraint_conflict · \"{reason}\""

    return f"{name}({_short(json.dumps(args), 100)})"


def _summarize_tool_response(name: str, response: dict[str, Any]) -> str:
    if name == "price_menu_plan":
        plan = response.get("grounded_plan") or {}
        total = plan.get("total_cost_usd") if isinstance(plan, dict) else None
        unknown = response.get("unknown_ingredients") or []
        total_str = f"${float(total):.2f}" if isinstance(total, (int, float)) else "?"
        source = response.get("source", "?")
        return f"{total_str} total · {len(unknown)} unknown · source={source}"

    if name == "audit_menu_plan":
        status = str(response.get("status", "")).upper()
        violations = response.get("violations") or []
        audited = response.get("audited_count", "?")
        if not violations:
            return f"{status} · {audited} ingredients checked · 0 violations"
        return f"{status} · {len(violations)} violation{'s' if len(violations) != 1 else ''} of {audited} checked"

    if name == "validate_nutrition_macros":
        status = str(response.get("status", "")).upper()
        kcal = response.get("calories_per_guest")
        protein = response.get("protein_g_per_guest")
        kcal_s = f"{kcal:.0f} kcal" if isinstance(kcal, (int, float)) else "? kcal"
        protein_s = f"{protein:.1f} g protein" if isinstance(protein, (int, float)) else "? g"
        return f"{status} · {kcal_s}/guest · {protein_s}/guest"

    if name == "check_equipment":
        status = str(response.get("status", "")).upper()
        missing = response.get("missing_equipment") or []
        if not missing:
            return f"{status} · all equipment available"
        return f"{status} · missing {missing}"

    if name == "validate_prep_time":
        status = str(response.get("status", "")).upper()
        total = response.get("total_prep_minutes", "?")
        return f"{status} · {total} min total prep"

    if name == "approve_plan":
        return "approved=True"

    if name == "flag_constraint_conflict":
        return "aborted=True"

    return _short(json.dumps(response), 120)


def _parse_agent_json(text: str) -> Any:
    """Best-effort JSON extraction — strip markdown fences, grab first object."""
    if not text:
        return None
    stripped = text.strip()
    # Strip ``` fences.
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[: -3]
        stripped = stripped.strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback: first balanced {...}.
    depth = 0
    start = stripped.find("{")
    if start == -1:
        return None
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stripped[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _short(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


_RULE = "━" * 60
_THIN = "─" * 60


def _print_header(scenario: str, backend: str, constraints: "EventConstraints") -> None:
    req = ", ".join(constraints.required_ingredients) or "—"
    res = ", ".join(constraints.dietary_restrictions) or "—"
    print(_RULE)
    print(f" AGEP · scenario '{scenario}' · backend {backend}")
    print(
        f" {constraints.guests} guests · ${constraints.budget_usd:.0f} budget"
        f" · required: {req} · restrictions: {res}"
    )
    extras: list[str] = []
    if constraints.kitchen_equipment:
        extras.append(f"equipment: {', '.join(constraints.kitchen_equipment)}")
    if constraints.max_prep_minutes:
        extras.append(f"max prep: {constraints.max_prep_minutes} min")
    if constraints.calorie_floor_per_guest:
        extras.append(f"kcal floor: {constraints.calorie_floor_per_guest}/guest")
    if constraints.protein_floor_per_guest_g:
        extras.append(f"protein floor: {constraints.protein_floor_per_guest_g} g/guest")
    if extras:
        print(f" {' · '.join(extras)}")
    print(_RULE)


def _print_receipt(
    final_state: dict[str, Any],
    iterations: int,
    cooking_script: dict[str, Any] | None = None,
    image_result: dict[str, Any] | None = None,
) -> None:
    raw_plan = final_state.get("grounded_plan") or final_state.get("current_plan")
    print()
    print(_THIN)
    plan = _parse_menu_plan(raw_plan)
    if plan is None:
        print(" Plan approved but the final payload did not parse as MenuPlan.")
        print(_THIN)
        print(f"Raw state: {raw_plan!r}")
        return
    iter_word = "iteration" if iterations == 1 else "iterations"
    total_prep = sum((r.prep_minutes or 0) for r in plan.recipes)
    prep_tail = f" · {total_prep} min prep" if total_prep else ""
    print(f" Plan approved in {iterations} {iter_word} · ${plan.total_cost_usd:.2f}{prep_tail}")
    print(_THIN)
    if plan.notes:
        print(f"\n{plan.notes}\n")
    instructions_by_name = _instructions_by_name(cooking_script)
    images_by_name = _images_by_name(image_result)
    print("MENU")
    for r in plan.recipes:
        tags = ", ".join(r.accommodates) if r.accommodates else "—"
        meta_bits: list[str] = []
        if r.prep_minutes is not None:
            meta_bits.append(f"{r.prep_minutes} min")
        if r.required_equipment:
            meta_bits.append(", ".join(r.required_equipment))
        meta = f" · {' · '.join(meta_bits)}" if meta_bits else ""
        print(f"\n• {r.name} (serves {r.serves} · accommodates: {tags}{meta})")
        for ing in r.ingredients:
            cost = (
                f"${ing.estimated_cost_usd:.2f}"
                if ing.estimated_cost_usd is not None
                else "$?.??"
            )
            print(f"    - {ing.quantity} {ing.unit} {ing.name:<20s} {cost}")
        steps = instructions_by_name.get(_name_key(r.name))
        if steps:
            print("    Instructions:")
            for i, step in enumerate(steps, 1):
                print(f"      {i}. {step}")
        img = images_by_name.get(_name_key(r.name))
        if img:
            print(f"    Image: {img}")

    _print_image_status(image_result)


def _name_key(name: str) -> str:
    """Normalize a recipe name for matching: lowercase, alphanumeric only.

    Tolerates typos like 'Sauté ed' vs 'Sautéed' and stray punctuation.
    """
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def _instructions_by_name(cooking_script: Any) -> dict[str, list[str]]:
    if not isinstance(cooking_script, dict):
        return {}
    out: dict[str, list[str]] = {}
    for entry in cooking_script.get("recipes") or []:
        key = _name_key(entry.get("recipe_name", ""))
        steps = entry.get("steps") or []
        if key and isinstance(steps, list):
            out[key] = [str(s) for s in steps if s]
    return out


def _images_by_name(image_result: Any) -> dict[str, str]:
    if not isinstance(image_result, dict):
        return {}
    out: dict[str, str] = {}
    for img in image_result.get("images") or []:
        key = _name_key(img.get("recipe_name", ""))
        path = str(img.get("path", "")).strip()
        if key and path:
            out[key] = path
    return out


def _print_image_status(image_result: Any) -> None:
    if not isinstance(image_result, dict):
        return
    status = image_result.get("status")
    if status == "ok":
        print(f"\n[images] generated {len(image_result.get('images') or [])} dish image(s) using {image_result.get('model')}")
    elif status == "skipped":
        print(f"\n[images] skipped: {image_result.get('reason', '')}")
    elif status == "error":
        print(f"\n[images] error: {image_result.get('reason', '')}")


def _parse_menu_plan(raw: Any) -> Optional[MenuPlan]:
    if isinstance(raw, MenuPlan):
        return raw
    if isinstance(raw, dict):
        try:
            return MenuPlan.model_validate(raw)
        except ValidationError:
            return None
    if isinstance(raw, str):
        try:
            return MenuPlan.model_validate_json(raw)
        except ValidationError:
            try:
                return MenuPlan.model_validate(json.loads(raw))
            except (ValidationError, json.JSONDecodeError):
                return None
    return None


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return text or "scenario"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agep",
        description="Autonomous Gourmet Event Planner — a Plan-Act-Reflect demo "
        "on Google ADK with a pluggable Claude Code / Gemini / Anthropic backend.",
    )
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS.keys()),
        default="happy",
        help="Which built-in scenario to run (default: happy).",
    )
    parser.add_argument(
        "--backend",
        choices=("claude-code", "anthropic", "gemini"),
        default=None,
        help="Override AGEP_LLM env var. Default: claude-code.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override AGEP_MODEL env var. Backend-specific model id or alias.",
    )
    parser.add_argument(
        "--no-chef",
        action="store_true",
        help="Skip the post-loop Chef stage (cooking instructions).",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Skip the post-loop image generation stage.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show DEBUG-level logs from ADK and the LLM adapter.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    args = _build_parser().parse_args(argv)

    if args.backend:
        os.environ["AGEP_LLM"] = args.backend
    if args.model:
        os.environ["AGEP_MODEL"] = args.model

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    constraints = SCENARIOS[args.scenario]
    _print_header(args.scenario, os.getenv("AGEP_LLM", "claude-code"), constraints)

    try:
        final_state = asyncio.run(run_scenario(constraints))
    except ConstraintConflictError as e:
        print(f"\nConstraintConflictError: {e}")
        return 2
    except RuntimeError as e:
        print(f"\n{e}")
        return 3

    cooking_script: dict[str, Any] = {}
    image_result: dict[str, Any] | None = None

    grounded_plan = final_state.get("grounded_plan") or final_state.get("current_plan")

    if not args.no_chef:
        print("\nChef")
        try:
            chef_payload = asyncio.run(run_chef(grounded_plan))
            cooking_script = _normalize_cooking_script(chef_payload)
        except Exception as e:
            print(f"  chef       → ERROR · {e}")

    if not args.no_images:
        from images import generate_dish_images

        plan = _parse_menu_plan(grounded_plan)
        if plan is not None:
            out_dir = os.path.join("generated", _slugify(args.scenario))
            recipes_for_images = [r.model_dump() for r in plan.recipes]
            image_result = generate_dish_images(recipes_for_images, out_dir)

    _print_receipt(
        final_state,
        final_state.get("_iterations", 0),
        cooking_script=cooking_script,
        image_result=image_result,
    )
    return 0


def _normalize_cooking_script(payload: Any) -> dict[str, Any]:
    """Coerce the Chef's output_key into a CookingScript-shaped dict.

    The Chef often wraps its JSON in ``` ```json ``` fences; the underlying
    parser strips those before json.loads.
    """
    if isinstance(payload, str):
        payload = _parse_agent_json(payload) or {}
    if not isinstance(payload, dict):
        return {}
    try:
        validated = CookingScript.model_validate(payload)
        return validated.model_dump()
    except ValidationError:
        return payload  # best-effort — let the renderer try


if __name__ == "__main__":
    sys.exit(main())
