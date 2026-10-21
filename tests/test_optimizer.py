import time
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.config import PlanConfig
from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import groceries
from bourdain.models import (
    BuyInfo,
    CatalogItem,
    Eater,
    IngredientLine,
    Macros,
    MealLock,
    Nutrition,
    Pantry,
    PantryItem,
    Plan,
    Recipe,
    Targets,
)
from bourdain.plan.candidates import select_candidates
from bourdain.plan.model import optimize
from bourdain.plan.presets import preferences
from bourdain.prices import PackagePrice
from bourdain.store import Repository, initialize


def world(count: int = 4) -> tuple:
    catalog, recipes, prices = {}, {}, {}
    for index in range(count):
        identifier = f"food-{index}"
        catalog[identifier] = CatalogItem(
            name=identifier,
            perishable=True,
            edible_fraction=1,
            buy=BuyInfo(package_g=[100], section="produce"),
        )
        prices[identifier] = [PackagePrice(package_g=100, price=1)]
        recipes[f"test/recipe-{index}"] = Recipe(
            id=f"test/recipe-{index}",
            title=f"Recipe {index}",
            tags=["dinner", "lunch"],
            servings=2,
            keeps_days=3,
            protein="plant",
            ingredients=[
                IngredientLine(
                    raw=f"200 g {identifier}",
                    item=identifier,
                    ingredient_id=identifier,
                    grams=200,
                )
            ],
            nutrition=Nutrition(
                source="fdc",
                coverage=1,
                per_serving=Macros(
                    kcal=100,
                    protein_g=10,
                    fat_g=2,
                    sat_fat_g=0,
                    carb_g=10,
                    fiber_g=2,
                    sugar_g=0,
                    sodium_mg=100,
                ),
            ),
        )
    pantry = Pantry(
        items=[
            PantryItem(ingredient_id="food-0", qty_g=200, expires=date(2026, 10, 19))
        ]
    )
    targets = Targets(
        days=["mon", "tue", "wed", "thu"],
        eaters=[Eater(name="me", daily={"protein_g": (10, 10)})],
    )
    return recipes, catalog, pantry, targets, prices


def run(lean: float = 0, **kwargs: object) -> Plan:
    recipes, catalog, pantry, targets, prices = world()
    return optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        lean=lean,
        config=PlanConfig(max_batches_per_cook=1, time_limit_s=5),
        **kwargs,
    )


def test_lean_changes_variety_and_packages_without_changing_macros() -> None:
    thrifty, variety = run(0), run(1)
    assert thrifty.macro_slack == variety.macro_slack == {}
    assert thrifty.daily_macros == variety.daily_macros
    assert thrifty.objective_breakdown["distinct_recipes"] == 2
    assert variety.objective_breakdown["distinct_recipes"] == 4
    assert sum(thrifty.package_counts.values()) < sum(variety.package_counts.values())
    assert thrifty.pantry_usage_g["food-0"] == 200
    assert all(
        sum(meal.recipe == identifier for meal in variety.meals) <= 2
        for identifier in set(meal.recipe for meal in variety.meals)
    )


def test_locks_pins_and_known_macro_shortfall() -> None:
    recipes, catalog, pantry, targets, prices = world()
    targets.eaters[0].daily = {"protein_g": (25, None)}
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        locks=[
            MealLock(day="thu", slot="dinner", value="out"),
            MealLock(day="mon", slot="dinner", value="test/recipe-1"),
        ],
        pinned={"test/recipe-3"},
        config=PlanConfig(time_limit_s=5),
    )
    assert (
        next(meal for meal in plan.meals if meal.day == "mon").recipe == "test/recipe-1"
    )
    assert not any(meal.day == "thu" for meal in plan.meals)
    assert any(meal.recipe == "test/recipe-3" for meal in plan.meals)
    assert plan.macro_slack["me:mon:protein_g:min"] == pytest.approx(15)
    assert plan.macro_slack["me:thu:protein_g:min"] == pytest.approx(25)
    assert any("below minimum" in warning for warning in plan.warnings)


def test_expired_stock_cannot_feed_a_later_cook() -> None:
    recipes, catalog, pantry, targets, prices = world()
    targets.days = ["mon", "tue"]
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        locks=[
            MealLock(day="mon", slot="dinner", value="test/recipe-1"),
            MealLock(day="tue", slot="dinner", value="test/recipe-0"),
        ],
        config=PlanConfig(time_limit_s=5),
    )
    assert plan.pantry_usage_g["food-0"] == 0
    assert plan.package_counts["food-0"] == 2


