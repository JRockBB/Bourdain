from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.models import (
    Catalog,
    Eater,
    History,
    IngredientLine,
    Pantry,
    Plan,
    Recipe,
    RecipeDraft,
    Targets,
)
from bourdain.store import Repository, initialize, load_yaml, merge_automated
from bourdain.store.yaml import atomic_write

FIXTURES = Path(__file__).parent / "fixtures" / "spec_examples"
SCHEMAS = {
    "recipe": Recipe,
    "catalog": Catalog,
    "pantry": Pantry,
    "plan": Plan,
    "targets": Targets,
    "history": History,
    "draft": RecipeDraft,
}


@pytest.mark.parametrize("name", SCHEMAS)
def test_spec_fixtures_round_trip(name: str, tmp_path: Path) -> None:
    path = tmp_path / f"{name}.yaml"
    original = (FIXTURES / path.name).read_bytes()
    path.write_bytes(original)
    document = load_yaml(path, SCHEMAS[name])
    document.save()
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "values",
    [
        {"grams": -1},
        {"qty": -1},
        {"match_confidence": 1.1},
        {"ingredient_id": "../salt"},
        {"grams": float("nan")},
        {"qty_range": (3, 2)},
    ],
)
def test_ingredient_validation(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IngredientLine(raw="salt", item="salt", **values)


def test_target_week_and_recipe_validation() -> None:
    with pytest.raises(ValidationError):
        Eater(name="me", daily={"kcal": (2500, 2200)})
    with pytest.raises(ValidationError):
        Plan(week="2026-W54")
    with pytest.raises(ValidationError):
        Recipe(id="../bad", title="bad")
    with pytest.raises(ValidationError):
        Targets(days=["mon", "mon"])


def test_init_and_force_preserve_edits(tmp_path: Path) -> None:
    root = tmp_path / "data"
    runner = CliRunner()
    assert runner.invoke(app, ["init", str(root)]).exit_code == 0
    repo = Repository(root)
    assert repo.catalog().model == {}
    assert repo.pantry().model.items == []
    assert repo.history().model == []
    assert repo.targets().model.eaters[0].name == "me"
    config = root / "bourdain.toml"
    config.write_text("# hand edit\n", encoding="utf-8")
    assert runner.invoke(app, ["init", str(root)]).exit_code == 2
    assert runner.invoke(app, ["init", str(root), "--force"]).exit_code == 0
    assert config.read_text(encoding="utf-8") == "# hand edit\n"


@pytest.mark.parametrize("protection", ["source", "locked"])
def test_comments_and_protected_nutrition_survive(
    tmp_path: Path, protection: str
) -> None:
    path = tmp_path / "recipe.yaml"
    path.write_bytes((FIXTURES / "recipe.yaml").read_bytes())
    doc = load_yaml(path, Recipe)
    if protection == "source":
        doc.model.nutrition.source = "user"
    else:
        doc.model.nutrition.locked = True
    doc.save(automated=False)
    doc.model.nutrition.per_serving.kcal = 1
    doc.model.title = "New title"
    saved = doc.save()
    assert saved.nutrition.per_serving.kcal == 512
    assert saved.title == "New title"
    assert "# Keep this hand-checked weight." in path.read_text(encoding="utf-8")
    doc.model.nutrition = None
    assert doc.save().nutrition is not None


def test_lock_merge_and_parent_replacement() -> None:
    before = {
        "nutrition": {"locked": True, "per_serving": {"kcal": 10}},
        "title": "old",
    }
    result = merge_automated(before, {"nutrition": None, "title": "new"})
    assert result["nutrition"] == before["nutrition"]
    assert result["title"] == "new"
    assert before["title"] == "old"
    assert merge_automated([{"source": "user", "value": 2}], []) == [
        {"source": "user", "value": 2}
    ]


def test_stale_document_refuses_write(tmp_path: Path) -> None:
    path = tmp_path / "recipe.yaml"
    path.write_bytes((FIXTURES / "recipe.yaml").read_bytes())
    doc = load_yaml(path, Recipe)
    modified = path.read_bytes() + b"# concurrent edit\n"
    path.write_bytes(modified)
    doc.model.title = "Overwrite attempt"
    with pytest.raises(BourdainError, match="changed since"):
        doc.save()
    assert path.read_bytes() == modified


def test_failed_atomic_write_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.yaml"
    path.write_bytes(b"original")

    def fail(*args: Any) -> None:
        raise OSError("simulated failure")

    monkeypatch.setattr("bourdain.store.yaml.os.replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, b"new")
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]


def test_repository_rejects_path_escape(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "kitchen"))
    with pytest.raises(BourdainError):
        repo.path("../outside.yaml")
