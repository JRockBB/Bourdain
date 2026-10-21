"""Scale cook events, subtract pantry once, and round purchases to packages."""

import math
from dataclasses import dataclass, field
from datetime import date

from bourdain.errors import BourdainError
from bourdain.models import Catalog, Freezer, PackagePurchase, Pantry, Plan, Recipe
from bourdain.normalize.units import shopping_unit
from bourdain.pantry.availability import fingerprint


@dataclass
class GroceryItem:
    ingredient_id: str
    name: str
    section: str
    need_g: float = 0
    buy_g: float = 0
    package_g: float | None = None
    packages: int | None = None
    recipes: list[str] = field(default_factory=list)
    check: bool = False
    notes: list[str] = field(default_factory=list)
    package_purchases: list[PackagePurchase] = field(default_factory=list)


@dataclass
class GroceryList:
    week: str
    items: list[GroceryItem]
    warnings: list[str] = field(default_factory=list)


def _planned_purchase(plan: Plan, item: GroceryItem) -> None:
    identifier = item.ingredient_id
    if identifier in plan.package_purchases:
        item.package_purchases = [
            purchase.model_copy() for purchase in plan.package_purchases[identifier]
        ]
        item.packages = sum(purchase.count for purchase in item.package_purchases)
        if item.packages != plan.package_counts.get(identifier):
            raise BourdainError(f"Inconsistent package counts in plan: {identifier}")
        item.package_g = (
            item.package_purchases[0].package_g
            if len(item.package_purchases) == 1
            else None
        )
        item.buy_g = sum(
            purchase.count * purchase.package_g for purchase in item.package_purchases
        )
    else:
        size = plan.package_sizes_g.get(identifier)
        if size is None:
            raise BourdainError(f"Plan lacks a package size: {identifier}")
        item.package_g, item.packages = size, plan.package_counts[identifier]
        item.buy_g = size * item.packages


