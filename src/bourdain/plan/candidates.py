"""Deterministic eligibility and scoring before the MILP; no external calls."""

from dataclasses import dataclass
from datetime import date

from bourdain.errors import BourdainError
from bourdain.models import Catalog, History, Pantry, Recipe, Targets

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Candidates:
    recipes: dict[str, Recipe]
    excluded: dict[str, list[str]]
    scores: dict[str, float]


def history_scores(
    identifier: str, history: History, today: date
) -> tuple[float, float]:
    events = [
        event for event in history if event.recipe == identifier and event.date <= today
    ]
    novelty = (
        min((today - max(event.date for event in events)).days, 56) / 56
        if events
        else 1.0
    )
    ratings = [event.rating for event in events if event.rating is not None]
    rating = sum(ratings) / len(ratings) / 5 if ratings else 0.6
    return novelty, rating


def select_candidates(
    recipes: dict[str, Recipe],
    catalog: Catalog,
    pantry: Pantry,
    targets: Targets,
    history: History,
    *,
    today: date,
    pinned: set[str] | None = None,
    excluded: set[str] | None = None,
    limit: int = 80,
    ignore_macros: bool = False,
    preferred: set[str] | None = None,
) -> Candidates:
    pinned, excluded = pinned or set(), excluded or set()
    unknown = (pinned | excluded) - recipes.keys()
    if unknown:
        raise BourdainError(f"Unknown recipe IDs: {', '.join(sorted(unknown))}")
    required = {
        name
        for eater in targets.eaters
        for name, bounds in (eater.daily or {}).items()
        if any(bound is not None for bound in bounds)
    }
    rejected, scores = {}, {}
    for identifier, recipe in sorted(recipes.items()):
        reasons = []
        if recipe.excluded or identifier in excluded:
            reasons.append("excluded by user")
        flags = [
            flag
            for flag in recipe.review.flags
            if not (ignore_macros and flag.startswith("nutrition:"))
        ]
        if flags or (
            recipe.review.status == "needs_review" and not recipe.review.flags
        ):
            reasons.append("needs review")
        if recipe.servings is None:
            reasons.append("missing servings")
        if (
            not set(recipe.tags) & set(targets.planned_slots)
            and identifier not in pinned
        ):
            reasons.append("no tag for a planned meal slot")
        lines = [line for line in recipe.ingredients if not line.optional]
        if not lines or any(
            line.grams is None or line.ingredient_id not in catalog for line in lines
        ):
            reasons.append("unresolved ingredients or grams")
        if not ignore_macros:
            if not recipe.nutrition or recipe.nutrition.coverage < 0.95:
                reasons.append("nutrition coverage below 95%")
            elif any(
                getattr(recipe.nutrition.per_serving, name) is None for name in required
            ):
                reasons.append("missing a constrained nutrient")
        if reasons:
            rejected[identifier] = reasons
            continue
        available = {
            stock.ingredient_id
            for stock in pantry.items
            if stock.status != "out"
            and (stock.expires is None or stock.expires >= today)
            and (stock.qty_g is None or stock.qty_g > 0)
        }
        overlap = sum(
            line.grams or 0 for line in lines if line.ingredient_id in available
        ) / max(1, sum(line.grams or 0 for line in lines))
        novelty, rating = history_scores(identifier, history, today)
        scores[identifier] = 3 * overlap + novelty + rating
    bad_pins = pinned & rejected.keys()
    if bad_pins:
        raise BourdainError(
            "Pinned recipe is ineligible: "
            + "; ".join(
                f"{identifier}: {', '.join(rejected[identifier])}"
                for identifier in sorted(bad_pins)
            )
        )
    ordered = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))
    priority = pinned | ((preferred or set()) & scores.keys())
    selected = (
        sorted(priority)
        + [identifier for identifier in ordered if identifier not in priority][
            : max(0, limit - len(priority))
        ]
    )
    for identifier in set(scores) - set(selected):
        rejected[identifier] = ["outside candidate limit"]
    return Candidates(
        {identifier: recipes[identifier] for identifier in sorted(selected)},
        rejected,
        scores,
    )
