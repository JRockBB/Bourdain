from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from ruamel.yaml import YAML

from bourdain.models import CatalogItem, IngredientLine
from bourdain.normalize.catalog import normalize_line
from bourdain.normalize.parse import parse_line
from bourdain.normalize.units import convert_mass, to_grams

CASES = YAML(typ="safe").load(
    (Path(__file__).parent / "fixtures" / "ingredient_lines.yaml").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CASES, ids=[case["raw"] for case in CASES])
def test_parser_golden(case: dict) -> None:
    parsed = parse_line(case["raw"]).model_dump(mode="json")
    for name, value in case["expected"].items():
        assert parsed[name] == value
    assert parsed["raw"] == case["raw"]


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("g", 2),
        ("kg", 2000),
        ("oz", 56.69904625),
        ("lb", 907.18474),
        ("ml", 2),
        ("cup", 473.176473),
        ("tbsp", 29.5735295625),
        ("tsp", 9.8578431875),
        ("each", 100),
        ("clove", 10),
        ("can", 800),
    ],
)
def test_units_table(unit: str, expected: float) -> None:
    catalog = CatalogItem(
        name="example",
        density_g_per_ml=1,
        unit_weights={"each": 50, "clove": 5, "can": 400},
    )
    result = to_grams(
        IngredientLine(raw="example", qty=2, unit=unit, item="example"), catalog
    )
    assert result.grams == pytest.approx(expected)
    assert not result.flags


@given(
    st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    st.sampled_from(["g", "kg", "oz", "lb"]),
)
def test_mass_round_trip(grams: float, unit: str) -> None:
    assert convert_mass(convert_mass(grams, "g", unit), unit) == pytest.approx(grams)


@pytest.mark.parametrize(
    ("raw", "flag"),
    [
        ("1 cup flour", "missing_density"),
        ("1 clove garlic", "missing_unit_weight"),
        ("some rice", "unresolved_quantity"),
        ("2-3 onions", "unresolved_quantity"),
    ],
)
def test_unknown_conversion_does_not_guess(raw: str, flag: str) -> None:
    result = to_grams(parse_line(raw))
    assert result.grams is None
    assert flag in result.flags


def test_exact_alias_and_stub_reuse() -> None:
    catalog = {
        "onion": CatalogItem(
            name="onion", aliases=["yellow onions"], unit_weights={"each": 100}
        )
    }
    line = normalize_line(parse_line("2 yellow onions"), catalog)
    assert line.ingredient_id == "onion"
    assert line.grams == 200
    unknown = normalize_line(parse_line("10 g parsley"), catalog)
    again = normalize_line(parse_line("20 g parsley"), catalog)
    assert unknown.ingredient_id == again.ingredient_id
    assert unknown.flags == again.flags == ["unreviewed_catalog:parsley"]
    assert len(catalog) == 2


def test_ambiguous_alias_is_flagged() -> None:
    catalog = {
        "red": CatalogItem(name="red", aliases=["onion"]),
        "yellow": CatalogItem(name="yellow", aliases=["onion"]),
    }
    line = normalize_line(parse_line("1 onion"), catalog)
    assert line.ingredient_id is None
    assert "ambiguous_catalog_match" in line.flags
