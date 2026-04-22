"""Agent definitions for AGEP.

Four specialized :class:`LlmAgent`s plus the outer :class:`LoopAgent`:

* Architect (temp 0.7) — strategic decomposition into a JSON menu plan
* Executor  (temp 0.1) — tool-grounded pricing of the plan
* Critic    (temp 0.0) — budget & macro validation; emits delta instructions
* Verifier  (temp 0.0) — zero-trust ingredient audit; the only agent that
                         terminates the loop via ``approve_plan``

The loop runs at most 5 iterations. Inter-agent communication is via
``session.state`` — each agent declares an ``output_key`` and reads prior
outputs via ``{placeholder}`` substitution in its instruction.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, LoopAgent
from google.genai import types as genai_types

from llm import get_model
from tools import (
    approve_plan,
    audit_menu_plan,
    flag_constraint_conflict,
    pantry_with_units,
    price_menu_plan,
)

_PANTRY_LINE = ", ".join(pantry_with_units())

MAX_ITERATIONS = 5


def _cfg(temperature: float) -> genai_types.GenerateContentConfig:
    return genai_types.GenerateContentConfig(temperature=temperature)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

ARCHITECT_PROMPT = f"""\
You are the Architect — the strategic planner for AGEP.

Inputs (already in session state):
- user_constraints: {{user_constraints}}
- critique (from the Critic, may be empty on iteration 1): {{critique}}
- verification (from the Verifier, may be empty on iteration 1): {{verification}}

Your job: produce a JSON menu plan that satisfies the constraints. You have NO
tools — just compose the plan from your culinary knowledge.

Semantics of dietary restrictions (important):
- Allergies like 'nut-allergy', 'gluten-free', 'dairy-free' are GLOBAL: NO
  dish on the menu may contain the allergen (cross-contamination risk).
- Preferences like 'vegan' or 'vegetarian' are PER-GUEST: at LEAST ONE dish
  must be fully compliant; the other dishes may freely be non-compliant.

Rules:
1. Honour every entry in required_ingredients — they must appear in at least
   one recipe.
2. For every allergy restriction, ensure EVERY recipe avoids the allergen.
3. For every preference, ensure AT LEAST ONE recipe is fully compliant.
4. If critique.status == "rejected", apply EVERY delta_instruction.
5. If verification.status == "rejected", swap out every listed violating
   ingredient for a safe alternative.
6. Use ONLY ingredients from the pantry below — anything else will be flagged
   as unknown and rejected. Match names exactly (case-insensitive is fine).
7. Use the unit shown in parentheses for each pantry ingredient. You may use
   common unit aliases (lb/lbs/pound, oz, g, kg, tbsp, tsp, cup, ml, count,
   clove, bunch, can) — the pricing system converts between compatible units.
   Do NOT mix incompatible units (e.g. 'kg' for an ingredient priced per 'count').

Available pantry — "name (priced per unit)":
{_PANTRY_LINE}

Respond with ONLY a JSON object matching this schema, no prose, no fences:
{{{{
  "recipes": [
    {{{{
      "name": "string",
      "serves": integer,
      "ingredients": [
        {{{{"name": "string from pantry", "quantity": number, "unit": "string", "estimated_cost_usd": null}}}}
      ],
      "accommodates": ["dietary tags this dish satisfies"]
    }}}}
  ],
  "total_cost_usd": 0,
  "notes": "string explaining key choices"
}}}}

Leave estimated_cost_usd as null and total_cost_usd as 0 — the Executor will
fill those in.
"""


EXECUTOR_PROMPT = """\
You are the Executor — you ground the Architect's plan in real grocery prices.

Input:
- current_plan: {current_plan}
- user_constraints: {user_constraints}

Your job (EXACTLY two steps):
1. Call `price_menu_plan` with plan_json set to the current_plan's JSON. This
   prices every ingredient in one shot and returns a `grounded_plan` field.
2. Then respond with ONLY the grounded_plan JSON (recipes[], total_cost_usd,
   notes), verbatim from the tool result. Do NOT wrap it in fences or prose.
   If the tool flagged unknown_ingredients, mention them in the plan's notes
   field but keep their estimated_cost_usd as null.

Do NOT call any other function. Do NOT invent prices. Two calls total: the
function, then your JSON-only response.
"""


CRITIC_PROMPT = """\
You are the Critic — zero-tolerance budget & macro validator.

