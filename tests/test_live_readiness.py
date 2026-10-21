"""Manifest-driven external acceptance; all writes stay in pytest temporary roots."""

import os
from pathlib import Path
from typing import Annotated, Literal

import pytest
from pydantic import Field, TypeAdapter

from bourdain.book_index import IndexExtraction
from bourdain.config import LLMConfig
from bourdain.ingest.documents import CachedDocumentExtractor
from bourdain.ingest.images import load_pages
from bourdain.models import CatalogItem
from bourdain.models.schemas import Model, Positive, Slug
from bourdain.nutrition.fdc import FDCClient
from bourdain.nutrition.portions import use_portion
from bourdain.nutrition.service import map_catalog
from bourdain.receipts import ReceiptExtraction
from bourdain.store import Repository, initialize

pytestmark = pytest.mark.live


class IndexExpected(Model):
    title: str
    pages: list[Annotated[int, Field(gt=0)]]


class IndexCase(Model):
    files: list[str] = Field(min_length=1)
    entries: list[IndexExpected] = Field(min_length=1)


class ReceiptExpected(Model):
    raw: str
    package_g: Positive | None
    quantity: Positive | None
    line_total: Annotated[float, Field(ge=0)] | None
    currency: str | None
    discount_uncertain: bool


class ReceiptCase(Model):
    files: list[str] = Field(min_length=1)
    entries: list[ReceiptExpected] = Field(min_length=1)


class PortionCase(Model):
    ingredient: Slug
    fdc_id: Annotated[int, Field(gt=0)]
    description: str
    portion_id: Annotated[int, Field(gt=0)]
    portion_label: str
    amount: Positive | None
    gram_weight: Positive
    edible_fraction: Annotated[float, Field(gt=0, le=1)]
    unit: str
    count: Positive
    expected_unit_g: Positive


class MappingCase(Model):
    ingredient: Slug
    name: str
    acceptable_fdc_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1)
    review: Literal["required", "optional"] = "optional"


def cases[T](variable: str, key: str, schema: type[T]) -> tuple[Path, list[T]]:
    if not os.environ.get(key) or not os.environ.get(variable):
        pytest.skip(f"Needs {key} and {variable} manifest")
    manifest = Path(os.environ[variable])
    rows = TypeAdapter(list[schema]).validate_json(manifest.read_bytes())
    assert rows, "Supply reviewed acceptance cases; an empty manifest is not a pass"
    return manifest, rows


def image_inputs(manifest: Path, rows: list[IndexCase] | list[ReceiptCase]) -> None:
    # Check the entire input set before making the first paid request.
    for row in rows:
        for name in row.files:
            assert (manifest.parent / name).is_file(), f"Missing fixture: {name}"


def test_live_cookbook_indexes(tmp_path: Path) -> None:
    manifest, rows = cases("BOURDAIN_LIVE_INDEXES", "ANTHROPIC_API_KEY", IndexCase)
    image_inputs(manifest, rows)
    extractor = CachedDocumentExtractor(
        tmp_path, os.environ.get("BOURDAIN_LIVE_VISION_MODEL", LLMConfig().model)
    )
    for row in rows:
        pages = [
            page
            for name in row.files
            for page in load_pages(manifest.parent / name, tmp_path)
        ]
        result = extractor.extract(pages, "index", IndexExtraction)
        assert sorted(
            (entry.title.casefold().strip(), sorted(entry.pages))
            for entry in result.entries
        ) == sorted(
            (entry.title.casefold().strip(), sorted(entry.pages))
            for entry in row.entries
        )


def test_live_receipt_rows(tmp_path: Path) -> None:
    manifest, rows = cases("BOURDAIN_LIVE_RECEIPTS", "ANTHROPIC_API_KEY", ReceiptCase)
    image_inputs(manifest, rows)
    extractor = CachedDocumentExtractor(
        tmp_path, os.environ.get("BOURDAIN_LIVE_VISION_MODEL", LLMConfig().model)
    )
    for row in rows:
        pages = [
            page
            for name in row.files
            for page in load_pages(manifest.parent / name, tmp_path)
        ]
        result = extractor.extract(pages, "receipt", ReceiptExtraction)
        assert len(result.entries) == len(row.entries)
        for actual, expected in zip(result.entries, row.entries, strict=True):
            assert actual.raw.casefold().strip() == expected.raw.casefold().strip()
            for field in ("package_g", "quantity", "line_total"):
                value = getattr(expected, field)
                assert getattr(actual, field) == (
                    None if value is None else pytest.approx(value)
                )
            assert actual.currency == expected.currency
            assert actual.discount_uncertain == expected.discount_uncertain


def test_live_usda_portions(tmp_path: Path) -> None:
    _, rows = cases("BOURDAIN_LIVE_PORTIONS", "FDC_API_KEY", PortionCase)
    provider = FDCClient(tmp_path)
    for number, row in enumerate(rows):
        food = provider.food(row.fdc_id)
        assert food.description == row.description
        matches = [
            portion
            for portion in food.food_portions or []
            if portion.id == row.portion_id
        ]
        assert len(matches) == 1
        portion = matches[0]
        assert portion.label == row.portion_label
        assert portion.amount == row.amount
        assert portion.gram_weight == pytest.approx(row.gram_weight)
        repo = Repository(initialize(tmp_path / f"portion-{number}"))
        document = repo.catalog()
        document.model[row.ingredient] = CatalogItem(
            name=row.description, fdc_id=row.fdc_id, edible_fraction=row.edible_fraction
        )
        document.save(automated=False)
        grams = use_portion(
            repo,
            provider,
            row.ingredient,
            row.portion_id,
            row.unit,
            count=row.count,
            buy=True,
        )
        assert grams == pytest.approx(row.expected_unit_g)


def test_thirty_live_fdc_mappings(tmp_path: Path) -> None:
    _, rows = cases("BOURDAIN_LIVE_MAPPINGS", "FDC_API_KEY", MappingCase)
    assert len(rows) == 30 and len({row.ingredient for row in rows}) == 30
    repo = Repository(initialize(tmp_path / "library"))
    document = repo.catalog()
    document.model.update({row.ingredient: CatalogItem(name=row.name) for row in rows})
    document.save(automated=False)
    mappings = map_catalog(repo, FDCClient(repo.root))
    for row in rows:
        assert mappings[row.ingredient].fdc_id in row.acceptable_fdc_ids, row.ingredient
        if row.review == "required":
            assert mappings[row.ingredient].flags, row.ingredient
