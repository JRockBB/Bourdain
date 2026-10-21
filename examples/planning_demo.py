"""Create an explicitly synthetic planning sandbox; never uses the default kitchen."""

import argparse
from datetime import date, timedelta
from pathlib import Path

from bourdain.models import (
    BuyInfo,
    CatalogItem,
    Eater,
    IngredientLine,
    Macros,
    Nutrition,
    Pantry,
    PantryItem,
    Recipe,
    RecipeTime,
    Targets,
)
from bourdain.prices import PackagePrice, price_document
from bourdain.store import Repository, initialize


def seed(root: Path) -> None:
    repo = Repository(initialize(root))
    catalog, prices = {}, {}
    names = [
        "Carrot stew",
        "Bean bowl",
        "Lentil soup",
        "Rice skillet",
        "Chickpea salad",
        "Pasta bake",
    ]
    # These quantities/macros are test data to demonstrate the planner, not real
    # nutrition estimates. Replace them before using a recipe in a real kitchen.
    for index, title in enumerate(names):
        item_id = f"demo-ingredient-{index}"
        catalog[item_id] = CatalogItem(
            name=f"Demo ingredient {index}",
            perishable=True,
            shelf_life_days=7,
            edible_fraction=1,
            buy=BuyInfo(package_g=[250], section="produce"),
        )
        prices[item_id] = [
            PackagePrice(package_g=250, price=1 + index * 0.25, source="user")
        ]
        repo.save_recipe(
            Recipe(
                id=f"demo/recipe-{index}",
                title=f"DEMO {title}",
                servings=2,
                keeps_days=2,
                tags=["dinner", "lunch"],
                protein="plant",
                time=RecipeTime(active_min=20, total_min=30),
                ingredients=[
                    IngredientLine(
                        raw=f"200 g {item_id}",
                        item=item_id,
                        ingredient_id=item_id,
                        grams=200,
                        source="user",
                    )
                ],
                nutrition=Nutrition(
                    source="user",
                    locked=True,
                    coverage=1,
                    per_serving=Macros(
                        kcal=500,
                        protein_g=25,
                        fat_g=15,
                        sat_fat_g=3,
                        carb_g=60,
                        fiber_g=10,
                        sugar_g=5,
                        sodium_mg=400,
                    ),
                ),
            )
        )
    repo.catalog().save(catalog)
    price_document(repo).save(prices)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    repo.pantry().save(
        Pantry(
            items=[
                PantryItem(
                    ingredient_id="demo-ingredient-0",
                    qty_g=400,
                    expires=monday + timedelta(days=7),
                )
            ]
        )
    )
    repo.targets().save(
        Targets(
            eaters=[
                Eater(name="demo", daily={"protein_g": (25, 25), "kcal": (500, 500)})
            ]
        )
    )
    print(f"Created synthetic demo: {repo.root}")
    print(f"Try: bourdain --data {root} plan --compare 0,0.5,1")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", type=Path, help="New sandbox folder; existing folders are refused"
    )
    seed(parser.parse_args().path)
