"""Validated, file-shaped schemas shared by the pipeline."""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NonNegative = Annotated[float, Field(ge=0)]
Positive = Annotated[float, Field(gt=0)]
Confidence = Annotated[float, Field(ge=0, le=1)]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
RecipeId = Annotated[
    str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)?$")
]
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Nutrient = Literal[
    "kcal",
    "protein_g",
    "fat_g",
    "sat_fat_g",
    "carb_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
]
Origin = Literal["user", "llm", "parser", "catalog", "fdc", "cronometer"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Derived(Model):
    source: Origin | None = None
    confidence: Confidence | None = None
    locked: bool = Field(default=False, strict=True)


class RecipeSource(Model):
    kind: Literal["cookbook", "url", "text", "manual", "epub"] = "manual"
    book: Slug | None = None
    page: Annotated[int, Field(gt=0)] | None = None
    pages: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    url: str | None = None
    images: list[str] = Field(default_factory=list)
    image_pages: dict[str, list[Annotated[int, Field(gt=0)]]] = Field(
        default_factory=dict
    )
    document: str | None = None
    section: str | None = None


class RecipeTime(Model):
    active_min: NonNegative | None = None
    total_min: NonNegative | None = None


class Review(Model):
    status: Literal["ok", "needs_review"] = "ok"
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def flag_status(self) -> Self:
        if self.flags:
            self.status = "needs_review"
        return self


class IngredientLine(Derived):
    raw: str
    qty: NonNegative | None = None
    qty_range: tuple[NonNegative, NonNegative] | None = None
    unit: str | None = None
    item: str
    prep: str | None = None
    optional: bool = False
    group: str | None = None
    ingredient_id: Slug | None = None
    grams: NonNegative | None = None
    match_confidence: Confidence | None = None
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered_range(self) -> Self:
        if self.qty_range and self.qty_range[0] > self.qty_range[1]:
            raise ValueError("quantity range must be ordered")
        return self


class Macros(Model):
    kcal: NonNegative | None = None
    protein_g: NonNegative | None = None
    fat_g: NonNegative | None = None
    sat_fat_g: NonNegative | None = None
    carb_g: NonNegative | None = None
    fiber_g: NonNegative | None = None
    sugar_g: NonNegative | None = None
    sodium_mg: NonNegative | None = None


class Nutrition(Derived):
    per_serving: Macros = Field(default_factory=Macros)
    coverage: Confidence = 0
    computed_at: datetime | None = None


class Recipe(Model):
    id: RecipeId
    title: Annotated[str, Field(min_length=1)]
    source: RecipeSource = Field(default_factory=RecipeSource)
    servings: Positive | None = None
    time: RecipeTime = Field(default_factory=RecipeTime)
    tags: list[str] = Field(default_factory=list)
    protein: str | None = None
    cuisine: str | None = None
    keeps_days: Annotated[int, Field(ge=0)] = 0
    freezes: bool = False
    ingredients: list[IngredientLine] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    nutrition: Nutrition | None = None
    review: Review = Field(default_factory=Review)
    excluded: bool = False
    locked: bool = Field(default=False, strict=True)
    user_fields: list[str] = Field(default_factory=list)
    extraction_reviewed: bool = False

    @field_validator("user_fields")
    @classmethod
    def known_user_fields(cls, values: list[str]) -> list[str]:
        if not set(values) <= cls.model_fields.keys():
            raise ValueError("user_fields must name recipe fields")
        return list(dict.fromkeys(values))


class RecipeDraft(Model):
    title: Annotated[str, Field(min_length=1)]
    source: RecipeSource = Field(default_factory=RecipeSource)
    servings: Positive | None = None
    time: RecipeTime = Field(default_factory=RecipeTime)
    ingredients: list[IngredientLine]
    steps: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    confidence: dict[str, Confidence] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    page_refs: list[str] = Field(default_factory=list)


class BuyInfo(Model):
    unit: str = "g"
    package_g: list[Positive] = Field(default_factory=list)
    section: str = "other"
    price_per_package: NonNegative | None = None


class UnitWeightSource(Model):
    """Snapshot of the edible USDA portion used to confirm a recipe count."""

    source: Literal["fdc"] = "fdc"
    fdc_id: Annotated[int, Field(gt=0)]
    description: str
    portion_id: Annotated[int, Field(gt=0)]
    portion_description: str
    amount: Positive
    portion_grams: Positive
    edible_fraction: Annotated[float, Field(gt=0, le=1)]


class CatalogItem(Derived):
    name: Annotated[str, Field(min_length=1)]
    aliases: list[str] = Field(default_factory=list)
    fdc_id: Annotated[int, Field(gt=0)] | None = None
    fdc_description: str | None = None
    edible_fraction: Confidence | None = None
    density_g_per_ml: Positive | None = None
    unit_weights: dict[str, Positive] = Field(default_factory=dict)
    unit_weight_sources: dict[Slug, UnitWeightSource] = Field(default_factory=dict)
    staple: bool = False
    perishable: bool | None = None
    shelf_life_days: Annotated[int, Field(ge=0)] | None = None
    buy: BuyInfo = Field(default_factory=BuyInfo)
    user_fields: list[str] = Field(default_factory=list)

    @field_validator("user_fields")
    @classmethod
    def known_user_fields(cls, values: list[str]) -> list[str]:
        if not set(values) <= cls.model_fields.keys():
            raise ValueError("user_fields must name catalog fields")
        return list(dict.fromkeys(values))


class PantryItem(Derived):
    ingredient_id: Slug
    status: Literal["have", "low", "out"] | None = None
    qty_g: NonNegative | None = None
    acquired: date | None = None
    expires: date | None = None
    note: str | None = None

    @model_validator(mode="after")
    def inventory(self) -> Self:
        if self.status is None and self.qty_g is None:
            raise ValueError("pantry item requires status or qty_g")
        if self.expires and self.acquired and self.expires < self.acquired:
            raise ValueError("expiry precedes acquisition")
        return self


class Pantry(Model):
    items: list[PantryItem] = Field(default_factory=list)
    applied_purchases: dict[str, NonNegative] = Field(default_factory=dict)


class Eater(Model):
    name: Annotated[str, Field(min_length=1)]
    servings_per_slot: Positive = 1
    daily: dict[Nutrient, tuple[NonNegative | None, NonNegative | None]] | None = None
    fixed_daily: dict[Nutrient, NonNegative] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ranges(self) -> Self:
        for lo, hi in (self.daily or {}).values():
            if lo is not None and hi is not None and lo > hi:
                raise ValueError("target minimum exceeds maximum")
        return self


class Targets(Model):
    planned_slots: list[Slug] = Field(default_factory=lambda: ["dinner"])
    days: list[Day] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    )
    eaters: list[Eater] = Field(default_factory=lambda: [Eater(name="me")])
    macro_mode: Literal["daily", "weekly_average"] = "daily"

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        for values in (self.planned_slots, self.days, [e.name for e in self.eaters]):
            if not values or len(set(values)) != len(values):
                raise ValueError("slots, days and eaters must be nonempty and unique")
        return self


