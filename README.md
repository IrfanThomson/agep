# AGEP — Autonomous Gourmet Event Planner

A four-agent **Plan-Act-Reflect** system on [Google Agent Development Kit][adk]
that plans a dinner event end-to-end. Each agent is specialized (Architect,
Executor, Critic, Verifier), each owns one concern, and together they form a
self-correcting loop with explicit budget-correction and
hallucination-prevention sub-loops.

The default backend is **Claude Code headless** — no API key needed to run
locally. Swap to Gemini or Anthropic with a single env var.

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
    end

    verifier -- audit clean<br/>AND critic approved --> approve([approve_plan<br/>escalate=True])
    critic -. budget impossible<br/>mid-loop .-> abort([flag_constraint_conflict<br/>escalate=True])
    verifier -. violations or critic rejected .-> architect

    approve --> result([Approved MenuPlan])
```

**The four agents:**

| Agent         | Temp | Tools                                    | What it does                                                                              |
|---------------|:----:|------------------------------------------|-------------------------------------------------------------------------------------------|
| **Architect** | 0.7  | *(none)*                                 | Decomposes user intent into a JSON menu plan. Creative; may need corrections.             |
| **Executor**  | 0.1  | `price_menu_plan`                        | Batch-prices every ingredient against a simulated grocery API.                            |
| **Critic**    | 0.0  | `flag_constraint_conflict`               | Validates budget + macros. Emits concrete delta-instructions on rejection.                |
| **Verifier**  | 0.0  | `audit_menu_plan`, `approve_plan`        | Zero-trust ingredient safety audit. The **only** agent that can terminate the loop.       |

**Shared state** flows through `session.state`. Each agent declares an
`output_key`; the next agent reads it via `{placeholder}` substitution in its
instruction. No direct agent-to-agent coupling.

**Two self-correction sub-loops:**

- **Budget Correction** — if the Critic rejects, it writes `delta_instructions`
  to state. The Architect reads them next iteration and re-plans.
- **Hallucination Prevention** — the Verifier's `audit_menu_plan` checks every
  ingredient against the user's dietary restrictions. Any unsafe ingredient
  forces a re-plan.

**Three termination paths:**

1. **Normal success** — Verifier calls `approve_plan`, which sets
   `escalate=True` and breaks the `LoopAgent`.
2. **Infeasible request** — either the preflight check raises
   `ConstraintConflictError` before any LLM call (cheap), or the Critic calls
   `flag_constraint_conflict` mid-loop.
3. **Iteration budget exhausted** — `main.py` raises `RuntimeError` after 5
   unsuccessful iterations.

---

## Live demo

The three scenarios below are built in. All logs are actual output captured
against the Claude Code backend with the default `sonnet` model.

### Scenario 1 — Happy path

> 6 guests · $200 · Salmon + Quinoa required · vegan + nut-allergy

Demonstrates: Iterative Loop; Verifier audit sweeping every ingredient.

```
$ python main.py --scenario happy

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AGEP · scenario 'happy' · backend claude-code
 6 guests · $200 budget · required: Salmon, Quinoa · restrictions: vegan, nut-allergy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iteration 1
  architect  → drafted 4 recipes
  executor   → price_menu_plan(4 recipes, 36 ingredients)
  executor   ← $87.20 total · 0 unknown ingredients
  executor   → returned priced plan ($87.20)
  critic     → APPROVED · "Total cost of $87.20 is well within the $200 budget, all four dishes are nut-free, vegan …"
  verifier   → audit_menu_plan(36 ingredients, [vegan, nut-allergy])
  verifier   ← APPROVED · 36 ingredients checked · 0 violations
  verifier   → approve_plan — loop exits
  verifier   ← approved=True
  verifier   → APPROVED · 0 violations

────────────────────────────────────────────────────────────
 Plan approved in 1 iteration · $87.20
────────────────────────────────────────────────────────────

MENU

• Pan-Seared Salmon with Lemon-Dill-Caper Sauce (serves 6 · accommodates: nut-free, gluten-free, dairy-free)
    - 3.0 lb salmon               $36.00
    - 3.0 tbsp olive oil            $1.50
    - 2.0 count lemon                $1.50
    - 1.0 bunch dill                 $1.80
    - 2.0 tbsp capers               $0.80
    - 4.0 clove garlic               $1.00
    - 2.0 tsp sea salt             $0.10
    - 1.0 tsp black pepper         $0.15

• Quinoa Tabbouleh Salad (serves 6 · accommodates: vegan, nut-free, gluten-free, dairy-free)
    - 1.5 lb quinoa               $6.00
    - 1.0 lb cherry tomatoes      $3.50
    - 2.0 count cucumber             $2.00
    - 2.0 bunch parsley              $3.00
    - 2.0 count lemon                $1.50
    - 3.0 tbsp olive oil            $1.50
    - 1.0 count onion                $0.75
    - 1.0 tsp sea salt             $0.05
    - 1.0 tsp black pepper         $0.15

