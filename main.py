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

    iteration = 0
    async for event in runner.run_async(
        user_id=USER_ID, session_id=SESSION_ID, new_message=new_message
    ):
        iteration = _log_event(event, iteration)

    final_state = await _final_state(runner)

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


def _log_event(event: Any, prior_iter: int) -> int:
    author = getattr(event, "author", None) or "?"
    content = getattr(event, "content", None)
    if content and getattr(content, "parts", None):
        for part in content.parts:
            text = getattr(part, "text", None)
            if text and text.strip():
                preview = text.strip().replace("\n", " ")
                if len(preview) > 160:
                    preview = preview[:157] + "..."
                print(f"  [{author}] {preview}")
                continue
            fc = getattr(part, "function_call", None)
            if fc and fc.name:
                print(
                    f"  [{author}] → tool {fc.name}({json.dumps(fc.args or {})[:120]})"
                )
                continue
            fr = getattr(part, "function_response", None)
            if fr and fr.name:
                preview = json.dumps(fr.response or {})
                if len(preview) > 120:
                    preview = preview[:117] + "..."
                print(f"  [{author}] ← {fr.name} = {preview}")
    return prior_iter


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


def _print_receipt(final_state: dict[str, Any]) -> None:
    raw_plan = final_state.get("grounded_plan") or final_state.get("current_plan")
    print("\n" + "=" * 60)
    print("AGEP RESULT — plan approved")
    print("=" * 60)
    plan = _parse_menu_plan(raw_plan)
    if plan is None:
        print("(Agents approved, but the final plan did not parse as MenuPlan.)")
        print(f"Raw state: {raw_plan!r}")
        return
    print(f"Total cost: ${plan.total_cost_usd:.2f}")
    print(f"Notes: {plan.notes}")
    for r in plan.recipes:
        print(f"\n• {r.name} (serves {r.serves}) — accommodates: {r.accommodates}")
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
    print(
        f"Running AGEP scenario '{args.scenario}' with backend "
        f"'{os.getenv('AGEP_LLM', 'claude-code')}'"
    )
    print(f"Constraints: {constraints.model_dump_json()}\n")

    try:
        final_state = asyncio.run(run_scenario(constraints))
    except ConstraintConflictError as e:
        print(f"\n❌ ConstraintConflictError: {e}")
        return 2
    except RuntimeError as e:
        print(f"\n❌ {e}")
        return 3

    _print_receipt(final_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
