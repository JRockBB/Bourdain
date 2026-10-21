"""Offline index/receipt workflows with synthetic image-extraction responses."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from typer.testing import CliRunner

from bourdain.book_index import (
    IndexExtraction,
    import_index,
    index_document,
    index_rows,
    link_entry,
    select_entry,
)
from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.ingest.documents import CachedDocumentExtractor
from bourdain.ingest.images import load_pages
from bourdain.ingest.text import from_text
from bourdain.library import import_draft
from bourdain.models import CatalogItem, RecipeSource
from bourdain.prices import price_document, set_price
from bourdain.receipts import accept_price, ignore_row, import_receipt, receipt_document
from bourdain.store import Repository, initialize


class FakeDocuments:
    def __init__(self, data: dict) -> None:
        self.data, self.calls = data, 0

    def extract(self, pages, kind, schema):
        self.calls += 1
        data = copy.deepcopy(self.data)
        for entry in data["entries"]:
            entry.setdefault("page_refs", [pages[0].reference])
        return schema.model_validate(data)


def setup(tmp_path: Path) -> tuple[Repository, Path]:
    repo = Repository(initialize(tmp_path / "data"))
    image = tmp_path / "photo.png"
    Image.new("RGB", (60, 80), "white").save(image)
    return repo, image


def test_document_transport_cache_and_source_binding(tmp_path: Path) -> None:
    repo, image = setup(tmp_path)
    pages = load_pages(image, repo.root)
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            stop_reason="end_turn",
            parsed_output={
                "entries": [
                    {"title": "Soup", "pages": [42], "page_refs": [pages[0].reference]}
                ]
            },
        )

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    extractor = CachedDocumentExtractor(repo.root, "fake", client=client)
    result = extractor.extract(pages, "index", IndexExtraction)
    assert result.entries[0].pages == [42]
    # The key is not required for a cache hit or an injected client.
    assert (
        CachedDocumentExtractor(repo.root, "fake").extract(
            pages, "index", IndexExtraction
        )
        == result
    )
    assert len(calls) == 1 and calls[0]["output_format"] is IndexExtraction
    assert "image" == calls[0]["messages"][0]["content"][2]["type"]
    bad = SimpleNamespace(
        messages=SimpleNamespace(
            parse=lambda **kwargs: SimpleNamespace(
                stop_reason="end_turn",
                parsed_output={
                    "entries": [{"title": "Soup", "page_refs": ["invented"]}]
                },
            )
        )
    )
    with pytest.raises(BourdainError, match="unknown page"):
        CachedDocumentExtractor(repo.root, "bad", client=bad).extract(
            pages, "index", IndexExtraction
        )
    assert len(list(repo.path(".cache/index").glob("*.json"))) == 1
    cache = next(repo.path(".cache/index").glob("*.json"))
    cache.write_text("broken", encoding="utf-8")
    with pytest.raises(BourdainError, match="Invalid index cache"):
        extractor.extract(pages, "index", IndexExtraction)
    assert len(calls) == 1


def test_index_selection_reimport_and_capture_queue(tmp_path: Path) -> None:
    repo, image = setup(tmp_path)
    fake = FakeDocuments(
        {
            "entries": [
                {"title": "Soup", "pages": [42, 42], "confidence": 0.98},
                {"title": "Br3ad", "pages": [], "confidence": 0.4},
            ]
        }
    )
    index = import_index(repo, image, "book", fake)
    soup, bread = list(index.entries)
    assert not repo.recipes() and not index_rows(repo, "book", queue=True)
    assert index.entries[soup].pages == [42]
    assert "missing_pages" in index.entries[bread].flags
    path = repo.path("indexes/book.yaml")
    before = path.read_bytes()
    with pytest.raises(BourdainError, match="--pages"):
        select_entry(repo, "book", bread)
    assert path.read_bytes() == before
    select_entry(repo, "book", soup)
    select_entry(repo, "book", bread, title="Bread", pages=[50])
    before = path.read_bytes()
    import_index(repo, image, "book", fake)
    assert path.read_bytes() == before
    assert [row["title"] for row in index_rows(repo, "book", queue=True)] == [
        "Soup",
        "Bread",
    ]
    draft = from_text("Soup\nServings: 1\n100 g carrots")
    draft.source = RecipeSource(kind="cookbook", book="book", page=42)
    import_draft(repo, draft)
    assert [row["title"] for row in index_rows(repo, "book", queue=True)] == ["Bread"]
    draft.title = "Actual bread title"
    recipe = import_draft(repo, draft)
    link_entry(repo, "book", bread, recipe.id)
    assert not index_rows(repo, "book", queue=True)
    assert index_document(repo, "book").model.entries[bread].recipe_id == recipe.id


def test_index_cli_and_missing_pages_correction(tmp_path: Path, monkeypatch) -> None:
    repo, image = setup(tmp_path)
    fake = FakeDocuments({"entries": [{"title": "Beans", "pages": []}]})
    monkeypatch.setattr("bourdain.cli.make_document_extractor", lambda settings: fake)
    runner, base = CliRunner(), ["--data", str(repo.root)]
    result = runner.invoke(
        app, [*base, "ingest", "index", str(image), "--book", "book"]
    )
    assert result.exit_code == 0, result.output
    entry = next(iter(index_document(repo, "book").model.entries))
    result = runner.invoke(
        app, [*base, "index", "select", "book", entry, "--pages", "12-13"]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, [*base, "index", "queue", "book", "--json"])
    assert result.exit_code == 0 and json.loads(result.output)[0]["pages"] == [12, 13]
    result = runner.invoke(app, [*base, "index", "queue", "book"])
    assert "Pages to photograph: 12, 13" in result.output


def receipt_fixture(repo: Repository, image: Path, **overrides):
    repo.catalog().save({"beans": CatalogItem(name="Beans", aliases=["BN BEANS"])})
    row = {
        "raw": "BN BEANS 250g 2 @ 2.50 5.00 USD",
        "description": "BN BEANS",
        "package_g": 250,
        "quantity": 2,
        "line_total": 5,
        "currency": "USD",
        "discount_uncertain": False,
        "confidence": 0.97,
    } | overrides
    fake = FakeDocuments({"entries": [row, row], "store": "Synthetic store"})
    return import_receipt(repo, image, fake), fake


def test_receipt_proposals_exact_prices_retry_and_manual_edits(tmp_path: Path) -> None:
    repo, image = setup(tmp_path)
    pantry = repo.path("pantry.yaml").read_bytes()
    receipt, fake = receipt_fixture(repo, image)
    assert len(receipt.entries) == 2  # Repeated purchased lines are not deduplicated.
    assert not price_document(repo).model
    assert receipt.entries["line-1"].ingredient == "beans"
    accepted = accept_price(repo, receipt.id, "line-1")
    quote = price_document(repo).model["beans"][0]
    assert quote.package_g == 250 and quote.price == 2.5
    assert quote.receipt_ref == f"{receipt.id}/line-1" and quote.locked
    prices_before = repo.path("prices.yaml").read_bytes()
    with pytest.raises(BourdainError, match="--replace"):
        accept_price(repo, receipt.id, "line-2")
    assert repo.path("prices.yaml").read_bytes() == prices_before
    accept_price(repo, receipt.id, "line-2", line_total=4, replace=True)
    set_price(repo, "beans", 250, 3)
    assert accept_price(repo, receipt.id, "line-1") == accepted
    assert price_document(repo).model["beans"][0].price == 3
    with pytest.raises(BourdainError, match="different values"):
        accept_price(repo, receipt.id, "line-1", line_total=8)
    again = import_receipt(repo, image, fake)
    assert fake.calls == 1 and again.entries["line-1"].accepted == accepted
    assert repo.path("pantry.yaml").read_bytes() == pantry


def test_receipt_ambiguities_and_currency_require_explicit_correction(
    tmp_path: Path,
) -> None:
    repo, image = setup(tmp_path)
    receipt, _ = receipt_fixture(
        repo, image, package_g=None, currency=None, discount_uncertain=True
    )
    with pytest.raises(BourdainError, match="package_g, currency"):
        accept_price(repo, receipt.id, "line-1")
    with pytest.raises(BourdainError, match="currency must match"):
        accept_price(repo, receipt.id, "line-1", package_g=500, currency="EUR")
    with pytest.raises(BourdainError, match="resolve-discount"):
        accept_price(repo, receipt.id, "line-1", package_g=500, currency="USD")
    assert not price_document(repo).model
    accept_price(
        repo,
        receipt.id,
        "line-1",
        package_g=500,
        currency="USD",
        resolve_discount=True,
        line_total=4,
    )
    assert price_document(repo).model["beans"][0].price == 2
    ignore_row(repo, receipt.id, "line-2")
    with pytest.raises(BourdainError, match="ignored"):
        accept_price(repo, receipt.id, "line-2")


def test_receipt_transaction_recovers_after_price_write(
    tmp_path: Path, monkeypatch
) -> None:
    import bourdain.store.transaction as transaction

    repo, image = setup(tmp_path)
    receipt, _ = receipt_fixture(repo, image)
    original = transaction.atomic_write

    def fail(path, data, **kwargs):
        if path == repo.path(f"receipts/{receipt.id}.yaml"):
            raise OSError("simulated interruption")
        return original(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(transaction, "atomic_write", fail)
        with pytest.raises(OSError, match="interruption"):
            accept_price(repo, receipt.id, "line-1")
    recovered = Repository(repo.root)
    assert not recovered.path(".transaction.json").exists()
    assert receipt_document(recovered, receipt.id).model.entries["line-1"].accepted
    before = recovered.path("prices.yaml").read_bytes()
    accept_price(recovered, receipt.id, "line-1")
    assert recovered.path("prices.yaml").read_bytes() == before


def test_receipt_cli_review_and_accept(tmp_path: Path, monkeypatch) -> None:
    repo, image = setup(tmp_path)
    receipt, fake = receipt_fixture(repo, image)
    monkeypatch.setattr("bourdain.cli.make_document_extractor", lambda settings: fake)
    runner, base = CliRunner(), ["--data", str(repo.root), "prices"]
    result = runner.invoke(app, [*base, "receipt", str(image)])
    assert result.exit_code == 0 and fake.calls == 1
    result = runner.invoke(app, [*base, "review", receipt.id, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["entries"]["line-1"]["accepted"] is None
    result = runner.invoke(app, [*base, "accept", receipt.id, "line-1"])
    assert result.exit_code == 0, result.output
    assert "2.5 USD" in result.output
    result = runner.invoke(app, [*base, "receipts"])
    assert "1 pending" in result.output
