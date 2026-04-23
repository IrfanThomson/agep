# AGEP v3 — Autonomous Gourmet Event Planner with Cookable Output

A **six-agent** Plan-Act-Reflect-RedTeam-then-Cook system on [Google Agent
Development Kit][adk] that turns a dinner-party request into a printable
booklet: a priced menu, step-by-step cooking instructions, and (optionally)
AI-generated dish photography. v3 takes the v2 safety pipeline — Architect,
Executor, Critic, Verifier, Saboteur — and bolts on a post-loop **Chef**
stage plus operational constraints (kitchen equipment, prep-time ceiling,
calorie / protein floors), so the approved plan is something you can actually
walk into the kitchen and execute.

The default backend is **Claude Code headless** — no API key needed to run
locally. The default grocery backend is the same in-process simulator from
v1/v2, with **Spoonacular** available as a one-env-var swap. Dish image
generation is opt-in and falls back silently when not configured.

> **Relationship to v2.** v3 is strictly additive on the safety side.
> Same five-agent loop, same `LoopAgent(max_iterations=5)`, same
> `approve_plan` gate, same Adversarial Consensus rule. The Critic gained
> three new validation tools (equipment, prep time, macros), the Architect's
> prompt knows how to respect them, and a sixth agent (Chef) now runs in
> its **own runner** after the loop terminates. v1 and v2 are preserved as
> the [`v1`](https://github.com/IrfanThomson/agep/tree/v1) and
> [`v2`](https://github.com/IrfanThomson/agep/tree/v2) branches for direct
> comparison.

[adk]: https://google.github.io/adk-docs/

---

## Architecture

```mermaid
flowchart LR
    user([User request]) --> preflight{{Preflight<br/>check}}
    preflight -- infeasible --> conflict([ConstraintConflictError])
    preflight -- ok --> loop

    subgraph loop ["LoopAgent · max_iterations = 5"]
        direction LR
        architect[Architect<br/>temp 0.7] --> executor[Executor<br/>temp 0.1]
        executor --> critic[Critic<br/>temp 0.0]
        critic --> verifier[Verifier<br/>temp 0.0]
        verifier --> saboteur[Saboteur<br/>temp 0.7]
    end

    saboteur -- consensus:<br/>verifier clean<br/>AND critic approved<br/>AND no loophole --> approve([approve_plan<br/>escalate=True])
    critic -. budget impossible<br/>mid-loop .-> abort([flag_constraint_conflict<br/>escalate=True])
    saboteur -. loophole found<br/>OR critic rejected .-> architect

    approve --> result([Approved MenuPlan])
```

After the loop terminates with an approved plan, control passes to a second,
independent runner:

```mermaid
flowchart LR
    approved([Approved MenuPlan]) --> chef_runner

    subgraph chef_runner ["InMemoryRunner · own session"]
        chef[Chef<br/>temp 0.2]
    end

    chef --> script([CookingScript])
    approved -. recipes .-> images{{Image gen<br/>optional}}
    images -- GOOGLE_API_KEY set --> pngs([PNGs in generated/])
    images -- not configured --> skip([no-op, skipped])
    script --> booklet([Printed booklet:<br/>menu + prices +<br/>instructions + images])
    pngs --> booklet
    skip --> booklet
```

**The six agents:**

| Agent         | Temp | Stage     | Tools                                                            | What it does                                                                          |
|---------------|:----:|-----------|------------------------------------------------------------------|---------------------------------------------------------------------------------------|
| **Architect** | 0.7  | in-loop   | *(none)*                                                         | Decomposes user intent into a JSON menu plan with `prep_minutes` + `required_equipment` per recipe. |
| **Executor**  | 0.1  | in-loop   | `price_menu_plan`                                                | Batch-prices every ingredient. Backend is simulated by default, Spoonacular if `AGEP_GROCERY=spoonacular`. |
| **Critic**    | 0.0  | in-loop   | `validate_nutrition_macros`, `check_equipment`, `validate_prep_time`, `flag_constraint_conflict` | Validates budget + macros + equipment + prep time. Emits concrete delta-instructions on rejection. |
| **Verifier**  | 0.0  | in-loop   | `audit_menu_plan`                                                | Deterministic ingredient audit against the tool's known restriction categories.       |
| **Saboteur**  | 0.7  | in-loop   | `approve_plan`                                                   | Red-team adversary; hunts loopholes the Verifier's tool can't see. Gates approval.    |
| **Chef**      | 0.2  | post-loop | *(none)*                                                         | Writes step-by-step cooking instructions for the approved menu, in its own runner.    |

**Shared state** flows through `session.state`. Each agent declares an
`output_key`; the next agent reads it via `{placeholder}` substitution in its
instruction. The Chef runs against a fresh session whose only seed is the
approved `grounded_plan` — there is no path by which Chef output can mutate
loop state.

**Three self-correction sub-loops:**

- **Budget / Operational Correction** — if the Critic rejects (over budget,
  missing equipment, prep over ceiling, macros below floor), it writes
  `delta_instructions` to state. The Architect reads them next iteration and
  re-plans.
- **Hallucination Prevention** — the Verifier's `audit_menu_plan` checks
  every ingredient against the user's dietary restrictions. Any unsafe
  ingredient forces a re-plan.
- **Adversarial Consensus** — the Saboteur reads the Verifier's verdict and
  tries to attack it. If it finds a credible loophole the tool missed, it
  writes a `SaboteurReport` to state; the Architect applies the
  `proposed_fix` on the next iteration.

**Three termination paths:**

1. **Normal success** — Saboteur calls `approve_plan`, which sets
   `escalate=True` and breaks the `LoopAgent`. Requires the Critic approved,
   the Verifier's audit is clean, AND the Saboteur found no loophole. Once
   the loop exits, `main.py` runs the Chef and (if configured) image gen.
2. **Infeasible request** — either the preflight check raises
   `ConstraintConflictError` before any LLM call (cheap), or the Critic calls
   `flag_constraint_conflict` mid-loop. Chef does not run on this path.
3. **Iteration budget exhausted** — `main.py` raises `RuntimeError` after 5
   unsuccessful iterations. Chef does not run on this path either.

---

## What v3 adds

v2 shipped a safe menu. v3 ships a *cookable* menu. Four pieces, in order
of how they wire into the loop:

### 1. Operational constraints (equipment, prep time, macros)

The Critic is no longer just a budget watchdog. Three new tools turn it into
a full operational gate:

- **`check_equipment(plan_json, available_equipment)`** — every recipe in
  the plan declares a `required_equipment` list. The tool computes
  `set(required) - set(available)` per recipe and rejects if anything is
  missing. Catches the failure mode where the Architect happily writes
  "blend until smooth" for a guest who explicitly said they only have a
  stovetop and a sheet pan.
- **`validate_prep_time(plan_json, max_prep_minutes)`** — sums each recipe's
  `prep_minutes` and rejects if the total exceeds the user's wall-clock
  ceiling. Single-cook assumption, no parallelism credit. Catches the
  failure mode where every individual dish looks fast but the four-course
  sum is three hours.
- **`validate_nutrition_macros(plan_json, guests, calorie_floor_per_guest, protein_floor_per_guest_g)`** —
  aggregates calories and protein across all recipes, divides by guest
  count, and rejects if either floor is unmet. Catches the failure mode
  where a "healthy" salad-heavy menu would leave four hungry adults under
  500 kcal and 25 g protein each.

Each tool returns a structured pass/fail with concrete delta hints. The
Critic prompt runs them in fixed order (budget → macros → equipment → prep
time), aggregates every violation into one `Critique`, and emits one
delta-instruction list. The Architect re-plans with the deltas in hand.

These constraints are all **opt-in via the EventConstraints model**. Leave
`kitchen_equipment` empty and `max_prep_minutes` / the macro floors `None`,
and the Critic skips those tools entirely — v2 behaviour is preserved
exactly.

### 2. Cooking instructions (Chef agent, outside the loop)

After the safety loop approves, `main.py` constructs a brand-new
`InMemoryRunner` for a single agent — the Chef — with a fresh session
containing only the approved `grounded_plan`. The Chef writes a
`CookingScript`: per-recipe atomic imperative steps with temperatures,
timing, and ingredient quantities pulled from the plan.

The Chef runs in its own runner **on purpose**. The deliberate property:
no Chef output — malformed JSON, missing recipes, hallucinated steps,
nothing — can retroactively invalidate or modify a menu the safety loop
already approved. The booklet either gets cooking instructions, or it
doesn't (and prints the menu without them); it cannot become unsafe because
the Chef misbehaved. If the Chef call raises, `main.py` catches the
exception, prints `chef → ERROR · …`, and renders the menu without steps.

`--no-chef` skips the stage entirely.

### 3. Real grocery pricing (Spoonacular adapter, optional)

The Executor's tool, `price_menu_plan`, is now a dispatcher:

- `AGEP_GROCERY=simulated` *(default)* routes to the bundled in-process
  database from v1/v2. Zero dependencies, deterministic, free.
- `AGEP_GROCERY=spoonacular` routes to `tools_spoonacular.py`, which calls
  the Spoonacular Food API over HTTPS. Requires `SPOONACULAR_API_KEY` and
  the `requests` package. The free tier is 150 requests/day.

Both backends honour the same return contract — `grounded_plan`,
`unknown_ingredients`, `source` — so no agent prompt changes when you flip
the env var. The Spoonacular adapter caches name→ID lookups and per-line
price results in process, so a session with repeated ingredients (olive oil
appearing in four recipes) costs one search and one price call rather than
eight. Failures on a single ingredient mark it `unknown` and let the loop
continue, mirroring the simulated backend's tolerance.

### 4. Dish images (Gemini image gen, optional)

When `GOOGLE_API_KEY` is set and `google-genai` is installed,
`images.generate_dish_images` runs once after the Chef stage, asks Gemini
(default `gemini-2.5-flash-image-preview`, override with
`AGEP_IMAGE_MODEL`) for one PNG per recipe, and writes them to
`generated/<scenario>/`. The booklet then prints `Image: <path>` next to
each dish.

If the key is missing or the package isn't installed, the function returns
`{"status": "skipped", "reason": "..."}` and AGEP prints the booklet
without images. There is no crash path — image gen is a pure garnish.

`--no-images` skips the stage even when configured.

---

## Live demo

The six built-in scenarios below exercise the full v3 surface. The first
four are unchanged from v2 and their captured logs are preserved verbatim;
the last two are new and exercise the v3 Critic tools.

### Scenario 1 — v3 happy path (every check passes on the first try)

> 6 guests · $200 · Salmon + Quinoa required · vegan + nut-allergy

Demonstrates: the cleanest v3 trace — Critic, Verifier, and Saboteur all
clear in iteration 1, the loop exits, and Chef appends per-recipe
instructions in its own runner afterward.

```
$ python main.py --scenario happy

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AGEP · scenario 'happy' · backend claude-code
 6 guests · $200 budget · required: Salmon, Quinoa · restrictions: vegan, nut-allergy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iteration 1
  architect  → drafted 4 recipes
  executor   → price_menu_plan(4 recipes, 34 ingredients)
  executor   ← $86.69 total · 0 unknown · source=simulated
  executor   → returned priced plan ($86.69)
  critic     → APPROVED · "all checks passed"
  verifier   → audit_menu_plan(34 ingredients, [vegan, nut-allergy])
  verifier   ← APPROVED · 34 ingredients checked · 0 violations
  verifier   → APPROVED · 0 violations
  saboteur   → approve_plan — loop exits
  saboteur   ← approved=True
  saboteur   → CLEAR · "Audited all 34 ingredients across 4 recipes against the full threat model: no hidden alle…"

Chef
  chef       → wrote instructions for 4 recipes

────────────────────────────────────────────────────────────
 Plan approved in 1 iteration · $86.69 · 115 min prep
────────────────────────────────────────────────────────────

REQUIRED INGREDIENTS: Salmon anchors the main course (3 lb for 6 guests); Quinoa forms the base of the tabbouleh side. NUT ALLERGY (global): every recipe has been scrubbed of all tree-nut and peanut-adjacent pantry items — almonds, walnuts, pine nuts, cashew cream, and almond flour are absent from the entire menu. VEGAN (per-guest preference): three of the four dishes (Quinoa Tabbouleh, Roasted Vegetables, Warm Chickpeas with Spinach) are fully plant-based, giving vegan guests a complete, balanced meal. The salmon main is intentionally non-vegan to leverage the required ingredient. KITCHEN EQUIPMENT: kitchen_equipment list was empty, so a standard home kitchen (oven, stovetop, mixing bowl) is assumed — all recipes use only these. BUDGET: estimated spend is well under $100, leaving ample headroom within the $200 ceiling.

MENU

• Herb-Baked Salmon with Lemon, Dill & Smoked Paprika (serves 6 · accommodates: nut-allergy · 30 min · oven, mixing bowl)
    - 3.0 lb salmon               $36.00
    - 4.0 tbsp olive oil            $2.00
    - 4.0 clove garlic               $1.00
    - 1.0 bunch dill                 $1.80
    - 1.0 bunch thyme                $2.00
    - 2.0 count lemon                $1.50
    - 1.0 tsp smoked paprika       $0.25
    - 1.0 tsp sea salt             $0.05
    - 1.0 tsp black pepper         $0.15
    Instructions:
      1. Preheat oven to 400 F.
      2. Mince 4 garlic cloves.
      3. Strip leaves from 1 bunch of dill and 1 bunch of thyme, then finely chop both.
      4. Juice 1 lemon into a mixing bowl; slice the second lemon into thin rounds and set aside.
      5. Add 4 tbsp olive oil, minced garlic, chopped dill, chopped thyme, 1 tsp smoked paprika, 1 tsp sea salt, and 1 tsp black pepper to the bowl with the lemon juice; stir until a uniform marinade forms.
      6. Line a rimmed baking sheet with foil or parchment and place the 3 lb salmon skin-side down on it.
      7. Spread the herb marinade evenly over the entire top surface of the salmon.
      8. Arrange the lemon rounds in a single layer on top of the coated salmon.
      9. Bake on the center rack for 20–25 minutes until the thickest part flakes easily with a fork and registers an internal temperature of 145 F.
      10. Remove from the oven and rest undisturbed for 5 minutes.
      11. Slice into 6 equal portions, spoon any accumulated pan juices over each piece, and transfer to a serving platter.

• Lemon-Herb Quinoa Tabbouleh (serves 6 · accommodates: vegan, nut-allergy · 30 min · stovetop, mixing bowl)
    - 1.5 lb quinoa               $6.00
    - 3.0 cup vegetable broth      $0.60
    - 1.0 lb cherry tomatoes      $3.50
    - 2.0 count cucumber             $2.00
    - 1.0 bunch parsley              $1.50
    - 2.0 count lemon                $1.50
    - 3.0 tbsp olive oil            $1.50
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07

• Roasted Zucchini, Bell Pepper & Eggplant with Oregano (serves 6 · accommodates: vegan, nut-allergy · 35 min · oven, mixing bowl)
    - 3.0 count zucchini             $4.50
    - 3.0 count bell pepper          $4.50
    - 1.0 lb eggplant             $2.00
    - 3.0 tbsp olive oil            $1.50
    - 4.0 clove garlic               $1.00
    - 1.0 bunch oregano              $2.00
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07

• Warm Cumin Chickpeas with Spinach and Lemon (serves 6 · accommodates: vegan, nut-allergy · 20 min · stovetop)
    - 1.5 lb chickpeas            $3.00
    - 1.0 lb spinach              $3.00
    - 2.0 count lemon                $1.50
    - 2.0 tbsp olive oil            $1.00
    - 3.0 clove garlic               $0.75
    - 1.0 tsp cumin                $0.20
    - 0.5 tsp chili flakes         $0.10
    - 1.0 tsp sea salt             $0.05

(instructions for remaining 3 recipes omitted for brevity — Chef writes them for every dish)
```

**What to notice:**
- **Iteration 1 clears every gate**: Critic approves on `"all checks
  passed"`, Verifier's deterministic audit finds 0 violations across 34
  ingredients, and the Saboteur — running its full hidden-allergen and
  cross-contamination threat model on the priced plan — finds nothing
  worth escalating and goes straight to `approve_plan`. The loop exits
  after one pass.
- **Each recipe header now carries v3 metadata**: prep minutes (e.g.
  `30 min`) and required equipment (e.g. `stovetop, mixing bowl`) appear
  inline. These are written by the Architect into the plan and are what
  the Critic's `validate_prep_time` and `check_equipment` tools read in
  the constrained scenarios below.
- **The Chef stage runs after the loop**, in its own runner. The trace
  shows `chef → wrote instructions for 4 recipes`, and the printed menu
  carries numbered, atomic steps with temperatures and timings under each
  ingredient list. If Chef had crashed, the loop's approved plan would
  still print — just without instructions.

---

### Scenario 2 — Budget Correction Loop

> Same as happy, but **$60 budget** — forces the Critic to reject and re-plan.

Demonstrates: Correction Loop with explicit delta-instructions; Saboteur
deferring to the Critic on budget rejections.

```
$ python main.py --scenario budget_crunch

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AGEP · scenario 'budget_crunch' · backend claude-code
 6 guests · $60 budget · required: Salmon, Quinoa · restrictions: vegan, nut-allergy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iteration 1
  architect  → drafted 3 recipes
  executor   → price_menu_plan(3 recipes, 23 ingredients)
  executor   ← $64.84 total · 0 unknown · source=simulated
  executor   → returned priced plan ($64.84)
  critic     → REJECTED · "Total cost of $64.84 exceeds the $60.00 budget by $4.84." · 5 delta-instructions
  verifier   → audit_menu_plan(23 ingredients, [vegan, nut-allergy])
  verifier   ← APPROVED · 23 ingredients checked · 0 violations
  verifier   → APPROVED · 0 violations
  saboteur   → LOOPHOLE · "plan rejected by Critic"

Iteration 2
  architect  → drafted 3 recipes
  executor   → price_menu_plan(3 recipes, 23 ingredients)
  executor   ← $56.34 total · 0 unknown · source=simulated
  executor   → returned priced plan ($56.34)
  critic     → APPROVED · "all checks passed"
  verifier   → audit_menu_plan(23 ingredients, [vegan, nut-allergy])
  verifier   ← APPROVED · 23 ingredients checked · 0 violations
  verifier   → APPROVED · 0 violations
  saboteur   → approve_plan — loop exits
  saboteur   ← approved=True
  saboteur   → CLEAR · "Audited all 23 ingredients across three recipes against hidden-allergen threat model: no …"

Chef
  chef       → wrote instructions for 3 recipes

────────────────────────────────────────────────────────────
 Plan approved in 2 iterations · $56.34 · 75 min prep
────────────────────────────────────────────────────────────

ITERATION 2 — Budget correction applied per Critic delta_instructions and Saboteur proposed_fix. Three cost levers pulled simultaneously to create comfortable headroom below $60: (1) Salmon reduced from 2.5 lb to 2.0 lb (~5.3 oz per guest, still a satisfying portion supplemented by the hearty quinoa salad and asparagus side); (2) Quinoa reduced from 1.5 lb to 1.25 lb (still ample at ~3.3 oz dry per guest, which yields ~6.7 oz cooked); (3) Parsley reduced from 2 bunches to 1 bunch (one bunch is sufficient for tabbouleh flavor at this scale). Estimated total savings vs. prior plan: ~$6.00 (salmon) + ~$1.00 (quinoa) + ~$1.50 (parsley) = ~$8.50, bringing expected total to approximately $56–57, well under the $60 cap. REQUIRED INGREDIENTS: Salmon in Recipe 1, Quinoa in Recipe 2. NUT-ALLERGY (global): zero nut ingredients across all recipes. VEGAN (preference): Recipes 2 and 3 are fully vegan. No new nut-containing ingredients introduced. All recipes remain gluten-free and dairy-free.

MENU

• Pan-Seared Salmon with Dill, Capers & Lemon (serves 6 · accommodates: gluten-free, nut-free, dairy-free · 25 min · stovetop)
    - 2.0 lb salmon               $24.00
    - 4.0 tbsp olive oil            $2.00
    - 1.0 bunch dill                 $1.80
    - 2.0 tbsp capers               $0.80
    - 2.0 count lemon                $1.50
    - 4.0 clove garlic               $1.00
    - 2.0 tsp sea salt             $0.10
    - 1.0 tsp black pepper         $0.15
    Instructions:
      1. Pat 2 lb of salmon fillets completely dry with paper towels.
      2. Season both sides of the salmon evenly with 2 tsp sea salt and 1 tsp black pepper.
      3. Mince 4 garlic cloves and pick the fronds from 1 bunch of dill, keeping them separate.
      4. Zest and juice both lemons, reserving the zest and juice in separate small bowls.
      5. Heat 4 tbsp olive oil in a large skillet over medium-high heat until the oil shimmers and just begins to smoke.
      6. Place the salmon fillets skin-side up in the skillet and sear undisturbed for 4 minutes.
      7. Flip each fillet skin-side down, add the minced garlic to the pan around the fillets, and sear for an additional 3–4 minutes until the skin is crisp and the flesh is opaque through the center.
      8. Scatter 2 tbsp capers directly into the pan and cook for 30 seconds, stirring them around the salmon.
      9. Remove the pan from heat and drizzle the lemon juice over all fillets.
      10. Transfer the salmon fillets to a serving platter, skin-side down.
      11. Spoon the pan juices, capers, and garlic over each fillet.
      12. Garnish with the fresh dill fronds and lemon zest, then serve immediately.

• Quinoa Tabbouleh Salad (serves 6 · accommodates: vegan, gluten-free, nut-free, dairy-free · 30 min · stovetop, mixing bowl)
    - 1.25 lb quinoa               $5.00
    - 0.5 lb cherry tomatoes      $1.75
    - 2.0 count cucumber             $2.00
    - 1.0 bunch parsley              $1.50
    - 4.0 count scallion             $1.00
    - 2.0 count lemon                $1.50
    - 3.0 tbsp olive oil            $1.50
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07

• Roasted Asparagus with Garlic & Lemon (serves 6 · accommodates: vegan, gluten-free, nut-free, dairy-free · 20 min · oven, sheet pan)
    - 2.0 lb asparagus            $8.00
    - 2.0 tbsp olive oil            $1.00
    - 3.0 clove garlic               $0.75
    - 1.0 count lemon                $0.75
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07

(instructions for remaining 2 recipes omitted for brevity — Chef writes them for every dish)
```

**What to notice:**
- Iteration 1 total: **$64.84** → Critic rejects with **5 delta-instructions**
  (a v3 polish over v2's single combined instruction — each cost lever
  becomes its own actionable delta the Architect can apply independently).
  The Saboteur *also* refuses to approve, citing `plan rejected by
  Critic`. This is by design: the Saboteur's prompt explicitly checks
  `critique.status` first, so the red-teamer won't rubber-stamp a plan the
  budget auditor has already rejected.
- Iteration 2: total drops to **$56.34** (salmon reduced from 2.5 lb to
  2.0 lb, quinoa from 1.5 lb to 1.25 lb, parsley from 2 bunches to 1 —
  three simultaneous cost levers from the granular deltas), all three
  agents clear, Saboteur calls `approve_plan`.
- The Saboteur running on a clean menu is cheap — the prompt is scoped so
  the red-teamer doesn't invent theoretical risks on a pared-down plan.
  Chef then writes per-recipe steps for all three dishes.

---

### Scenario 3 — Human Escalation (preflight)

> 20 guests · $10 budget — mathematically infeasible before any agent runs.

Demonstrates: `ConstraintConflictError` short-circuiting the loop.

```
$ python main.py --scenario impossible

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AGEP · scenario 'impossible' · backend claude-code
 20 guests · $10 budget · required: — · restrictions: —
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ConstraintConflictError: Budget of $10.00 for 20 guests = $0.50/guest, below the floor of $8.00/guest. Constraints are mathematically infeasible.
```

**What to notice:**
- Zero LLM calls. Zero tokens spent. The preflight catches the impossibility
  before the `LoopAgent` is even constructed. The Critic has a mirror tool
  (`flag_constraint_conflict`) for cases that only become
  obviously-impossible mid-loop. Neither the Saboteur nor the Chef ever
  runs on this path — both are gated behind a successful loop exit.

---

### Scenario 4 — Hidden Gluten *(the v2 headline demo)*

> 6 guests · $150 · **rolled oats** required · **gluten-free**

Demonstrates: Adversarial Consensus catching a real-world cross-contamination
risk the Verifier's tool can't see — this run, an unspecified `vinegar` in
a salad dressing that could just as easily be malt vinegar (gluten-bearing,
derived from barley) as apple cider or rice vinegar (safe).

```
$ python main.py --scenario hidden_gluten

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AGEP · scenario 'hidden_gluten' · backend claude-code
 6 guests · $150 budget · required: rolled oats · restrictions: gluten-free
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iteration 1
  architect  → drafted 5 recipes
  executor   → price_menu_plan(5 recipes, 39 ingredients)
  executor   ← $73.03 total · 0 unknown · source=simulated
  executor   → returned priced plan ($73.03)
  critic     → APPROVED · "all checks passed"
  verifier   → audit_menu_plan(39 ingredients, [gluten-free])
  verifier   ← APPROVED · 39 ingredients checked · 0 violations
  verifier   → APPROVED · 0 violations
  saboteur   → LOOPHOLE · "The Avocado and Mixed Greens Salad calls for 'vinegar' with no type specified; malt vineg…"

Iteration 2
  architect  → drafted 5 recipes
  executor   → price_menu_plan(5 recipes, 39 ingredients)
  executor   ← $73.03 total · 0 unknown · source=simulated
  executor   → returned priced plan ($73.03)
  critic     → APPROVED · "all checks passed"
  verifier   → audit_menu_plan(39 ingredients, [gluten-free])
  verifier   ← APPROVED · 39 ingredients checked · 0 violations
  verifier   → APPROVED · 0 violations
  saboteur   → approve_plan — loop exits
  saboteur   ← approved=True
  saboteur   → CLEAR · "Audited all 39 ingredients across 5 recipes against the gluten-free threat model: certifi…"

Chef
  chef       → wrote instructions for 5 recipes

────────────────────────────────────────────────────────────
 Plan approved in 2 iterations · $73.03 · 165 min prep
────────────────────────────────────────────────────────────

GLUTEN-FREE SOURCING CAVEATS — TWO ITEMS REQUIRE CARE:

1. ROLLED OATS: Naturally gluten-free but routinely cross-contaminated with wheat during milling and transport. You MUST purchase certified gluten-free rolled oats (e.g., Bob's Red Mill Gluten Free Rolled Oats). Standard supermarket oats are NOT safe for guests with celiac disease or gluten intolerance.

2. VINEGAR (Avocado & Mixed Greens Salad): The 'vinegar' in this salad MUST be apple cider vinegar or white wine vinegar — both are distilled from gluten-free sources. Do NOT use malt vinegar; it is derived from barley and contains gluten. Rice vinegar is also an acceptable safe alternative. When shopping, read the label: any vinegar whose source grain is listed as 'barley' or 'malt' is off-limits.

All other ingredients are inherently free of gluten-containing grains. Soy sauce (wheat-containing) was deliberately excluded from the entire menu. Menu structure: starter (oat fritters) → protein main (lemon-dill roasted salmon) → grain side (quinoa pilaf) → hearty vegetable side (sweet potato & black bean salad) → fresh salad. All five dishes are gluten-free; the three plant-based dishes are also vegan, providing broad dietary coverage. Total ingredient cost is well under the $150 budget for 6 guests.

MENU

• Savory Zucchini & Oat Fritters (serves 6 · accommodates: gluten-free · 30 min · stovetop, mixing bowl)
    - 0.5 lb rolled oats          $0.75
    - 2.0 count zucchini             $3.00
    - 4.0 count eggs                 $1.60
    - 1.0 count onion                $0.75
    - 3.0 clove garlic               $0.75
    - 3.0 tbsp olive oil            $1.50
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07
    Instructions:
      1. Grate the 2 zucchini on the large holes of a box grater into a mixing bowl.
      2. Sprinkle 0.5 tsp sea salt over the grated zucchini, toss to combine, and let stand 10 minutes to draw out moisture.
      3. Transfer the salted zucchini to a clean kitchen towel and wring out as much liquid as possible.
      4. Finely dice the 1 onion and mince the 3 garlic cloves.
      5. In a large mixing bowl, combine the wrung-out zucchini, 0.5 lb certified gluten-free rolled oats, 4 eggs, diced onion, minced garlic, the remaining 0.5 tsp sea salt, and 0.5 tsp black pepper, then stir until a cohesive batter forms.
      6. Heat 1.5 tbsp olive oil in a large skillet over medium heat until shimmering.
      7. Scoop approximately 3 tablespoons of batter per fritter into the skillet, pressing each mound gently into a 3-inch round.
      8. Cook the fritters 4 minutes per side until deep golden brown and cooked through.
      9. Transfer the cooked fritters to a paper-towel-lined plate to drain briefly.
      10. Repeat the oil-heating, scooping, and frying steps with the remaining 1.5 tbsp olive oil and remaining batter.
      11. Arrange all fritters on a serving platter and serve hot.

• Lemon-Dill Roasted Salmon (serves 6 · accommodates: gluten-free, dairy-free · 35 min · oven, mixing bowl)
    - 2.5 lb salmon               $30.00
    - 2.0 count lemon                $1.50
    - 1.0 bunch dill                 $1.80
    - 3.0 tbsp olive oil            $1.50
    - 4.0 clove garlic               $1.00
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07

• Cumin Quinoa Pilaf with Roasted Bell Peppers (serves 6 · accommodates: gluten-free, vegan, dairy-free · 40 min · stovetop, oven, mixing bowl)
    - 0.75 lb quinoa               $3.00
    - 2.0 count bell pepper          $3.00
    - 0.5 lb cherry tomatoes      $1.75
    - 3.0 cup vegetable broth      $0.60
    - 2.0 tbsp olive oil            $1.00
    - 3.0 clove garlic               $0.75
    - 1.0 tsp cumin                $0.20
    - 1.0 tsp sea salt             $0.05

• Roasted Sweet Potato & Black Bean Salad (serves 6 · accommodates: gluten-free, vegan, dairy-free · 45 min · oven, mixing bowl)
    - 2.0 lb sweet potato         $3.00
    - 1.0 lb black beans          $2.00
    - 1.0 bunch cilantro             $1.50
    - 2.0 count lime                 $1.00
    - 1.0 tsp cumin                $0.20
    - 1.0 tsp smoked paprika       $0.25
    - 2.0 tbsp olive oil            $1.00
    - 0.5 tsp sea salt             $0.03

• Avocado & Mixed Greens Salad (serves 6 · accommodates: gluten-free, vegan, dairy-free · 15 min · mixing bowl)
    - 0.5 lb mixed greens         $2.00
    - 2.0 count avocado              $3.50
    - 0.25 lb cherry tomatoes      $0.88
    - 1.0 count cucumber             $1.00
    - 1.0 count lemon                $0.75
    - 2.0 tbsp olive oil            $1.00
    - 1.0 tbsp vinegar              $0.15
    - 0.5 tsp sea salt             $0.03

(instructions for remaining 4 recipes omitted for brevity — Chef writes them for every dish)
```

**What to notice:**
- **The Verifier's audit approved on both iterations.** That's not a bug —
  the tool faithfully reported what it knew: a generic ingredient name
  like `vinegar` has no `gluten` tag in `_NUTRITION_DB`, so the
  deterministic audit has no basis to flag it. A celiac guest following
  only the v1 pipeline could have been served malt vinegar.
- **Iteration 1 Saboteur catches the unspecified vinegar**: *"The Avocado
  and Mixed Greens Salad calls for 'vinegar' with no type specified; malt
  vinegar is derived from barley and contains gluten."* The report is
  written to `session.state.saboteur_report`.
- **Iteration 2 Architect reads `saboteur_report` and applies the fix** —
  but does more than asked. Rather than narrowly renaming the salad's
  vinegar, it adds explicit sourcing caveats for BOTH the vinegar AND the
  rolled oats (oats face the same cross-contamination risk, which the
  Architect correctly generalized from the vinegar example). The caveats
  appear in the plan's `notes` field — human-facing sourcing instructions
  that would land on the grocery list. Saboteur verifies the mitigations
  are now documented, calls `approve_plan`, loop exits.
- **This is exactly the v2 thesis, surviving into v3**: the deterministic
  audit is necessary but insufficient; the LLM-based adversary covers the
  gaps. Either one alone ships a broken menu; together they ship a safe
  one.

---

### Scenario 5 — Weeknight (equipment + prep-time floor) *(new in v3)*

> 4 guests · $80 · **chicken breast** required · equipment = stovetop +
> sheet pan + mixing bowl · max prep 45 min

Demonstrates: v3's new operational validators in action. Both
`check_equipment` and `validate_prep_time` fire in iteration 1, both
clear, and the Architect respects the 45-minute ceiling on its first
draft.

```
$ python main.py --scenario weeknight --no-images

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AGEP · scenario 'weeknight' · backend claude-code
 4 guests · $80 budget · required: chicken breast · restrictions: —
 equipment: stovetop, sheet pan, mixing bowl · max prep: 45 min
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iteration 1
  architect  → drafted 3 recipes
  executor   → price_menu_plan(3 recipes, 23 ingredients)
  executor   ← $30.72 total · 0 unknown · source=simulated
  executor   → returned priced plan ($30.72)
  critic     → check_equipment(available=[stovetop, sheet pan, mixing bowl])
  critic     ← APPROVED · all equipment available
  critic     → validate_prep_time(max=45 min)
  critic     ← APPROVED · 42 min total prep
  critic     → APPROVED · "all checks passed"
  verifier   → audit_menu_plan(23 ingredients, [])
  verifier   ← APPROVED · 23 ingredients checked · 0 violations
  verifier   → APPROVED · 0 violations
  saboteur   → approve_plan — loop exits
  saboteur   ← approved=True
  saboteur   → CLEAR · "Audited all 23 ingredients against hidden-allergen, cross-contamination, and animal-deriv…"

Chef
  chef       → wrote instructions for 3 recipes

────────────────────────────────────────────────────────────
 Plan approved in 1 iteration · $30.72 · 42 min prep
────────────────────────────────────────────────────────────

Three-course dinner for 4 within a 45-minute single-cook window (25 + 12 + 5 = 42 min total). Chicken breast (required) is centre-stage: pounded thin, marinated briefly in a mixing bowl with lemon, garlic and herbs, then seared in butter and olive oil on the stovetop for a golden crust and juicy interior. While the chicken rests, couscous is prepared stovetop using warm vegetable broth — the bell peppers and cherry tomatoes are softened in the same pan beforehand for maximum flavour with minimal equipment. The arugula salad needs only a mixing bowl and comes together in minutes. All required equipment (stovetop, mixing bowl) is drawn from the available list; sheet pan is intentionally not required so it remains unused. No dietary restrictions apply. Budget should land well under $80 given commodity pricing on chicken (~$5–7/lb), couscous, and produce.

MENU

• Garlic-Herb Seared Chicken Breast (serves 4 · accommodates: — · 25 min · stovetop, mixing bowl)
    - 2.0 lb chicken breast       $10.00
    - 4.0 clove garlic               $1.00
    - 2.0 count lemon                $1.50
    - 3.0 tbsp olive oil            $1.50
    - 0.25 lb butter               $1.25
    - 0.5 bunch thyme                $1.00
    - 0.5 bunch rosemary             $1.00
    - 2.0 tsp sea salt             $0.10
    - 1.0 tsp black pepper         $0.15
    Instructions:
      1. Place the 2 lb chicken breasts between two sheets of plastic wrap or in a zip-lock bag and pound to an even ¾-inch thickness with a meat mallet or heavy skillet.
      2. Mince all 4 garlic cloves and strip the leaves from the 0.5 bunch of thyme and 0.5 bunch of rosemary.
      3. In a mixing bowl, combine 2 tbsp of the olive oil, the minced garlic, thyme, rosemary, 1 tsp sea salt, and ½ tsp black pepper, then add the chicken breasts and toss to coat; let marinate at room temperature for 10 minutes.
      4. Juice 1 lemon into a small bowl and cut the remaining lemon into wedges; set both aside.
      5. Heat a large skillet over medium-high heat until hot, then add the remaining 1 tbsp olive oil and swirl to coat the pan.
      6. Lay the marinated chicken breasts flat in the skillet and sear undisturbed for 4 minutes until a deep golden crust forms on the bottom.
      7. Flip the chicken breasts, add the 0.25 lb butter in small pieces around the chicken, and let it melt and foam.
      8. Tilt the pan slightly and use a spoon to continuously baste the chicken with the melted butter for 3–4 minutes until the internal temperature reaches 165 °F.
      9. Pour the reserved lemon juice over the chicken in the pan and swirl briefly to deglaze.
      10. Transfer the chicken to a cutting board and season with the remaining 1 tsp sea salt and ½ tsp black pepper; let rest for 5 minutes before slicing.
      11. Slice each breast against the grain and plate with the pan drippings spooned over the top and lemon wedges on the side.

• Herbed Couscous with Roasted Bell Peppers & Cherry Tomatoes (serves 4 · accommodates: vegan, vegetarian · 12 min · stovetop, mixing bowl)
    - 0.5 lb couscous             $1.50
    - 2.0 count bell pepper          $3.00
    - 0.5 lb cherry tomatoes      $1.75
    - 2.0 tbsp olive oil            $1.00
    - 0.5 bunch parsley              $0.75
    - 2.0 cup vegetable broth      $0.40
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07

• Lemon-Arugula Salad with Cherry Tomatoes (serves 4 · accommodates: vegan, vegetarian · 5 min · mixing bowl)
    - 0.5 lb arugula              $2.00
    - 0.25 lb cherry tomatoes      $0.88
    - 1.0 count lemon                $0.75
    - 2.0 tbsp olive oil            $1.00
    - 0.5 tsp sea salt             $0.03
    - 0.25 tsp black pepper         $0.04

(instructions for remaining 2 recipes omitted for brevity — Chef writes them for every dish)
```

**What to notice:**
- **The headline lines are the two new tool calls in the Critic's trace**:
  `check_equipment(available=[stovetop, sheet pan, mixing bowl])` and
  `validate_prep_time(max=45 min)`. Both clear in iteration 1 — every
  recipe's `required_equipment` is a subset of what's available, and the
  sum of `prep_minutes` is **42 minutes** against the 45-minute ceiling.
  Budget is checked inline against the grounded plan total without a
  tool call.
- The Architect respected both constraints on the first try — no
  oven-roasted dishes (sheet pan goes unused), no blender purées, no
  slow-braised anything. This is the prompt addition doing its job: the
  Architect now reads `kitchen_equipment` and `max_prep_minutes` from
  `user_constraints` and treats them as hard targets, not aspirational.
- The receipt summary now reports both cost AND prep time:
  `Plan approved in 1 iteration · $30.72 · 42 min prep`.
- The **Chef** stage runs after the loop and writes per-recipe step lists
  for all three dishes (chicken breast, couscous side, arugula salad). It
  runs in its own runner; the loop has already exited by the time Chef
  takes its first turn.

---

### Scenario 6 — Macro Floor (calorie + protein floors) *(new in v3)*

> 4 guests · $120 · **salmon** required · nut-allergy · calorie floor
> 700 kcal/guest · protein floor 40 g/guest

Demonstrates: v3's `validate_nutrition_macros` tool confirming portion
sizes meet the stated floors. The Critic adds a fourth tool call to its
sequence; the tool aggregates per-guest macros across every recipe and
clears comfortably.

```
$ python main.py --scenario macro_floor

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AGEP · scenario 'macro_floor' · backend claude-code
 4 guests · $120 budget · required: salmon · restrictions: nut-allergy
 kcal floor: 700/guest · protein floor: 40 g/guest
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iteration 1
  architect  → drafted 4 recipes
  executor   → price_menu_plan(4 recipes, 37 ingredients)
  executor   ← $64.14 total · 0 unknown · source=simulated
  executor   → returned priced plan ($64.14)
  critic     → validate_nutrition_macros(guests=4, kcal_floor=700, protein_floor=40g)
  critic     ← APPROVED · 1324 kcal/guest · 68.7 g protein/guest
  critic     → APPROVED · "all checks passed"
  verifier   → audit_menu_plan(37 ingredients, [nut-allergy])
  verifier   ← APPROVED · 37 ingredients checked · 0 violations
  verifier   → APPROVED · 0 violations
  saboteur   → approve_plan — loop exits
  saboteur   ← approved=True
  saboteur   → CLEAR · "Audited all 37 ingredients across 4 recipes against the nut-allergy threat model plus hid…"

Chef
  chef       → wrote instructions for 4 recipes

────────────────────────────────────────────────────────────
 Plan approved in 1 iteration · $64.14 · 85 min prep
────────────────────────────────────────────────────────────

Four-course dinner for 4 guests, strictly nut-free across every dish — no almonds, walnuts, pine nuts, cashew cream, or almond flour appear anywhere. Salmon (2.5 lb total, ~10 oz / guest) satisfies the required_ingredients constraint and delivers an estimated 580 cal and 57 g protein per guest from the fish alone; combined with the quinoa pilaf (~300 cal, 11 g protein), roasted asparagus (~35 cal, 4 g protein), olive oil and butter (~400 cal), and the salad, the full menu lands at approximately 1 350–1 400 cal and 72 g protein per guest — well above the 700 cal and 40 g protein floors. Estimated market cost is ~$65–70, comfortably within the $120 budget. Kitchen equipment assumed: standard home kitchen (oven, stovetop, mixing bowl, sheet pan). The quinoa and asparagus can cook concurrently (oven + stovetop) while the salmon is seared last for best texture; actual wall-clock time for a single cook is roughly 40–45 minutes.

MENU

• Mixed Greens Salad with Lemon-Dijon Vinaigrette (serves 4 · accommodates: nut-free, gluten-free, vegetarian, vegan · 10 min · mixing bowl)
    - 0.5 lb mixed greens         $2.00
    - 1.0 count cucumber             $1.00
    - 0.25 lb cherry tomatoes      $0.88
    - 1.0 count shallot              $0.80
    - 3.0 tbsp olive oil            $1.50
    - 2.0 tbsp vinegar              $0.30
    - 1.0 tbsp dijon mustard        $0.30
    - 1.0 count lemon                $0.75
    - 1.0 tbsp honey                $0.40
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07
    Instructions:
      1. Wash and thoroughly dry 0.5 lb mixed greens, then place them in a large mixing bowl.
      2. Halve the cucumber lengthwise and slice it into half-moons approximately 1/4 inch thick.
      3. Halve 0.25 lb cherry tomatoes and add them to the bowl with the cucumber slices.
      4. Peel and mince 1 shallot as finely as possible.
      5. Juice and zest 1 lemon into a small bowl.
      6. Add 3 tbsp olive oil, 2 tbsp vinegar, 1 tbsp dijon mustard, 1 tbsp honey, 1 tsp sea salt, and 0.5 tsp black pepper to the lemon juice and zest, then whisk vigorously until fully emulsified.
      7. Stir the minced shallot into the vinaigrette.
      8. Add the cucumber and cherry tomatoes to the bowl of greens.
      9. Drizzle the vinaigrette over the salad just before serving and toss gently until every leaf is lightly coated.
      10. Divide evenly among 4 plates and serve immediately.

• Pan-Seared Salmon with Lemon-Dill Caper Sauce (serves 4 · accommodates: nut-free, gluten-free · 25 min · stovetop)
    - 2.5 lb salmon               $30.00
    - 3.0 tbsp olive oil            $1.50
    - 0.125 lb butter               $0.62
    - 4.0 clove garlic               $1.00
    - 2.0 count lemon                $1.50
    - 1.0 bunch dill                 $1.80
    - 2.0 tbsp capers               $0.80
    - 1.0 tbsp dijon mustard        $0.30
    - 1.0 tsp sea salt             $0.05
    - 1.0 tsp black pepper         $0.15

• Garlic Roasted Asparagus with Lemon Zest (serves 4 · accommodates: nut-free, gluten-free, vegetarian, vegan · 20 min · oven, sheet pan)
    - 1.5 lb asparagus            $6.00
    - 2.0 tbsp olive oil            $1.00
    - 3.0 clove garlic               $0.75
    - 1.0 count lemon                $0.75
    - 0.5 tsp sea salt             $0.03
    - 0.5 tsp black pepper         $0.07

• Quinoa Pilaf with Cherry Tomatoes and Fresh Herbs (serves 4 · accommodates: nut-free, gluten-free, vegetarian, vegan · 30 min · stovetop)
    - 0.75 lb quinoa               $3.00
    - 2.0 cup vegetable broth      $0.40
    - 0.5 lb cherry tomatoes      $1.75
    - 1.0 count onion                $0.75
    - 2.0 clove garlic               $0.50
    - 2.0 tbsp olive oil            $1.00
    - 1.0 bunch parsley              $1.50
    - 1.0 count lemon                $0.75
    - 1.0 tsp sea salt             $0.05
    - 0.5 tsp black pepper         $0.07

(instructions for remaining 3 recipes omitted for brevity — Chef writes them for every dish)
```

**What to notice:**
- **The headline line is the macro tool call**:
  `validate_nutrition_macros(guests=4, kcal_floor=700, protein_floor=40g)`
  returns `1324 kcal/guest · 68.7 g protein/guest` — comfortably over both
  floors (about 1.9× the calorie floor and 1.7× the protein floor). The
  tool aggregates calories and protein from `_NUTRITION_DB` across all
  four recipes and divides by guest count.
- The Critic ran the macro tool first and then approved on `"all checks
  passed"` — no equipment or prep-time tools fire here because this
  scenario leaves `kitchen_equipment` empty and `max_prep_minutes` unset.
  Each constraint is opt-in.
- The Architect's first draft hit the floors without help: 2.5 lb of
  salmon (~10 oz / guest) supplies most of the protein, and the quinoa
  pilaf + roasted asparagus + dijon-vinaigrette salad fill out calories
  while staying strictly nut-free (the Verifier confirms 0 violations
  against the nut-allergy restriction). Loop exits in one iteration; Chef
  writes per-recipe steps for all four dishes.

---

## Quickstart

### Default — Claude Code (no API key)

```bash
git clone https://github.com/IrfanThomson/agep.git
cd agep
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py --scenario weeknight
```

The `claude-agent-sdk` package ships a bundled Claude Code CLI and reuses
your local auth. No key configuration required.

### Anthropic API key

```bash
pip install "google-adk[extensions]"     # adds LiteLLM support
export ANTHROPIC_API_KEY=sk-ant-...
python main.py --scenario hidden_gluten --backend anthropic
```

### Google Gemini API key

```bash
export GOOGLE_API_KEY=...
python main.py --scenario hidden_gluten --backend gemini
```

### Optional integrations *(new in v3)*

Both are no-op when not configured — AGEP's default zero-config path
doesn't pay for them.

**Live grocery pricing via Spoonacular** (free tier: 150 requests/day):

```bash
pip install requests
export AGEP_GROCERY=spoonacular
export SPOONACULAR_API_KEY=...
python main.py --scenario weeknight
```

The Executor's tool dispatches to the HTTP backend; per-name and per-line
results are cached in process so repeated ingredients across recipes cost
one search and one price lookup, not N.

**Dish image generation via Gemini**:

```bash
pip install google-genai
export GOOGLE_API_KEY=...
python main.py --scenario weeknight
# PNGs land in generated/weeknight/<dish-slug>.png
```

If `GOOGLE_API_KEY` is unset or `google-genai` isn't installed, image gen
prints `[images] skipped: <reason>` and the booklet renders without
images. Override the model with `AGEP_IMAGE_MODEL=...`.

### CLI reference

```
usage: agep [-h] [--scenario {budget_crunch,happy,hidden_gluten,impossible,macro_floor,weeknight}]
            [--backend {claude-code,anthropic,gemini}]
            [--model MODEL] [--no-chef] [--no-images] [--verbose]
```

| Flag           | Default          | Purpose                                                       |
|----------------|------------------|---------------------------------------------------------------|
| `--scenario`   | `happy`          | Built-in scenario selector.                                   |
| `--backend`    | `claude-code`    | Overrides `AGEP_LLM`.                                         |
| `--model`      | backend-specific | Overrides `AGEP_MODEL`. `sonnet`, `opus`, `gemini-2.5-pro`, … |
| `--no-chef`    | off              | Skip the post-loop Chef stage entirely.                       |
| `--no-images`  | off              | Skip the post-loop image-generation stage entirely.           |
| `--verbose`    | off              | DEBUG logs from ADK and the adapter.                          |

Set `AGEP_DEBUG=1` to stream the Claude Code subprocess stderr to your
terminal — useful when debugging the adapter itself.

---

## Code map

```
agep/
├── README.md             # You are here
├── requirements.txt
├── .env.example          # Backend selector + grocery selector + optional API keys
├── state.py              # Pydantic models: EventConstraints, MenuPlan, Critique,
│                         # SafetyAudit, SaboteurReport, RecipeInstructions, CookingScript
├── tools.py              # Simulated grocery/nutrition DBs, validation tools,
│                         # AGEP_GROCERY dispatcher, escalation hooks
├── tools_spoonacular.py  # Optional Spoonacular HTTP adapter (lazy-imported)
├── images.py             # Optional Gemini dish-image generator (lazy-imported)
├── llm.py                # Pluggable backend selector + ClaudeCodeLlm BaseLlm adapter
├── agents.py             # 5 in-loop LlmAgents + LoopAgent(max_iterations=5) + Chef
└── main.py               # CLI + preflight + safety-loop runner + Chef runner +
                          # image-gen orchestration + booklet formatter
```

Six files plus two optional adapters, no magic. Each file's top-of-module
docstring explains its scope. The two optional adapters
(`tools_spoonacular.py`, `images.py`) are imported lazily, so the default
zero-config path doesn't load `requests` or `google-genai`.

---

## How the Claude Code backend works

Google ADK is Gemini-first, but it exposes a clean abstract base class called
`BaseLlm` you can subclass to bring any model backend. `llm.py::ClaudeCodeLlm`
implements that interface on top of the `claude-agent-sdk` Python package,
routing each ADK `LlmRequest` into a Claude Code subprocess.

Three details worth highlighting:

1. **Prompt-level tool orchestration.** Claude Code doesn't speak ADK's
   native tool-use protocol, so the adapter serializes tool schemas into the
   system prompt and asks Claude to respond with a JSON envelope:
   `{"action": "tool_call", "tool_name": "...", "arguments": {...}}` or
   `{"action": "text", "content": "..."}`. The adapter parses that back into
   the `Content`/`Part` structure ADK expects. Less robust than native
   function-calling, but it's what buys the no-API-key story.

2. **Tools are framed as "functions", never as "tools".** If the system
   prompt mentions "tools", Claude Code attempts real tool execution and the
   subprocess crashes. The adapter carefully describes them as *"external
   functions to request via JSON"*.

3. **Temperature is a soft hint.** The CLI does not expose a temperature
   flag, so the adapter prepends a style directive to the system prompt
   (`0.0 → "Be strictly deterministic"`, `0.7 → "Be creative"`). For precise
   temperature control, route through the `anthropic` (LiteLLM) or `gemini`
   backend.

---

## Extending

**Swap in a different grocery API** — `tools.price_menu_plan` is a
dispatcher keyed off `AGEP_GROCERY`. Add a new branch (e.g. `kroger`,
`instacart`) and a sibling adapter file that mirrors the
`{grounded_plan, unknown_ingredients, source}` contract; no agent prompt
needs to change.

**Add dietary restrictions** — edit `_RESTRICTION_BLOCKS`,
`_GLOBAL_EXCLUSIONS`, and `_PER_GUEST_PREFERENCES` in `tools.py`. The
Saboteur will often catch gaps in these lists before you even notice them.

**Add threat categories to the Saboteur** — edit the `Threat model` block in
`SABOTEUR_PROMPT` (`agents.py`). Adding a new category (e.g. "bioaccumulated
mercury in large predatory fish") gives the Saboteur a new dimension to
audit without changing any other agent's code.

**Add a new in-loop validator** — write a function in `tools.py` that
returns `{"status": "approved|rejected", "violations": [...]}`, attach it
to the Critic's `tools=[...]` list in `agents.py::build_agents`, and add a
matching step to `CRITIC_PROMPT`. The new validator runs alongside macros,
equipment, and prep time without touching any other agent.

**Swap Gemini for OpenAI image generation** — `images.generate_dish_images`
is the only call site. Replace the `genai` import and the
`client.models.generate_content` block with the OpenAI Images SDK; preserve
the return shape (`status`, `images`, `model`) and the rest of the booklet
keeps working unchanged.

**Make the Chef call back into the Verifier** — if the Chef's instructions
introduce a new ingredient or an implicit serving size, you can wire the
post-loop runner to feed Chef output through a second audit pass. Today
the Chef only emits steps and quantities are fixed by the approved plan,
but the runner is wired to make this trivial.

**Persist sessions** — swap `InMemoryRunner` for a persistent
`SessionService` (e.g. `VertexAiSessionService`). Both runners (loop +
Chef) accept the swap independently.

**Add pytest** — the six scenarios are dispatch-ready for pytest; assert on
`plan_approved=True`, `total_cost_usd <= budget`, and that
`ConstraintConflictError` fires for `impossible`. For `hidden_gluten`,
assert the final plan's `notes` contains a certified-GF sourcing caveat.
For `weeknight`, assert `sum(r.prep_minutes) <= 45` and that no recipe's
`required_equipment` includes anything outside the available set. For
`macro_floor`, assert per-guest calories ≥ 700 and protein ≥ 40 g.

---

## Requirements

- Python 3.10+
- One of:
  - Claude Code installed and authenticated locally (default), OR
  - `ANTHROPIC_API_KEY` + `google-adk[extensions]`, OR
  - `GOOGLE_API_KEY`
- Optional v3 extras (no-op when absent):
  - `requests` + `SPOONACULAR_API_KEY` for live grocery pricing
  - `google-genai` + `GOOGLE_API_KEY` for dish-image generation
