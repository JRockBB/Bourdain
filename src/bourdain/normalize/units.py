"""Mass conversion and explicit density/count conversions using Pint."""

from functools import lru_cache
from math import isclose

import pint

from bourdain.models import CatalogItem, IngredientLine
from bourdain.normalize.parse import ALIASES

PINT_NAMES = {
    "fl oz": "fluid_ounce",
    "tbsp": "tablespoon",
    "tsp": "teaspoon",
    "ml": "milliliter",
}


@lru_cache(maxsize=1)
def registry() -> pint.UnitRegistry:
    return pint.UnitRegistry()


def convert_mass(value: float, unit: str, target: str = "g") -> float:
    return float(
        registry().Quantity(value, ALIASES.get(unit, unit)).to(target).magnitude
    )


def unit_weight(item: CatalogItem, unit: str) -> float | None:
    """Reject a sourced conversion whose food, edible basis or weight changed."""
    weight = item.unit_weights.get(unit)
    origin = item.unit_weight_sources.get(unit)
    if origin and (
        item.fdc_id != origin.fdc_id
        or item.edible_fraction != origin.edible_fraction
        or weight is None
        or not isclose(
            weight, origin.portion_grams / origin.amount / origin.edible_fraction
        )
    ):
        return None
    return weight


def shopping_unit(item: CatalogItem) -> tuple[str, float] | None:
    """A shopping count needs a chosen unit and a confirmed catalog weight."""
    unit = ALIASES.get(item.buy.unit, item.buy.unit)
    if (
        item.source in {"llm", "parser"}
        and not item.locked
        and "unit_weights" not in item.user_fields
    ):
        return None
    weight = unit_weight(item, unit)
    return (unit, weight) if weight else None


def to_grams(
    line: IngredientLine, catalog: CatalogItem | None = None
) -> IngredientLine:
    result = line.model_copy(deep=True)
    if result.locked or result.source == "user" or result.grams is not None:
        return result
    flag: str | None = None
    if result.qty is None:
        flag = "unresolved_quantity"
    elif result.qty == 0:
        result.grams = 0
    else:
        unit = ALIASES.get(result.unit or "each", result.unit or "each")
        try:
            quantity = registry().Quantity(result.qty, PINT_NAMES.get(unit, unit))
            if quantity.check("[mass]"):
                result.grams = float(quantity.to("g").magnitude)
            elif quantity.check("[volume]"):
                if catalog and catalog.density_g_per_ml is not None:
                    result.grams = (
                        float(quantity.to("ml").magnitude) * catalog.density_g_per_ml
                    )
                else:
                    flag = "missing_density"
            else:
                flag = "missing_unit_weight"
        except (pint.UndefinedUnitError, pint.DimensionalityError):
            flag = "missing_unit_weight"
        if flag == "missing_unit_weight" and catalog and unit in catalog.unit_weights:
            weight = unit_weight(catalog, unit)
            if weight is None:
                flag = "stale_unit_weight"
            else:
                result.grams = result.qty * weight
                flag = None
    if flag and flag not in result.flags:
        result.flags.append(flag)
    if flag:
        result.confidence = min(result.confidence or 0.5, 0.5)
    return result
