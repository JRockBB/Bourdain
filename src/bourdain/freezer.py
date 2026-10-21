"""Prepared servings carried across weeks; quantities change only on explicit events."""

import hashlib
import json
import uuid
from datetime import date

from bourdain.errors import BourdainError
from bourdain.models import Freezer, FreezerConsumption, FreezerLot, Recipe
from bourdain.store import Repository
from bourdain.store.yaml import YamlDocument, create_yaml, load_yaml


def read_freezer(repo: Repository) -> Freezer:
    path = repo.path("freezer.yaml")
    return load_yaml(path, Freezer).model if path.exists() else Freezer()


def freezer_document(repo: Repository) -> YamlDocument[Freezer]:
    path = repo.path("freezer.yaml")
    if not path.exists():
        repo.catalog()  # Require an initialized data folder before creating files.
        create_yaml(path, Freezer(), Freezer)
    return load_yaml(path, Freezer)


def recipe_signature(recipe: Recipe) -> str:
    value = {
        "servings": recipe.servings,
        "ingredients": [
            (line.ingredient_id, line.grams, line.optional)
            for line in recipe.ingredients
        ],
        "nutrition": recipe.nutrition.model_dump(
            mode="json", exclude={"computed_at", "source", "locked", "confidence"}
        )
        if recipe.nutrition
        else None,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def fingerprint(freezer: Freezer, ids: set[str]) -> str:
    rows = [
        item.model_dump(mode="json")
        for item in sorted(freezer.items, key=lambda item: item.id)
        if item.id in ids
    ]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def make_lot(
    recipe: Recipe, servings: float, expires: date, *, lot_id: str, today: date
) -> FreezerLot:
    if not recipe.freezes:
        raise BourdainError(
            "Mark the recipe freezable before recording frozen portions"
        )
    if recipe.servings is None:
        raise BourdainError("Recipe needs a known serving yield before freezing")
    if servings <= 0:
        raise BourdainError("Frozen servings must be positive")
    return FreezerLot(
        id=lot_id,
        recipe=recipe.id,
        servings=servings,
        frozen_on=today,
        expires=expires,
        recipe_signature=recipe_signature(recipe),
        recipe_servings=recipe.servings,
        source="user",
    )


def add_portions(
    repo: Repository,
    recipe_id: str,
    servings: float,
    expires: date,
    *,
    lot_id: str | None = None,
    today: date | None = None,
) -> FreezerLot:
    lot = make_lot(
        repo.recipe(recipe_id).model,
        servings,
        expires,
        lot_id=lot_id or f"batch-{uuid.uuid4().hex}",
        today=today or date.today(),
    )
    document = freezer_document(repo)
    if any(item.id == lot.id for item in document.model.items):
        raise BourdainError("Freezer lot ID already exists; use a new lot ID")
    document.model.items.append(lot)
    document.save(automated=False)
    return lot


def eat_portions(
    repo: Repository,
    lot_id: str,
    servings: float,
    *,
    event_id: str | None = None,
    today: date | None = None,
) -> None:
    today = today or date.today()
    event = FreezerConsumption(lot=lot_id, servings=servings, date=today)
    document = freezer_document(repo)
    identifier = event_id or uuid.uuid4().hex
    if identifier in document.model.consumed:
        if document.model.consumed[identifier] != event:
            raise BourdainError("Freezer event ID already has different details")
        return
    lot = next((item for item in document.model.items if item.id == lot_id), None)
    if lot is None:
        raise BourdainError(f"Unknown freezer lot: {lot_id}")
    if not lot.frozen_on <= today <= lot.expires:
        raise BourdainError("Freezer lot is outside its recorded availability dates")
    if servings > lot.servings + 1e-7:
        raise BourdainError("Not enough servings in this freezer lot")
    lot.servings = max(0, lot.servings - servings)
    document.model.consumed[identifier] = event
    document.save(automated=False)