def test_leftovers_respect_windows_and_do_not_create_extra_batches() -> None:
    recipes, catalog, pantry, targets, prices = world()
    targets.days = ["mon", "wed"]
    recipes = {"test/recipe-0": recipes["test/recipe-0"]}
    recipes["test/recipe-0"].keeps_days = 0
    with pytest.raises(BourdainError, match="leftover window"):
        optimize(
            "2026-W43",
            recipes,
            catalog,
            pantry,
            targets,
            [],
            prices,
            config=PlanConfig(time_limit_s=3),
        )
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        config=PlanConfig(allow_repeat_cook=True, time_limit_s=3),
    )
    assert len(plan.cook_events) == 2
    assert all(event.batches == 1 for event in plan.cook_events)
    assert not any(meal.leftover for meal in plan.meals)


def test_groceries_use_exact_solver_purchases_and_reject_stale_inventory() -> None:
    recipes, catalog, pantry, targets, prices = world()
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        lean=0,
        config=PlanConfig(time_limit_s=5),
    )
    shopping = groceries(plan, recipes, catalog, pantry, today=date(2026, 10, 19))
    assert {item.ingredient_id: item.packages for item in shopping.items} == {
        identifier: count
        for identifier, count in plan.package_counts.items()
        if count > 0
    }
    with pytest.raises(BourdainError, match="Optional ingredients"):
        groceries(plan, recipes, catalog, pantry, include_optional=True)
    recipe = recipes[plan.cook_events[0].recipe]
    original = recipe.ingredients
    catalog["new-food"] = CatalogItem(name="new food")
    added = IngredientLine(
        raw="50 g new food", item="new food", ingredient_id="new-food", grams=50
    )
    for changed in [
        [],
        original + [added],
        [original[0].model_copy(update={"grams": 1})],
    ]:
        recipe.ingredients = changed
        with pytest.raises(BourdainError, match="Recipe quantities changed"):
            groceries(plan, recipes, catalog, pantry, today=date(2026, 10, 19))
    recipe.ingredients = original
    pantry.items[0].qty_g = 0
    with pytest.raises(BourdainError, match="Pantry changed"):
        groceries(plan, recipes, catalog, pantry, today=date(2026, 10, 19))


def test_candidate_filter_determinism_and_bad_pins() -> None:
    recipes, catalog, pantry, targets, _ = world()
    recipes["test/recipe-2"].excluded = True
    recipes["test/recipe-3"].nutrition = None
    first = select_candidates(
        recipes,
        catalog,
        pantry,
        targets,
        [],
        today=date(2026, 10, 19),
        limit=1,
        pinned={"test/recipe-1"},
    )
    assert list(first.recipes) == ["test/recipe-1"]
    assert first == select_candidates(
        dict(reversed(list(recipes.items()))),
        catalog,
        pantry,
        targets,
        [],
        today=date(2026, 10, 19),
        limit=1,
        pinned={"test/recipe-1"},
    )
    with pytest.raises(BourdainError, match="ineligible"):
        select_candidates(
            recipes,
            catalog,
            pantry,
            targets,
            [],
            today=date(2026, 10, 19),
            pinned={"test/recipe-2"},
        )


def test_replan_freezes_past_and_uses_current_stock() -> None:
    recipes, catalog, pantry, targets, prices = world()
    first = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        lean=0,
        config=PlanConfig(max_batches_per_cook=1, time_limit_s=5),
    )
    current = Pantry()
    updated = optimize(
        "2026-W43",
        recipes,
        catalog,
        current,
        targets,
        [],
        prices,
        lean=1,
        previous=first,
        replan_from="wed",
        config=PlanConfig(max_batches_per_cook=1, time_limit_s=5),
    )
    assert [meal for meal in updated.meals if meal.day in {"mon", "tue"}] == [
        meal for meal in first.meals if meal.day in {"mon", "tue"}
    ]
    assert [event for event in updated.cook_events if event.day in {"mon", "tue"}] == [
        event for event in first.cook_events if event.day in {"mon", "tue"}
    ]
    shopping = groceries(updated, recipes, catalog, current, today=date(2026, 10, 21))
    assert sum(item.buy_g for item in shopping.items) == sum(
        count * updated.package_sizes_g[identifier]
        for identifier, count in updated.package_counts.items()
    )


def test_twenty_recipe_plan_to_groceries_under_ten_seconds() -> None:
    recipes, catalog, pantry, targets, prices = world(20)
    targets.days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    targets.planned_slots = ["lunch", "dinner"]
    targets.eaters[0].daily = {"protein_g": (20, 20)}
    started = time.monotonic()
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        config=PlanConfig(time_limit_s=5),
    )
    groceries(plan, recipes, catalog, pantry, today=date(2026, 10, 19))
    assert len(plan.meals) == 14 and plan.macro_slack == {}
    assert time.monotonic() - started < 10


