"""Recipe extraction contract and deterministic response cache."""

import hashlib
import json
from pathlib import Path
from typing import Protocol

from pydantic import Field, TypeAdapter, ValidationError

from bourdain.errors import BourdainError
from bourdain.ingest.images import PageImage
from bourdain.models import RecipeDraft, RecipeSource
from bourdain.models.schemas import Model, Slug
from bourdain.store.repository import safe_path
from bourdain.store.yaml import atomic_write


class ExtractHints(Model):
    book: Slug | None = None
    store_steps: bool = False
    group: str | None = None
    previous_titles: list[str] = Field(default_factory=list)


class RecipeExtractor(Protocol):
    @property
    def cache_identity(self) -> str: ...

    def extract(
        self, pages: list[PageImage], hints: ExtractHints
    ) -> list[RecipeDraft]: ...


def validate_drafts(
    drafts: list[RecipeDraft], pages: list[PageImage], hints: ExtractHints
) -> list[RecipeDraft]:
    """Bind references to actual inputs; model output cannot invent source paths."""
    by_ref = {page.reference: page for page in pages}
    validated = []
    for original in drafts:
        draft = original.model_copy(deep=True)
        if not draft.page_refs or not set(draft.page_refs) <= by_ref.keys():
            raise BourdainError("Extractor returned missing or unknown page references")
        draft.page_refs = list(dict.fromkeys(draft.page_refs))
        image_pages: dict[str, list[int]] = {}
        for reference in draft.page_refs:
            page = by_ref[reference]
            image_pages.setdefault(page.original, []).append(page.page)
        draft.source = RecipeSource(
            kind="cookbook",
            book=hints.book,
            page=draft.source.page,
            pages=draft.source.pages,
            images=list(image_pages),
            image_pages=image_pages,
        )
        if not hints.store_steps:
            draft.steps = []
        if draft.servings is None:
            draft.flags.append("missing_servings")
        for name in ("title", "servings"):
            if draft.confidence.get(name, 0) < 0.85:
                draft.flags.append(f"low_extraction:{name}")
        for name, confidence in draft.confidence.items():
            if not name.startswith("ingredients.") and confidence < 0.85:
                draft.flags.append(f"low_extraction:{name}")
        for index, line in enumerate(draft.ingredients):
            confidence = draft.confidence.get(f"ingredients.{index}", line.confidence)
            line.confidence = confidence
            if confidence is None or confidence < 0.85:
                line.flags = list(dict.fromkeys(line.flags + ["low_extraction"]))
        if not draft.ingredients:
            draft.flags.append("missing_ingredients")
        draft.flags = list(dict.fromkeys(draft.flags))
        validated.append(draft)
    return validated


class FakeExtractor:
    """Read fixture drafts; page_refs may use zero-based @0, @1 placeholders."""

    def __init__(self, fixture: Path) -> None:
        self.fixture = fixture
        self.calls = 0

    @property
    def cache_identity(self) -> str:
        return "fake-v1:" + hashlib.sha256(self.fixture.read_bytes()).hexdigest()

    def extract(self, pages: list[PageImage], hints: ExtractHints) -> list[RecipeDraft]:
        self.calls += 1
        data = json.loads(self.fixture.read_text(encoding="utf-8"))
        for draft in data:
            draft["page_refs"] = [
                pages[int(ref[1:])].reference if ref.startswith("@") else ref
                for ref in draft["page_refs"]
            ]
        return validate_drafts(
            TypeAdapter(list[RecipeDraft]).validate_python(data), pages, hints
        )


class CachedExtractor:
    def __init__(self, extractor: RecipeExtractor, root: Path) -> None:
        self.extractor = extractor
        self.root = root.resolve()

    @property
    def cache_identity(self) -> str:
        return self.extractor.cache_identity

    def extract(self, pages: list[PageImage], hints: ExtractHints) -> list[RecipeDraft]:
        if not pages:
            raise BourdainError("No pages supplied for extraction")
        payload = {
            "version": 1,
            "extractor": self.cache_identity,
            "pages": [(p.reference, p.content_hash) for p in pages],
            "hints": hints.model_dump(mode="json"),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        path = safe_path(self.root, f".cache/extraction/{digest}.json")
        adapter = TypeAdapter(list[RecipeDraft])
        if path.exists():
            try:
                return validate_drafts(
                    adapter.validate_json(path.read_bytes()), pages, hints
                )
            except (ValueError, ValidationError) as exc:
                raise BourdainError(
                    f"Invalid extraction cache; remove {path.name} and retry"
                ) from exc
        result = validate_drafts(self.extractor.extract(pages, hints), pages, hints)
        atomic_write(path, adapter.dump_json(result))
        return result
