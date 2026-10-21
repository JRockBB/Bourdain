"""Explicit stock edits, checked grocery receipts and cooking events."""

import hashlib
import json
import math
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from bourdain.errors import BourdainError
from bourdain.models import HistoryEvent, Pantry, PantryItem
from bourdain.store import Repository
from bourdain.store.transaction import commit


def set_stock(
    repo: Repository,
    identifier: str,
    *,
    status: Literal["have", "low", "out"] | None = None,
    qty_g: float | None = None,
    expires: date | None = None,
    today: date | None = None,
) -> Pantry:
    if identifier not in repo.catalog().model:
        raise BourdainError(f"Unknown catalog ingredient: {identifier}")
    document = repo.pantry()
    entry = PantryItem(
        ingredient_id=identifier,
        status=status,
        qty_g=qty_g,
        expires=expires,
        acquired=today,
        source="user",
    )
    # set replaces all lots for this ingredient; bought appends a distinct lot.
    document.model.items = [
        item for item in document.model.items if item.ingredient_id != identifier
    ] + [entry]
    return document.save(automated=False)


def expire_stock(repo: Repository, *, today: date | None = None) -> list[str]:
    today = today or date.today()
    document, expired = repo.pantry(), []
    for item in document.model.items:
        if item.expires and item.expires < today and item.status != "out":
            expired.append(item.ingredient_id)
            # Expiry is an automatic interpretation, so protected entries remain
            # on disk unchanged; every consumer independently ignores expiry.
            if not item.locked and item.source != "user":
                item.status, item.qty_g = "out", 0
                item.note = f"Expired {item.expires.isoformat()}"
    document.save()
    return expired


def _quantity_grams(quantity: str) -> float | None:
    """Parse explicit grams or a sum of package quantities without guessing."""
    total = 0.0
    for part in re.split(r"\s+\+\s+", quantity.strip()):
        counted = re.fullmatch(
            r"(\d+)\s+×\s+[a-z][a-z0-9-]*\s+@\s+~([\d.eE+-]+)\s+g each", part
        )
        if counted:
            part = f"{counted[1]} × {counted[2]} g package"
        match = re.fullmatch(
            r"(?:(\d+)\s+×\s+)?(\d+(?:\.\d*)?|\.\d+)([eE][+-]?\d+)?"
            r"\s+g(?: package)?",
            part,
        )
        if not match:
            return None
        try:
            amount = float(match[2] + (match[3] or "")) * int(match[1] or 1)
        except (ValueError, OverflowError):
            return None
        if not math.isfinite(amount) or amount <= 0:
            return None
        total += amount
    return total if math.isfinite(total) else None


def bought(
    repo: Repository,
    path: Path,
    *,
    today: date | None = None,
    purchase_id: str | None = None,
) -> list[str]:
    today = today or date.today()
    document, catalog = repo.pantry(), repo.catalog().model
    text = path.read_text(encoding="utf-8-sig")
    additions, seen = [], set()
    # Unchecked/checked versions of the same list share an identity.
    identity = (
        purchase_id
        or hashlib.sha256(re.sub(r"\[[xX ]\]", "[ ]", text).encode()).hexdigest()
    )
    for index, line in enumerate(text.splitlines()):
        if not re.match(r"^\s*-\s*\[[xX]\]\s+", line):
            continue
        metadata = re.search(r"<!-- bourdain:(.+?) -->\s*$", line)
        if metadata:
            try:
                data = json.loads(metadata[1])
                identifier, grams = data["ingredient_id"], float(data["grams"])
                entry_id = (
                    f"{purchase_id}:{identifier}"
                    if purchase_id
                    else data["purchase_id"]
                )
            except (KeyError, ValueError, TypeError):
                raise BourdainError(
                    f"Invalid grocery metadata on line {index + 1}"
                ) from None
            visible = re.search(
                r"—\s+(.+?)\s+\(.*\)\s*$",
                line[: metadata.start()],
            )
            displayed = _quantity_grams(visible[1]) if visible else None
            if displayed is None or abs(displayed - grams) > 1e-6:
                raise BourdainError(
                    "Visible grocery quantity differs from its metadata; "
                    "regenerate the list or remove the metadata after editing"
                )
        else:
            match = re.match(
                r"^\s*-\s*\[[xX]\]\s+(.+?)\s+—\s+(.+?)\s+\(.*\)\s*$",
                line,
            )
            grams = _quantity_grams(match[2]) if match else None
            if not match or grams is None:
                raise BourdainError(
                    f"Cannot read purchase quantity on line {index + 1}; "
                    "supply a gram quantity"
                )
            candidates = [
                key
                for key, item in catalog.items()
                if item.name.casefold() == match[1].casefold()
            ]
            if len(candidates) != 1:
                raise BourdainError(f"Ambiguous grocery name on line {index + 1}")
            identifier = candidates[0]
            entry_id = f"{identity}:{identifier}"
        if (
            not isinstance(identifier, str)
            or identifier not in catalog
            or not isinstance(entry_id, str)
            or not math.isfinite(grams)
            or grams <= 0
        ):
            raise BourdainError(
                f"Invalid purchase on line {index + 1}; "
                "choose a known ingredient and positive grams"
            )
        if entry_id in seen:
            raise BourdainError("Duplicate checked grocery item")
        seen.add(entry_id)
        if entry_id in document.model.applied_purchases:
            if document.model.applied_purchases[entry_id] != grams:
                raise BourdainError(
                    "Purchase already recorded with a different quantity; "
                    "use a new --purchase-id"
                )
            continue
        item = catalog[identifier]
        expires = (
            today + timedelta(days=item.shelf_life_days)
            if item.shelf_life_days is not None
            else None
        )
        document.model.items.append(
            PantryItem(
                ingredient_id=identifier,
                qty_g=grams,
                acquired=today,
                expires=expires,
                source="user",
                note="Quantity estimated from confirmed item weights"
                if "@ ~" in line
                else None,
            )
        )
        document.model.applied_purchases[entry_id] = grams
        additions.append(identifier)
    document.save(automated=False)
    return additions