def groceries(
    plan: Plan,
    recipes: dict[str, Recipe],
    catalog: Catalog,
    pantry: Pantry,
    *,
    today: date | None = None,
    include_optional: bool = False,
    freezer: Freezer | None = None,
) -> GroceryList:
    today = today or date.today()
    if plan.freezer_usage:
        from bourdain.freezer import (
            fingerprint as freezer_fingerprint,
        )
        from bourdain.freezer import (
            recipe_signature,
        )

        if (
            freezer is None
            or freezer_fingerprint(freezer, set(plan.freezer_usage))
            != plan.freezer_fingerprint
        ):
            raise BourdainError(
                "Freezer stock changed or was not supplied; "
                "replan before exporting groceries"
            )
        for lot in freezer.items:
            if lot.id in plan.freezer_usage and (
                lot.expires < today
                or lot.recipe not in recipes
                or recipe_signature(recipes[lot.recipe]) != lot.recipe_signature
            ):
                raise BourdainError(
                    "Planned freezer portion is no longer usable; replan first"
                )
    if include_optional and plan.solver_status:
        raise BourdainError(
            "Optional ingredients were excluded from optimization; use a manual plan "
            "or mark them required and replan"
        )
    needs: dict[str, GroceryItem] = {}
    warnings = []
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    if (
        plan.pantry_fingerprint
        and fingerprint(pantry, set(plan.package_counts)) != plan.pantry_fingerprint
    ):
        raise BourdainError(
            "Pantry changed since optimization; replan before exporting groceries"
        )
    for event in plan.cook_events:
        if plan.replan_from and days.index(event.day) < days.index(plan.replan_from):
            continue
        if event.recipe not in recipes:
            raise BourdainError(f"Plan references unknown recipe: {event.recipe}")
        recipe = recipes[event.recipe]
        for line in recipe.ingredients:
            if line.optional and not include_optional:
                continue
            identifier = line.ingredient_id
            if identifier is None or identifier not in catalog:
                raise BourdainError(
                    f"Match ingredient before shopping: {recipe.id}: {line.raw}"
                )
            item = catalog[identifier]
            if line.grams is None and not item.staple:
                raise BourdainError(
                    f"Resolve grams before shopping: {recipe.id}: {line.raw}"
                )
            if identifier not in needs:
                needs[identifier] = GroceryItem(identifier, item.name, item.buy.section)
            need = needs[identifier]
            need.need_g += (line.grams or 0) * event.batches
            if recipe.title not in need.recipes:
                need.recipes.append(recipe.title)
    if plan.solver_status:
        actual_needs = {identifier: item.need_g for identifier, item in needs.items()}
        if any(
            abs(
                actual_needs.get(identifier, 0)
                - plan.ingredient_needs_g.get(identifier, 0)
            )
            > 1e-6
            for identifier in actual_needs.keys() | plan.ingredient_needs_g.keys()
        ):
            raise BourdainError(
                "Recipe quantities changed since optimization; replan first"
            )
    result = []
    for identifier, need in needs.items():
        item = catalog[identifier]
        stocks = [stock for stock in pantry.items if stock.ingredient_id == identifier]
        usable = []
        for stock in stocks:
            if stock.expires and stock.expires < today:
                warnings.append(f"Expired pantry item ignored: {item.name}")
            else:
                usable.append(stock)
        low = any(stock.status == "low" for stock in usable)
        if plan.pantry_fingerprint and identifier in plan.package_counts:
            free = any(
                stock.qty_g is None and stock.status in {"have", "low"}
                for stock in usable
            ) or (item.staple and not stocks)
            available = sum(
                stock.qty_g or 0 for stock in usable if stock.status != "out"
            )
            if not free and available + 1e-6 < plan.pantry_usage_g.get(identifier, 0):
                raise BourdainError(
                    f"Planned pantry stock is no longer usable: {item.name}; "
                    "replan first"
                )
            _planned_purchase(plan, need)
            if (
                not free
                and need.buy_g + plan.pantry_usage_g.get(identifier, 0) + 1e-6
                < need.need_g
            ):
                raise BourdainError(
                    f"Planned packages no longer cover {item.name}; replan first"
                )
            if need.buy_g == 0:
                if low:
                    need.check = True
                    need.notes.append("pantry low")
                    result.append(need)
                continue
            result.append(need)
            continue
        if item.staple:
            if low:
                need.check = True
                need.notes.append("pantry low")
                result.append(need)
            continue
        if any(stock.status == "have" and stock.qty_g is None for stock in usable):
            continue
        if any(stock.status == "low" and stock.qty_g is None for stock in usable):
            need.check = True
            need.notes.append("pantry low")
            result.append(need)
            continue
        pantry_g = sum(stock.qty_g or 0 for stock in usable if stock.status != "out")
        shortfall = max(0, need.need_g - pantry_g)
        need.check = low and shortfall <= 1e-9
        if shortfall <= 1e-9 and not need.check:
            continue
        if identifier in plan.package_counts:
            _planned_purchase(plan, need)
            if need.buy_g + 1e-7 < shortfall:
                raise BourdainError(
                    f"Pantry changed; planned packages no longer cover {item.name}"
                )
        elif shortfall > 0 and item.buy.package_g:
            # Use one available size, minimize surplus, then number of packages.
            choices = [
                (math.ceil(shortfall / size - 1e-12), size)
                for size in item.buy.package_g
            ]
            count, size = min(choices, key=lambda pair: (pair[0] * pair[1], pair[0]))
            need.packages, need.package_g, need.buy_g = count, size, count * size
        else:
            need.buy_g = shortfall
            unit = shopping_unit(item)
            if shortfall > 0 and unit:
                count = math.ceil(shortfall / unit[1] - 1e-12)
                need.package_purchases = [
                    PackagePurchase(package_g=unit[1], count=count, unit=unit[0])
                ]
                need.packages, need.package_g = count, unit[1]
                need.buy_g = count * unit[1]
            elif shortfall > 0 and item.buy.unit not in {
                "g",
                "kg",
                "lb",
                "oz",
                "ml",
                "l",
            }:
                warnings.append(
                    f"{item.name}: confirm the shopping unit weight "
                    "before buying by count"
                )
        if low:
            need.notes.append("pantry low")
        result.append(need)
    return GroceryList(plan.week, result, list(dict.fromkeys(warnings)))
