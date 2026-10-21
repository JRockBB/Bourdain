"""Manual plans scale batches to the household's servings per meal."""

from bourdain.errors import BourdainError
from bourdain.models import CookEvent, Meal, MealLock, Plan, Recipe, Targets


def manual_plan(
    week: str, assignments: list[str], recipes: dict[str, Recipe], targets: Targets
) -> Plan:
    plan = Plan(week=week)
    seen: set[tuple[str, str]] = set()
    servings = sum(eater.servings_per_slot for eater in targets.eaters)
    plan.household_servings = servings
    for assignment in assignments:
        try:
            when, recipe_id = assignment.split("=", 1)
            day, slot = when.strip().lower().split()
            recipe_id = recipe_id.strip()
        except ValueError as exc:
            raise BourdainError(f"Use 'mon dinner=book/recipe': {assignment}") from exc
        if day not in targets.days or slot not in targets.planned_slots:
            raise BourdainError(f"Meal is not enabled in targets.yaml: {day} {slot}")
        if (day, slot) in seen:
            raise BourdainError(f"Duplicate meal: {day} {slot}")
        seen.add((day, slot))
        if recipe_id == "out":
            plan.locks.append(MealLock(day=day, slot=slot, value="out"))
            continue
        if recipe_id not in recipes:
            raise BourdainError(f"Unknown recipe: {recipe_id}")
        recipe = recipes[recipe_id]
        if recipe.servings is None:
            raise BourdainError(f"Set servings before planning: {recipe_id}")
        if recipe.excluded:
            raise BourdainError(f"Recipe is excluded: {recipe_id}")
        plan.recipe_servings[recipe_id] = recipe.servings
        plan.meals.append(Meal(day=day, slot=slot, recipe=recipe_id))
        plan.cook_events.append(
            CookEvent(
                day=day, slot=slot, recipe=recipe_id, batches=servings / recipe.servings
            )
        )
    if not seen:
        raise BourdainError("Add at least one meal to the plan")
    return plan
