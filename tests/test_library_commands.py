from pathlib import Path

import pytest
from typer.testing import CliRunner

from bourdain.catalog import search_recipes
from bourdain.cli import app
from bourdain.models import Recipe, RecipeSource, Review
from bourdain.store import Repository, initialize


def recipes() -> dict[str, Recipe]:
    return {
        "book/soup": Recipe(
            id="book/soup",
            title="Soup",
            source=RecipeSource(kind="cookbook", book="book"),
            tags=["dinner"],
            protein="beans",
        ),
        "web/salad": Recipe(
            id="web/salad",
            title="Salad",
            tags=["lunch"],
            review=Review(flags=["missing_servings"]),
        ),
    }


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"tag": "dinner"}, ["book/soup"]),
        ({"book": "book"}, ["book/soup"]),
        ({"protein": "beans"}, ["book/soup"]),
        ({"status": "needs_review"}, ["web/salad"]),
        ({"query": "SOUP"}, ["book/soup"]),
        ({"tag": "none"}, []),
    ],
)
def test_recipe_filters(filters: dict, expected: list[str]) -> None:
    assert [recipe.id for recipe in search_recipes(recipes(), **filters)] == expected


def test_library_cli(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    for recipe in recipes().values():
        repo.save_recipe(recipe)
    runner = CliRunner()
    prefix = ["--data", str(repo.root), "recipes"]
    for command in [
        ["ls", "--book", "book"],
        ["search", "soup"],
        ["show", "book/soup"],
    ]:
        result = runner.invoke(app, [*prefix, *command])
        assert result.exit_code == 0, result.output
        assert "book/soup" in result.stdout and "web/salad" not in result.stdout
    assert runner.invoke(app, [*prefix, "tag", "book/soup", "favorite"]).exit_code == 0
    assert "favorite" in repo.recipe("book/soup").model.tags
    assert (
        runner.invoke(
            app, [*prefix, "tag", "book/soup", "favorite", "--remove"]
        ).exit_code
        == 0
    )
    assert "favorite" not in repo.recipe("book/soup").model.tags
    assert runner.invoke(app, [*prefix, "exclude", "book/soup"]).exit_code == 0
    assert repo.recipe("book/soup").model.excluded
    assert (
        runner.invoke(app, [*prefix, "exclude", "book/soup", "--include"]).exit_code
        == 0
    )
    assert not repo.recipe("book/soup").model.excluded
