"""Read-only library readiness and review-backlog statistics."""

from collections import Counter
from datetime import date
from typing import Any

from bourdain.freezer import read_freezer
from bourdain.plan.candidates import select_candidates
from bourdain.store import Repository


def summarize(repo: Repository, *, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    recipes, catalog, pantry = repo.recipes(), repo.catalog().model, repo.pantry().model
    freezer = read_freezer(repo)
    selection = select_candidates(
        recipes,
        catalog,
        pantry,
        repo.targets().model,
        repo.history().model,
        today=today,
        limit=max(1, len(recipes)),
    )
    flags = Counter(
        flag for recipe in recipes.values() for flag in set(recipe.review.flags)
    )
    reasons = Counter(
        reason for values in selection.excluded.values() for reason in values
    )
    review = [
        recipe.id
        for recipe in recipes.values()
        if recipe.review.status == "needs_review"
    ]
    return {
        "recipes": len(recipes),
        "needs_review": len(review),
        "excluded": sum(recipe.excluded for recipe in recipes.values()),
        "planning_ready": len(selection.recipes),
        "nutrition_complete": sum(
            bool(
                recipe.nutrition
                and recipe.nutrition.coverage >= 0.95
                and all(
                    value is not None
                    for value in recipe.nutrition.per_serving.model_dump().values()
                )
            )
            for recipe in recipes.values()
        ),
        "mean_nutrition_coverage": sum(
            recipe.nutrition.coverage if recipe.nutrition else 0
            for recipe in recipes.values()
        )
        / max(1, len(recipes)),
        "flags_per_recipe": sum(flags.values()) / max(1, len(recipes)),
        "catalog_ingredients": len(catalog),
        "freezer_servings_available": sum(
            lot.servings
            for lot in freezer.items
            if lot.frozen_on <= today <= lot.expires
        ),
        "missing_grams": sum(
            line.grams is None
            for recipe in recipes.values()
            for line in recipe.ingredients
            if not line.optional
        ),
        "unmatched_lines": sum(
            line.ingredient_id not in catalog
            for recipe in recipes.values()
            for line in recipe.ingredients
            if not line.optional
        ),
        "expired_pantry_lots": sum(
            bool(item.expires and item.expires < today and item.status != "out")
            for item in pantry.items
        ),
        "review_recipes": sorted(review),
        "ready_recipes": sorted(selection.recipes),
        "flags": dict(sorted(flags.items())),
        "blocking_reasons": dict(sorted(reasons.items())),
        "by_book": dict(
            sorted(
                Counter(
                    recipe.source.book or recipe.source.kind
                    for recipe in recipes.values()
                ).items()
            )
        ),
    }
