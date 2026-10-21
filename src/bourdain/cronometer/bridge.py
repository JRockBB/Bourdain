"""Local paste blocks and explicit-basis CSV nutrition imports; no account access."""

import csv
import hashlib
import io
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from bourdain.errors import BourdainError
from bourdain.models import Catalog, Macros, Nutrition, Recipe, Review
from bourdain.models.schemas import Model
from bourdain.store import Repository
from bourdain.store.yaml import atomic_write, create_yaml, load_yaml


class ExportRecord(Model):
    digest: str
    exported_at: datetime


type ExportHistory = dict[str, ExportRecord]


def paste_block(recipe: Recipe, catalog: Catalog) -> str:
    if recipe.servings is None:
        raise BourdainError(f"Supply servings before exporting {recipe.id}")
    lines = [recipe.title, f"Servings: {recipe.servings:g}", ""]
    for line in recipe.ingredients:
        if line.optional:
            continue
        if line.grams is None:
            raise BourdainError(
                f"Supply grams before exporting {recipe.id}: {line.raw}"
            )
        if line.grams == 0:
            continue
        if line.ingredient_id not in catalog:
            raise BourdainError(
                f"Match ingredient before exporting {recipe.id}: {line.raw}"
            )
        item = catalog[line.ingredient_id]
        if item.edible_fraction is None:
            raise BourdainError(
                f"Supply edible_fraction before exporting: {line.ingredient_id}"
            )
        grams = line.grams * item.edible_fraction
        name = " ".join((item.fdc_description or item.name).split())
        lines.append(f"{grams:g} g {name}")
    if len(lines) == 3:
        raise BourdainError(f"No weighed ingredients to export: {recipe.id}")
    return "\n".join(lines) + "\n"


