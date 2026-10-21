"""Explicitly opted-in API checks; never run with the default offline suite."""

import json
import os
from pathlib import Path

import pytest

from bourdain.config import LLMConfig
from bourdain.ingest.anthropic import AnthropicExtractor
from bourdain.ingest.extractor import CachedExtractor, ExtractHints
from bourdain.ingest.images import load_pages
from bourdain.models import CatalogItem
from bourdain.normalize.matcher import AnthropicMatcher
from bourdain.nutrition.fdc import FDCClient

pytestmark = pytest.mark.live


def test_five_live_fdc_foods(tmp_path: Path) -> None:
    if not os.environ.get("FDC_API_KEY") or not os.environ.get("BOURDAIN_LIVE_FDC"):
        pytest.skip("Needs FDC_API_KEY and BOURDAIN_LIVE_FDC manifest")
    cases = json.loads(
        Path(os.environ["BOURDAIN_LIVE_FDC"]).read_text(encoding="utf-8")
    )
    assert len(cases) == 5
    assert all(
        isinstance(case["fdc_id"], int)
        and case["fdc_id"] > 0
        and isinstance(case["description"], str)
        and isinstance(case["protein_g"], (int, float))
        for case in cases
    ), "Replace food manifest placeholders before calling USDA"
    client = FDCClient(tmp_path)
    for case in cases:
        food = client.food(case["fdc_id"])
        assert food.description == case["description"]
        assert food.macros().protein_g == pytest.approx(case["protein_g"])
        assert client.food(case["fdc_id"]) == food


def test_real_cookbook_pages(tmp_path: Path) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get(
        "BOURDAIN_LIVE_PAGES"
    ):
        pytest.skip("Needs ANTHROPIC_API_KEY and BOURDAIN_LIVE_PAGES manifest")
    manifest = Path(os.environ["BOURDAIN_LIVE_PAGES"])
    cases = json.loads(manifest.read_text(encoding="utf-8"))
    assert {case["case"] for case in cases} == {
        "two-page",
        "two-recipes",
        "handwritten",
    }
    for case in cases:
        assert case["files"] and case["recipes"]
        for file in case["files"]:
            assert (manifest.parent / file).is_file(), f"Missing fixture: {file}"
    extractor = CachedExtractor(
        AnthropicExtractor(
            os.environ.get("BOURDAIN_LIVE_VISION_MODEL", LLMConfig().model)
        ),
        tmp_path,
    )
    for case in cases:
        pages = [
            page
            for file in case["files"]
            for page in load_pages(manifest.parent / file, tmp_path)
        ]
        drafts = extractor.extract(pages, ExtractHints(book="live-fixtures"))
        assert len(drafts) == len(case["recipes"])
        actual = {draft.title.casefold(): draft for draft in drafts}
        for expected in case["recipes"]:
            draft = actual[expected["title"].casefold()]
            assert draft.servings == expected["servings"]
            assert [line.raw.casefold().strip() for line in draft.ingredients] == [
                line.casefold().strip() for line in expected["ingredients"]
            ]


ALIASES = [
    ("scallions", "green onions"),
    ("EVOO", "extra virgin olive oil"),
    ("aubergine", "eggplant"),
    ("courgette", "zucchini"),
    ("caster sugar", "superfine sugar"),
    ("icing sugar", "powdered sugar"),
    ("rocket leaves", "arugula"),
    ("coriander leaves", "cilantro"),
    ("garbanzo beans", "chickpeas"),
    ("Chinese parsley", "cilantro"),
    ("gai lan", "Chinese broccoli"),
    ("mooli", "daikon radish"),
    ("arame seaweed", "arame"),
    ("hing", "asafoetida"),
    ("amchur", "dried mango powder"),
    ("kala namak", "black salt"),
    ("mahlab", "ground mahaleb cherry seeds"),
    ("loomi", "dried black lime"),
    ("pimenton", "smoked paprika"),
    ("katsuo-bushi", "bonito flakes"),
]


def test_twenty_live_ingredient_matches(tmp_path: Path) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("Needs ANTHROPIC_API_KEY; makes 20 catalog matching calls")
    matcher = AnthropicMatcher(
        os.environ.get("BOURDAIN_LIVE_TEXT_MODEL", LLMConfig().text_model), tmp_path
    )
    for alias, name in ALIASES:
        options = {
            "expected": CatalogItem(name=name),
            "rice": CatalogItem(name="white rice"),
            "butter": CatalogItem(name="unsalted butter"),
        }
        proposal = matcher.match(alias, options)
        assert proposal.ingredient_id == "expected", alias
