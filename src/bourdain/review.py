"""Explicit review decisions and a terminal UI for flagged recipes only."""

from io import BytesIO

import click
from pydantic import ValidationError
from rich.color import Color
from rich.columns import Columns
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from bourdain.errors import BourdainError
from bourdain.models import IngredientLine, Recipe, Review
from bourdain.normalize.catalog import candidates, confirm_alias
from bourdain.normalize.units import to_grams
from bourdain.store import Repository


def accept_ingredient(
    repo: Repository,
    recipe_id: str,
    index: int,
    *,
    edited: IngredientLine | None = None,
) -> Recipe:
    """Confirm the displayed line and catalog properties, then learn its alias."""
    document, catalog_doc = repo.recipe(recipe_id), repo.catalog()
    recipe, catalog = document.model, catalog_doc.model
    if not 0 <= index < len(recipe.ingredients):
        raise BourdainError("Ingredient index is out of range")
    old = recipe.ingredients[index]
    line = (edited or old).model_copy(deep=True)
    if line.ingredient_id is None or line.ingredient_id not in catalog:
        raise BourdainError("Choose a catalog ingredient before accepting")
    # Retry conversion after an inline edit or an external catalog correction.
    line.source, line.locked = None, False
    line = to_grams(line, catalog[line.ingredient_id])
    if line.grams is None:
        raise BourdainError("Supply grams or a catalog conversion before accepting")
    line.source, line.locked = "user", True
    line.confidence, line.match_confidence = 1, 1
    line.flags = []
    line = IngredientLine.model_validate(line.model_dump())
    confirm_alias(catalog, line.ingredient_id, line.item)
    entry = catalog[line.ingredient_id]
    entry.source, entry.confidence = "user", 1
    recipe.ingredients[index] = line
    remaining = [flag for ingredient in recipe.ingredients for flag in ingredient.flags]
    recipe.review = Review(
        flags=list(
            dict.fromkeys(
                [flag for flag in recipe.review.flags if flag not in old.flags]
                + remaining
            )
        )
    )
    # Each file uses the round-trip store and rejects concurrent edits. Learning an
    # alias first is safe if a subsequent recipe save encounters a concurrent edit.
    catalog_doc.save(automated=False)
    return document.save(automated=False)


def accept_recipe(repo: Repository, recipe_id: str, edited: Recipe) -> Recipe:
    """Protect accepted extraction fields while allowing later nutrition updates."""
    if edited.id != recipe_id:
        raise BourdainError("Review cannot change a recipe ID")
    if edited.servings is None or not edited.ingredients:
        raise BourdainError("Supply servings and ingredients before accepting")
    document = repo.recipe(recipe_id)
    edited = Recipe.model_validate(edited.model_dump())
    edited.review = Review(
        flags=list(
            dict.fromkeys(
                [flag for flag in edited.review.flags if flag.startswith("nutrition:")]
                + [flag for line in edited.ingredients for flag in line.flags]
            )
        )
    )
    edited.user_fields = list(
        dict.fromkeys(
            edited.user_fields
            + [
                "title",
                "servings",
                "source",
                "time",
                "steps",
                "tags",
                "extraction_reviewed",
            ]
        )
    )
    edited.extraction_reviewed = True
    return document.save(edited, automated=False)


def source_preview(repo: Repository, recipe: Recipe) -> Panel:
    """Small terminal page preview; full originals remain linked by their path."""
    if recipe.source.document:
        return Panel(
            Text(f"{recipe.source.document}\n{recipe.source.section or ''}"),
            title="Source document",
        )
    if not recipe.source.images:
        return Panel(Text(recipe.source.url or "Text/manual source"), title="Source")
    from PIL import Image, ImageOps

    relative = recipe.source.images[0]
    path = repo.path(relative)
    page = (recipe.source.image_pages.get(relative) or [1])[0]
    try:
        if path.suffix.lower() == ".pdf":
            import pymupdf

            with pymupdf.open(path) as pdf:
                rectangle = pdf[page - 1].rect
                scale = 160 / max(rectangle.width, rectangle.height)
                pixmap = pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale))
                picture = Image.open(BytesIO(pixmap.tobytes("png"))).convert("RGB")
        else:
            if path.suffix.lower() in {".heic", ".heif"}:
                from pillow_heif import register_heif_opener

                register_heif_opener()
            with Image.open(path) as original:
                picture = ImageOps.exif_transpose(original).convert("RGB")
        picture.thumbnail((32, 40))
        text = Text()
        for y in range(0, picture.height - 1, 2):
            for x in range(picture.width):
                top, bottom = picture.getpixel((x, y)), picture.getpixel((x, y + 1))
                text.append(
                    "▀",
                    style=Style(
                        color=Color.from_rgb(*top), bgcolor=Color.from_rgb(*bottom)
                    ),
                )
            text.append("\n")
        text.append(f"{relative}\nPDF/image page {page}")
        return Panel(text, title="Source page", width=40)
    except (OSError, ValueError, IndexError, RuntimeError):
        return Panel(
            Text(f"Preview unavailable: {relative}, page {page}"), title="Source"
        )