Inputs:
- grounded_plan: {grounded_plan}
- user_constraints: {user_constraints}

Your job:
1. Compute total_cost_usd from the grounded_plan. If it exceeds
   user_constraints.budget_usd, the plan is REJECTED.
2. Sanity-check portion sizes: every guest should get at least ~400 kcal of
   food (rough heuristic). If protein/calorie content is obviously too low,
   REJECT.
3. If you believe the constraints are MATHEMATICALLY IMPOSSIBLE (e.g. even
   the cheapest reasonable menu would exceed the budget), call the tool
   `flag_constraint_conflict` with a clear reason. The loop will abort.
4. Otherwise emit a JSON Critique:
   - status: "approved" if the plan is within budget and sensible, else "rejected"
   - reason: one-sentence summary
   - delta_instructions: concrete changes (e.g. "Swap salmon for cod to save
     $30", "Reduce quinoa from 3 lb to 2 lb"). Empty list if approved.

Respond with ONLY a JSON object in this shape, no prose, no fences:
{{"status": "approved|rejected", "reason": "...", "delta_instructions": [...]}}

Do NOT call `approve_plan` — that is the Verifier's responsibility.
"""


VERIFIER_PROMPT = """\
You are the Verifier — Zero-Trust ingredient safety auditor. Hallucinations
stop here.

Inputs:
- grounded_plan: {grounded_plan}
- critique: {critique}
- user_constraints: {user_constraints}

Your job (EXACTLY these steps):
1. Call `audit_menu_plan` with plan_json set to grounded_plan's JSON and
   restrictions set to user_constraints.dietary_restrictions.
2. Inspect the tool result:
   - If result.status == "approved" AND critique.status == "approved": call
     `approve_plan` — this terminates the loop.
   - Otherwise: do NOT call approve_plan.
3. Respond with ONLY a JSON SafetyAudit reflecting YOUR OWN audit:
   {{"status": "<mirror result.status from audit_menu_plan>",
     "violations": <mirror result.violations from audit_menu_plan>}}

Your SafetyAudit describes your own ingredient audit. Do NOT fold in the
Critic's budget complaints — those belong to the Critic. If your audit is
clean, emit {{"status": "approved", "violations": []}} even if the Critic
rejected the plan.

Never emit status=approved with a non-empty violations list — that defeats
the entire purpose of this agent.
"""


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def build_agents() -> tuple[LlmAgent, LlmAgent, LlmAgent, LlmAgent]:
    """Construct the four agents.

    Packaged in a function so the model backend (which reads env vars) is
    resolved at call time, not at import time — this lets tests and CLIs
    adjust the environment before agents are materialized.
    """
    architect = LlmAgent(
        name="architect",
        model=get_model(0.7),
        generate_content_config=_cfg(0.7),
        description="Decomposes user intent into a structured JSON menu plan.",
        instruction=ARCHITECT_PROMPT,
        output_key="current_plan",
    )

    executor = LlmAgent(
        name="executor",
        model=get_model(0.1),
        generate_content_config=_cfg(0.1),
        description="Grounds the menu plan in real-world grocery pricing.",
        instruction=EXECUTOR_PROMPT,
        tools=[price_menu_plan],
        output_key="grounded_plan",
    )

    critic = LlmAgent(
        name="critic",
        model=get_model(0.0),
        generate_content_config=_cfg(0.0),
        description="Validates budget & macros; emits delta instructions on rejection.",
        instruction=CRITIC_PROMPT,
        tools=[flag_constraint_conflict],
        output_key="critique",
    )

    verifier = LlmAgent(
        name="verifier",
        model=get_model(0.0),
        generate_content_config=_cfg(0.0),
        description="Zero-trust ingredient safety audit; the only agent that can approve.",
        instruction=VERIFIER_PROMPT,
        tools=[audit_menu_plan, approve_plan],
        output_key="verification",
    )

    return architect, executor, critic, verifier


def build_loop() -> LoopAgent:
    """Construct the outer Plan-Act-Reflect loop (max 5 iterations)."""
    architect, executor, critic, verifier = build_agents()
    return LoopAgent(
        name="AGEP_Loop",
        description=(
            "Plan-Act-Reflect loop with nested Correction and "
            "Hallucination-Prevention sub-loops."
        ),
        sub_agents=[architect, executor, critic, verifier],
        max_iterations=MAX_ITERATIONS,
    )