def copy_text(text: str) -> bool:
    if sys.platform == "win32":
        command, content = ["clip.exe"], text.encode("utf-16le")
    elif sys.platform == "darwin":
        command, content = ["pbcopy"], text.encode()
    elif shutil.which("wl-copy"):
        command, content = ["wl-copy"], text.encode()
    elif shutil.which("xclip"):
        command, content = ["xclip", "-selection", "clipboard"], text.encode()
    else:
        return False
    try:
        subprocess.run(
            command,
            input=content,
            check=True,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def export_recipes(
    repo: Repository,
    *,
    recipe_id: str | None = None,
    all_new: bool = False,
    clipboard: Callable[[str], bool] = copy_text,
) -> tuple[str, bool]:
    if (recipe_id is None) == (not all_new):
        raise BourdainError("Choose a recipe ID or --all-new")
    path = repo.path("cronometer_exports.yaml")
    history_doc = load_yaml(path, ExportHistory) if path.exists() else None
    history = history_doc.model if history_doc else {}
    recipes = {recipe_id: repo.recipe(recipe_id).model} if recipe_id else repo.recipes()
    catalog, blocks = repo.catalog().model, []
    updates = {}
    for identifier, recipe in sorted(recipes.items()):
        block = paste_block(recipe, catalog)
        digest = hashlib.sha256(block.encode()).hexdigest()
        if all_new and identifier in history and history[identifier].digest == digest:
            continue
        blocks.append((identifier, block))
        updates[identifier] = ExportRecord(digest=digest, exported_at=datetime.now(UTC))
    if not blocks:
        return "No new recipes to export.\n", False
    if all_new:
        text = "# Cronometer queue\n\n" + "\n".join(
            f"## {identifier}\n\n```text\n{block}```\n" for identifier, block in blocks
        )
        atomic_write(repo.path("cronometer_queue.md"), text.encode())
        copied = clipboard(blocks[0][1])
    else:
        text, copied = blocks[0][1], False
    history.update(updates)
    if history_doc:
        history_doc.save(history, automated=False)
    else:
        create_yaml(path, history, ExportHistory)
    return text, copied


HEADERS = {
    "energy": "kcal",
    "calories": "kcal",
    "kcal": "kcal",
    "protein": "protein_g",
    "fat": "fat_g",
    "total fat": "fat_g",
    "saturated": "sat_fat_g",
    "saturated fat": "sat_fat_g",
    "carbs": "carb_g",
    "carbohydrate": "carb_g",
    "carbohydrates": "carb_g",
    "fiber": "fiber_g",
    "fibre": "fiber_g",
    "sugars": "sugar_g",
    "sugar": "sugar_g",
    "sodium": "sodium_mg",
}


def _header(value: str) -> tuple[str | None, str | None]:
    value = value.strip().casefold().replace("_", " ")
    match = re.fullmatch(r"(.+?)\s*\((kcal|kj|g|mg)\)", value)
    if match:
        return HEADERS.get(match[1].strip()), match[2]
    if value in HEADERS:
        name = HEADERS[value]
        return name, "kcal" if name == "kcal" else "mg" if name == "sodium_mg" else "g"
    return None, None


def _amount(value: str, name: str, unit: str) -> float | None:
    if not value.strip():
        return None
    try:
        amount = float(value.strip())
    except ValueError:
        raise BourdainError(f"Invalid CSV amount for {name}") from None
    target = "kcal" if name == "kcal" else "mg" if name == "sodium_mg" else "g"
    if unit == "kj" and target == "kcal":
        amount /= 4.184
    elif unit == "g" and target == "mg":
        amount *= 1000
    elif unit == "mg" and target == "g":
        amount /= 1000
    elif unit != target:
        raise BourdainError(f"Unexpected unit for {name}: {unit}")
    return amount


def parse_csv(text: str) -> list[tuple[str | None, Macros]]:
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    rows = [row for row in rows if row and any(value.strip() for value in row)]
    if not rows:
        raise BourdainError("Nutrition CSV is empty")
    headers = [value.strip().casefold().replace("_", " ") for value in rows[0]]
    if "nutrient" in headers and "amount" in headers:
        values = {}
        for row in rows[1:]:
            if len(row) != len(headers):
                raise BourdainError("Malformed nutrition CSV row")
            name, unit = _header(row[headers.index("nutrient")])
            if name is None:
                continue
            unit = (
                row[headers.index("unit")].strip().casefold()
                if "unit" in headers
                else unit
            )
            if name in values:
                raise BourdainError(f"Duplicate CSV nutrient: {name}")
            values[name] = _amount(row[headers.index("amount")], name, unit)
        if not values:
            raise BourdainError("No supported nutrients in CSV")
        return [(None, Macros.model_validate(values))]
    fields = [_header(value) for value in rows[0]]
    if not any(name for name, _ in fields):
        raise BourdainError(
            "Unsupported CSV: use nutrient/amount/unit rows or named nutrient columns"
        )
    name_index = next(
        (
            index
            for index, value in enumerate(headers)
            if value in {"name", "recipe", "food name", "food"}
        ),
        None,
    )
    result = []
    for row in rows[1:]:
        if len(row) != len(headers):
            raise BourdainError("Malformed nutrition CSV row")
        values = {}
        for value, (name, unit) in zip(row, fields, strict=True):
            if name:
                if name in values:
                    raise BourdainError(f"Duplicate CSV nutrient: {name}")
                values[name] = _amount(value, name, unit)
        result.append(
            (
                row[name_index] if name_index is not None else None,
                Macros.model_validate(values),
            )
        )
    if not result:
        raise BourdainError("Nutrition CSV contains no data rows")
    return result


def import_nutrition(
    repo: Repository,
    path: Path,
    *,
    basis: Literal["per-serving", "full-recipe", "per-100g"],
    recipe_id: str | None = None,
    recipe_weight_g: float | None = None,
) -> list[str]:
    if basis not in {"per-serving", "full-recipe", "per-100g"}:
        raise BourdainError("Specify CSV basis: per-serving, full-recipe or per-100g")
    rows, library = parse_csv(path.read_text(encoding="utf-8-sig")), repo.recipes()
    if recipe_id and len(rows) != 1:
        raise BourdainError("--recipe requires a CSV containing one nutrition record")
    updates = {}
    for name, macros in rows:
        matches = (
            [recipe_id]
            if recipe_id
            else [
                identifier
                for identifier, recipe in library.items()
                if name and recipe.title.casefold() == name.strip().casefold()
            ]
        )
        if len(matches) != 1 or matches[0] not in library:
            raise BourdainError(
                f"Recipe name is missing or ambiguous: {name}; "
                "use a one-record CSV and --recipe"
            )
        identifier, factor = matches[0], 1.0
        recipe = library[identifier]
        if identifier in updates:
            raise BourdainError(f"Duplicate recipe record in CSV: {identifier}")
        if basis != "per-serving":
            if recipe.servings is None:
                raise BourdainError(f"Supply recipe servings first: {identifier}")
            factor /= recipe.servings
        if basis == "per-100g":
            if (
                recipe_weight_g is None
                or not 0 < recipe_weight_g < float("inf")
                or len(rows) != 1
            ):
                raise BourdainError(
                    "per-100g requires one record and positive --recipe-weight-g"
                )
            factor *= recipe_weight_g / 100
        values = {
            name: value * factor if value is not None else None
            for name, value in macros.model_dump().items()
        }
        complete = all(value is not None for value in values.values())
        recipe.nutrition = Nutrition(
            source="cronometer",
            locked=True,
            confidence=1,
            coverage=1 if complete else 0,
            per_serving=Macros(**values),
            computed_at=datetime.now(UTC),
        )
        flags = [
            flag for flag in recipe.review.flags if not flag.startswith("nutrition:")
        ]
        if not complete:
            flags.append("nutrition:incomplete_cronometer")
        recipe.review = Review(flags=flags)
        updates[identifier] = recipe
    for recipe in updates.values():
        repo.save_recipe(recipe, automated=False)
    return list(updates)
