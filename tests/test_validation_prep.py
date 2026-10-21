"""Offline checks of acceptance harness isolation and failure detection."""

import json
import runpy
from pathlib import Path

import pytest
import test_live_readiness as live

from bourdain.backup import create_backup
from bourdain.errors import BourdainError
from bourdain.ingest.images import PageImage
from bourdain.models import (
    CatalogItem,
    IngredientLine,
    Macros,
    Meal,
    Nutrition,
    Plan,
    Recipe,
    Targets,
)
from bourdain.store import Repository, initialize


def test_receipt_harness_rejects_guessed_unknown_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "receipt.jpg"
    image.write_bytes(b"placeholder")
    manifest = tmp_path / "receipts.json"
    expected = {
        "raw": "onion",
        "package_g": None,
        "quantity": None,
        "line_total": 2,
        "currency": None,
        "discount_uncertain": True,
    }
    manifest.write_text(json.dumps([{"files": [image.name], "entries": [expected]}]))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-offline")
    monkeypatch.setenv("BOURDAIN_LIVE_RECEIPTS", str(manifest))
    monkeypatch.setattr(
        live,
        "load_pages",
        lambda *args: [PageImage("raw/receipt.jpg", 1, 1, 1, b"fake")],
    )
    guessed = {"value": None}

    class Extractor:
        def __init__(self, *args):
            pass

        def extract(self, pages, kind, schema):
            return schema.model_validate(
                {
                    "entries": [
                        expected
                        | {
                            "description": "onion",
                            "page_refs": [pages[0].reference],
                            "package_g": guessed["value"],
                        }
                    ]
                }
            )

    monkeypatch.setattr(live, "CachedDocumentExtractor", Extractor)
    live.test_live_receipt_rows(tmp_path)
    guessed["value"] = 100
    with pytest.raises(AssertionError):
        live.test_live_receipt_rows(tmp_path)
    image.unlink()
    with pytest.raises(AssertionError, match="Missing fixture"):
        live.test_live_receipt_rows(tmp_path)


def test_live_manifest_is_required_nonempty_and_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "portions.json"
    monkeypatch.setenv("FDC_API_KEY", "fake-offline")
    monkeypatch.setenv("BOURDAIN_LIVE_PORTIONS", str(manifest))
    manifest.write_text("[]")
    with pytest.raises(AssertionError, match="empty manifest"):
        live.test_live_usda_portions(tmp_path)
    template = Path(__file__).parents[1] / "examples/validation/portions.example.json"
    manifest.write_bytes(template.read_bytes())
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        live.test_live_usda_portions(tmp_path)


def test_benchmark_uses_disposable_restore_and_checks_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    functions = runpy.run_path(
        str(Path(__file__).parents[1] / "examples/benchmark_library.py")
    )
    benchmark = functions["benchmark"]
    repo = Repository(initialize(tmp_path / "data"))
    archive = tmp_path / "small.zip"
    create_backup(repo.root, archive)
    with pytest.raises(BourdainError, match="14 enabled"):
        benchmark(archive, "2026-W43")
    targets = repo.targets()
    targets.save(Targets(planned_slots=["lunch", "dinner"]), automated=False)
    archive = tmp_path / "too-few.zip"
    create_backup(repo.root, archive)
    with pytest.raises(BourdainError, match="80 eligible"):
        benchmark(archive, "2026-W43")
    catalog = repo.catalog()
    catalog.model["food"] = CatalogItem(name="Food")
    catalog.save(automated=False)
    for number in range(80):
        repo.save_recipe(
            Recipe(
                id=f"recipe-{number}",
                title=f"Recipe {number}",
                servings=1,
                tags=["lunch", "dinner"],
                ingredients=[
                    IngredientLine(
                        raw="1 g food", item="food", ingredient_id="food", grams=1
                    )
                ],
                nutrition=Nutrition(per_serving=Macros(kcal=1), coverage=1),
            )
        )
    archive = tmp_path / "eighty.zip"
    create_backup(repo.root, archive)
    original = archive.read_bytes()
    calls = []

    def plan(copy, settings, week, *, lean, save):
        assert copy.root != repo.root and not save
        assert settings.plan.candidate_limit == 80
        calls.append((copy.root, lean))
        return Plan(
            week=week,
            meals=[
                Meal(day=day, slot=slot, recipe="recipe-0")
                for day in targets.model.days
                for slot in ["lunch", "dinner"]
            ],
        )

    monkeypatch.setitem(benchmark.__globals__, "automatic_plan", plan)
    report = benchmark(archive, "2026-W43")
    assert [trial["lean"] for trial in report["trials"]] == [0, 0.5, 1]
    assert report["candidates"] == 80 and report["meal_times"] == 14
    assert len(calls) == 3 and not calls[0][0].exists()
    assert archive.read_bytes() == original
    assert not list((repo.root / "plans").iterdir())
