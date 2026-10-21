from pathlib import Path
from urllib.error import HTTPError

import pytest

from bourdain.errors import BourdainError
from bourdain.models import CatalogItem, IngredientLine, Macros, Nutrition, Recipe
from bourdain.nutrition.fdc import FakeFDC, FDCClient, Food
from bourdain.nutrition.service import (
    FoodMapping,
    calculate,
    confirm_mapping,
    differences,
    map_catalog,
    mapping_document,
    rank_foods,
    refresh,
)
from bourdain.store import Repository, initialize


def food(
    identifier: int, name: str, protein: float = 10, *, data_type: str = "Foundation"
) -> Food:
    values = {
        1008: (100, "kcal"),
        1003: (protein, "g"),
        1004: (4, "g"),
        1258: (1, "g"),
        1005: (8, "g"),
        1079: (2, "g"),
        2000: (3, "g"),
        1093: (50, "mg"),
    }
    return Food(
        fdcId=identifier,
        description=name,
        dataType=data_type,
        foodNutrients=[
            {"nutrient": {"id": key, "unitName": unit}, "amount": value}
            for key, (value, unit) in values.items()
        ],
    )


def test_fdc_transport_cache_preference_and_secret_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FDC_API_KEY", "private-test-key")
    calls = []
    foods = [food(1, "carrot", data_type="Branded"), food(2, "carrot")]

    def fetch(endpoint: str, params: dict, api_key: str) -> dict:
        calls.append((endpoint, params))
        assert api_key == "private-test-key"
        return (
            {"foods": [entry.model_dump(by_alias=True) for entry in foods]}
            if endpoint == "foods/search"
            else foods[1].model_dump(by_alias=True)
        )

    client = FDCClient(tmp_path, fetch=fetch)
    assert [entry.fdc_id for entry in client.search("carrot")] == [2, 1]
    assert client.food(2).macros().protein_g == 10
    monkeypatch.delenv("FDC_API_KEY")
    assert client.food(2).fdc_id == 2
    assert len(client.search("carrot")) == 2
    assert len(calls) == 2
    for path in (tmp_path / ".cache").rglob("*.json"):
        assert "private-test-key" not in path.read_text()
    with pytest.raises(BourdainError, match="FDC_API_KEY"):
        client.food(3)


def test_fdc_backoff_and_sanitized_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FDC_API_KEY", "private")
    sleeps, attempts = [], []

    def fetch(*args: object) -> dict:
        attempts.append(1)
        raise HTTPError("secret-url", 429, "secret-body", {"Retry-After": "500"}, None)

    with pytest.raises(BourdainError, match="HTTP 429") as error:
        FDCClient(tmp_path, fetch=fetch, sleep=sleeps.append).food(1)
    assert sleeps == [30, 30] and len(attempts) == 3
    assert "secret" not in str(error.value)
    assert not list(tmp_path.rglob("*.json"))


def test_nutrient_units_energy_precedence_and_missing_values() -> None:
    entry = Food(
        fdcId=1,
        description="x",
        foodNutrients=[
            {"nutrient": {"id": 1008, "unitName": "kcal"}, "amount": 100},
            {"nutrient": {"id": 2048, "unitName": "kcal"}, "amount": 95},
            {"nutrient": {"id": 1093, "unitName": "g"}, "amount": 0.25},
        ],
    )
    macros = entry.macros()
    assert macros.kcal == 95 and macros.sodium_mg == 250
    assert macros.fiber_g is None


COMMON = [
    "carrots, raw",
    "onions, raw",
    "garlic, raw",
    "tomatoes, raw",
    "spinach, raw",
    "broccoli, raw",
    "cabbage, raw",
    "celery, raw",
    "potatoes, raw",
    "sweet potatoes, raw",
    "eggplant, raw",
    "zucchini, raw",
    "cucumber, raw",
    "lettuce, raw",
    "apples, raw",
    "bananas, raw",
    "oranges, raw",
    "lemons, raw",
    "rice, white, raw",
    "lentils, raw",
    "chickpeas, raw",
    "black beans, raw",
    "oats, raw",
    "wheat flour",
    "olive oil",
    "butter, unsalted",
    "milk, whole",
    "eggs, raw",
    "chicken breast, raw",
    "salmon, raw",
]


@pytest.mark.parametrize("name", COMMON)
def test_thirty_common_ingredient_rankings(name: str) -> None:
    item = CatalogItem(name=name)
    foods = [
        food(1, name, data_type="Branded"),
        food(2, "mixed frozen meal"),
        food(3, name, data_type="SR Legacy"),
    ]
    assert rank_foods(item, foods)[0][0].fdc_id == 3