• Roasted Asparagus with Garlic and Lemon (serves 6 · accommodates: vegan, nut-free, gluten-free, dairy-free)
    - 2.0 lb asparagus            $8.00
    - 2.0 tbsp olive oil            $1.00
    - 3.0 clove garlic               $0.75
    - 1.0 count lemon                $0.75
    - 1.0 tsp sea salt             $0.05
    - 1.0 tsp black pepper         $0.15

• Spiced Chickpea and Spinach Stew (serves 6 · accommodates: vegan, nut-free, gluten-free, dairy-free)
    - 1.0 lb chickpeas            $2.00
    - 1.0 lb spinach              $3.00
    - 4.0 count tomato               $5.00
    - 1.0 count onion                $0.75
    - 4.0 clove garlic               $1.00
    - 2.0 tsp cumin                $0.40
    - 1.0 tsp smoked paprika       $0.25
    - 1.0 tsp turmeric             $0.20
    - 2.0 tbsp olive oil            $1.00
    - 2.0 cup vegetable broth      $0.40
    - 2.0 tbsp tomato paste         $1.00
    - 1.0 tsp sea salt             $0.05
    - 1.0 tsp black pepper         $0.15
```

**What to notice:**
- Converged in **one iteration** — the cheap, clean path.
- `36 ingredients checked · 0 violations` — every ingredient was individually
  audited against the nut-allergy + vegan rules. The Hallucination Prevention
  Loop runs on every iteration, even when clean.
- Salmon is isolated to one dish (non-vegan but nut-safe). Three other dishes
  are fully vegan, so the vegan guest has real options.

---

### Scenario 2 — Budget Correction Loop

> Same as happy, but **$60 budget** — forces the Critic to reject and re-plan.

Demonstrates: Correction Loop with explicit delta-instructions.

```
$ python main.py --scenario budget_crunch

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AGEP · scenario 'budget_crunch' · backend claude-code
 6 guests · $60 budget · required: Salmon, Quinoa · restrictions: vegan, nut-allergy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iteration 1
  architect  → drafted 3 recipes
  executor   → price_menu_plan(3 recipes, 23 ingredients)
  executor   ← $61.60 total · 0 unknown ingredients
  executor   → returned priced plan ($61.60)
  critic     → REJECTED · "Total cost of $61.60 exceeds the $60.00 budget by $1.60." · 2 delta-instructions
  verifier   → audit_menu_plan(23 ingredients, [vegan, nut-allergy])
  verifier   ← APPROVED · 23 ingredients checked · 0 violations
  verifier   → APPROVED · 0 violations

Iteration 2
  architect  → drafted 3 recipes
  executor   → price_menu_plan(3 recipes, 23 ingredients)
  executor   ← $58.60 total · 0 unknown ingredients
  executor   → returned priced plan ($58.60)
  critic     → APPROVED · "Total cost of $58.60 is within the $60.00 budget, all required ingredients are present, n…"
  verifier   → audit_menu_plan(23 ingredients, [vegan, nut-allergy])
  verifier   ← APPROVED · 23 ingredients checked · 0 violations
  verifier   → approve_plan — loop exits
  verifier   ← approved=True
  verifier   → APPROVED · 0 violations

────────────────────────────────────────────────────────────
 Plan approved in 2 iterations · $58.60
────────────────────────────────────────────────────────────

MENU

• Lemon-Dill Baked Salmon (serves 6 · accommodates: nut-free)
    - 2.25 lb salmon               $27.00
    - 3.0 count lemon                $2.25
    - 1.0 bunch dill                 $1.80
    - 4.0 clove garlic               $1.00
    - 3.0 tbsp olive oil            $1.50
    - 2.0 tbsp capers               $0.80
    - 2.0 tsp sea salt             $0.10
    - 1.0 tsp black pepper         $0.15

• Herbed Quinoa Tabbouleh (Vegan) (serves 6 · accommodates: vegan, nut-free)
    - 1.5 lb quinoa               $6.00
    - 2.0 count cucumber             $2.00
    - 0.5 lb cherry tomatoes      $1.75
    - 1.0 bunch parsley              $1.50
    - 2.0 count lemon                $1.50
    - 3.0 tbsp olive oil            $1.50
    - 1.0 tsp sea salt             $0.05
    - 1.0 tsp black pepper         $0.15
    - 3.0 count scallion             $0.75

• Garlic-Roasted Asparagus with Lemon (Vegan) (serves 6 · accommodates: vegan, nut-free)
    - 1.5 lb asparagus            $6.00
    - 3.0 clove garlic               $0.75
    - 2.0 tbsp olive oil            $1.00
    - 1.0 count lemon                $0.75
    - 1.0 tsp sea salt             $0.05
    - 1.0 tsp smoked paprika       $0.25
```

**What to notice:**
- Iteration 1 total: **$61.60** → Critic rejects with *2 delta-instructions*
  written to state (e.g. "reduce salmon from 2.5 lb to 2.25 lb").
- Iteration 2: Architect applied the deltas literally — salmon dropped to
  2.25 lb, total lands at **$58.60**, under budget. Verifier clean →
  `approve_plan` → loop exits.
- The Verifier approved both iterations; only the Critic objected. The loop
  keeps going because the Verifier gates `approve_plan` on *both* its own
  audit *and* the Critic's verdict being approved.

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

❌ ConstraintConflictError: Budget of $10.00 for 20 guests = $0.50/guest, below the floor of $8.00/guest. Constraints are mathematically infeasible.
```

