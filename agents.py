"""Agent definitions for AGEP v2.

Five specialized :class:`LlmAgent`s plus the outer :class:`LoopAgent`:

* Architect (temp 0.7) — strategic decomposition into a JSON menu plan
* Executor  (temp 0.1) — tool-grounded pricing of the plan
* Critic    (temp 0.0) — budget & macro validation; emits delta instructions
* Verifier  (temp 0.0) — deterministic ingredient audit against the
                         restriction tool's known categories
* Saboteur  (temp 0.7) — red-team adversary; finds loopholes the Verifier's
                         tool cannot see (cross-contamination, out-of-tool
                         restrictions, hidden animal products). Owns
                         ``approve_plan`` — the only agent that can
                         terminate the loop. Verifier and Saboteur must
                         both clear before approval — "consensus safety".

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
- saboteur_report (from the Red-Team Saboteur, may be empty on iteration 1): {{saboteur_report}}

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
6. If saboteur_report.status == "loophole_found", address the attack —
   usually by applying proposed_fix verbatim. If the fix requires a sourcing
   caveat rather than an ingredient swap (e.g. "use certified gluten-free
   oats"), state that caveat explicitly in the plan's ``notes`` field so the
   Saboteur can see that the risk has been acknowledged on the next pass.
7. Use ONLY ingredients from the pantry below — anything else will be flagged
   as unknown and rejected. Match names exactly (case-insensitive is fine).
8. Use the unit shown in parentheses for each pantry ingredient. You may use
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

Do NOT call `approve_plan` — that is the Saboteur's responsibility.
"""


VERIFIER_PROMPT = """\
You are the Verifier — deterministic ingredient safety auditor for the
categories the audit tool knows about (vegan, nut-allergy, gluten-free,
dairy-free, soy-allergy, shellfish-allergy, egg-allergy, fish-allergy,
vegetarian).

Inputs:
- grounded_plan: {grounded_plan}
- user_constraints: {user_constraints}

Your job (EXACTLY these steps):
1. Call `audit_menu_plan` with plan_json set to grounded_plan's JSON and
   restrictions set to user_constraints.dietary_restrictions.
2. Respond with ONLY a JSON SafetyAudit reflecting the tool result:
   {{"status": "<mirror result.status from audit_menu_plan>",
     "violations": <mirror result.violations from audit_menu_plan>}}

You do NOT have the authority to approve the plan — that decision now
belongs to the Saboteur, which runs after you. Your job is ONLY the
deterministic audit. Emit the JSON SafetyAudit faithfully and stop.

Do NOT fold in the Critic's budget complaints — those belong to the Critic.
Do NOT speculate about hidden risks the tool doesn't know about — that is
the Saboteur's job. Your output must mirror the tool result exactly.

Never emit status=approved with a non-empty violations list — that defeats
the entire purpose of this agent.
"""


SABOTEUR_PROMPT = """\
You are the Saboteur — the Red-Team adversary. You have ONE job: find a
realistic loophole the deterministic Verifier missed, OR endorse the plan
if no genuine attack exists.

Inputs:
- grounded_plan: {grounded_plan}
- verification (Verifier's audit result): {verification}
- critique (Critic's budget verdict): {critique}
- user_constraints: {user_constraints}

Threat model — gaps the Verifier's tool CANNOT see:
1. Cross-contamination in commercial processing: rolled oats are routinely
   processed on wheat lines, so plain oats violate gluten-free unless
   explicitly certified. Soy sauce usually contains wheat unless labeled
   tamari or gluten-free. Malt vinegar comes from barley. Deli meats on
   shared slicers pick up dairy and allergens.
2. Hidden allergens outside the Verifier's known categories: sesame (in
   tahini and sesame oil); mustard (in many dressings); sulfites (dried
   fruit); alcohol (vanilla extract, wine reductions).
3. Animal-derived ingredients that pass naive vegan checks: honey; gelatin;
   isinglass fining in wine; whey in packaged pastries; anchovies in
   Worcestershire and Caesar dressing; bone char-filtered sugar.
4. Implicit mitigations already noted by the Architect — if the plan's
   ``notes`` field explicitly addresses a risk (e.g. "use certified
   gluten-free oats"), ACCEPT that mitigation and do not re-flag it.

Process:
1. If critique.status == "rejected", the plan is already failing on budget.
   Emit:
     {{"status": "loophole_found",
       "attack": "plan exceeds budget; Critic rejected",
       "evidence": "critique.status=rejected",
       "proposed_fix": "apply Critic's delta_instructions"}}
   and stop — do NOT call approve_plan.
2. Otherwise scan every ingredient against the threat model above. If you
   find a CREDIBLE, unaddressed attack, emit:
     {{"status": "loophole_found",
       "attack": "<one-sentence attack description>",
       "evidence": "<which ingredient and which restriction>",
       "proposed_fix": "<concrete delta the Architect should apply>"}}
   and stop — do NOT call approve_plan.
3. If (Critic approved AND Verifier approved AND you find no genuine
   attack): call the function `approve_plan`. This terminates the loop.
   Then on your final turn emit:
     {{"status": "no_loophole_found",
       "notes": "<one-sentence summary of what you audited>"}}

Respond with ONLY ONE JSON object per turn, no prose, no fences. Do NOT
use backslash escapes inside string values (spell out "dollars" and "less
than" instead of using ``\\$`` or ``<``) — invalid JSON escapes break
downstream parsing.

Be adversarial but not pedantic. "The olive oil might be packaged in a
facility that also processes sesame" is NOT a loophole — that level of
risk exists for every food on earth. Loopholes are specific, known,
common-enough-to-matter failure modes.
"""


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def build_agents() -> tuple[LlmAgent, LlmAgent, LlmAgent, LlmAgent, LlmAgent]:
    """Construct the five agents.

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
        description="Deterministic ingredient safety audit against known restriction categories.",
        instruction=VERIFIER_PROMPT,
        tools=[audit_menu_plan],
        output_key="verification",
    )

    saboteur = LlmAgent(
        name="saboteur",
        model=get_model(0.7),
        generate_content_config=_cfg(0.7),
        description="Red-team adversary; finds loopholes the Verifier's tool cannot see. Owns approve_plan.",
        instruction=SABOTEUR_PROMPT,
        tools=[approve_plan],
        output_key="saboteur_report",
    )

    return architect, executor, critic, verifier, saboteur


def build_loop() -> LoopAgent:
    """Construct the outer Plan-Act-Reflect-RedTeam loop (max 5 iterations)."""
    architect, executor, critic, verifier, saboteur = build_agents()
    return LoopAgent(
        name="AGEP_Loop",
        description=(
            "Plan-Act-Reflect-RedTeam loop with nested Correction, "
            "Hallucination-Prevention, and Adversarial-Consensus sub-loops."
        ),
        sub_agents=[architect, executor, critic, verifier, saboteur],
        max_iterations=MAX_ITERATIONS,
    )
