"""TOML configuration; credentials are read from the environment only."""

import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from bourdain.errors import BourdainError

SECTION_ORDER = ["produce", "meat", "dairy", "bakery", "pantry", "frozen", "other"]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PathsConfig(ConfigModel):
    data: Path = Field(default_factory=lambda: Path.home() / "kitchen")


class LLMConfig(ConfigModel):
    provider: Literal["anthropic"] = "anthropic"
    model: str = "claude-sonnet-5-5"
    text_model: str = "claude-haiku-5-5"


class IngestConfig(ConfigModel):
    store_steps: bool = False
    match_threshold: Annotated[float, Field(ge=0, le=100)] = 85


class GroceryConfig(ConfigModel):
    section_order: list[str] = Field(default_factory=lambda: SECTION_ORDER.copy())


class PlanConfig(ConfigModel):
    lean: Annotated[float, Field(ge=0, le=1)] = 0.3
    time_limit_s: Annotated[float, Field(gt=0)] = 20
    candidate_limit: Annotated[int, Field(ge=1, le=500)] = 80
    max_meals_per_recipe: Annotated[int, Field(ge=1)] | None = None
    max_per_protein: dict[str, Annotated[int, Field(ge=1)]] = Field(
        default_factory=dict
    )
    allow_repeat_cook: bool = False
    max_batches_per_cook: Annotated[int, Field(ge=1, le=20)] = 4
    max_cooks: Annotated[int, Field(ge=1)] | None = None
    weekday_active_minutes: Annotated[float, Field(gt=0)] | None = None
    weights: dict[str, Annotated[float, Field(ge=0)]] = Field(default_factory=dict)


class PricesConfig(ConfigModel):
    currency: str = "USD"
    section_per_kg: dict[str, Annotated[float, Field(gt=0)]] = Field(
        default_factory=dict
    )


class Settings(ConfigModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    fdc: ConfigModel = Field(default_factory=ConfigModel)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    books: dict[str, str] = Field(default_factory=dict)
    grocery: GroceryConfig = Field(default_factory=GroceryConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    prices: PricesConfig = Field(default_factory=PricesConfig)

    @property
    def anthropic_api_key(self) -> SecretStr | None:
        value = os.environ.get("ANTHROPIC_API_KEY")
        return SecretStr(value) if value else None

    @property
    def fdc_api_key(self) -> SecretStr | None:
        value = os.environ.get("FDC_API_KEY")
        return SecretStr(value) if value else None


def data_path(data: str | Path | None = None) -> Path:
    """Select a root without creating it: CLI > environment > home default."""
    return (
        Path(data or os.environ.get("BOURDAIN_DATA") or Path.home() / "kitchen")
        .expanduser()
        .resolve()
    )


def load_settings(data: str | Path | None = None) -> Settings:
    root = data_path(data)
    path = root / "bourdain.toml"
    try:
        content = (
            tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        # TOML errors can contain the source text, including misplaced credentials.
        raise BourdainError(f"Cannot read {path}: invalid or unreadable TOML") from exc
    try:
        settings = Settings.model_validate(content)
    except ValidationError as exc:
        issue = exc.errors(include_input=False)[0]
        field = ".".join(str(part) for part in issue["loc"])
        raise BourdainError(
            f"Invalid configuration at {field}: {issue['type']}"
        ) from exc
    # The selected folder is authoritative; a stale paths.data never redirects writes.
    settings.paths.data = root
    return settings
