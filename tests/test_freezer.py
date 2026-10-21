from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.config import PlanConfig
from bourdain.errors import BourdainError
from bourdain.freezer import add_portions, eat_portions, make_lot, read_freezer
from bourdain.grocery.aggregate import groceries
from bourdain.models import (
    CatalogItem,
    Eater,
    Freezer,
    IngredientLine,
    Macros,
    Nutrition,
    Pantry,
    PantryItem,
    Recipe,
    Targets,
)
from bourdain.pantry.service import cooked
from bourdain.plan.model import optimize
from bourdain.store import Repository, initialize


def world() -> tuple:
    recipe = Recipe(
        id="soup",
        title="Soup",
        servings=2,
        freezes=True,
        tags=["dinner"],
        keeps_days=1,
        ingredients=[
            IngredientLine(
                raw="200 g beans", item="beans", ingredient_id="beans", grams=200
            )
        ],
        nutrition=Nutrition(coverage=1, per_serving=Macros(kcal=100, protein_g=10)),
    )
    catalog = {"beans": CatalogItem(name="Beans")}
    lot = make_lot(
        recipe, 3, date(2026, 11, 1), lot_id="old-soup", today=date(2026, 10, 1)
    )
    return {recipe.id: recipe}, catalog, Pantry(), Freezer(items=[lot])


def solve(
    recipes: dict, catalog: dict, pantry: Pantry, freezer: Freezer, **kwargs: object
):
    return optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        Targets(days=["mon", "tue", "wed"]),
        [],
        {},
        freezer=freezer,
        config=PlanConfig(time_limit_s=3),
        **kwargs,
    )


def test_freezer_servings_replace_cooking_and_groceries_without_mutation() -> None:
    recipes, catalog, pantry, freezer = world()
    before = freezer.model_dump()
    plan = solve(recipes, catalog, pantry, freezer)
    assert plan.cook_events == []
    assert plan.freezer_usage == {"old-soup": 3}
    assert all(
        meal.freezer_lot == "old-soup" and meal.cook_day is None for meal in plan.meals
    )
    assert plan.daily_macros["mon"].protein_g == 10
    assert (
        groceries(
            plan, recipes, catalog, pantry, freezer=freezer, today=date(2026, 10, 19)
        ).items
        == []
    )
    assert freezer.model_dump() == before
    with pytest.raises(BourdainError, match="Freezer stock changed"):
        groceries(plan, recipes, catalog, pantry)
    freezer.items[0].servings = 1
    with pytest.raises(BourdainError, match="Freezer stock changed"):
        groceries(plan, recipes, catalog, pantry, freezer=freezer)
    replanned = solve(
        recipes, catalog, pantry, freezer, previous=plan, replan_from="wed"
    )
    assert replanned.meals[:2] == plan.meals[:2]
    assert replanned.freezer_usage == {"old-soup": 1}
    assert replanned.cook_events == []


def test_expiry_capacity_and_recipe_changes() -> None:
    recipes, catalog, pantry, freezer = world()
    freezer.items[0].servings = 1
    freezer.items[0].expires = date(2026, 10, 19)
    plan = solve(recipes, catalog, pantry, freezer)
    assert plan.freezer_usage == {"old-soup": 1}
    assert plan.meals[0].freezer_lot == "old-soup"
    assert len(plan.cook_events) == 1
    assert plan.ingredient_needs_g["beans"] == 200
    recipes["soup"].keeps_days = 2
    recipes["soup"].nutrition.per_serving.kcal = 200
    plan = solve(recipes, catalog, pantry, freezer)
    assert plan.freezer_usage == {}
    assert any("nutrition changed" in warning for warning in plan.warnings)


def test_household_can_combine_portions_from_multiple_lots() -> None:
    recipes, catalog, pantry, freezer = world()
    freezer.items[0].servings = 1
    freezer.items.append(freezer.items[0].model_copy(update={"id": "another-soup"}))
    targets = Targets(days=["mon"], eaters=[Eater(name="me"), Eater(name="partner")])
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        {},
        freezer=freezer,
        config=PlanConfig(time_limit_s=2),
    )
    assert plan.cook_events == []
    assert plan.meals[0].freezer_portions == {"old-soup": 1, "another-soup": 1}
    assert plan.freezer_usage == plan.meals[0].freezer_portions
    assert plan.meals[0].freezer_lot is None and plan.meals[0].leftover
    replan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        targets,
        [],
        {},
        freezer=Freezer(),
        previous=plan,
        replan_from="tue",
        config=PlanConfig(time_limit_s=2),
    )
    assert replan.meals == plan.meals and replan.freezer_usage == {}


def test_cooking_freezer_transaction_and_consumption_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bourdain.store.transaction as transaction

    repo = Repository(initialize(tmp_path / "data"))
    recipes, catalog, _, _ = world()
    repo.save_recipe(recipes["soup"])
    repo.catalog().save(catalog)
    repo.pantry().save(Pantry(items=[PantryItem(ingredient_id="beans", qty_g=1000)]))
    original = transaction.atomic_write

    def fail_freezer(path: Path, content: bytes, **kwargs: object) -> None:
        if path.name == "freezer.yaml":
            raise OSError("interrupted freezer write")
        original(path, content, **kwargs)

    monkeypatch.setattr(transaction, "atomic_write", fail_freezer)
    with pytest.raises(OSError, match="interrupted"):
        cooked(
            repo,
            "soup",
            batches=2,
            freeze_servings=3,
            freezer_expires=date(2026, 11, 1),
            today=date(2026, 10, 19),
            event_id="cook-once",
        )
    monkeypatch.setattr(transaction, "atomic_write", original)
    repo = Repository(repo.root)
    assert repo.pantry().model.items[0].qty_g == 600
    assert len(repo.history().model) == 1
    lot = read_freezer(repo).items[0]
    assert lot.servings == 3
    cooked(
        repo,
        "soup",
        batches=2,
        freeze_servings=3,
        freezer_expires=date(2026, 11, 1),
        today=date(2026, 10, 19),
        event_id="cook-once",
    )
    assert len(read_freezer(repo).items) == 1
    for _ in range(2):
        eat_portions(repo, lot.id, 1, today=date(2026, 10, 20), event_id="eat-once")
    assert read_freezer(repo).items[0].servings == 2
    assert repo.pantry().model.items[0].qty_g == 600
    with pytest.raises(BourdainError, match="Not enough"):
        eat_portions(repo, lot.id, 99, today=date(2026, 10, 20))
    result = CliRunner().invoke(app, ["--data", str(repo.root), "freezer", "ls"])
    assert result.exit_code == 0 and lot.id in result.output
    with pytest.raises(BourdainError, match="no more"):
        cooked(repo, "soup", freeze_servings=3, freezer_expires=date(2026, 11, 1))
    recipes["soup"].freezes = False
    repo.save_recipe(recipes["soup"], automated=False)
    with pytest.raises(BourdainError, match="freezable"):
        add_portions(repo, "soup", 1, date(2026, 11, 1))
