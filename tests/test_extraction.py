import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from typer.testing import CliRunner

from bourdain.cli import app
from bourdain.errors import BourdainError
from bourdain.ingest.anthropic import AnthropicExtractor, ExtractionResponse
from bourdain.ingest.batch import extract_groups, page_groups
from bourdain.ingest.extractor import CachedExtractor, ExtractHints, FakeExtractor
from bourdain.ingest.images import PageImage, load_pages
from bourdain.library import import_draft
from bourdain.models import RecipeDraft, RecipeSource
from bourdain.normalize.parse import parse_line
from bourdain.store import Repository, initialize

FIXTURES = Path(__file__).parent / "fixtures" / "extraction"


def sample(tmp_path: Path) -> tuple[Path, list[PageImage]]:
    path = tmp_path / "page.png"
    Image.new("RGB", (50, 70), "white").save(path)
    return path, load_pages(path, tmp_path / "data")


def test_fake_cache_and_normalization_pipeline(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    _, pages = sample(tmp_path)
    fake = FakeExtractor(FIXTURES / "single.json")
    cached = CachedExtractor(fake, repo.root)
    hints = ExtractHints(book="my-book")
    first = cached.extract(pages, hints)
    assert cached.extract(pages, hints) == first
    assert fake.calls == 1
    recipe = import_draft(repo, first[0])
    assert recipe.id == "my-book/carrot-soup"
    assert recipe.ingredients[0].grams == 200
    assert recipe.source.image_pages == {pages[0].original: [1]}
    cached.extract(pages, hints.model_copy(update={"store_steps": True}))
    assert fake.calls == 2
    cached.extract(list(reversed(pages)), hints.model_copy(update={"book": "another"}))
    assert fake.calls == 3


class ScriptedClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.messages = self
        self.outcomes = iter(outcomes)
        self.requests: list[dict] = []

    def parse(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


def response(page: PageImage) -> SimpleNamespace:
    draft = RecipeDraft(
        title="Soup",
        servings=2,
        page_refs=[page.reference],
        ingredients=[parse_line("200 g carrots")],
        confidence={"title": 0.4},
    )
    return SimpleNamespace(
        stop_reason="end_turn", parsed_output=ExtractionResponse(recipes=[draft])
    )


def test_anthropic_request_validation_and_prompt_identity(tmp_path: Path) -> None:
    _, pages = sample(tmp_path)
    client = ScriptedClient([response(pages[0])])
    extractor = AnthropicExtractor("test-model", client=client)
    drafts = extractor.extract(pages, ExtractHints(book="book"))
    assert "low_extraction:title" in drafts[0].flags
    assert drafts[0].ingredients[0].source == "llm"
    request = client.requests[0]
    assert request["output_format"] is ExtractionResponse
    assert request["model"] == "test-model"
    assert request["messages"][0]["content"][2]["source"]["media_type"] == "image/jpeg"
    assert "api_key" not in str(request)
    before = extractor.cache_identity
    extractor.prompt += " changed prompt"
    assert extractor.cache_identity != before
    assert AnthropicExtractor("other-model").cache_identity != before


def test_anthropic_missing_key_and_sanitized_retries(tmp_path: Path) -> None:
    import anthropic
    import httpx2

    _, pages = sample(tmp_path)
    with pytest.raises(BourdainError, match="ANTHROPIC_API_KEY"):
        AnthropicExtractor("model").extract(pages, ExtractHints())
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.RateLimitError(
        "do-not-display-secret",
        response=httpx2.Response(429, request=request),
        body=None,
    )
    client = ScriptedClient([error, response(pages[0])])
    sleeps = []
    extractor = AnthropicExtractor("model", client=client, sleep=sleeps.append)
    assert extractor.extract(pages, ExtractHints())
    assert sleeps == [1]
    failing = AnthropicExtractor(
        "model", client=ScriptedClient([error]), max_attempts=1
    )
    with pytest.raises(BourdainError) as caught:
        failing.extract(pages, ExtractHints())
    assert "429" in str(caught.value)
    assert "secret" not in str(caught.value)


def test_bad_source_and_truncation_not_cached(tmp_path: Path) -> None:
    _, pages = sample(tmp_path)
    bad = response(pages[0])
    bad.parsed_output.recipes[0].page_refs = ["../../private.png"]
    extractor = CachedExtractor(
        AnthropicExtractor("model", client=ScriptedClient([bad])), tmp_path / "data"
    )
    with pytest.raises(BourdainError, match="references"):
        extractor.extract(pages, ExtractHints())
    assert not list((tmp_path / "data" / ".cache").rglob("*.json"))
    truncated = response(pages[0])
    truncated.stop_reason = "max_tokens"
    with pytest.raises(BourdainError, match="incomplete"):
        AnthropicExtractor("model", client=ScriptedClient([truncated])).extract(
            pages, ExtractHints()
        )


class SevenPageExtractor:
    cache_identity = "seven-page-fixture-v1"

    def __init__(self, pages: list[PageImage]) -> None:
        self.numbers = {page.reference: index + 1 for index, page in enumerate(pages)}
        self.calls = 0

    def extract(self, pages: list[PageImage], hints: ExtractHints) -> list[RecipeDraft]:
        self.calls += 1
        fixtures = json.loads((FIXTURES / "seven-pages.json").read_text())
        result = []
        for fixture in fixtures:
            relevant = [
                page
                for page in pages
                if str(self.numbers[page.reference]) in fixture["pages"]
            ]
            if relevant:
                printed = [self.numbers[p.reference] + 10 for p in relevant]
                lines = [
                    parse_line(raw)
                    for page in relevant
                    for raw in fixture["pages"][str(self.numbers[page.reference])]
                ]
                result.append(
                    RecipeDraft(
                        title=fixture["title"],
                        servings=2,
                        ingredients=lines,
                        page_refs=[p.reference for p in relevant],
                        source=RecipeSource(
                            kind="cookbook", page=min(printed), pages=printed
                        ),
                    )
                )
        return result


def test_seven_pages_five_recipes_and_cached_rerun(tmp_path: Path) -> None:
    pages = [
        PageImage(f"raw/day/{index}.jpg", 1, 10, 10, bytes([index]))
        for index in range(7)
    ]
    fake = SevenPageExtractor(pages)
    cached = CachedExtractor(fake, tmp_path)
    hints = ExtractHints(book="book")
    drafts = extract_groups([(None, pages)], cached, hints)
    assert [draft.title for draft in drafts] == [
        "Soup",
        "Salad",
        "Rice",
        "Bread",
        "Stew",
    ]
    assert [draft.source.pages for draft in drafts] == [
        [11, 12],
        [12, 13],
        [14, 15],
        [16],
        [17],
    ]
    assert [len(draft.ingredients) for draft in drafts] == [2, 2, 2, 1, 1]
    assert extract_groups([(None, pages)], cached, hints) == drafts
    assert fake.calls == 6


def test_filename_order_and_subfolders(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    (inbox / "forced").mkdir(parents=True)
    for name, color in [
        ("page10.png", "red"),
        ("page2.png", "blue"),
        ("forced/one.png", "green"),
    ]:
        Image.new("RGB", (10, 10), color).save(inbox / name)
    groups = page_groups(inbox, tmp_path / "data")
    assert {name for name, _ in groups} == {None, "forced"}
    root_pages = dict(groups)[None]
    assert (
        root_pages[0].reference
        == load_pages(inbox / "page2.png", tmp_path / "data")[0].reference
    )
    with pytest.raises(BourdainError, match="capture time"):
        page_groups(inbox, tmp_path / "data", order="exif")


def test_photos_cli_with_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    path, _ = sample(tmp_path)
    monkeypatch.setattr(
        "bourdain.cli.make_extractor",
        lambda settings: CachedExtractor(
            FakeExtractor(FIXTURES / "single.json"), repo.root
        ),
    )
    result = CliRunner().invoke(
        app, ["--data", str(repo.root), "ingest", "photos", str(path), "--book", "book"]
    )
    assert result.exit_code == 0, result.output
    assert repo.recipe("book/carrot-soup").model.source.page == 12


def test_merge_retains_conflicts_and_ingredient_uncertainty() -> None:
    from bourdain.ingest.batch import _merge

    first = RecipeDraft(
        title="Soup", servings=2, ingredients=[parse_line("100 g carrots")]
    )
    second = first.model_copy(deep=True)
    second.servings = 4
    second.ingredients[0].flags = ["low_extraction"]
    second.ingredients[0].confidence = 0.3
    merged = _merge(_merge(first, second), first)
    assert merged.servings is None and "conflicting_servings" in merged.flags
    assert "low_extraction" in merged.ingredients[0].flags
    assert merged.ingredients[0].confidence == 0.3


def test_missing_confidence_and_explicit_raw_weight(tmp_path: Path) -> None:
    _, pages = sample(tmp_path)
    draft = RecipeDraft(
        title="Sauce",
        servings=2,
        page_refs=[pages[0].reference],
        ingredients=[parse_line("1 cup (200 g) flour")],
    )
    draft.ingredients[0].grams = 9999  # An untrusted model-derived weight.
    draft.ingredients[0].confidence = None
    client = ScriptedClient(
        [
            SimpleNamespace(
                stop_reason="end_turn",
                parsed_output=ExtractionResponse(recipes=[draft]),
            )
        ]
    )
    result = AnthropicExtractor("model", client=client).extract(pages, ExtractHints())[
        0
    ]
    assert result.ingredients[0].grams == 200
    assert "low_extraction" in result.ingredients[0].flags
    assert "low_extraction:title" in result.flags
    assert "low_extraction:servings" in result.flags


def test_pdf_title_collision_uses_physical_pages(tmp_path: Path) -> None:
    repo = Repository(initialize(tmp_path / "data"))
    draft = RecipeDraft(
        title="Soup",
        servings=2,
        ingredients=[parse_line("100 g carrots")],
        source=RecipeSource(
            kind="cookbook",
            book="book",
            images=["raw/book.pdf"],
            image_pages={"raw/book.pdf": [1]},
        ),
    )
    import_draft(repo, draft)
    draft.source.image_pages = {"raw/book.pdf": [3]}
    with pytest.raises(BourdainError, match="Different cookbook pages"):
        import_draft(repo, draft)


@pytest.mark.parametrize("value", ["", "0", "-1", "2-1", "1,1", "1-101", "x"])
def test_invalid_page_selection(value: str) -> None:
    from bourdain.cli import parse_page_selection

    with pytest.raises(BourdainError, match="unique PDF"):
        parse_page_selection(value)


def test_valid_page_selection() -> None:
    from bourdain.cli import parse_page_selection

    assert parse_page_selection("3-5,8") == [3, 4, 5, 8]
