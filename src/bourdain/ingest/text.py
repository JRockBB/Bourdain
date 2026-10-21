"""Deterministic text ingestion for the first local workflow."""

import re

from bourdain.errors import BourdainError
from bourdain.models import RecipeDraft, RecipeSource
from bourdain.normalize.parse import parse_line


def parse_servings(text: str) -> float | None:
    match = re.fullmatch(
        r"\s*(?:(?:servings?|serves|yield)\s*:?\s*)?(\d+(?:\.\d+)?)\s*(?:servings?|people)?\s*",
        text,
        re.IGNORECASE,
    )
    return float(match[1]) if match and float(match[1]) > 0 else None


def from_text(text: str, *, store_steps: bool = False) -> RecipeDraft:
    """Read a title, optional Servings line, ingredients, and optional instructions."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise BourdainError("Recipe text is empty")
    title = lines.pop(0).lstrip("# ")
    servings = None
    ingredients = []
    steps = []
    group = None
    in_steps = False
    for line in lines:
        heading = line.strip("# :").casefold()
        if re.match(r"^(servings?|serves|yield)\b", line, re.IGNORECASE):
            servings = parse_servings(line)
        elif heading == "ingredients":
            continue
        elif heading in {"instructions", "directions", "method", "steps"}:
            in_steps = True
        elif in_steps:
            if store_steps:
                steps.append(line)
        elif line.endswith(":"):
            group = line.rstrip(":")
        else:
            try:
                ingredients.append(parse_line(line.removeprefix("- "), group=group))
            except ValueError as exc:
                raise BourdainError(f"Cannot parse ingredient: {line}") from exc
    if not ingredients:
        raise BourdainError("Recipe has no ingredient lines")
    return RecipeDraft(
        title=title,
        servings=servings,
        ingredients=ingredients,
        steps=steps,
        source=RecipeSource(kind="text"),
        flags=[] if servings else ["missing_servings"],
    )
