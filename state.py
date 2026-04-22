"""Pydantic state models for AGEP.

Every object that flows through `session.state` between agents is defined
here. Keeping schemas in one file makes the data model easy to audit.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class EventConstraints(BaseModel):
    """User-supplied constraints for the event being planned."""

    guests: int = Field(ge=1, description="Number of diners to serve.")
    budget_usd: float = Field(gt=0, description="Total grocery budget, USD.")
    required_ingredients: list[str] = Field(
        default_factory=list,
        description="Ingredients the user wants featured (e.g. 'Salmon').",
    )
    dietary_restrictions: list[str] = Field(
        default_factory=list,
        description="Tags like 'vegan', 'nut-allergy', 'gluten-free'.",
    )


class Ingredient(BaseModel):
    """A single ingredient line on a recipe."""

    name: str
    quantity: float
    unit: str = Field(description="g / oz / count / tbsp / cup")
    estimated_cost_usd: Optional[float] = None


class Recipe(BaseModel):
    """One dish in the menu."""

    name: str
    serves: int = Field(ge=1)
    ingredients: list[Ingredient]
    accommodates: list[str] = Field(
        default_factory=list,
        description="Which dietary tags this dish satisfies.",
    )


class MenuPlan(BaseModel):
    """Full menu output by the Architect (after Executor pricing)."""

    recipes: list[Recipe]
    total_cost_usd: float = Field(ge=0)
    notes: str = ""


class Critique(BaseModel):
    """Critic verdict on a grounded plan."""

    status: Literal["approved", "rejected"]
    reason: str
    delta_instructions: list[str] = Field(
        default_factory=list,
        description="Concrete re-plan hints for the Architect, e.g. "
        "'Replace salmon with cod to cut $40'.",
    )


class SafetyAudit(BaseModel):
    """Verifier's ingredient-level safety audit."""

    status: Literal["approved", "rejected"]
    violations: list[str] = Field(
        default_factory=list,
        description="Specific violations, e.g. '\"Pesto Quinoa\" contains pine nuts (nut-allergy).'",
    )


class SaboteurReport(BaseModel):
    """Red-team adversary's finding on a plan the Verifier already cleared.

    The Saboteur runs after the Verifier and tries to invent a credible
    attack the deterministic audit would miss — cross-contamination risk,
    restrictions outside the audit tool's known categories, hidden animal
    products, etc.
    """

    status: Literal["no_loophole_found", "loophole_found"]
    attack: str = Field(
        default="",
        description="One-sentence description of how a guest could still be harmed.",
    )
    evidence: str = Field(
        default="",
        description="Which ingredient(s) and which restriction(s) the attack targets.",
    )
    proposed_fix: str = Field(
        default="",
        description="Concrete delta for the Architect to apply on the next iteration.",
    )
    notes: str = Field(
        default="",
        description="Free-form notes on what the Saboteur audited (only on no_loophole_found).",
    )
