from collections.abc import Callable
from pathlib import Path

import pytest

from bourdain.cronometer.bridge import (
    export_recipes,
    import_nutrition,
    parse_csv,
    paste_block,
)
from bourdain.errors import BourdainError
from bourdain.models import CatalogItem, IngredientLine, Recipe
from bourdain.store import Repository, initialize


def setup(tmp_path: Path) -> Repository:
    repo = Repository(initialize(tmp_path / "data"))
    doc = repo.catalog()
    doc.model["carrot"] = CatalogItem(name="carrots", edible_fraction=0.9)
    doc.save()
    repo.save_recipe(
        Recipe(
            id="text/soup",
            title="Carrot Soup",
            servings=2,
            ingredients=[
                IngredientLine(
                    raw="200 g carrots",
                    item="carrots",
                    ingredient_id="carrot",
                    grams=200,
                )
            ],
        )
    )
    return repo


def test_paste_export_tracking_and_changed_recipe(
    tmp_path: Path, golden: Callable[[str, str], None]
) -> None:
    repo = setup(tmp_path)
    expected = "Carrot Soup\nServings: 2\n\n180 g carrots\n"
    assert paste_block(repo.recipe("text/soup").model, repo.catalog().model) == expected
    golden("cronometer.txt", expected)
    clipboard = []
    queue, copied = export_recipes(
        repo, all_new=True, clipboard=lambda text: clipboard.append(text) or True
    )
    assert copied and clipboard == [expected]
    assert repo.path("cronometer_queue.md").read_text() == queue
    assert export_recipes(repo, all_new=True)[0] == "No new recipes to export.\n"
    doc = repo.recipe("text/soup")
    doc.model.ingredients[0].grams = 300
    doc.save()
    assert (
        "270 g carrots"
        in export_recipes(repo, all_new=True, clipboard=lambda text: False)[0]
    )


def test_csv_unit_conversion_and_explicit_serving_basis(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    path = tmp_path / "recipe.csv"
    path.write_text(
        "Name,Energy (kJ),Protein (g),Fat (g),Saturated (g),Carbs (g),"
        "Fiber (g),Sugars (g),Sodium (g)\nCarrot Soup,836.8,10,4,1,20,5,6,0.2\n"
    )
    assert import_nutrition(repo, path, basis="full-recipe") == ["text/soup"]
    nutrition = repo.recipe("text/soup").model.nutrition
    assert nutrition.locked and nutrition.source == "cronometer"
    assert nutrition.per_serving.kcal == pytest.approx(100)
    assert nutrition.per_serving.sodium_mg == 100
    assert nutrition.coverage == 1
    import_nutrition(repo, path, basis="per-100g", recipe_weight_g=400)
    assert repo.recipe("text/soup").model.nutrition.per_serving.kcal == pytest.approx(
        400
    )


def test_vertical_partial_csv_and_ambiguous_recipe(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    path = tmp_path / "recipe.csv"
    path.write_text("Nutrient,Amount,Unit\nEnergy,100,kcal\nProtein,10,g\nSodium,,mg\n")
    assert parse_csv(path.read_text())[0][1].sodium_mg is None
    with pytest.raises(BourdainError, match="ambiguous"):
        import_nutrition(repo, path, basis="per-serving")
    import_nutrition(repo, path, basis="per-serving", recipe_id="text/soup")
    recipe = repo.recipe("text/soup").model
    assert recipe.nutrition.coverage == 0
    assert "nutrition:incomplete_cronometer" in recipe.review.flags


def test_csv_rejects_duplicates_without_partial_writes(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    before = repo.recipe_path("text/soup").read_bytes()
    path = tmp_path / "recipe.csv"
    path.write_text("Name,Energy (kcal)\nCarrot Soup,100\nCarrot Soup,200\n")
    with pytest.raises(BourdainError, match="Duplicate"):
        import_nutrition(repo, path, basis="per-serving")
    assert repo.recipe_path("text/soup").read_bytes() == before
