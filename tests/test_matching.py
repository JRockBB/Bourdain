import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from bourdain.errors import BourdainError
from bourdain.models import Catalog, CatalogItem
from bourdain.normalize.catalog import confirm_alias, normalize_line
from bourdain.normalize.matcher import AnthropicMatcher, FakeMatcher, MatchProposal
from bourdain.normalize.parse import parse_line

FIXTURES = Path(__file__).parent / "fixtures"


def catalog() -> Catalog:
    return {
        "green-onions": CatalogItem(name="green onions", aliases=["scallions"]),
        "olive-oil": CatalogItem(name="extra virgin olive oil", aliases=["EVOO"]),
        "chicken-thigh": CatalogItem(name="chicken thighs"),
    }


@pytest.mark.parametrize(
    "example", json.loads((FIXTURES / "catalog_matches.json").read_text())
)
def test_matching_golden_and_learning(example: dict) -> None:
    items = catalog()
    line = parse_line(f"100 g {example['name']}")
    result = normalize_line(line, items)
    assert result.ingredient_id == example["id"]
    assert bool(result.flags) is example["fuzzy"]
    assert result.grams == 100
    confirm_alias(items, result.ingredient_id, line.item)
    assert normalize_line(line, items).flags == []
    assert normalize_line(line, items).match_confidence == 1


def test_threshold_ambiguity_and_protected_lines() -> None:
    line = parse_line("100 g chiken thighs")
    assert (
        normalize_line(line, catalog(), threshold=100).ingredient_id == "chiken-thighs"
    )
    ambiguous = {
        "red": CatalogItem(name="red onions"),
        "white": CatalogItem(name="white onions"),
    }
    result = normalize_line(parse_line("100 g onions"), ambiguous)
    assert result.ingredient_id is None
    assert "ambiguous_catalog_match" in result.flags
    line.source, line.grams = "user", 5
    assert normalize_line(line, catalog()) == line


def test_fake_matcher_existing_and_new_catalog_branches() -> None:
    items = catalog()
    before = items["green-onions"].model_dump()
    matcher = FakeMatcher(
        {
            "spring alliums": MatchProposal(
                ingredient_id="green-onions", confidence=0.8
            ),
            "black lime": MatchProposal(
                new_item=CatalogItem(
                    name="dried black lime",
                    source="user",
                    locked=True,
                    fdc_id=123,
                    unit_weights={"each": 5},
                    user_fields=["unit_weights"],
                ),
                confidence=0.6,
            ),
        }
    )
    matched = normalize_line(parse_line("100 g spring alliums"), items, matcher=matcher)
    assert matched.ingredient_id == "green-onions"
    assert matched.source == "llm"
    assert matched.flags == ["llm_match:green-onions"]
    assert items["green-onions"].model_dump() == before
    proposed = normalize_line(parse_line("2 black lime"), items, matcher=matcher)
    entry = items[proposed.ingredient_id]
    assert entry.source == "llm" and not entry.locked
    assert entry.user_fields == []
    assert entry.fdc_id is None
    assert proposed.grams == 10
    assert "unreviewed_catalog:dried-black-lime" in proposed.flags
    repeated = normalize_line(parse_line("2 black lime"), items)
    assert repeated.ingredient_id == proposed.ingredient_id and repeated.flags
    assert matcher.calls == 2


def test_matcher_rejects_invalid_choice_and_alias_conflicts() -> None:
    with pytest.raises(ValidationError):
        MatchProposal(confidence=1)
    bad = FakeMatcher(
        {"saffron": MatchProposal(ingredient_id="invented", confidence=1)}
    )
    with pytest.raises(BourdainError, match="outside"):
        normalize_line(parse_line("1 g saffron"), catalog(), matcher=bad)
    with pytest.raises(BourdainError, match="already belongs"):
        confirm_alias(catalog(), "green-onions", "EVOO")


class Client:
    def __init__(self, outcome: MatchProposal) -> None:
        self.messages, self.outcome, self.calls = self, outcome, 0

    def parse(self, **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        assert kwargs["output_format"] is MatchProposal
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.outcome)


def test_structured_match_cache_and_invalidation(tmp_path: Path) -> None:
    client = Client(MatchProposal(ingredient_id="green-onions", confidence=0.9))
    matcher = AnthropicMatcher("model", tmp_path, client=client)
    first = matcher.match("spring onions", catalog())
    assert matcher.match("spring onions", catalog()) == first and client.calls == 1
    path = next((tmp_path / ".cache/matches").glob("*.json"))
    changed = catalog()
    changed["green-onions"].aliases.append("new alias")
    matcher.match("spring onions", changed)
    assert client.calls == 2
    with pytest.raises(BourdainError, match="ANTHROPIC_API_KEY"):
        AnthropicMatcher("model", tmp_path).match("different name", catalog())
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(BourdainError, match="Invalid match cache"):
        matcher.match("spring onions", catalog())


def test_cli_passes_threshold_and_optional_matcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from bourdain.cli import app
    from bourdain.store import Repository, initialize

    repo = Repository(initialize(tmp_path / "data"))
    document = repo.catalog()
    document.model.update(catalog())
    document.save()
    config = repo.path("bourdain.toml")
    config.write_text(
        config.read_text().replace("match_threshold = 85", "match_threshold = 100")
    )
    matcher = FakeMatcher(
        {"chiken thighs": MatchProposal(ingredient_id="chicken-thigh", confidence=0.9)}
    )
    monkeypatch.setattr("bourdain.cli.make_matcher", lambda settings: matcher)
    result = CliRunner().invoke(
        app,
        ["--data", str(repo.root), "ingest", "text", "-", "--llm-match"],
        input="Soup\nServings: 2\n100 g chiken thighs\n",
    )
    assert result.exit_code == 0, result.output
    assert matcher.calls == 1
    assert repo.recipe("text/soup").model.ingredients[0].source == "llm"
