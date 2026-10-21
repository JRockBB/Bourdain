"""Synthetic USDA-shaped portions; live food accuracy remains an acceptance check."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bourdain.catalog import set_unit_weight
from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import groceries
from bourdain.ingest.text import from_text
from bourdain.library import import_draft
from bourdain.models import CatalogItem, Pantry, Targets
from bourdain.normalize.parse import parse_line
from bourdain.normalize.units import shopping_unit, to_grams
from bourdain.nutrition.fdc import FakeFDC, FDCClient, Food
from bourdain.nutrition.portions import portion_food, use_portion
from bourdain.plan.manual import manual_plan
from bourdain.store import Repository, initialize


def fixture_food() -> Food:
    return Food(
        fdcId=123,
        description="Synthetic onions, raw",
        dataType="SR Legacy",
        foodPortions=[
            {"id": 11, "amount": 2, "gramWeight": 240, "modifier": "medium"},
            {"id": 12, "gramWeight": 120, "portionDescription": "1 medium"},
            {
                "id": 13,
                "amount": 1,
                "gramWeight": 90,
                "modifier": "cup, chopped",
                "measureUnit": {"name": "undetermined"},
            },
            {"id": 14, "amount": 1, "gramWeight": 0, "modifier": "each"},
            {"id": 15, "amount": 0, "gramWeight": 120, "modifier": "medium"},
        ],
    )


def fixture_repo(tmp_path: Path) -> Repository:
    repo = Repository(initialize(tmp_path / "data"))
    repo.catalog().save(
        {"onion": CatalogItem(name="onion", fdc_id=123, edible_fraction=0.8)}
    )
    return repo


def test_portion_cache_roundtrip_and_explicit_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setenv("FDC_API_KEY", "test-private")

    def fetch(endpoint: str, params: dict, api_key: str) -> dict:
        calls.append(endpoint)
        return fixture_food().model_dump(by_alias=True)

    client = FDCClient(tmp_path, fetch=fetch)
    food = client.food(123)
    monkeypatch.delenv("FDC_API_KEY")
    assert client.food(123).food_portions == food.food_portions
    assert calls == ["food/123"]
    path = tmp_path / ".cache/fdc/123.json"
    original = path.read_bytes()
    with pytest.raises(BourdainError, match="FDC_API_KEY"):
        client.food(123, refresh=True)
    assert path.read_bytes() == original
    # Older caches stay readable, but require explicit refresh to regain portions.
    old = json.loads(original)
    del old["foodPortions"]
    path.write_text(json.dumps(old), encoding="utf-8")
    assert client.food(123).food_portions is None
    monkeypatch.setenv("FDC_API_KEY", "test-private")
    assert client.food(123, refresh=True).food_portions == food.food_portions
    assert len(calls) == 2
    assert "test-private" not in path.read_text()
    original = path.read_bytes()
    client.fetch = lambda *args: {"fdcId": 999, "description": "wrong"}
    with pytest.raises(BourdainError, match="does not match"):
        client.food(123, refresh=True)
    assert path.read_bytes() == original


def test_reviewed_portion_edible_basis_provenance_and_groceries(tmp_path: Path) -> None:
    repo = fixture_repo(tmp_path)
    provider = FakeFDC([fixture_food()])
    path = repo.path("catalog/ingredients.yaml")
    before = path.read_bytes()
    assert portion_food(repo, provider, "onion").food_portions
    assert path.read_bytes() == before  # Looking never accepts a conversion.
    assert use_portion(repo, provider, "onion", 11, "medium", buy=True) == 150
    item = repo.catalog().model["onion"]
    assert item.unit_weight_sources["medium"].portion_grams == 240
    assert item.unit_weight_sources["medium"].amount == 2
    assert to_grams(parse_line("1 medium onion"), item).grams == 150
    assert shopping_unit(item) == ("medium", 150)
    assert item.source is None  # Does not approve unrelated catalog fields.
    recipe = import_draft(
        repo, from_text("Soup\nServings: 1\n170 g onion\n1 medium onion")
    )
    recipes = {recipe.id: recipe}
    plan = manual_plan(
        "2026-W43", [f"mon dinner={recipe.id}"], recipes, Targets(days=["mon"])
    )
    shopping = groceries(plan, recipes, repo.catalog().model, Pantry()).items[0]
    assert shopping.need_g == 320 and shopping.buy_g == 450
    # Retry is byte-stable; automatic catalog rewrites cannot erase the provenance.
    before = path.read_bytes()
    use_portion(repo, provider, "onion", 11, "medium", buy=True)
    assert path.read_bytes() == before
    document = repo.catalog()
    document.model["onion"].unit_weights = {"medium": 999}
    document.model["onion"].unit_weight_sources = {}
    document.save()
    assert path.read_bytes() == before
    # A manual measurement intentionally replaces the source snapshot.
    set_unit_weight(repo, "onion", "medium", 160)
    assert not repo.catalog().model["onion"].unit_weight_sources
    before = path.read_bytes()
    with pytest.raises(BourdainError, match="--replace"):
        use_portion(repo, provider, "onion", 11, "medium")
    assert path.read_bytes() == before
    use_portion(repo, provider, "onion", 11, "medium", replace=True)
    assert repo.catalog().model["onion"].unit_weights["medium"] == 150


@pytest.mark.parametrize("field,value", [("fdc_id", 456), ("edible_fraction", 1)])
def test_changed_food_or_edible_basis_stops_reusing_source(
    tmp_path: Path, field: str, value: int
) -> None:
    repo = fixture_repo(tmp_path)
    use_portion(repo, FakeFDC([fixture_food()]), "onion", 11, "medium", buy=True)
    item = repo.catalog().model["onion"]
    setattr(item, field, value)
    line = to_grams(parse_line("1 medium onion"), item)
    assert line.grams is None and "stale_unit_weight" in line.flags
    assert shopping_unit(item) is None


@pytest.mark.parametrize(
    "portion,unit,count,error",
    [
        (11, "cup", None, "count unit"),
        (13, "each", None, "mass/volume"),
        (14, "each", None, "positive grams"),
        (15, "medium", None, "positive grams"),
        (12, "medium", None, "--count"),
        (11, "medium", 1, "must agree"),
        (11, "medium", float("nan"), "positive grams"),
        (999, "medium", None, "missing or ambiguous"),
    ],
)
def test_invalid_or_ambiguous_portions_do_not_mutate(
    tmp_path: Path, portion: int, unit: str, count: float | None, error: str
) -> None:
    repo = fixture_repo(tmp_path)
    path = repo.path("catalog/ingredients.yaml")
    before = path.read_bytes()
    with pytest.raises(BourdainError, match=error):
        use_portion(
            repo, FakeFDC([fixture_food()]), "onion", portion, unit, count=count
        )
    assert path.read_bytes() == before


def test_missing_amount_requires_explicit_count_and_mapping(tmp_path: Path) -> None:
    repo = fixture_repo(tmp_path)
    provider = FakeFDC([fixture_food()])
    assert use_portion(repo, provider, "onion", 12, "pieces", count=1) == 150
    assert repo.catalog().model["onion"].unit_weights["piece"] == 150
    document = repo.catalog()
    document.model["onion"].edible_fraction = None
    document.save(automated=False)
    with pytest.raises(BourdainError, match="edible fraction"):
        use_portion(repo, provider, "onion", 11, "medium")
    document = repo.catalog()
    document.model["onion"].fdc_id = None
    document.save(automated=False)
    with pytest.raises(BourdainError, match="Confirm a USDA food"):
        portion_food(repo, provider, "onion")
    assert not repo.path("catalog/fdc.yaml").exists()


def test_cli_lists_and_accepts_cached_portions_without_key(tmp_path: Path) -> None:
    repo = fixture_repo(tmp_path)
    cache = repo.path(".cache/fdc/123.json")
    cache.parent.mkdir(parents=True)
    cache.write_text(fixture_food().model_dump_json(by_alias=True), encoding="utf-8")
    runner = CliRunner()
    base = ["--data", str(repo.root), "catalog"]
    path = repo.path("catalog/ingredients.yaml")
    before = path.read_bytes()
    result = runner.invoke(app, [*base, "portions", "onion"])
    assert result.exit_code == 0, result.output
    assert "medium" in result.output and "Edible grams" in result.output
    result = runner.invoke(app, [*base, "portions", "onion", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["foodPortions"][0]["gramWeight"] == 240
    assert path.read_bytes() == before
    result = runner.invoke(
        app, [*base, "use-portion", "onion", "11", "--unit", "medium", "--buy"]
    )
    assert result.exit_code == 0, result.output
    assert "150 g" in result.output
    assert shopping_unit(repo.catalog().model["onion"]) == ("medium", 150)
