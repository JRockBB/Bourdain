"""Cookbook index stubs and page selection, outside the recipe library."""

import hashlib
import json
from pathlib import Path
from typing import Annotated

from pydantic import Field, TypeAdapter

from bourdain.errors import BourdainError
from bourdain.ingest.batch import page_groups
from bourdain.ingest.documents import (
    DocumentExtractor,
    VisualDocument,
    VisualRow,
    validate_document,
)
from bourdain.models.schemas import Derived, Model, RecipeId, Slug
from bourdain.normalize.catalog import slugify
from bourdain.store import Repository
from bourdain.store.yaml import YamlDocument, create_yaml, load_yaml

type PageNumber = Annotated[int, Field(gt=0)]


class IndexDraft(VisualRow):
    title: Annotated[str, Field(min_length=1)]
    pages: list[PageNumber] = Field(default_factory=list, max_length=100)


class IndexExtraction(VisualDocument):
    entries: list[IndexDraft] = Field(default_factory=list, max_length=1000)


class IndexStub(Derived):
    title: Annotated[str, Field(min_length=1)]
    pages: list[PageNumber] = Field(default_factory=list, max_length=100)
    index_refs: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    selected: bool = False
    recipe_id: RecipeId | None = None


class BookIndex(Model):
    book: Slug
    entries: dict[Slug, IndexStub] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def index_document(repo: Repository, book: str) -> YamlDocument[BookIndex]:
    TypeAdapter(Slug).validate_python(book)
    doc = load_yaml(repo.path(f"indexes/{book}.yaml"), BookIndex)
    if doc.model.book != book:
        raise BourdainError("Index book does not match its filename")
    return doc


def import_index(
    repo: Repository,
    source: Path,
    book: str,
    extractor: DocumentExtractor,
    *,
    page_numbers: list[int] | None = None,
) -> BookIndex:
    TypeAdapter(Slug).validate_python(book)
    repo.catalog()  # Require initialization before archiving or calling the API.
    incoming: dict[str, IndexStub] = {}
    warnings: list[str] = []
    for _, pages in page_groups(source, repo.root, page_numbers=page_numbers):
        for start in range(0, len(pages), 2):
            window = pages[start : start + 2]
            result = validate_document(
                extractor.extract(window, "index", IndexExtraction),
                window,
                IndexExtraction,
            )
            warnings.extend(result.warnings)
            for entry in result.entries:
                title = " ".join(entry.title.split())
                if not title:
                    raise BourdainError("Index title cannot be blank")
                numbers = sorted(set(entry.pages))
                identity = json.dumps([title.casefold(), numbers])
                identifier = (
                    "entry-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
                )
                flags = list(entry.flags)
                if not numbers:
                    flags.append("missing_pages")
                if entry.confidence is None or entry.confidence < 0.85:
                    flags.append("review_index_entry")
                refs = (
                    incoming[identifier].index_refs if identifier in incoming else []
                ) + entry.page_refs
                if identifier in incoming:
                    flags += incoming[identifier].flags
                incoming[identifier] = IndexStub(
                    title=title,
                    pages=numbers,
                    index_refs=list(dict.fromkeys(refs)),
                    confidence=entry.confidence,
                    source="llm",
                    flags=list(dict.fromkeys(flags)),
                )
    path = repo.path(f"indexes/{book}.yaml")
    if not path.exists():
        create_yaml(path, BookIndex(book=book), BookIndex)
    document = index_document(repo, book)
    for identifier, entry in incoming.items():
        if identifier in document.model.entries:
            entry.index_refs = list(
                dict.fromkeys(
                    document.model.entries[identifier].index_refs + entry.index_refs
                )
            )
    document.model.entries.update(incoming)
    document.model.warnings = list(dict.fromkeys(document.model.warnings + warnings))
    return document.save()


def select_entry(
    repo: Repository,
    book: str,
    entry_id: str,
    *,
    selected: bool = True,
    title: str | None = None,
    pages: list[int] | None = None,
) -> IndexStub:
    document = index_document(repo, book)
    if entry_id not in document.model.entries:
        raise BourdainError(f"Unknown index entry: {entry_id}")
    entry = document.model.entries[entry_id]
    if title is not None:
        if not title.strip():
            raise BourdainError("Index title cannot be blank")
        entry.title = title.strip()
    if pages is not None:
        entry.pages = sorted(set(pages))
    if selected and not entry.pages:
        raise BourdainError("Supply printed recipe pages with --pages before selecting")
    entry.selected, entry.source, entry.locked = selected, "user", True
    entry.flags = []
    document.model.entries[entry_id] = IndexStub.model_validate(entry.model_dump())
    document.save(automated=False)
    return entry


def link_entry(repo: Repository, book: str, entry_id: str, recipe_id: str) -> None:
    recipe = repo.recipe(recipe_id).model
    if recipe.source.book != book:
        raise BourdainError("Linked recipe must belong to the same cookbook")
    document = index_document(repo, book)
    if entry_id not in document.model.entries:
        raise BourdainError(f"Unknown index entry: {entry_id}")
    entry = document.model.entries[entry_id]
    entry.recipe_id, entry.source, entry.locked = recipe_id, "user", True
    document.save(automated=False)


def index_rows(repo: Repository, book: str, *, queue: bool = False) -> list[dict]:
    index = index_document(repo, book).model
    recipes = repo.recipes()
    rows = []
    for identifier, entry in index.entries.items():
        candidate = recipes.get(entry.recipe_id or f"{book}/{slugify(entry.title)}")
        captured = bool(
            candidate
            and candidate.ingredients
            and (
                entry.recipe_id
                or (
                    candidate.source.book == book
                    and set(entry.pages)
                    & set(
                        candidate.source.pages
                        + ([candidate.source.page] if candidate.source.page else [])
                    )
                )
            )
        )
        if queue and (not entry.selected or captured):
            continue
        rows.append(
            {
                "id": identifier,
                "title": entry.title,
                "pages": entry.pages,
                "selected": entry.selected,
                "captured": captured,
                "recipe_id": candidate.id if captured else entry.recipe_id,
                "index_refs": entry.index_refs,
                "flags": entry.flags,
            }
        )
    return sorted(
        rows, key=lambda row: (row["pages"] or [10**9], row["title"], row["id"])
    )
