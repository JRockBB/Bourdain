"""Search, tag, and exclude recipes without changing automated fields."""

from bourdain.errors import BourdainError
from bourdain.models import CatalogItem, Recipe
from bourdain.models.schemas import UnitWeightSource
from bourdain.normalize.parse import ALIASES
from bourdain.store import Repository


def search_recipes(
    recipes: dict[str, Recipe],
    query: str = "",
    *,
    tag: str | None = None,
    book: str | None = None,
    protein: str | None = None,
    status: str | None = None,
) -> list[Recipe]:
    needle = query.casefold().strip()
    return [
        recipe
        for _, recipe in sorted(recipes.items())
        if (not tag or tag in recipe.tags)
        and (not book or book == recipe.source.book)
        and (not protein or protein == recipe.protein)
        and (not status or status == recipe.review.status)
        and (
            not needle
            or needle
            in " ".join(
                [
                    recipe.id,
                    recipe.title,
                    *recipe.tags,
                    *(line.item for line in recipe.ingredients),
                ]
            ).casefold()
        )
    ]


def tag_recipe(
    repo: Repository, recipe_id: str, tag: str, *, remove: bool = False
) -> Recipe:
    document = repo.recipe(recipe_id)
    if remove:
        document.model.tags = [item for item in document.model.tags if item != tag]
    elif tag not in document.model.tags:
        document.model.tags.append(tag)
    return document.save(automated=False)


def exclude_recipe(
    repo: Repository, recipe_id: str, *, excluded: bool = True
) -> Recipe:
    document = repo.recipe(recipe_id)
    document.model.excluded = excluded
    return document.save(automated=False)


def set_unit_weight(
    repo: Repository,
    ingredient: str,
    unit: str,
    grams: float,
    *,
    buy: bool = False,
    provenance: UnitWeightSource | None = None,
) -> None:
    document = repo.catalog()
    if ingredient not in document.model:
        raise BourdainError(f"Unknown ingredient: {ingredient}")
    unit = ALIASES.get(unit.casefold(), unit.casefold())
    from pydantic import TypeAdapter

    from bourdain.models.schemas import Slug

    TypeAdapter(Slug).validate_python(unit)
    item = document.model[ingredient].model_dump()
    item["unit_weights"][unit] = grams
    if provenance is None:
        item["unit_weight_sources"].pop(unit, None)
    else:
        item["unit_weight_sources"][unit] = provenance.model_dump()
    item["user_fields"] = list(
        dict.fromkeys(
            item["user_fields"]
            + ["unit_weights", "unit_weight_sources"]
            + (["buy"] if buy else [])
        )
    )
    if buy:
        item["buy"]["unit"] = unit
    document.model[ingredient] = CatalogItem.model_validate(item)
    document.save(automated=False)
