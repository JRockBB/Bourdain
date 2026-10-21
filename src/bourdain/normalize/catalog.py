"""Exact and fuzzy catalog matching, with conservative fallback proposals."""

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz.fuzz import token_set_ratio

from bourdain.errors import BourdainError
from bourdain.models import Catalog, CatalogItem, IngredientLine
from bourdain.normalize.matcher import CatalogMatcher
from bourdain.normalize.units import to_grams


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise BourdainError("A name must contain at least one letter or number")
    return slug


def key(value: str) -> str:
    return " ".join(value.casefold().split())


def unreviewed(item: CatalogItem) -> bool:
    return item.source == "llm" or (item.source == "parser" and item.confidence == 0)


@dataclass(frozen=True)
class Candidate:
    ingredient_id: str
    score: float


def candidates(item: str, catalog: Catalog, *, limit: int = 5) -> list[Candidate]:
    ranked = [
        Candidate(
            identifier,
            max(
                token_set_ratio(key(item), key(name))
                for name in [entry.name, *entry.aliases]
            ),
        )
        for identifier, entry in catalog.items()
    ]
    return sorted(ranked, key=lambda match: (-match.score, match.ingredient_id))[:limit]


def confirm_alias(catalog: Catalog, identifier: str, alias: str) -> None:
    """Explicit user confirmation only; an inferred match never teaches itself."""
    if identifier not in catalog:
        raise BourdainError(f"Unknown catalog ingredient: {identifier}")
    if not key(alias):
        raise BourdainError("An ingredient alias cannot be blank")
    for other_id, entry in catalog.items():
        if (
            other_id != identifier
            and not unreviewed(entry)
            and key(alias)
            in {key(entry.name), *(key(value) for value in entry.aliases)}
        ):
            raise BourdainError(
                f"Alias already belongs to {other_id}; edit the catalog first"
            )
    entry = catalog[identifier]
    if key(alias) not in {key(value) for value in entry.aliases}:
        entry.aliases.append(alias)


def normalize_line(
    line: IngredientLine,
    catalog: Catalog,
    *,
    threshold: float = 85,
    matcher: CatalogMatcher | None = None,
) -> IngredientLine:
    if not 0 <= threshold <= 100:
        raise ValueError("Match threshold must be between 0 and 100")
    if line.locked or line.source == "user":
        return line.model_copy(deep=True)
    result = line.model_copy(deep=True)
    matches = [
        identifier
        for identifier, item in catalog.items()
        if key(line.item) in {key(item.name), *(key(alias) for alias in item.aliases)}
    ]
    confirmed = [
        identifier for identifier in matches if not unreviewed(catalog[identifier])
    ]
    matches = confirmed or matches
    if len(matches) > 1:
        result.flags.append("ambiguous_catalog_match")
        result.match_confidence = 0
        return to_grams(result)
    if matches:
        identifier = matches[0]
        result.match_confidence = 1
    else:
        ranked = candidates(line.item, catalog)
        if ranked and ranked[0].score >= threshold:
            if len(ranked) > 1 and ranked[0].score - ranked[1].score < 3:
                result.flags.append("ambiguous_catalog_match")
                result.ingredient_id, result.match_confidence = None, 0
                return to_grams(result)
            identifier = ranked[0].ingredient_id
            result.match_confidence = ranked[0].score / 100
            # A token subset can hide important modifiers (e.g. salted/unsalted).
            result.flags.append(f"fuzzy_match:{identifier}")
            result.ingredient_id = identifier
            return _convert(result, catalog[identifier])
        if matcher is not None:
            options = {
                entry.ingredient_id: catalog[entry.ingredient_id] for entry in ranked
            }
            proposal = matcher.match(line.item, options)
            if proposal.ingredient_id:
                identifier = proposal.ingredient_id
                if identifier not in options:
                    raise BourdainError(
                        "Matcher selected an ingredient outside its candidates"
                    )
            else:
                assert proposal.new_item is not None
                identifier = _new_id(proposal.new_item.name, catalog)
                entry = proposal.new_item.model_copy(deep=True)
                entry.source, entry.locked, entry.confidence = (
                    "llm",
                    False,
                    proposal.confidence,
                )
                # Nutrition mapping is a separate stage, not a model guess.
                entry.fdc_id, entry.fdc_description = None, None
                entry.edible_fraction = None
                # Only explicit user actions may protect catalog fields.
                entry.user_fields = []
                entry.unit_weight_sources = {}
                if key(line.item) not in {key(alias) for alias in entry.aliases}:
                    entry.aliases.append(line.item)
                catalog[identifier] = entry
            result.source, result.confidence = "llm", proposal.confidence
            result.ingredient_id, result.match_confidence = (
                identifier,
                proposal.confidence,
            )
            result.flags.append(f"llm_match:{identifier}")
            return _convert(result, catalog[identifier])
        base = slugify(line.item)
        identifier = _new_id(base, catalog)
        catalog[identifier] = CatalogItem(
            name=line.item, aliases=[line.item], source="parser", confidence=0
        )
        result.match_confidence = 0
    result.ingredient_id = identifier
    return _convert(result, catalog[identifier])


def _new_id(name: str, catalog: Catalog) -> str:
    base = slugify(name)
    identifier, index = base, 2
    while identifier in catalog:
        identifier = f"{base}-{index}"
        index += 1
    return identifier


def _convert(result: IngredientLine, entry: CatalogItem) -> IngredientLine:
    if unreviewed(entry):
        result.match_confidence = 0
        result.flags.append(f"unreviewed_catalog:{result.ingredient_id}")
    return to_grams(result, entry)