class CookEvent(Model):
    recipe: RecipeId
    day: Day
    slot: Slug
    batches: Positive = 1


class Meal(Model):
    day: Day
    slot: Slug
    recipe: RecipeId
    leftover: bool = False
    cook_day: Day | None = None
    freezer_lot: Slug | None = None
    freezer_portions: dict[Slug, Positive] = Field(default_factory=dict)


class MealLock(Model):
    day: Day
    slot: Slug
    value: RecipeId  # "out" or a recipe id


class PackagePurchase(Model):
    package_g: Positive
    count: Annotated[int, Field(ge=1)]
    unit_price: NonNegative | None = None
    currency: str | None = None
    unit: Slug | None = None


class Plan(Model):
    week: str
    lean: Confidence = 0.3
    cook_events: list[CookEvent] = Field(default_factory=list)
    meals: list[Meal] = Field(default_factory=list)
    locks: list[MealLock] = Field(default_factory=list)
    daily_macros: dict[Day, Macros] = Field(default_factory=dict)
    objective_breakdown: dict[str, float] = Field(default_factory=dict)
    package_counts: dict[Slug, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict
    )
    package_sizes_g: dict[Slug, Positive] = Field(default_factory=dict)
    package_purchases: dict[Slug, list[PackagePurchase]] = Field(default_factory=dict)
    pantry_usage_g: dict[Slug, NonNegative] = Field(default_factory=dict)
    eater_daily_macros: dict[str, dict[Day, Macros]] = Field(default_factory=dict)
    macro_slack: dict[str, NonNegative] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    explanations: dict[str, list[str]] = Field(default_factory=dict)
    solver_status: str | None = None
    solver_gap: NonNegative | None = None
    replan_from: Day | None = None
    pantry_fingerprint: str | None = None
    ingredient_needs_g: dict[Slug, NonNegative] = Field(default_factory=dict)
    recipe_servings: dict[RecipeId, Positive] = Field(default_factory=dict)
    household_servings: Positive | None = None
    freezer_usage: dict[Slug, NonNegative] = Field(default_factory=dict)
    freezer_fingerprint: str | None = None

    @field_validator("week")
    @classmethod
    def iso_week(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"\d{4}-W\d{2}", value):
            raise ValueError("week must be YYYY-Www")
        date.fromisocalendar(int(value[:4]), int(value[-2:]), 1)
        return value

    @model_validator(mode="after")
    def unique_meals(self) -> Self:
        times = [(meal.day, meal.slot) for meal in self.meals]
        if len(set(times)) != len(times):
            raise ValueError("a meal slot may only occur once")
        return self

    @model_validator(mode="after")
    def consistent_packages(self) -> Self:
        for identifier, purchases in self.package_purchases.items():
            sizes = [purchase.package_g for purchase in purchases]
            if len(set(sizes)) != len(sizes):
                raise ValueError(
                    "package purchases must have unique sizes per ingredient"
                )
            if self.package_counts.get(identifier) != sum(p.count for p in purchases):
                raise ValueError("package counts must agree with package purchases")
            legacy_size = self.package_sizes_g.get(identifier)
            if legacy_size is not None and sizes and sizes != [legacy_size]:
                raise ValueError(
                    "a single package size cannot describe a mixed purchase"
                )
        return self


class HistoryEvent(Model):
    date: date
    recipe: RecipeId
    batches: Positive = 1
    rating: Annotated[int, Field(ge=1, le=5)] | None = None
    event_id: str | None = None
    freeze_servings: NonNegative = 0
    freezer_expires: date | None = None


class FreezerLot(Derived):
    id: Slug
    recipe: RecipeId
    servings: NonNegative
    frozen_on: date
    expires: date
    recipe_signature: str
    recipe_servings: Positive

    @model_validator(mode="after")
    def dates(self) -> Self:
        if self.expires < self.frozen_on:
            raise ValueError("freezer expiry precedes freezing")
        return self


class FreezerConsumption(Model):
    lot: Slug
    servings: Positive
    date: date


class Freezer(Model):
    items: list[FreezerLot] = Field(default_factory=list)
    consumed: dict[str, FreezerConsumption] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_lots(self) -> Self:
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("freezer lot IDs must be unique")
        return self


type Catalog = dict[Slug, CatalogItem]
type History = list[HistoryEvent]