def test_optimizer_cli_compare_and_save(tmp_path: Path) -> None:
    from bourdain.prices import price_document

    recipes, catalog, pantry, targets, prices = world()
    repo = Repository(initialize(tmp_path / "data"))
    for recipe in recipes.values():
        repo.save_recipe(recipe)
    for document, model in [
        (repo.catalog(), catalog),
        (repo.pantry(), pantry),
        (repo.targets(), targets),
        (price_document(repo), prices),
    ]:
        document.save(model)
    runner = CliRunner()
    prefix = ["--data", str(repo.root), "plan", "--week", "2026-W43"]
    compared = runner.invoke(app, [*prefix, "--compare", "0,0.5,1"])
    assert compared.exit_code == 0, compared.output
    assert "Macro slack" in compared.output
    assert not repo.path("plans/2026-W43.yaml").exists()
    saved = runner.invoke(app, [*prefix, "--lean", "0", "--explain"])
    assert saved.exit_code == 0, saved.output
    assert "Saved plans/2026-W43.yaml" in saved.output
    assert repo.plan("2026-W43").model.meals


def test_presets_hold_macro_priority_constant() -> None:
    assert preferences(0).weights["macro"] == preferences(1).weights["macro"]
    assert preferences(0.5).max_meals_per_recipe == 4
    with pytest.raises(BourdainError, match="Macro priority"):
        preferences(0, overrides={"macro": 0})


def test_infeasibility_diagnostic_identifies_cook_budget() -> None:
    recipes, catalog, pantry, targets, prices = world()
    targets.days = ["mon", "tue"]
    for recipe in recipes.values():
        recipe.keeps_days = 0
    with pytest.raises(BourdainError, match="relaxing: cook event budget"):
        optimize(
            "2026-W43",
            recipes,
            catalog,
            pantry,
            targets,
            [],
            prices,
            config=PlanConfig(max_cooks=1, time_limit_s=3),
        )


def test_weekly_average_and_two_eaters() -> None:
    recipes, catalog, pantry, targets, prices = world()
    targets.days = ["mon", "tue"]
    targets.macro_mode = "weekly_average"
    targets.eaters = [
        Eater(
            name="me",
            servings_per_slot=1,
            daily={"protein_g": (13, 13)},
            fixed_daily={"protein_g": 3},
        ),
        Eater(
            name="partner",
            servings_per_slot=0.5,
            daily={"protein_g": (7, 7)},
            fixed_daily={"protein_g": 2},
        ),
    ]
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        config=PlanConfig(time_limit_s=3),
    )
    assert plan.macro_slack == {}
    assert plan.eater_daily_macros["me"]["mon"].protein_g == 13
    assert plan.eater_daily_macros["partner"]["mon"].protein_g == 7


def test_all_out_and_explicit_ignore_macros() -> None:
    targets = Targets(days=["mon"])
    plan = optimize(
        "2026-W43",
        {},
        {},
        Pantry(),
        targets,
        [],
        {},
        locks=[MealLock(day="mon", slot="dinner", value="out")],
    )
    assert plan.meals == [] and plan.cook_events == []
    recipes, catalog, pantry, targets, prices = world()
    for recipe in recipes.values():
        recipe.nutrition = None
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        ignore_macros=True,
        config=PlanConfig(time_limit_s=3),
    )
    assert plan.daily_macros["mon"].protein_g is None
    assert "Macro targets were explicitly ignored" in plan.warnings


def test_returns_macro_incumbent_if_preference_solve_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import pulp

    original, calls = pulp.LpProblem.solve, []

    def solve(problem: pulp.LpProblem, solver: object) -> object:
        calls.append(1)
        if len(calls) == 2:
            return SimpleNamespace(
                has_solution=False, status_str="TimeLimit", gap_rel=None
            )
        return original(problem, solver)

    monkeypatch.setattr(pulp.LpProblem, "solve", solve)
    plan = run()
    assert plan.solver_status == "Macro incumbent"
    assert plan.macro_slack == {}
    assert len(plan.meals) == 4


def test_protein_cap_and_same_day_leftovers() -> None:
    recipes, catalog, pantry, targets, prices = world()
    targets.days = ["mon"]
    targets.planned_slots = ["lunch", "dinner"]
    targets.eaters[0].daily = {"protein_g": (20, 20)}
    only = {"test/recipe-0": recipes["test/recipe-0"]}
    plan = optimize(
        "2026-W43",
        only,
        catalog,
        pantry,
        targets,
        [],
        prices,
        config=PlanConfig(time_limit_s=3),
    )
    assert len(plan.cook_events) == 1 and plan.cook_events[0].batches == 1
    assert [meal.leftover for meal in plan.meals] == [False, True]
    recipes["test/recipe-2"].protein = "fish"
    recipes["test/recipe-3"].protein = "fish"
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        prices,
        config=PlanConfig(max_per_protein={"plant": 1}, time_limit_s=3),
    )
    assert sum(recipes[meal.recipe].protein == "plant" for meal in plan.meals) <= 1
