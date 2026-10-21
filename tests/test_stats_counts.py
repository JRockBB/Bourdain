import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bourdain.catalog import set_unit_weight
from bourdain.cli import app
from bourdain.config import PlanConfig
from bourdain.grocery.aggregate import groceries
from bourdain.grocery.export import export_list
from bourdain.ingest.text import from_text
from bourdain.library import import_draft
from bourdain.models import CatalogItem, Pantry, Recipe, Targets
from bourdain.normalize.parse import parse_line
from bourdain.normalize.units import to_grams
from bourdain.pantry.service import bought
from bourdain.plan.manual import manual_plan
from bourdain.plan.model import optimize
from bourdain.stats import summarize
from bourdain.store import Repository, initialize


def test_count_conversion_rounding_and_bought(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    repo.catalog().save(
        {"onion": CatalogItem(name="Onions", aliases=["onions", "onion"])}
    )
    result = CliRunner().invoke(
        app,
        [
            "--data",
            str(repo.root),
            "catalog",
            "unit-weight",
            "onion",
            "medium",
            "150",
            "--buy",
        ],
    )
    assert result.exit_code == 0, result.output
    item = repo.catalog().model["onion"]
    assert to_grams(parse_line("1 medium onion"), item).grams == 150
    assert to_grams(parse_line("1 onion"), item).grams is None
    assert to_grams(parse_line("1 cup onion"), item).grams is None
    recipe = import_draft(
        repo, from_text("Onion soup\nServings: 1\n170 g onion\n1 medium onion")
    )
    recipes, catalog, pantry = {recipe.id: recipe}, repo.catalog().model, Pantry()
    plan = manual_plan(
        "2026-W43", [f"mon dinner={recipe.id}"], recipes, Targets(days=["mon"])
    )
    result = groceries(plan, recipes, catalog, pantry)
    assert result.items[0].need_g == 320
    assert result.items[0].buy_g == 450
    for format_ in ["md", "text", "todoist-csv"]:
        assert "3 × medium @ ~150 g each" in export_list(result, format_)
    path = tmp_path / "shopping.md"
    path.write_text(export_list(result).replace("[ ]", "[x]"), encoding="utf-8")
    assert bought(repo, path) == ["onion"]
    assert repo.pantry().model.items[0].qty_g == 450
    # A selected shopping unit also becomes a whole-item option in optimization.
    recipe.tags = ["dinner"]
    plan = optimize(
        "2026-W43",
        recipes,
        catalog,
        pantry,
        Targets(days=["mon"]),
        [],
        {},
        ignore_macros=True,
        config=PlanConfig(time_limit_s=2),
    )
    assert plan.package_purchases["onion"][0].count == 3
    assert plan.package_purchases["onion"][0].unit == "medium"
    assert plan.ingredient_needs_g["onion"] == 320
    assert groceries(plan, recipes, catalog, pantry).items[0].buy_g == 450
    with pytest.raises(ValueError):
        set_unit_weight(repo, "onion", "medium", -1)


def test_stats_empty_library_and_read_only_backlog(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    assert summarize(repo)["flags_per_recipe"] == 0
    repo.save_recipe(Recipe(id="unknown", title="Unknown"))
    import_draft(repo, from_text("Soup\n1 medium onion"))
    before = {
        path.relative_to(repo.root): path.read_bytes()
        for path in repo.root.rglob("*")
        if path.is_file()
    }
    report = summarize(repo, today=date(2026, 10, 19))
    assert report["recipes"] == 2 and report["planning_ready"] == 0
    assert report["missing_grams"] == 1
    assert report["blocking_reasons"]["missing servings"] == 2
    result = CliRunner().invoke(app, ["--data", str(repo.root), "stats", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["recipes"] == 2
    assert before == {
        path.relative_to(repo.root): path.read_bytes()
        for path in repo.root.rglob("*")
        if path.is_file()
    }
