"""Explicit confirmation of USDA food identity and edible fractions."""

import click
from rich.console import Console
from rich.text import Text

from bourdain.errors import BourdainError
from bourdain.nutrition.fdc import FoodProvider
from bourdain.nutrition.service import confirm_mapping, mapping_document
from bourdain.store import Repository


def review_mappings(
    repo: Repository, provider: FoodProvider, *, console: Console | None = None
) -> int:
    console = console or Console()
    mappings = mapping_document(repo).model
    count = 0
    for identifier, item in repo.catalog().model.items():
        mapping = mappings.get(identifier)
        fdc_id = item.fdc_id or (mapping.fdc_id if mapping else None)
        if not (mapping and mapping.flags) and not (
            fdc_id and item.edible_fraction is None
        ):
            continue
        description = item.fdc_description or (
            mapping.description if mapping else "No match"
        )
        console.print(Text(f"{identifier}: {item.name}\nUSDA {fdc_id}: {description}"))
        action = click.prompt(
            "USDA mapping: [a]ccept / [m]atch ID / [s]kip / [q]uit", default="s"
        ).lower()
        if action == "q":
            break
        if action not in {"a", "m"}:
            continue
        if action == "m":
            fdc_id = click.prompt("FDC ID", type=click.IntRange(min=1))
        if fdc_id is None:
            console.print("Choose an FDC ID before accepting")
            continue
        fraction = click.prompt(
            "Edible fraction of recipe grams (0 to 1)",
            type=click.FloatRange(min=0, max=1),
            default=item.edible_fraction if item.edible_fraction is not None else 1.0,
        )
        try:
            food = provider.food(fdc_id)
            console.print(Text(food.description))
            if action == "m" and not click.confirm("Use this USDA food?", default=True):
                continue
            confirm_mapping(repo, identifier, food, edible_fraction=fraction)
            count += 1
        except BourdainError as exc:
            console.print(Text(str(exc)))
    return count
