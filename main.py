"""AGEP — Autonomous Gourmet Event Planner CLI.

Entry point. Reads CLI args, runs a preflight sanity check, constructs the
loop, streams events, and reports the outcome. Never requires an API key when
using the default Claude Code backend.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
from typing import Any, Optional

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types
from pydantic import ValidationError

from state import EventConstraints, MenuPlan

APP_NAME = "AGEP"
USER_ID = "local-user"
SESSION_ID = "agep-session"
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

    await _create_session(runner, initial_state)

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

    final_state = await _final_state(runner)
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


async def _create_session(runner: InMemoryRunner, initial_state: dict[str, Any]) -> None:
    result = runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
        state=initial_state,
    )
    if inspect.isawaitable(result):
        await result


async def _final_state(runner: InMemoryRunner) -> dict[str, Any]:
    result = runner.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
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
        return f"{total_str} total · {len(unknown)} unknown ingredient{'s' if len(unknown) != 1 else ''}"

    if name == "audit_menu_plan":
        status = str(response.get("status", "")).upper()
        violations = response.get("violations") or []
        audited = response.get("audited_count", "?")
        if not violations:
            return f"{status} · {audited} ingredients checked · 0 violations"
        return f"{status} · {len(violations)} violation{'s' if len(violations) != 1 else ''} of {audited} checked"

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
    print(_RULE)


def _print_receipt(final_state: dict[str, Any], iterations: int) -> None:
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
    print(f" Plan approved in {iterations} {iter_word} · ${plan.total_cost_usd:.2f}")
    print(_THIN)
    if plan.notes:
        print(f"\n{plan.notes}\n")
    print("MENU")
    for r in plan.recipes:
        tags = ", ".join(r.accommodates) if r.accommodates else "—"
        print(f"\n• {r.name} (serves {r.serves} · accommodates: {tags})")
        for ing in r.ingredients:
            cost = (
                f"${ing.estimated_cost_usd:.2f}"
                if ing.estimated_cost_usd is not None
                else "$?.??"
            )
            print(f"    - {ing.quantity} {ing.unit} {ing.name:<20s} {cost}")


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

    _print_receipt(final_state, final_state.get("_iterations", 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