def test_mapping_sidecar_preserves_user_catalog_and_accepts_explicit_choice(
    tmp_path: Path,
) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    document = repo.catalog()
    document.model["carrot"] = CatalogItem(name="carrot", source="user", locked=True)
    document.save()
    before = document.path.read_bytes()
    provider = FakeFDC([food(1, "carrot")])
    assert map_catalog(repo, provider)["carrot"].fdc_id == 1
    assert document.path.read_bytes() == before
    confirm_mapping(repo, "carrot", provider.food(1), edible_fraction=0.9)
    assert repo.catalog().model["carrot"].edible_fraction == 0.9
    assert mapping_document(repo).model["carrot"].locked


@pytest.mark.parametrize(
    ("grams", "fraction", "servings", "expected"),
    [(200, 1, 2, 100), (300, 0.8, 4, 60), (50, 0.5, 1, 25)],
)
def test_recipe_macros_hand_calculated(
    grams: float, fraction: float, servings: float, expected: float
) -> None:
    recipe = Recipe(
        id="soup",
        title="Soup",
        servings=servings,
        ingredients=[
            IngredientLine(raw="food", item="food", ingredient_id="food", grams=grams)
        ],
    )
    catalog = {"food": CatalogItem(name="food", fdc_id=1, edible_fraction=fraction)}
    result, flags = calculate(recipe, catalog, {}, FakeFDC([food(1, "food")]))
    assert result.coverage == 1 and flags == []
    assert result.per_serving.kcal == pytest.approx(expected)
    assert result.per_serving.protein_g == pytest.approx(expected / 10)
    assert result.per_serving.sodium_mg == pytest.approx(expected / 2)


def test_unknown_grams_and_unreviewed_mapping_never_become_zero_macros() -> None:
    recipe = Recipe(
        id="soup",
        title="Soup",
        servings=2,
        ingredients=[
            IngredientLine(
                raw="carrot", item="carrot", ingredient_id="carrot", grams=100
            )
        ],
    )
    catalog = {"carrot": CatalogItem(name="carrot", edible_fraction=1)}
    mapping = {"carrot": FoodMapping(fdc_id=1, flags=["review_fdc_match"])}
    result, flags = calculate(recipe, catalog, mapping, FakeFDC([food(1, "carrot")]))
    assert result.coverage == 0 and result.per_serving.kcal is None
    assert "nutrition:low_coverage" in flags
    recipe.ingredients[0].grams = None
    result, _ = calculate(recipe, catalog, {}, FakeFDC([]))
    assert result.coverage == 0 and result.per_serving.protein_g is None


def test_refresh_idempotent_and_locked_cronometer_comparison(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    document = repo.catalog()
    document.model["food"] = CatalogItem(name="food", fdc_id=1, edible_fraction=1)
    document.save()
    recipe = Recipe(
        id="soup",
        title="Soup",
        servings=1,
        ingredients=[
            IngredientLine(
                raw="100 g food", item="food", ingredient_id="food", grams=100
            )
        ],
    )
    repo.save_recipe(recipe)
    provider = FakeFDC([food(1, "food")])
    refresh(repo, provider)
    before = repo.recipe_path("soup").read_bytes()
    comparison = repo.path("nutrition/fdc/soup.yaml").read_bytes()
    refresh(repo, provider)
    assert repo.recipe_path("soup").read_bytes() == before
    assert repo.path("nutrition/fdc/soup.yaml").read_bytes() == comparison
    protected = repo.recipe("soup")
    protected.model.user_fields = ["nutrition"]
    protected.model.nutrition.per_serving.kcal = 333
    protected.model.review.flags = ["nutrition:manual_review"]
    protected.save(automated=False)
    protected_bytes = protected.path.read_bytes()
    refresh(repo, provider)
    assert protected.path.read_bytes() == protected_bytes
    doc = repo.recipe("soup")
    doc.model.user_fields = []
    doc.model.nutrition = Nutrition(
        source="cronometer",
        locked=True,
        coverage=1,
        per_serving=Macros(kcal=120, protein_g=10),
    )
    doc.save(automated=False)
    locked = doc.path.read_bytes()
    refresh(repo, provider)
    assert doc.path.read_bytes() == locked
    assert differences(repo)[0][:4] == ("soup", "kcal", 100, 120)
