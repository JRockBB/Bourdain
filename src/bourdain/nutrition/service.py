"""Reviewable USDA mappings, recipe calculations and independent comparison data."""

import re
from datetime import UTC, datetime
from typing import Annotated

from pydantic import Field
from rapidfuzz.fuzz import token_sort_ratio

from bourdain.errors import BourdainError
from bourdain.models import Catalog, CatalogItem, Macros, Nutrition, Recipe, Review
from bourdain.models.schemas import Derived, Slug
from bourdain.nutrition.fdc import DATA_TYPES, Food, FoodProvider
from bourdain.store import Repository
from bourdain.store.yaml import YamlDocument, create_yaml, load_yaml


class FoodMapping(Derived):
    fdc_id: Annotated[int, Field(gt=0)] | None = None
    description: str | None = None
    data_type: str | None = None
    flags: list[str] = Field(default_factory=list)


type Mappings = dict[Slug, FoodMapping]


def mapping_document(repo: Repository) -> YamlDocument[Mappings]:
    if not repo.path("catalog/ingredients.yaml").exists():
        raise BourdainError("Initialize the data folder before mapping nutrition")
    path = repo.path("catalog/fdc.yaml")
    if not path.exists():
        create_yaml(path, {}, Mappings)
    return load_yaml(path, Mappings)


def rank_foods(item: CatalogItem, foods: list[Food]) -> list[tuple[Food, float]]:
    names = [item.name, *item.aliases]
    ranked = []
    # Contradictory preparation states should not win on shared words.
    opposites = [
        ("raw", "cooked"),
        ("salted", "unsalted"),
        ("dried", "fresh"),
        ("boneless", "bone-in"),
    ]
    for food in foods:
        description = food.description.casefold()
        score = max(
            token_sort_ratio(
                name.casefold().replace(",", " "), description.replace(",", " ")
            )
            for name in names
        )
        tokens = set(re.findall(r"[a-z]+(?:-[a-z]+)*", description))
        target = set(re.findall(r"[a-z]+(?:-[a-z]+)*", item.name.casefold()))
        if any(
            (a in target and b in tokens) or (b in target and a in tokens)
            for a, b in opposites
        ):
            score *= 0.4
        ranked.append((food, score))
    return sorted(
        ranked,
        key=lambda entry: (
            -round(entry[1] / 5),
            DATA_TYPES.get(entry[0].data_type, 4),
            -entry[1],
            entry[0].fdc_id,
        ),
    )


def map_catalog(
    repo: Repository, provider: FoodProvider, *, ingredient: str | None = None
) -> Mappings:
    catalog, document = repo.catalog().model, mapping_document(repo)
    if ingredient is not None and ingredient not in catalog:
        raise BourdainError(f"Unknown catalog ingredient: {ingredient}")
    for identifier, item in catalog.items():
        if ingredient is not None and identifier != ingredient:
            continue
        prior = document.model.get(identifier)
        if item.fdc_id is not None or (
            prior
            and (prior.fdc_id is not None or prior.locked or prior.source == "user")
        ):
            continue
        ranked = rank_foods(item, provider.search(item.name))
        if not ranked:
            document.model[identifier] = FoodMapping(
                source="fdc", confidence=0, flags=["no_fdc_match"]
            )
            continue
        food, score = ranked[0]
        gap = score - ranked[1][1] if len(ranked) > 1 else 100
        flags = [] if score >= 95 and gap >= 5 else ["review_fdc_match"]
        document.model[identifier] = FoodMapping(
            fdc_id=food.fdc_id,
            description=food.description,
            data_type=food.data_type,
            source="fdc",
            confidence=score / 100,
            flags=flags,
        )
    return document.save()


def confirm_mapping(
    repo: Repository,
    identifier: str,
    food: Food,
    *,
    edible_fraction: float | None = None,
) -> None:
    catalog_doc, document = repo.catalog(), mapping_document(repo)
    if identifier not in catalog_doc.model:
        raise BourdainError(f"Unknown catalog ingredient: {identifier}")
    if edible_fraction is not None:
        if not 0 <= edible_fraction <= 1:
            raise BourdainError("Edible fraction must be between 0 and 1")
        catalog_doc.model[identifier].edible_fraction = edible_fraction
        catalog_doc.save(automated=False)
    document.model[identifier] = FoodMapping(
        fdc_id=food.fdc_id,
        description=food.description,
        data_type=food.data_type,
        source="user",
        confidence=1,
        locked=True,
    )
    # An explicit mapping overrides the legacy ID as an intentional user edit.
    catalog_doc.model[identifier].fdc_id = food.fdc_id
    catalog_doc.model[identifier].fdc_description = food.description
    catalog_doc.save(automated=False)
    document.save(automated=False)


