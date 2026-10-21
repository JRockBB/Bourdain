from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.config import PlanConfig
from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import groceries
from bourdain.grocery.export import export_list
from bourdain.models import (
    BuyInfo,
    CatalogItem,
    IngredientLine,
    PackagePurchase,
    Pantry,
    PantryItem,
    Plan,
    Recipe,
    Targets,
)
from bourdain.pantry.service import bought
from bourdain.plan.model import optimize
from bourdain.prices import PackagePrice, package_options, price_document
from bourdain.store import Repository, initialize


def package_world(need_g: float, stock_g: float = 0) -> tuple:
    recipe = Recipe(
        id="test/soup",
        title="Soup",
        servings=1,
        tags=["dinner"],
        ingredients=[
            IngredientLine(
                raw=f"{need_g:g} g carrots",
                item="carrots",
                ingredient_id="carrot",
                grams=need_g,
            )
        ],
    )
    recipes = {recipe.id: recipe}
    catalog = {
        "carrot": CatalogItem(
            name="Carrots",
            perishable=True,
            shelf_life_days=7,
            buy=BuyInfo(section="produce", package_g=[250, 1000]),
        )
    }
    pantry = Pantry(items=[PantryItem(ingredient_id="carrot", qty_g=stock_g)])
    prices = {
        "carrot": [
            PackagePrice(package_g=250, price=2),
            PackagePrice(package_g=1000, price=5),
        ]
    }
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        Targets(days=["mon"]),
        [],
        prices,
        ignore_macros=True,
        lean=0,
        config=PlanConfig(time_limit_s=3),
    )
    return plan, recipes, catalog, pantry, prices


@pytest.mark.parametrize(
    ("need_g", "stock_g", "expected", "cost"),
    [
        (200, 0, [(250, 1)], 2),
        (1250, 0, [(250, 1), (1000, 1)], 7),
        (1100, 0, [(250, 1), (1000, 1)], 7),
        (1250, 250, [(1000, 1)], 5),
        (1250, 1250, [], 0),
    ],
)
def test_optimizer_selects_package_mix_for_actual_shortfall(
    need_g: float, stock_g: float, expected: list[tuple[float, int]], cost: float
) -> None:
    plan, recipes, catalog, pantry, _ = package_world(need_g, stock_g)
    purchases = plan.package_purchases["carrot"]
    assert [(purchase.package_g, purchase.count) for purchase in purchases] == expected
    assert plan.objective_breakdown["cost"] == pytest.approx(cost)
    assert sum(p.count * p.unit_price for p in purchases) == pytest.approx(cost)
    assert plan.package_counts["carrot"] == sum(count for _, count in expected)
    if len(expected) > 1:
        assert "carrot" not in plan.package_sizes_g
    saved = Plan.model_validate_json(plan.model_dump_json())
    result = groceries(saved, recipes, catalog, pantry, today=date(2026, 10, 19))
    assert sum(item.buy_g for item in result.items) == sum(
        size * count for size, count in expected
    )
    assert sum(item.packages or 0 for item in result.items) == sum(
        count for _, count in expected
    )


def test_mixed_package_exports_and_purchase_recording(
    tmp_path: Path, golden: Callable[[str, str], None]
) -> None:
    plan, recipes, catalog, pantry, prices = package_world(1250)
    shopping = groceries(plan, recipes, catalog, pantry, today=date(2026, 10, 19))
    markdown = export_list(shopping)
    golden("mixed-packages.md", markdown)
    for format_ in ["text", "todoist-csv"]:
        assert "1 × 250 g package + 1 × 1000 g package" in export_list(
            shopping, format_
        )
    repo = Repository(initialize(tmp_path / "data"))
    repo.catalog().save(catalog)
    repo.pantry().save(pantry)
    price_document(repo).save(prices)
    for recipe in recipes.values():
        repo.save_recipe(recipe)
    repo.save_plan(plan)
    assert repo.plan(plan.week).model.package_purchases == plan.package_purchases
    output = tmp_path / "groceries.md"
    result = CliRunner().invoke(
        app,
        [
            "--data",
            str(repo.root),
            "list",
            "--week",
            plan.week,
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 × 250 g package + 1 × 1000 g package" in output.read_text(
        encoding="utf-8"
    )
    checked = markdown.replace("[ ]", "[x]")
    output.write_text(
        checked.replace("250 g package", "500 g package"), encoding="utf-8"
    )
    with pytest.raises(BourdainError, match="differs from its metadata"):
        bought(repo, output)
    assert repo.pantry().model == pantry
    output.write_text(checked, encoding="utf-8")
    assert bought(repo, output, today=date(2026, 10, 19)) == ["carrot"]
    assert bought(repo, output, today=date(2026, 10, 19)) == []
    assert sum(item.qty_g or 0 for item in repo.pantry().model.items) == 1250
    output.write_text(
        "- [x] Carrots — 1 × 250 g package + 1 × 1000 g package (Soup)\n",
        encoding="utf-8",
    )
    assert bought(repo, output, purchase_id="second-trip") == ["carrot"]
    assert sum(item.qty_g or 0 for item in repo.pantry().model.items) == 2500


def test_price_options_are_deduplicated_and_fallback_sizes_stay_visible() -> None:
    item = CatalogItem(
        name="Carrots", buy=BuyInfo(package_g=[1000, 250, 250], section="produce")
    )
    estimates = package_options("carrot", item, {})
    assert [(option.package_g, option.price) for option in estimates] == [
        (250, 1),
        (1000, 4),
    ]
    assert all("estimated" in option.warning for option in estimates)
    options = package_options(
        "carrot",
        item,
        {
            "carrot": [
                PackagePrice(package_g=1000, price=4),
                PackagePrice(package_g=1000, price=3),
                PackagePrice(package_g=250, price=1),
            ]
        },
    )
    assert [(option.package_g, option.price) for option in options] == [
        (250, 1),
        (1000, 3),
    ]
    with pytest.raises(BourdainError, match="currency"):
        package_options(
            "carrot",
            item,
            {"carrot": [PackagePrice(package_g=250, price=1, currency="EUR")]},
        )


def test_plan_rejects_inconsistent_package_decisions() -> None:
    purchases = [
        PackagePurchase(package_g=250, count=1),
        PackagePurchase(package_g=1000, count=1),
    ]
    with pytest.raises(ValidationError, match="counts must agree"):
        Plan(
            week="2026-W43",
            package_counts={"carrot": 1},
            package_purchases={"carrot": purchases},
        )
    with pytest.raises(ValidationError, match="single package size"):
        Plan(
            week="2026-W43",
            package_counts={"carrot": 2},
            package_sizes_g={"carrot": 1000},
            package_purchases={"carrot": purchases},
        )
    with pytest.raises(ValidationError, match="unique sizes"):
        Plan(
            week="2026-W43",
            package_counts={"carrot": 2},
            package_purchases={"carrot": [purchases[0], purchases[0]]},
        )
