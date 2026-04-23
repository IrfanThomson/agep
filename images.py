"""Optional dish-image generation for AGEP v3.

Runs once, *after* the safety loop has approved the plan and the Chef has
written cooking instructions. Has zero effect on the loop itself — purely a
post-approval garnish for the printable menu booklet.

Backend: Google's Gemini image-generation model (default
``gemini-2.5-flash-image-preview``). Requires ``GOOGLE_API_KEY`` and
``google-genai``. If either is missing, ``generate_dish_images`` returns an
empty result with a human-readable reason and AGEP continues normally.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image-preview"


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return text or "dish"


def _prompt_for(recipe: dict[str, Any]) -> str:
    """Compose a tight visual prompt from a recipe's name and ingredients."""
    name = recipe.get("name", "a plated dish")
    ings = [
        i.get("name", "")
        for i in (recipe.get("ingredients") or [])
        if i.get("name")
    ][:6]
    ing_line = ", ".join(ings) if ings else "the dish"
    return (
        f"Overhead food-magazine photograph of '{name}'. Featured ingredients: "
        f"{ing_line}. Plated on a neutral ceramic dish, soft natural light, "
        f"shallow depth of field, no text, no logos."
    )


def generate_dish_images(
    recipes: list[dict[str, Any]],
    output_dir: str | Path,
    model: str | None = None,
) -> dict[str, Any]:
    """Generate one image per recipe and write PNGs to ``output_dir``.

    Returns:
        {"status": "ok" | "skipped" | "error",
         "reason": str,
         "images": [{"recipe_name": str, "path": str}],
         "model": str}
    """
    if not os.getenv("GOOGLE_API_KEY"):
        return {
            "status": "skipped",
            "reason": "GOOGLE_API_KEY not set — set it to enable dish images.",
            "images": [],
            "model": model or DEFAULT_IMAGE_MODEL,
        }
    try:
        from google import genai  # type: ignore
    except ImportError:
        return {
            "status": "skipped",
            "reason": "google-genai not installed — pip install google-genai.",
            "images": [],
            "model": model or DEFAULT_IMAGE_MODEL,
        }

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    chosen_model = model or os.getenv("AGEP_IMAGE_MODEL") or DEFAULT_IMAGE_MODEL
    client = genai.Client()

    images: list[dict[str, str]] = []
    for recipe in recipes:
        name = recipe.get("name", "dish")
        try:
            resp = client.models.generate_content(
                model=chosen_model,
                contents=_prompt_for(recipe),
            )
        except Exception as e:
            logger.warning("Image generation failed for %r: %s", name, e)
            continue

        png_bytes = _extract_png(resp)
        if png_bytes is None:
            logger.warning("No image data returned for %r", name)
            continue

        path = out / f"{_slugify(name)}.png"
        path.write_bytes(png_bytes)
        images.append({"recipe_name": name, "path": str(path)})

    return {
        "status": "ok" if images else "error",
        "reason": "" if images else "model returned no image parts",
        "images": images,
        "model": chosen_model,
    }


def _extract_png(response: Any) -> bytes | None:
    """Pull the first inline image part out of a google-genai response."""
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                return data if isinstance(data, bytes) else bytes(data)
    return None
