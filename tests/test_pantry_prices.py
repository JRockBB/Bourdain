from datetime import date, timedelta
from pathlib import Path

import pytest

from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import GroceryItem, GroceryList
from bourdain.grocery.export import export_list
from bourdain.models import CatalogItem, IngredientLine, PantryItem, Recipe
from bourdain.pantry.service import bought, cooked, expire_stock, set_stock
from bourdain.prices import price_document, quote, set_price
from bourdain.store import Repository, initialize


def setup(tmp_path: Path) -> Repository:
    repo = Repository(initialize(tmp_path / "data"))
    document = repo.catalog()
    document.model["carrot"] = CatalogItem(name="Carrots", shelf_life_days=7)
    document.save()
    repo.save_recipe(
        Recipe(
            id="soup",
            title="Soup",
            servings=2,
            ingredients=[
                IngredientLine(
                    raw="150 g carrots",
                    item="carrots",
                    ingredient_id="carrot",
                    grams=150,
                )
            ],
        )
    )
    return repo


def test_bought_cooked_sequence_and_idempotence(tmp_path: Path) -> None:
    repo, today = setup(tmp_path), date(2026, 10, 21)
    shopping = GroceryList(
        "2026-W43",
        [
            GroceryItem(
                "carrot", "Carrots", "produce", buy_g=400, package_g=200, packages=2
            )
        ],
    )
    path = tmp_path / "groceries.md"
    path.write_text(export_list(shopping).replace("[ ]", "[x]"), encoding="utf-8")
    assert bought(repo, path, today=today) == ["carrot"]
    assert bought(repo, path, today=today) == []
    assert repo.pantry().model.items[0].qty_g == 400
    assert repo.pantry().model.items[0].expires == today + timedelta(days=7)
    assert (
        cooked(repo, "soup", batches=2, rating=4, today=today, event_id="dinner") == []
    )
    assert repo.pantry().model.items[0].qty_g == 100
    assert (
        cooked(repo, "soup", batches=2, rating=4, today=today, event_id="dinner") == []
    )
    assert len(repo.history().model) == 1
    assert repo.history().model[0].rating == 4
    assert bought(repo, path, today=today, purchase_id="second-trip") == ["carrot"]
    assert sum(item.qty_g or 0 for item in repo.pantry().model.items) == 500


def test_cooking_uses_earliest_expiry_and_preserves_unweighed_stock(
    tmp_path: Path,
) -> None:
    repo, today = setup(tmp_path), date(2026, 10, 21)
    document = repo.pantry()
    document.model.items = [
        PantryItem(
            ingredient_id="carrot", qty_g=200, expires=today + timedelta(days=5)
        ),
        PantryItem(ingredient_id="carrot", qty_g=100, expires=today),
        PantryItem(
            ingredient_id="carrot", qty_g=900, expires=today - timedelta(days=1)
        ),
        PantryItem(ingredient_id="carrot", status="have"),
    ]
    document.save()
    cooked(repo, "soup", today=today)
    assert [item.qty_g for item in repo.pantry().model.items] == [150, 0, 900, None]
    expire_stock(repo, today=today)
    assert repo.pantry().model.items[2].status == "out"


def test_interrupted_cooking_recovers_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bourdain.store.transaction as transaction

    repo, today = setup(tmp_path), date(2026, 10, 21)
    set_stock(repo, "carrot", qty_g=200, today=today)
    original = transaction.atomic_write

    def fail_history(path: Path, content: bytes, **kwargs: object) -> None:
        if path.name == "history.yaml":
            raise OSError("simulated interruption")
        original(path, content, **kwargs)

    monkeypatch.setattr(transaction, "atomic_write", fail_history)
    with pytest.raises(OSError, match="interruption"):
        cooked(repo, "soup", today=today, event_id="once")
    assert repo.path(".transaction.json").exists()
    monkeypatch.setattr(transaction, "atomic_write", original)
    recovered = Repository(repo.root)
    assert recovered.pantry().model.items[0].qty_g == 50
    assert recovered.history().model[0].event_id == "once"
    assert cooked(recovered, "soup", today=today, event_id="once") == []
    assert not repo.path(".transaction.json").exists()


def test_price_choice_and_visible_defaults(tmp_path: Path) -> None:
    repo = setup(tmp_path)
    set_price(repo, "carrot", 400, 2)
    set_price(repo, "carrot", 1000, 3)
    result = quote("carrot", repo.catalog().model["carrot"], price_document(repo).model)
    assert result.package_g == 1000 and result.price == 3 and result.warning is None
    missing = quote("other", CatalogItem(name="Unknown"), {})
    assert missing.price > 0 and "estimated" in missing.warning
    assert "by the gram" in missing.warning
    with pytest.raises(BourdainError, match="currency"):
        quote(
            "carrot",
            repo.catalog().model["carrot"],
            price_document(repo).model,
            currency="EUR",
        )
