from pathlib import Path

import pytest

from bourdain.errors import BourdainError
from bourdain.ingest.text import from_text
from bourdain.ingest.url import from_html, from_url
from bourdain.library import import_draft
from bourdain.models import Macros, Nutrition
from bourdain.store import Repository, initialize

HTML = Path(__file__).parent / "fixtures" / "html"


@pytest.mark.parametrize(
    ("name", "title", "servings", "count", "minutes"),
    [
        ("bbcgoodfood", "Easy pancakes", None, 6, 30),
        ("budgetbytes", "Creamy Tomato and Spinach Pasta", 4, 15, 25),
        ("loveandlemons", "Best Lentil Soup", None, 17, 60),
    ],
)
def test_recorded_pages_offline(
    name: str, title: str, servings: float | None, count: int, minutes: int
) -> None:
    url = (HTML / f"{name}.url").read_text().strip()
    html = (HTML / f"{name}.html").read_text(encoding="utf-8")
    draft = from_url(url, fetch=lambda _: html)
    assert draft.title == title
    assert draft.servings == servings
    assert len(draft.ingredients) == count
    assert draft.time.total_min == minutes
    assert draft.source.url == url
    assert draft.steps == []
    assert bool(draft.flags) == (servings is None)
    if name == "budgetbytes":
        assert draft.ingredients[0].item == "penne pasta"
        assert draft.ingredients[1].item == "yellow onion"
        assert draft.ingredients[1].grams == 340


def test_text_groups_steps_and_missing_servings() -> None:
    text = (
        "Soup\nServings: 4\nIngredients:\nFor the sauce:\n"
        "100 g tomatoes\nInstructions:\nMix."
    )
    draft = from_text(text)
    assert draft.ingredients[0].group == "For the sauce"
    assert draft.steps == []
    assert from_text(text, store_steps=True).steps == ["Mix."]
    assert from_text("Soup\n100 g tomatoes").flags == ["missing_servings"]
    assert from_text("Soup\nServes 4-6\n100 g tomatoes").servings is None


def test_no_recipe_is_readable_error() -> None:
    with pytest.raises(BourdainError):
        from_html("<html></html>", "https://example.org/no-recipe")


def test_import_idempotence_and_existing_fields(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    draft = from_text("Soup\nServings: 2\n200 g carrots\n100 g beans")
    recipe = import_draft(repo, draft)
    before = repo.recipe_path(recipe.id).read_bytes()
    catalog_before = repo.path("catalog/ingredients.yaml").read_bytes()
    assert import_draft(repo, draft) == recipe
    assert repo.recipe_path(recipe.id).read_bytes() == before
    assert repo.path("catalog/ingredients.yaml").read_bytes() == catalog_before
    doc = repo.recipe(recipe.id)
    doc.model.nutrition = Nutrition(
        source="user", per_serving=Macros(kcal=300), locked=True
    )
    doc.model.tags = ["favorite"]
    doc.save(automated=False)
    imported = import_draft(repo, draft)
    assert imported.nutrition.per_serving.kcal == 300
    assert imported.tags == ["favorite"]
    assert len(repo.catalog().model) == 2