def cooked(
    repo: Repository,
    recipe_id: str,
    *,
    batches: float = 1,
    rating: int | None = None,
    today: date | None = None,
    event_id: str | None = None,
    include_optional: bool = False,
    freeze_servings: float = 0,
    freezer_expires: date | None = None,
) -> list[str]:
    today = today or date.today()
    event = HistoryEvent(
        date=today,
        recipe=recipe_id,
        batches=batches,
        rating=rating,
        event_id=event_id or uuid.uuid4().hex,
        freeze_servings=freeze_servings,
        freezer_expires=freezer_expires,
    )
    pantry_doc, history_doc = repo.pantry(), repo.history()
    for prior in history_doc.model:
        if prior.event_id == event.event_id:
            if prior != event:
                raise BourdainError("Cooking event ID already has different details")
            return []
    recipe, catalog = repo.recipe(recipe_id).model, repo.catalog().model
    freezer_doc = None
    if freeze_servings:
        from bourdain.freezer import freezer_document, make_lot

        if (
            freezer_expires is None
            or recipe.servings is None
            or freeze_servings > recipe.servings * batches
        ):
            raise BourdainError(
                "Supply a freezer expiry and freeze no more than "
                "the cooked serving yield"
            )
        lot = make_lot(
            recipe,
            freeze_servings,
            freezer_expires,
            lot_id="batch-" + hashlib.sha256(event.event_id.encode()).hexdigest()[:24],
            today=today,
        )
        freezer_doc = freezer_document(repo)
        if any(item.id == lot.id for item in freezer_doc.model.items):
            raise BourdainError("Freezer lot already exists for this cooking event")
        freezer_doc.model.items.append(lot)
    elif freezer_expires:
        raise BourdainError("Supply --freeze-servings with a freezer expiry")
    needs: dict[str, float] = {}
    warnings = []
    for line in recipe.ingredients:
        if line.optional and not include_optional:
            continue
        identifier = line.ingredient_id
        if identifier not in catalog or line.grams is None:
            raise BourdainError(
                f"Resolve ingredient and grams before logging cooking: {line.raw}"
            )
        needs[identifier] = needs.get(identifier, 0) + line.grams * batches
    for identifier, needed in needs.items():
        if needed == 0:
            continue
        lots = sorted(
            [
                stock
                for stock in pantry_doc.model.items
                if stock.ingredient_id == identifier
                and stock.status != "out"
                and (stock.expires is None or stock.expires >= today)
            ],
            key=lambda stock: (stock.expires or date.max, stock.acquired or date.min),
        )
        for stock in lots:
            if stock.qty_g is not None:
                used = min(needed, stock.qty_g)
                needed -= used
                stock.qty_g -= used
                if stock.qty_g == 0:
                    stock.status = "out"
            if needed <= 1e-9:
                break
        if needed > 1e-9:
            coarse = any(
                stock.qty_g is None and stock.status in {"have", "low"}
                for stock in lots
            )
            warnings.append(
                f"{identifier}: {needed:g} g used from "
                f"{'unweighed stock' if coarse else 'unrecorded stock'}"
            )
    history_doc.model.append(event)
    commit(
        repo.root,
        [
            (pantry_doc, pantry_doc.model, False),
            (history_doc, history_doc.model, False),
        ]
        + ([(freezer_doc, freezer_doc.model, False)] if freezer_doc else []),
    )
    return warnings