def calculate(
    recipe: Recipe, catalog: Catalog, mappings: Mappings, provider: FoodProvider
) -> tuple[Nutrition, list[str]]:
    totals = {name: 0.0 for name in Macros.model_fields}
    missing: set[str] = set()
    flags: list[str] = []
    total_g = known_g = 0.0
    unknown_weight = False
    for line in recipe.ingredients:
        if line.optional:
            continue
        if line.grams is None:
            unknown_weight = True
            missing.update(totals)
            flags.append(f"nutrition:missing_grams:{line.item}")
            continue
        if line.grams == 0:
            continue
        total_g += line.grams
        item = catalog.get(line.ingredient_id or "")
        mapping = mappings.get(line.ingredient_id or "")
        identifier = item.fdc_id if item else None
        if identifier is None and mapping:
            identifier = mapping.fdc_id
        if (
            not item
            or identifier is None
            or (mapping and mapping.flags and item.fdc_id is None)
        ):
            missing.update(totals)
            flags.append(f"nutrition:unmapped:{line.item}")
            continue
        if item.edible_fraction is None:
            missing.update(totals)
            flags.append(f"nutrition:missing_edible_fraction:{line.ingredient_id}")
            continue
        values = provider.food(identifier).macros().model_dump()
        complete = True
        for name, value in values.items():
            if value is None:
                missing.add(name)
                complete = False
                flags.append(f"nutrition:missing_{name}:{line.ingredient_id}")
            else:
                totals[name] += line.grams * item.edible_fraction * value / 100
        if complete:
            known_g += line.grams
    coverage = known_g / total_g if total_g else 0
    if unknown_weight:
        coverage = 0
    if recipe.servings is None:
        missing.update(totals)
        flags.append("nutrition:missing_servings")
    macros = Macros(
        **{
            name: None if name in missing else value / (recipe.servings or 1)
            for name, value in totals.items()
        }
    )
    if coverage < 0.95:
        flags.append("nutrition:low_coverage")
    nutrition = Nutrition(source="fdc", coverage=coverage, per_serving=macros)
    return nutrition, list(dict.fromkeys(flags))


def refresh(
    repo: Repository, provider: FoodProvider, *, recipe_id: str | None = None
) -> dict[str, Nutrition]:
    catalog, mappings = repo.catalog().model, mapping_document(repo).model
    recipes = {recipe_id: repo.recipe(recipe_id).model} if recipe_id else repo.recipes()
    results = {}
    for identifier, recipe in recipes.items():
        computed, flags = calculate(recipe, catalog, mappings, provider)
        path = repo.path(f"nutrition/fdc/{identifier}.yaml")
        previous = load_yaml(path, Nutrition) if path.exists() else None
        if previous and previous.model.model_dump(
            exclude={"computed_at"}
        ) == computed.model_dump(exclude={"computed_at"}):
            computed.computed_at = previous.model.computed_at
        else:
            computed.computed_at = datetime.now(UTC)
        if previous:
            previous.save(computed)
        else:
            create_yaml(path, computed, Nutrition)
        results[identifier] = computed
        if (
            recipe.locked
            or "nutrition" in recipe.user_fields
            or (
                recipe.nutrition
                and (
                    recipe.nutrition.locked
                    or recipe.nutrition.source in {"user", "cronometer"}
                )
            )
        ):
            continue
        recipe.nutrition = computed
        recipe.review = Review(
            flags=[
                flag
                for flag in recipe.review.flags
                if not flag.startswith("nutrition:")
            ]
            + flags
        )
        repo.save_recipe(recipe)
    return results


def differences(
    repo: Repository, *, threshold: float = 0.1
) -> list[tuple[str, str, float, float, float]]:
    result = []
    for identifier, recipe in repo.recipes().items():
        path = repo.path(f"nutrition/fdc/{identifier}.yaml")
        if (
            not recipe.nutrition
            or recipe.nutrition.source != "cronometer"
            or not path.exists()
        ):
            continue
        fdc = load_yaml(path, Nutrition).model.per_serving
        for name in Macros.model_fields:
            left, right = (
                getattr(fdc, name),
                getattr(recipe.nutrition.per_serving, name),
            )
            if left is None or right is None or left == right:
                continue
            deviation = abs(left - right) / max(abs(right), 1e-12)
            if deviation > threshold:
                result.append((identifier, name, left, right, deviation))
    return result
