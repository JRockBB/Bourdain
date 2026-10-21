from pathlib import Path

from bourdain.ingest.text import from_text
from bourdain.library import import_draft
from bourdain.models import Macros, Nutrition
from bourdain.nutrition.fdc import FakeFDC, Food
from bourdain.nutrition.service import confirm_mapping, refresh
from bourdain.review import accept_ingredient, accept_recipe
from bourdain.store import Repository, initialize


def test_review_protects_extraction_without_blocking_nutrition(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    draft = from_text("Soup\n100 g carrots")
    recipe = import_draft(repo, draft)
    recipe = accept_ingredient(repo, recipe.id, 0)
    recipe.servings = 2
    accept_recipe(repo, recipe.id, recipe)
    nutrition = Nutrition(source="fdc", coverage=1, per_serving=Macros(kcal=100))
    document = repo.recipe(recipe.id)
    document.model.servings = 999
    document.model.nutrition = nutrition
    saved = document.save()
    assert saved.servings == 2 and saved.nutrition == nutrition
    repeated = import_draft(repo, draft)
    assert repeated.servings == 2 and repeated.review.status == "ok"
    values = {
        1008: (100, "kcal"),
        1003: (10, "g"),
        1004: (2, "g"),
        1258: (0, "g"),
        1005: (5, "g"),
        1079: (1, "g"),
        2000: (0, "g"),
        1093: (10, "mg"),
    }
    food = Food(
        fdcId=1,
        description="carrots",
        dataType="Foundation",
        foodNutrients=[
            {"nutrient": {"id": name, "unitName": unit}, "amount": amount}
            for name, (amount, unit) in values.items()
        ],
    )
    confirm_mapping(repo, "carrots", food, edible_fraction=1)
    refresh(repo, FakeFDC([food]))
    updated = repo.recipe(recipe.id).model
    assert updated.nutrition.per_serving.kcal == 50
    assert updated.servings == 2
