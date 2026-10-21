from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter
from ruamel.yaml import YAML
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import groceries
from bourdain.grocery.export import export_list
from bourdain.models import (
    Catalog,
    CatalogItem,
    CookEvent,
    IngredientLine,
    Pantry,
    PantryItem,
    Plan,
    Recipe,
    Targets,
)
from bourdain.plan.manual import manual_plan
from bourdain.store import Repository, initialize


def world() -> tuple:
    data = YAML(typ="safe").load(
        (Path(__file__).parent / "fixtures" / "week.yaml").read_text(encoding="utf-8")
    )
    recipes = {raw["id"]: Recipe.model_validate(raw) for raw in data["recipes"]}
    targets = Targets.model_validate(data["targets"])
    catalog = TypeAdapter(Catalog).validate_python(data["catalog"])
    pantry = Pantry.model_validate(data["pantry"])
    plan = manual_plan("2026-W43", data["assignments"], recipes, targets)
    return plan, recipes, catalog, pantry


@pytest.mark.parametrize(
    ("format", "filename"),
    [
        ("md", "groceries.md"),
        ("text", "groceries.txt"),
        ("todoist-csv", "groceries.csv"),
    ],
)
def test_grocery_golden(
    format: str, filename: str, golden: Callable[[str, str], None]
) -> None:
    plan, recipes, catalog, pantry = world()
    assert [event.batches for event in plan.cook_events] == [0.5, 1, 2]
    result = groceries(plan, recipes, catalog, pantry, today=date(2026, 10, 21))
    by_id = {item.ingredient_id: item for item in result.items}
    assert by_id["beans"].need_g == 700
    assert by_id["beans"].buy_g == 800
    assert by_id["tomato"].buy_g == 400
    assert "oil" not in by_id and "cheese" not in by_id
    golden(filename, export_list(result, format))


def test_unknown_grams_fail_and_optional_ingredients_are_opt_in() -> None:
    plan, recipes, catalog, pantry = world()
    result = groceries(plan, recipes, catalog, pantry, include_optional=True)
    assert (
        next(item for item in result.items if item.ingredient_id == "cheese").buy_g
        == 50
    )
    recipes["test/soup"].ingredients[0].grams = None
    with pytest.raises(BourdainError, match="Resolve grams"):
        groceries(plan, recipes, catalog, pantry)


def test_expired_pantry_is_not_subtracted() -> None:
    plan, recipes, catalog, pantry = world()
    pantry.items[2].expires = date(2026, 10, 20)
    pantry.items[2].qty_g = 1000
    result = groceries(plan, recipes, catalog, pantry, today=date(2026, 10, 21))
    assert (
        next(item for item in result.items if item.ingredient_id == "beans").buy_g
        == 800
    )
    assert result.warnings == ["Expired pantry item ignored: Beans"]


@pytest.mark.parametrize("status", ["have", "low", "out"])
def test_coarse_inventory_semantics(status: str) -> None:
    plan, recipes, catalog, pantry = world()
    pantry.items[2] = PantryItem(ingredient_id="beans", status=status)
    items = {
        item.ingredient_id: item
        for item in groceries(plan, recipes, catalog, pantry).items
    }
    if status == "have":
        assert "beans" not in items
    elif status == "low":
        assert items["beans"].check
        assert items["beans"].buy_g == 0
    else:
        assert items["beans"].buy_g == 800


def test_plan_packages_are_used_or_stale_plan_reported() -> None:
    plan, recipes, catalog, pantry = world()
    plan.package_counts = {"beans": 3}
    plan.package_sizes_g = {"beans": 300}
    assert (
        next(
            item
            for item in groceries(plan, recipes, catalog, pantry).items
            if item.ingredient_id == "beans"
        ).buy_g
        == 900
    )
    plan.package_counts = {"beans": 1}
    with pytest.raises(BourdainError, match="no longer cover"):
        groceries(plan, recipes, catalog, pantry)


@given(
    need=st.floats(min_value=0.1, max_value=1e5),
    stock=st.floats(min_value=0, max_value=1e5),
)
def test_loose_goods_conserve_grams(need: float, stock: float) -> None:
    recipe = Recipe(
        id="test/recipe",
        title="Test",
        servings=1,
        ingredients=[
            IngredientLine(
                raw="ingredient",
                item="ingredient",
                ingredient_id="ingredient",
                grams=need,
            )
        ],
    )
    plan = Plan(
        week="2026-W43",
        cook_events=[CookEvent(recipe=recipe.id, day="mon", slot="dinner")],
    )
    result = groceries(
        plan,
        {recipe.id: recipe},
        {"ingredient": CatalogItem(name="Ingredient")},
        Pantry(items=[PantryItem(ingredient_id="ingredient", qty_g=stock)]),
    )
    bought = sum(item.buy_g for item in result.items)
    assert bought >= 0
    assert bought + min(need, stock) == pytest.approx(need)


def test_end_to_end_cli_and_utf8_output(tmp_path: Path) -> None:
    runner = CliRunner()
    root = tmp_path / "data"
    assert runner.invoke(app, ["init", str(root)]).exit_code == 0
    prefix = ["--data", str(root)]
    text = "Carrot Soup\nServings: 2\n200 g carrots\n"
    result = runner.invoke(app, [*prefix, "ingest", "text", "-"], input=text)
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [*prefix, "plan", "--week", "2026-W43", "--add", "mon dinner=text/carrot-soup"],
    )
    assert result.exit_code == 0, result.output
    output = tmp_path / "groceries.md"
    result = runner.invoke(app, [*prefix, "list", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert "carrots — 100 g" in output.read_text(encoding="utf-8")
    assert (
        runner.invoke(
            app,
            [
                *prefix,
                "plan",
                "--week",
                "2026-W43",
                "--add",
                "mon dinner=text/carrot-soup",
            ],
        ).exit_code
        == 2
    )


def test_manual_editor_and_duplicate_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    repo.save_recipe(Recipe(id="test/soup", title="Soup", servings=1))
    monkeypatch.setattr(
        "bourdain.cli.click.edit",
        lambda *args, **kwargs: "mon dinner: test/soup\nfri dinner: out\n",
    )
    result = CliRunner().invoke(
        app, ["--data", str(repo.root), "plan", "--manual", "--week", "2026-W43"]
    )
    assert result.exit_code == 0, result.output
    assert repo.plan("2026-W43").model.locks[0].value == "out"
    with pytest.raises(BourdainError, match="Duplicate"):
        manual_plan(
            "2026-W43",
            ["mon dinner=test/soup", "mon dinner=out"],
            repo.recipes(),
            repo.targets().model,
        )
