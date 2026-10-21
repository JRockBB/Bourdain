from io import StringIO
from pathlib import Path

import pytest
from PIL import Image
from rich.console import Console
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.ingest.text import from_text
from bourdain.library import import_draft
from bourdain.models import CatalogItem
from bourdain.review import accept_ingredient, accept_recipe, source_preview
from bourdain.store import Repository, initialize


def test_scripted_review_teaches_alias_and_survives_reimport(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    document = repo.catalog()
    document.model["green-onions"] = CatalogItem(name="green onions")
    document.save()
    draft = from_text("Soup\nServings: 2\n100 g green onion")
    first = import_draft(repo, draft)
    path = repo.recipe_path(first.id)
    path.write_text(
        "# keep my comment\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["--data", str(repo.root), "review"], input="a\n")
    assert result.exit_code == 0, result.output
    assert "Reviewed 1 item(s)" in result.output
    reviewed = repo.recipe(first.id).model
    assert reviewed.review.status == "ok"
    assert reviewed.ingredients[0].locked and reviewed.ingredients[0].source == "user"
    assert "green onion" in repo.catalog().model["green-onions"].aliases
    assert path.read_text(encoding="utf-8").startswith("# keep my comment")
    second = import_draft(repo, from_text("Stew\nServings: 2\n200 g green onion"))
    assert second.review.status == "ok"
    assert import_draft(repo, draft).review.status == "ok"
    result = CliRunner().invoke(app, ["--data", str(repo.root), "review"])
    assert result.exit_code == 0 and "Reviewed 0" in result.output


def test_choose_match_from_stub_skip_other_and_edit_grams(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    document = repo.catalog()
    document.model["olive-oil"] = CatalogItem(name="olive oil")
    document.save()
    recipe = import_draft(
        repo, from_text("Soup\nServings: 2\n1 tbsp EVOO\n1 mystery herb")
    )
    result = CliRunner().invoke(
        app,
        ["--data", str(repo.root), "review", "--recipe", recipe.id],
        input="m\nolive-oil\na\n13\ns\n",
    )
    assert result.exit_code == 0, result.output
    reviewed = repo.recipe(recipe.id).model
    assert reviewed.ingredients[0].ingredient_id == "olive-oil"
    assert reviewed.ingredients[0].grams == 13
    assert reviewed.review.status == "needs_review"
    assert reviewed.ingredients[1].flags
    # The reviewed synonym supersedes an old, unreviewed parser stub.
    second = import_draft(repo, from_text("Stew\nServings: 2\n13 g EVOO"))
    assert second.ingredients[0].ingredient_id == "olive-oil"
    assert second.review.status == "ok"


def test_unresolved_values_cannot_be_silently_accepted(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    recipe = import_draft(repo, from_text("Soup\n1 carrot"))
    before = repo.recipe_path(recipe.id).read_bytes()
    with pytest.raises(BourdainError, match="Supply grams"):
        accept_ingredient(repo, recipe.id, 0)
    with pytest.raises(BourdainError, match="Supply servings"):
        accept_recipe(repo, recipe.id, recipe)
    assert repo.recipe_path(recipe.id).read_bytes() == before
    result = CliRunner().invoke(
        app,
        ["--data", str(repo.root), "review"],
        input="e\ncarrot\n1\neach\n80\na\ne\nSoup\n2\n",
    )
    assert result.exit_code == 0, result.output
    reviewed = repo.recipe(recipe.id).model
    assert reviewed.servings == 2 and "servings" in reviewed.user_fields
    assert reviewed.review.status == "ok"
    assert import_draft(repo, from_text("Soup\n1 carrot")) == reviewed


def test_shared_flags_survive_until_all_lines_reviewed(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    recipe = import_draft(repo, from_text("Soup\nServings: 2\n1 carrot\n1 potato"))
    edited = recipe.ingredients[0].model_copy(update={"grams": 80})
    updated = accept_ingredient(repo, recipe.id, 0, edited=edited)
    assert "missing_unit_weight" in updated.review.flags


def test_source_thumbnail_and_missing_source(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    recipe = import_draft(repo, from_text("Soup\nServings: 2\n100 g carrots"))
    Image.new("RGB", (60, 80), "orange").save(repo.path("raw/page.png"))
    recipe.source.images = ["raw/page.png"]
    output = StringIO()
    console = Console(file=output, width=100)
    console.print(source_preview(repo, recipe))
    assert "Source page" in output.getvalue() and "▀" in output.getvalue()
    recipe.source.images = ["raw/missing.png"]
    console.print(source_preview(repo, recipe))
    assert "Preview unavailable" in output.getvalue()