**What to notice:**
- Zero LLM calls. Zero tokens spent. The preflight catches the impossibility
  before the `LoopAgent` is even constructed. That's the cheap escalation
  path. The Critic has a mirror tool (`flag_constraint_conflict`) for cases
  that only become obviously-impossible mid-loop.

---

### Hallucination Prevention — the Verifier's audit in detail

You see `audit_menu_plan` on every iteration in the transcripts above. On a
clean run, the one-liner summary reads:

```
  verifier   → audit_menu_plan(36 ingredients, [vegan, nut-allergy])
  verifier   ← APPROVED · 36 ingredients checked · 0 violations
```

`36 ingredients checked` means 36 individual ingredient-vs-restriction checks
were performed. The audit applies two different rules depending on the
restriction type:

- **Global exclusions** (allergies like `nut-allergy`, `gluten-free`,
  `dairy-free`): NO dish may contain the allergen (cross-contamination).
- **Per-guest preferences** (`vegan`, `vegetarian`): at least ONE dish must
  be fully compliant; others may be non-compliant.

If a violation is found (e.g. the Architect reached for `almond flour` in a
vegan dish while `nut-allergy` was in effect), the summary becomes:

```
  verifier   ← REJECTED · 1 violation of 34 checked
  verifier   → REJECTED · 1 violation
```

The full violation string (e.g. `"'almond flour' in 'Vegan Almond Tart' contains
['nuts'] — violates 'nut-allergy' (affects every guest)."`) is written to
`session.state.verification.violations`. The LoopAgent runs again, and the
Architect sees the exact offending ingredients on the next iteration — the
Hallucination Prevention Loop.

---

## Quickstart

### Default — Claude Code (no API key)

```bash
git clone https://github.com/IrfanThomson/agep.git
cd agep
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py --scenario happy
```

The `claude-agent-sdk` package ships a bundled Claude Code CLI and reuses
your local auth. No key configuration required.

### Anthropic API key

```bash
pip install "google-adk[extensions]"     # adds LiteLLM support
export ANTHROPIC_API_KEY=sk-ant-...
python main.py --scenario happy --backend anthropic
```

### Google Gemini API key

```bash
export GOOGLE_API_KEY=...
python main.py --scenario happy --backend gemini
```

### CLI reference

```
usage: agep [-h] [--scenario {budget_crunch,happy,impossible}]
            [--backend {claude-code,anthropic,gemini}]
            [--model MODEL] [--verbose]
```

| Flag         | Default       | Purpose                                                          |
|--------------|---------------|------------------------------------------------------------------|
| `--scenario` | `happy`       | Built-in scenario selector.                                      |
| `--backend`  | `claude-code` | Overrides `AGEP_LLM`.                                            |
| `--model`    | backend-specific | Overrides `AGEP_MODEL`. `sonnet`, `opus`, `gemini-2.5-pro`, … |
| `--verbose`  | off           | DEBUG logs from ADK and the adapter.                             |

Set `AGEP_DEBUG=1` to stream the Claude Code subprocess stderr to your
terminal — useful when debugging the adapter itself.

---

## Code map

```
agep/
├── README.md          # You are here
├── requirements.txt
├── .env.example       # Backend selector + optional API keys
├── state.py           # Pydantic models: EventConstraints, MenuPlan, Critique, SafetyAudit
├── tools.py           # Simulated grocery/nutrition DBs + escalation hooks
├── llm.py             # Pluggable backend selector + ClaudeCodeLlm BaseLlm adapter
├── agents.py          # 4 LlmAgents + the LoopAgent(max_iterations=5)
└── main.py            # CLI + preflight + Runner + human-readable event formatter
```

Five files, ~1800 lines, no magic. Each file's top-of-module docstring
explains its scope.

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

**Swap in a real grocery API** — replace `price_menu_plan` in `tools.py` with
a call to Kroger / Instacart / USDA. Agent code doesn't change; that's the
point of the tool abstraction.

**Add dietary restrictions** — edit `_RESTRICTION_BLOCKS`,
`_GLOBAL_EXCLUSIONS`, and `_PER_GUEST_PREFERENCES` in `tools.py`.

**Persist sessions** — swap `InMemoryRunner` for a persistent `SessionService`
(e.g. `VertexAiSessionService`).

**Add pytest** — the three scenarios are dispatch-ready for pytest; assert on
`plan_approved=True`, `total_cost_usd <= budget`, and that
`ConstraintConflictError` fires for `impossible`.

---

## Requirements

- Python 3.10+
- One of:
  - Claude Code installed and authenticated locally (default), OR
  - `ANTHROPIC_API_KEY` + `google-adk[extensions]`, OR
  - `GOOGLE_API_KEY`
