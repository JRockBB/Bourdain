"""Receipt price proposals with explicit confirmation and recoverable publication."""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Annotated

from pydantic import Field, TypeAdapter

from bourdain.errors import BourdainError
from bourdain.ingest.documents import (
    DocumentExtractor,
    VisualDocument,
    VisualRow,
    validate_document,
)
from bourdain.ingest.images import load_pages
from bourdain.models.schemas import Model, NonNegative, Positive, Slug
from bourdain.normalize.catalog import candidates, key
from bourdain.prices import PackagePrice, price_document
from bourdain.store import Repository
from bourdain.store.transaction import commit
from bourdain.store.yaml import YamlDocument, create_yaml, load_yaml

type Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class ReceiptDraft(VisualRow):
    raw: Annotated[str, Field(min_length=1)]
    description: Annotated[str, Field(min_length=1)]
    package_g: Positive | None = None
    quantity: Positive | None = None
    line_total: NonNegative | None = None
    currency: Currency | None = None
    discount_uncertain: bool = True


class ReceiptExtraction(VisualDocument):
    entries: list[ReceiptDraft] = Field(default_factory=list, max_length=1000)
    store: str | None = None
    purchased_on: date | None = None


class AcceptedPrice(Model):
    ingredient: Slug
    package_g: Positive
    quantity: Positive
    line_total: NonNegative
    currency: Currency


class ReceiptProposal(Model):
    observed: ReceiptDraft
    ingredient: Slug | None = None
    flags: list[str] = Field(default_factory=list)
    accepted: AcceptedPrice | None = None
    ignored: bool = False


class Receipt(Model):
    id: Slug
    page_refs: list[str]
    store: str | None = None
    purchased_on: date | None = None
    entries: dict[Slug, ReceiptProposal] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def receipt_document(repo: Repository, identifier: str) -> YamlDocument[Receipt]:
    TypeAdapter(Slug).validate_python(identifier)
    document = load_yaml(repo.path(f"receipts/{identifier}.yaml"), Receipt)
    if document.model.id != identifier:
        raise BourdainError("Receipt ID does not match its filename")
    return document


def import_receipt(
    repo: Repository,
    source: Path,
    extractor: DocumentExtractor,
    *,
    page_numbers: list[int] | None = None,
) -> Receipt:
    catalog = repo.catalog().model
    pages = load_pages(source, repo.root, page_numbers=page_numbers)
    if len(pages) > 10:
        raise BourdainError("Select at most 10 pages of one receipt")
    refs = [page.reference for page in pages]
    digest = hashlib.sha256(json.dumps(refs).encode()).hexdigest()
    identifier = f"receipt-{digest[:24]}"
    path = repo.path(f"receipts/{identifier}.yaml")
    if path.exists():
        previous = receipt_document(repo, identifier).model
        if previous.page_refs != refs:
            raise BourdainError("Receipt source conflicts with a previous import")
        return previous
    result = validate_document(
        extractor.extract(pages, "receipt", ReceiptExtraction), pages, ReceiptExtraction
    )
    receipt = Receipt(
        id=identifier,
        page_refs=refs,
        store=result.store,
        purchased_on=result.purchased_on,
        warnings=result.warnings,
    )
    for index, row in enumerate(result.entries, 1):
        exact = [
            identifier
            for identifier, item in catalog.items()
            if key(row.description) in {key(item.name), *(key(a) for a in item.aliases)}
        ]
        suggested = exact[0] if len(exact) == 1 else None
        flags = list(row.flags)
        if suggested is None:
            ranked = candidates(row.description, catalog, limit=2)
            if (
                len(exact) == 0
                and ranked
                and ranked[0].score >= 85
                and (len(ranked) == 1 or ranked[0].score - ranked[1].score >= 3)
            ):
                suggested = ranked[0].ingredient_id
            flags.append("review_catalog_match")
        for name in ("package_g", "quantity", "line_total", "currency"):
            if getattr(row, name) is None:
                flags.append(f"missing_{name}")
        if row.discount_uncertain:
            flags.append("review_discount")
        if row.confidence is None or row.confidence < 0.85:
            flags.append("review_receipt_text")
        receipt.entries[f"line-{index}"] = ReceiptProposal(
            observed=row, ingredient=suggested, flags=list(dict.fromkeys(flags))
        )
    create_yaml(path, receipt, Receipt)
    return receipt


def accept_price(
    repo: Repository,
    receipt_id: str,
    row_id: str,
    *,
    ingredient: str | None = None,
    package_g: float | None = None,
    quantity: float | None = None,
    line_total: float | None = None,
    currency: str | None = None,
    expected_currency: str = "USD",
    resolve_discount: bool = False,
    replace: bool = False,
) -> AcceptedPrice:
    document = receipt_document(repo, receipt_id)
    row = document.model.entries.get(row_id)
    if row is None:
        raise BourdainError(f"Unknown receipt row: {row_id}")
    if row.ignored:
        raise BourdainError("This receipt row was ignored")
    effective = (
        row.accepted.model_dump()
        if row.accepted
        else {
            "ingredient": row.ingredient,
            "package_g": row.observed.package_g,
            "quantity": row.observed.quantity,
            "line_total": row.observed.line_total,
            "currency": row.observed.currency,
        }
    )
    overrides = {
        "ingredient": ingredient,
        "package_g": package_g,
        "quantity": quantity,
        "line_total": line_total,
        "currency": currency,
    }
    effective.update(
        {name: value for name, value in overrides.items() if value is not None}
    )
    missing = [name for name, value in effective.items() if value is None]
    if missing:
        raise BourdainError(
            "Supply receipt values before accepting: " + ", ".join(missing)
        )
    accepted = AcceptedPrice.model_validate(effective)
    if row.accepted:
        if row.accepted != accepted:
            raise BourdainError("This row was already accepted with different values")
        return row.accepted  # Never undo a subsequent manual price edit on retry.
    if accepted.ingredient not in repo.catalog().model:
        raise BourdainError(f"Unknown catalog ingredient: {accepted.ingredient}")
    if accepted.currency != expected_currency:
        raise BourdainError(
            f"Receipt currency must match configured prices: {expected_currency}"
        )
    if row.observed.discount_uncertain and not resolve_discount:
        raise BourdainError("Confirm the final item total with --resolve-discount")
    entry = PackagePrice(
        package_g=accepted.package_g,
        price=accepted.line_total / accepted.quantity,
        currency=accepted.currency,
        source="user",
        locked=True,
        receipt_ref=f"{receipt_id}/{row_id}",
    )
    prices = price_document(repo)
    prior = prices.model.get(accepted.ingredient, [])
    if any(item.package_g == entry.package_g for item in prior) and not replace:
        raise BourdainError(
            "A price already exists for this package; use --replace to update it"
        )
    prices.model[accepted.ingredient] = sorted(
        [item for item in prior if item.package_g != entry.package_g] + [entry],
        key=lambda item: item.package_g,
    )
    row.accepted = accepted
    commit(
        repo.root, [(prices, prices.model, False), (document, document.model, False)]
    )
    return accepted


def ignore_row(repo: Repository, receipt_id: str, row_id: str) -> None:
    document = receipt_document(repo, receipt_id)
    row = document.model.entries.get(row_id)
    if row is None or row.accepted:
        raise BourdainError("Choose an existing, unaccepted receipt row")
    row.ignored = True
    document.save(automated=False)
