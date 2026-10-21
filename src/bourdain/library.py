"""Orchestrate draft normalization and persistence without external API calls."""

from bourdain.errors import BourdainError
from bourdain.models import Recipe, RecipeDraft, Review
from bourdain.normalize.catalog import normalize_line, slugify
from bourdain.normalize.matcher import CatalogMatcher
from bourdain.store import Repository


def import_draft(
    repo: Repository,
    draft: RecipeDraft,
    *,
    book: str | None = None,
    match_threshold: float = 85,
    matcher: CatalogMatcher | None = None,
) -> Recipe:
    namespace = (
        book or draft.source.book or ("web" if draft.source.kind == "url" else "text")
    )
    recipe_id = f"{slugify(namespace)}/{slugify(draft.title)}"
    existing = None
    if repo.recipe_path(recipe_id).exists():
        existing = repo.recipe(recipe_id).model
        if existing.source.url != draft.source.url:
            raise BourdainError(f"Another recipe already uses {recipe_id}")
        if (
            existing.source.document != draft.source.document
            or existing.source.section != draft.source.section
        ):
            raise BourdainError(f"Different source sections already use {recipe_id}")
        if (
            existing.source.images
            and draft.source.images
            and not set(existing.source.images) & set(draft.source.images)
        ):
            raise BourdainError(f"Different cookbook pages already use {recipe_id}")
        if existing.source.image_pages and draft.source.image_pages:
            previous = {
                (image, page)
                for image, pages in existing.source.image_pages.items()
                for page in pages
            }
            incoming = {
                (image, page)
                for image, pages in draft.source.image_pages.items()
                for page in pages
            }
            if not previous & incoming:
                raise BourdainError(f"Different cookbook pages already use {recipe_id}")
        if existing.locked:
            return existing
    catalog_doc = repo.catalog()
    # The store preserves protected lists as a whole. Compute flags from that same
    # list, so an automated re-import cannot resurrect resolved ingredient flags.
    if existing and any(
        line.locked or line.source == "user" for line in existing.ingredients
    ):
        ingredients = existing.ingredients
    else:
        ingredients = [
            normalize_line(
                line, catalog_doc.model, threshold=match_threshold, matcher=matcher
            )
            for line in draft.ingredients
        ]
    flags = list(
        dict.fromkeys(
            ([] if existing and existing.extraction_reviewed else draft.flags)
            + [flag for line in ingredients for flag in line.flags]
            + (
                [
                    flag
                    for flag in existing.review.flags
                    if flag.startswith("nutrition:")
                ]
                if existing
                else []
            )
        )
    )
    if (
        draft.servings is None
        and "missing_servings" not in flags
        and not (existing and "servings" in existing.user_fields)
    ):
        flags.append("missing_servings")
    source = draft.source.model_copy(deep=True)
    if book:
        source.book = slugify(book)
    recipe = Recipe(
        id=recipe_id,
        title=draft.title,
        source=source,
        servings=draft.servings,
        time=draft.time,
        ingredients=ingredients,
        steps=draft.steps,
        tags=draft.tags,
        review=Review(flags=flags),
    )
    if existing is not None:
        updated = existing.model_copy(deep=True)
        for name in (
            "title",
            "source",
            "servings",
            "time",
            "ingredients",
            "steps",
            "review",
        ):
            setattr(updated, name, getattr(recipe, name))
        updated.tags = list(dict.fromkeys(existing.tags + recipe.tags))
        recipe = updated
    catalog_doc.save()
    return repo.save_recipe(recipe)
