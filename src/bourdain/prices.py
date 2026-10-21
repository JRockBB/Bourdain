"""Package prices and visible per-section fallbacks for optimization."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from bourdain.errors import BourdainError
from bourdain.models import CatalogItem
from bourdain.models.schemas import Derived, NonNegative, Positive, Slug
from bourdain.normalize.units import shopping_unit
from bourdain.store import Repository
from bourdain.store.yaml import YamlDocument, load_yaml

DEFAULT_PER_KG = {
    "produce": 4.0,
    "meat": 12.0,
    "dairy": 8.0,
    "bakery": 7.0,
    "pantry": 5.0,
    "frozen": 6.0,
    "other": 10.0,
}


class PackagePrice(Derived):
    package_g: Positive
    price: NonNegative
    currency: Annotated[str, Field(min_length=1)] = "USD"
    receipt_ref: str | None = None


type Prices = dict[Slug, list[PackagePrice]]


@dataclass(frozen=True)
class PriceQuote:
    package_g: float
    price: float
    warning: str | None = None


def price_document(repo: Repository) -> YamlDocument[Prices]:
    return load_yaml(repo.path("prices.yaml"), Prices)


def set_price(
    repo: Repository,
    identifier: str,
    package_g: float,
    price: float,
    *,
    currency: str = "USD",
) -> None:
    if identifier not in repo.catalog().model:
        raise BourdainError(f"Unknown catalog ingredient: {identifier}")
    entry = PackagePrice(
        package_g=package_g, price=price, currency=currency, source="user"
    )
    document = price_document(repo)
    entries = [
        item
        for item in document.model.get(identifier, [])
        if item.package_g != package_g
    ]
    document.model[identifier] = sorted(
        entries + [entry], key=lambda item: item.package_g
    )
    document.save(automated=False)


def package_options(
    identifier: str,
    item: CatalogItem,
    prices: Prices,
    *,
    per_kg: dict[str, float] | None = None,
    currency: str = "USD",
) -> list[PriceQuote]:
    """Return every maintained price, or estimate all catalog package sizes."""
    entries = prices.get(identifier, [])
    if any(entry.currency != currency for entry in entries):
        raise BourdainError(
            f"Price currency mismatch for {identifier}; expected {currency}"
        )
    if entries:
        by_size: dict[float, float] = {}
        for entry in entries:
            by_size[entry.package_g] = min(
                entry.price, by_size.get(entry.package_g, entry.price)
            )
        return [PriceQuote(size, price) for size, price in sorted(by_size.items())]
    count_unit = shopping_unit(item)
    sizes = sorted(set(item.buy.package_g or ([count_unit[1]] if count_unit else [])))
    if len(sizes) == 1 and item.buy.price_per_package is not None:
        return [PriceQuote(sizes[0], item.buy.price_per_package)]
    defaults = DEFAULT_PER_KG | (per_kg or {})
    rate = defaults.get(item.buy.section, defaults["other"])
    if rate <= 0:
        raise BourdainError("Fallback prices per kg must be positive")
    # Without a package size, model a bulk purchase in single-gram increments.
    warning = (
        f"{identifier}: estimated price {rate:g} {currency}/kg for {item.buy.section}"
    )
    if not sizes:
        warning += "; no package size, buying by the gram"
    return [PriceQuote(size, rate * size / 1000, warning) for size in sizes or [1.0]]


def quote(
    identifier: str,
    item: CatalogItem,
    prices: Prices,
    *,
    per_kg: dict[str, float] | None = None,
    currency: str = "USD",
) -> PriceQuote:
    """Return the cheapest unit-price quote for callers needing a single estimate."""
    return min(
        package_options(identifier, item, prices, per_kg=per_kg, currency=currency),
        key=lambda entry: (entry.price / entry.package_g, entry.price, entry.package_g),
    )
