"""Ordered, overlapping page extraction with conservative continuation merging."""

import re
from collections import Counter
from pathlib import Path
from typing import Literal

from bourdain.errors import BourdainError
from bourdain.ingest.extractor import ExtractHints, RecipeExtractor, validate_drafts
from bourdain.ingest.images import SUPPORTED_SUFFIXES, PageImage, load_pages
from bourdain.models import RecipeDraft
from bourdain.normalize.catalog import key


def _natural(value: str) -> tuple:
    return tuple(
        (1, int(piece)) if piece.isdigit() else (0, piece.casefold())
        for piece in re.split(r"(\d+)", value)
    )


def page_groups(
    source: Path,
    root: Path,
    *,
    order: Literal["filename", "exif"] = "filename",
    page_numbers: list[int] | None = None,
) -> list[tuple[str | None, list[PageImage]]]:
    """Subdirectories form separate groups; directory order never crosses a boundary."""
    if order not in {"filename", "exif"}:
        raise BourdainError("Page order must be filename or exif")
    if source.is_file():
        return [(None, load_pages(source, root, page_numbers=page_numbers))]
    if not source.is_dir():
        raise BourdainError(f"Input does not exist: {source}")
    if page_numbers is not None:
        raise BourdainError("Select PDF pages when importing a single PDF file")
    files = sorted(
        (
            p
            for p in source.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        ),
        key=lambda p: _natural(p.relative_to(source).as_posix()),
    )
    if not files:
        raise BourdainError("No JPEG, PNG, HEIC, or PDF files found")
    groups: dict[str | None, list[PageImage]] = {}
    for path in files:
        relative = path.parent.relative_to(source).as_posix()
        group = None if relative == "." else relative
        groups.setdefault(group, []).extend(load_pages(path, root))
    if order == "exif":
        for pages in groups.values():
            if any(page.captured_at is None for page in pages):
                raise BourdainError(
                    "EXIF ordering requires a capture time on every photo; "
                    "use filename order"
                )
            pages.sort(key=lambda page: page.captured_at)
    return list(groups.items())


def _merge(left: RecipeDraft, right: RecipeDraft) -> RecipeDraft:
    merged = left.model_copy(deep=True)
    merged.page_refs = list(dict.fromkeys(left.page_refs + right.page_refs))
    merged.flags = list(dict.fromkeys(left.flags + right.flags))
    if "conflicting_servings" in merged.flags:
        merged.servings = None
    elif left.servings is None:
        merged.servings = right.servings
    elif right.servings is not None and left.servings != right.servings:
        merged.servings = None
        merged.flags.append("conflicting_servings")
    # Multiset union retains legitimate repeated lines while removing overlap copies.
    counts = Counter((line.group, key(line.raw)) for line in left.ingredients)
    seen: Counter = Counter()
    for line in right.ingredients:
        identity = line.group, key(line.raw)
        seen[identity] += 1
        if seen[identity] > counts[identity]:
            merged.ingredients.append(line.model_copy(deep=True))
        else:
            prior = [
                item
                for item in merged.ingredients
                if (item.group, key(item.raw)) == identity
            ][seen[identity] - 1]
            prior.flags = list(dict.fromkeys(prior.flags + line.flags))
            if line.confidence is not None:
                prior.confidence = min(prior.confidence or 0, line.confidence)
            if (prior.qty, prior.unit, prior.item) != (line.qty, line.unit, line.item):
                merged.flags.append("conflicting_extraction")
    merged.steps = list(dict.fromkeys(left.steps + right.steps))
    merged.tags = list(dict.fromkeys(left.tags + right.tags))
    for field, confidence in right.confidence.items():
        if field.startswith("ingredients."):
            continue
        merged.confidence[field] = min(
            confidence, merged.confidence.get(field, confidence)
        )
    # Ingredient confidences already live on the lines; indices change on merge.
    merged.confidence = {
        name: value
        for name, value in merged.confidence.items()
        if not name.startswith("ingredients.")
    }
    printed = [p for p in [left.source.page, right.source.page] if p is not None]
    merged.source.pages = sorted(set(left.source.pages + right.source.pages + printed))
    merged.source.page = min(merged.source.pages) if merged.source.pages else None
    return merged


def extract_groups(
    groups: list[tuple[str | None, list[PageImage]]],
    extractor: RecipeExtractor,
    hints: ExtractHints,
    *,
    window_size: int = 2,
) -> list[RecipeDraft]:
    if not 2 <= window_size <= 10:
        raise BourdainError("Page window must contain between 2 and 10 pages")
    results: list[RecipeDraft] = []
    for group, pages in groups:
        drafts: list[RecipeDraft] = []
        # Advance one page at a time; shared references are the merge evidence.
        starts = range(max(1, len(pages) - window_size + 1))
        for start in starts:
            window = pages[start : start + window_size]
            context = hints.model_copy(
                update={
                    "group": group,
                    "previous_titles": [draft.title for draft in drafts],
                }
            )
            incoming = validate_drafts(
                extractor.extract(window, context), window, context
            )
            for draft in incoming:
                overlaps = [
                    index
                    for index, previous in enumerate(drafts)
                    if key(previous.title) == key(draft.title)
                    and set(previous.page_refs) & set(draft.page_refs)
                ]
                if len(overlaps) > 1:
                    raise BourdainError(
                        f"Ambiguous page grouping for {draft.title}; "
                        "use separate subfolders"
                    )
                if overlaps:
                    drafts[overlaps[0]] = _merge(drafts[overlaps[0]], draft)
                else:
                    drafts.append(draft)
        if group is not None and len(drafts) > 1:
            for draft in drafts:
                draft.flags.append("multiple_recipes_in_forced_group")
        results.extend(validate_drafts(drafts, pages, hints))
    titles = [key(draft.title) for draft in results]
    if len(titles) != len(set(titles)):
        raise BourdainError(
            "Distinct recipes share a title; import their groups separately "
            "with different book slugs"
        )
    return results


def extract_path(
    source: Path,
    root: Path,
    extractor: RecipeExtractor,
    hints: ExtractHints,
    *,
    order: Literal["filename", "exif"] = "filename",
    page_numbers: list[int] | None = None,
) -> list[RecipeDraft]:
    return extract_groups(
        page_groups(source, root, order=order, page_numbers=page_numbers),
        extractor,
        hints,
    )
