import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.doctor import diagnose
from bourdain.freezer import make_lot
from bourdain.models import (
    CatalogItem,
    Freezer,
    IngredientLine,
    Plan,
    Recipe,
    RecipeSource,
)
from bourdain.models.schemas import UnitWeightSource
from bourdain.store import Repository, initialize
from bourdain.store.yaml import create_yaml


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_health_is_read_only_even_with_pending_transaction(tmp_path: Path) -> None:
    root = initialize(tmp_path / "data")
    (root / ".transaction.json").write_text("not a valid transaction")
    before = snapshot(root)
    report = diagnose(root)
    assert [item.code for item in report.findings] == ["pending_transaction"]
    assert snapshot(root) == before
    assert not report.ok


def test_empty_and_legacy_library_and_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    assert not diagnose(missing).ok
    assert not missing.exists()
    root = initialize(tmp_path / "data")
    (root / "freezer.yaml").unlink()
    before = snapshot(root)
    assert diagnose(root).ok
    assert snapshot(root) == before


def test_all_bad_files_reported_and_config_secrets_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = initialize(tmp_path / "data")
    for name in ("pantry.yaml", "history.yaml"):
        (root / name).write_text("[broken")
    (root / "prices.yaml").unlink()
    (root / "bourdain.toml").write_text('secret_key = "do-not-print-this"')
    monkeypatch.setenv("FDC_API_KEY", "also-do-not-print")
    result = CliRunner().invoke(app, ["--data", str(root), "doctor", "--json"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert {row["code"] for row in report["findings"]} == {
        "invalid_data",
        "invalid_config",
        "missing_file",
    }
    assert report["credentials"]["FDC_API_KEY"] is True
    assert "do-not-print" not in result.stdout


def test_references_sources_stale_conversion_freezer_and_plan(tmp_path: Path) -> None:
    from datetime import date

    repo = Repository(initialize(tmp_path / "data"))
    catalog = repo.catalog()
    catalog.model["onion"] = CatalogItem(
        name="onion",
        fdc_id=2,
        edible_fraction=1,
        unit_weights={"medium": 100},
        unit_weight_sources={
            "medium": UnitWeightSource(
                fdc_id=1,
                description="onion",
                portion_id=1,
                portion_description="medium",
                amount=1,
                portion_grams=100,
                edible_fraction=1,
            )
        },
    )
    catalog.save(automated=False)
    recipe = Recipe(
        id="book/soup",
        title="Soup",
        servings=2,
        freezes=True,
        source=RecipeSource(book="book", images=["../../outside.jpg"]),
        ingredients=[
            IngredientLine(
                raw="missing", item="missing", ingredient_id="absent", grams=10
            )
        ],
    )
    repo.save_recipe(recipe)
    lot = make_lot(recipe, 2, date(2030, 1, 1), lot_id="lot", today=date(2026, 1, 1))
    lot.recipe_signature = "stale"
    freezer_path = repo.path("freezer.yaml")
    freezer_path.unlink()
    create_yaml(freezer_path, Freezer(items=[lot]), Freezer)
    repo.save_plan(Plan(week="2026-W43", pantry_fingerprint="stale"))
    report = diagnose(repo.root)
    assert {item.code for item in report.findings} == {
        "broken_reference",
        "missing_source",
        "stale_conversion",
        "stale_freezer",
        "unusable_plan",
    }
    assert {item.code for item in report.findings if item.severity == "warning"} == {
        "stale_conversion",
        "stale_freezer",
        "unusable_plan",
    }


def test_index_receipt_and_price_links(tmp_path: Path) -> None:
    from bourdain.book_index import BookIndex, IndexStub
    from bourdain.prices import PackagePrice, Prices
    from bourdain.receipts import AcceptedPrice, Receipt, ReceiptDraft, ReceiptProposal

    root = initialize(tmp_path / "data")
    create_yaml(
        root / "indexes/book.yaml",
        BookIndex(
            book="book",
            entries={
                "soup": IndexStub(
                    title="Soup",
                    recipe_id="book/missing",
                    index_refs=["raw/missing.png#page=1"],
                ),
            },
        ),
        BookIndex,
    )
    create_yaml(
        root / "receipts/receipt-a.yaml",
        Receipt(
            id="receipt-a",
            page_refs=[],
            entries={
                "line-1": ReceiptProposal(
                    observed=ReceiptDraft(
                        raw="onion",
                        description="onion",
                        page_refs=["raw/missing.png#page=1"],
                    ),
                    accepted=AcceptedPrice(
                        ingredient="onion",
                        package_g=100,
                        quantity=1,
                        line_total=2,
                        currency="USD",
                    ),
                ),
            },
        ),
        Receipt,
    )
    (root / "prices.yaml").unlink()
    create_yaml(
        root / "prices.yaml",
        {
            "onion": [
                PackagePrice(
                    package_g=100, price=2, receipt_ref="receipt-a/line-missing"
                )
            ]
        },
        Prices,
    )
    findings = diagnose(root).findings
    assert {item.path for item in findings} >= {
        "indexes/book.yaml",
        "receipts/receipt-a.yaml",
        "prices.yaml",
    }
    assert all(item.code in {"broken_reference", "missing_source"} for item in findings)


def test_unsafe_paths_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = initialize(tmp_path / "data")
    original = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda p: p.name == "catalog" or original(p)
    )
    report = diagnose(root)
    assert {item.code for item in report.findings} == {"unsafe_path", "missing_file"}


def test_bad_identity_and_retry_state(tmp_path: Path) -> None:
    from bourdain.grocery.todoist import PushBatch

    root = initialize(tmp_path / "data")
    create_yaml(root / "recipes/soup.yaml", Recipe(id="other", title="Soup"), Recipe)
    create_yaml(
        root / "exports/todoist/batch.yaml",
        PushBatch(
            project="test",
            week="2026-W43",
            name="weekly",
            payload_digest="digest",
            commands=[{"uuid": "same"}, {"uuid": "same"}],
            completed=["unknown"],
        ),
        PushBatch,
    )
    assert {item.code for item in diagnose(root).findings} == {
        "identity_mismatch",
        "invalid_retry_state",
    }
