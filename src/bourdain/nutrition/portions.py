"""Review USDA portions and explicitly bind a selected measure to a count unit."""

import math
import re

from bourdain.catalog import set_unit_weight
from bourdain.errors import BourdainError
from bourdain.models import CatalogItem
from bourdain.models.schemas import UnitWeightSource
from bourdain.normalize.parse import ALIASES
from bourdain.nutrition.fdc import Food, FoodPortion, FoodProvider
from bourdain.store import Repository

MASS_VOLUME = {
    "g",
    "kg",
    "oz",
    "lb",
    "ml",
    "l",
    "cup",
    "tbsp",
    "tsp",
    "fl oz",
    "pint",
    "quart",
}
COUNT_UNITS = set(ALIASES.values()) - MASS_VOLUME


def _item(repo: Repository, ingredient: str) -> CatalogItem:
    item = repo.catalog().model.get(ingredient)
    if item is None:
        raise BourdainError(f"Unknown ingredient: {ingredient}")
    if item.fdc_id is None:
        raise BourdainError(
            f"Confirm a USDA food for {ingredient} with review --nutrition "
            "or nutrition set before using its portions"
        )
    return item


def portion_food(
    repo: Repository, provider: FoodProvider, ingredient: str, *, refresh: bool = False
) -> Food:
    """Read portions without changing the catalog; only refresh bypasses the cache."""
    item = _item(repo, ingredient)
    assert item.fdc_id is not None
    food = (
        provider.food(item.fdc_id, refresh=True)
        if refresh
        else provider.food(item.fdc_id)
    )
    if food.fdc_id != item.fdc_id:
        raise BourdainError("USDA food ID does not match the catalog")
    return food


def _is_mass_or_volume(portion: FoodPortion) -> bool:
    # SR/FNDDS often put the actual measure in free text and use "undetermined"
    # for measureUnit. A cup of chopped food must not become a whole-item count.
    text = portion.label.casefold()
    return any(
        re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text)
        for alias, unit in ALIASES.items()
        if unit in MASS_VOLUME
    ) or any(
        ALIASES.get(str(value).casefold()) in MASS_VOLUME
        for value in (portion.measure_unit or {}).values()
    )


def use_portion(
    repo: Repository,
    provider: FoodProvider,
    ingredient: str,
    portion_id: int,
    unit: str,
    *,
    count: float | None = None,
    buy: bool = False,
    replace: bool = False,
) -> float:
    """An explicit selection confirms the food, portion description and count label.

    USDA edible grams are converted to the catalog's recipe-weight basis. A
    missing amount (common in FNDDS) requires the caller to confirm its count.
    """
    unit = ALIASES.get(unit.strip().casefold(), unit.strip().casefold())
    if unit not in COUNT_UNITS:
        raise BourdainError("Choose a count unit such as each, medium, clove or head")
    item = _item(repo, ingredient)
    if item.edible_fraction is None or item.edible_fraction <= 0:
        raise BourdainError("Confirm an edible fraction greater than zero first")
    food = portion_food(repo, provider, ingredient)
    matches = [row for row in food.food_portions or [] if row.id == portion_id]
    if len(matches) != 1:
        raise BourdainError("Portion ID is missing or ambiguous; run catalog portions")
    portion = matches[0]
    if not portion.label or _is_mass_or_volume(portion):
        raise BourdainError(
            "Select a described count portion; mass/volume portions cannot be "
            "saved as whole-item weights"
        )
    amount = portion.amount if count is None else count
    if (
        amount is None
        or not math.isfinite(amount)
        or amount <= 0
        or portion.gram_weight is None
        or portion.gram_weight <= 0
    ):
        raise BourdainError(
            "Portion needs positive grams and a count; use --count only when "
            "the source omits its amount"
        )
    if portion.amount is not None and amount != portion.amount:
        raise BourdainError("The count must agree with the USDA portion amount")
    origin = UnitWeightSource(
        fdc_id=food.fdc_id,
        description=food.description,
        portion_id=portion_id,
        portion_description=portion.label,
        amount=amount,
        portion_grams=portion.gram_weight,
        edible_fraction=item.edible_fraction,
    )
    grams = portion.gram_weight / amount / item.edible_fraction
    if not math.isfinite(grams) or grams <= 0:
        raise BourdainError("Portion conversion is outside the supported range")
    if (
        unit in item.unit_weights
        and not replace
        and (
            item.unit_weights[unit] != grams
            or item.unit_weight_sources.get(unit) != origin
        )
    ):
        raise BourdainError(
            f"{ingredient} already has a {unit} weight; use --replace to change it"
        )
    set_unit_weight(repo, ingredient, unit, grams, buy=buy, provenance=origin)
    return grams