def _edit_line(line: IngredientLine) -> IngredientLine:
    edited = line.model_copy(deep=True)
    edited.item = click.prompt("Ingredient name", default=line.item)
    edited.qty = click.prompt(
        "Quantity (blank if unknown)",
        default=str(line.qty) if line.qty is not None else "",
        show_default=False,
    )
    edited.qty = float(edited.qty) if edited.qty else None
    edited.unit = (
        click.prompt("Unit", default=line.unit or "", show_default=False) or None
    )
    changed = (edited.item, edited.qty, edited.unit) != (line.item, line.qty, line.unit)
    if edited.item != line.item:
        edited.ingredient_id = None
    value = click.prompt(
        "Total grams (blank if unknown)",
        default=str(line.grams) if line.grams is not None and not changed else "",
        show_default=False,
    )
    edited.grams = float(value) if value else None
    return IngredientLine.model_validate(edited.model_dump())


def review_session(
    repo: Repository, *, recipe_id: str | None = None, console: Console | None = None
) -> int:
    console = console or Console()
    recipes = (
        [repo.recipe(recipe_id).model] if recipe_id else list(repo.recipes().values())
    )
    reviewed = 0
    for initial in recipes:
        if initial.review.status != "needs_review":
            continue
        for index, original in enumerate(initial.ingredients):
            if not original.flags:
                continue
            line = original.model_copy(deep=True)
            while True:
                catalog = repo.catalog().model
                payload = {"ingredient": line.model_dump()}
                if line.ingredient_id in catalog:
                    payload["catalog"] = catalog[line.ingredient_id].model_dump()
                console.print(Text(f"{initial.id} — ingredient {index + 1}"))
                console.print(
                    Columns(
                        [
                            source_preview(repo, initial),
                            Panel(JSON.from_data(payload), title="Parsed data"),
                        ]
                    )
                )
                action = click.prompt(
                    "[a]ccept / [e]dit / [m]atch / [s]kip / [q]uit", default="s"
                ).lower()
                try:
                    if action == "q":
                        return reviewed
                    if action == "s":
                        break
                    if action == "e":
                        line = _edit_line(line)
                    elif action == "m":
                        for match in candidates(line.item, catalog):
                            console.print(
                                Text(
                                    f"{match.ingredient_id}: "
                                    f"{catalog[match.ingredient_id].name} "
                                    f"({match.score:.0f})"
                                )
                            )
                        identifier = click.prompt("Catalog ID")
                        if identifier not in catalog:
                            raise BourdainError("Unknown catalog ID")
                        line.ingredient_id = identifier
                        # Retain explicit raw weights; clear weights derived from a
                        # different ingredient's density or count conversion.
                        from bourdain.normalize.parse import parse_line

                        line.grams = parse_line(line.raw).grams
                        line.source, line.locked = None, False
                        line = to_grams(line, catalog[identifier])
                    elif action == "a":
                        if line.grams is None:
                            line.grams = click.prompt(
                                "Total grams", type=click.FloatRange(min=0)
                            )
                        accept_ingredient(repo, initial.id, index, edited=line)
                        reviewed += 1
                        break
                except (BourdainError, ValidationError, ValueError) as exc:
                    console.print(Text(f"Cannot accept: {exc}"))
        current = repo.recipe(initial.id).model
        ingredient_flags = {flag for line in current.ingredients for flag in line.flags}
        extra_flags = [
            flag
            for flag in current.review.flags
            if flag not in ingredient_flags and not flag.startswith("nutrition:")
        ]
        if not extra_flags:
            continue
        console.print(
            Columns(
                [
                    source_preview(repo, current),
                    Panel(
                        JSON.from_data(
                            {
                                "title": current.title,
                                "servings": current.servings,
                                "time": current.time.model_dump(),
                                "flags": extra_flags,
                            }
                        ),
                        title="Recipe fields",
                    ),
                ]
            )
        )
        action = click.prompt(
            "Recipe fields: [a]ccept / [e]dit / [s]kip / [q]uit", default="s"
        ).lower()
        if action == "q":
            return reviewed
        if action in {"e", "a"}:
            if action == "e":
                current.title = click.prompt("Title", default=current.title)
                current.servings = click.prompt(
                    "Servings", type=click.FloatRange(min=0, min_open=True)
                )
            try:
                accept_recipe(repo, initial.id, current)
                reviewed += 1
            except BourdainError as exc:
                console.print(Text(f"Cannot accept: {exc}"))
    return reviewed
