"""FoodData Central search/details with an injectable, cached HTTP boundary."""

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bourdain.errors import BourdainError
from bourdain.models import Macros
from bourdain.store.repository import safe_path
from bourdain.store.yaml import atomic_write

DATA_TYPES = {"Foundation": 0, "SR Legacy": 1, "Survey (FNDDS)": 2, "Branded": 3}
NUTRIENTS = {
    "protein_g": (1003,),
    "fat_g": (1004,),
    "sat_fat_g": (1258,),
    "carb_g": (1005,),
    "fiber_g": (1079,),
    "sugar_g": (2000, 1063),
    "sodium_mg": (1093,),
    "kcal": (2048, 1008, 2047),
}


class FoodPortion(BaseModel):
    """A USDA measure, retained verbatim until a user selects its count meaning."""

    model_config = ConfigDict(
        extra="ignore", allow_inf_nan=False, populate_by_name=True
    )
    id: int | None = None
    amount: float | None = None
    gram_weight: float | None = Field(default=None, alias="gramWeight")
    modifier: str | None = None
    portion_description: str | None = Field(default=None, alias="portionDescription")
    measure_unit: dict[str, Any] | None = Field(default=None, alias="measureUnit")

    @property
    def label(self) -> str:
        parts = [self.portion_description, self.modifier]
        if self.measure_unit:
            parts.append(self.measure_unit.get("name"))
        return " / ".join(dict.fromkeys(str(part) for part in parts if part))


class Food(BaseModel):
    model_config = ConfigDict(
        extra="ignore", allow_inf_nan=False, populate_by_name=True
    )
    fdc_id: int = Field(alias="fdcId", gt=0)
    description: str
    data_type: str = Field(default="", alias="dataType")
    food_nutrients: list[dict[str, Any]] = Field(
        default_factory=list, alias="foodNutrients"
    )
    # None also identifies older caches which discarded this part of the response.
    food_portions: list[FoodPortion] | None = Field(default=None, alias="foodPortions")

    def macros(self) -> Macros:
        """USDA foodNutrients amounts are per 100 g, not label serving values."""
        values: dict[int, tuple[float, str]] = {}
        for row in self.food_nutrients:
            nutrient = row.get("nutrient", {})
            identifier = nutrient.get("id", row.get("nutrientId"))
            amount = row.get("amount", row.get("value"))
            unit = str(nutrient.get("unitName", row.get("unitName", ""))).casefold()
            if identifier is not None and amount is not None:
                try:
                    values[int(identifier)] = (float(amount), unit)
                except (TypeError, ValueError):
                    raise BourdainError(
                        "Invalid nutrient amount in USDA response"
                    ) from None
        macros = {}
        for name, identifiers in NUTRIENTS.items():
            for identifier in identifiers:
                if identifier not in values:
                    continue
                amount, unit = values[identifier]
                expected = (
                    "kcal" if name == "kcal" else "mg" if name == "sodium_mg" else "g"
                )
                if unit == expected:
                    macros[name] = amount
                elif unit in {"g", "mg"} and expected in {"g", "mg"}:
                    macros[name] = amount * (1000 if expected == "mg" else 0.001)
                else:
                    continue
                break
        try:
            return Macros.model_validate(macros)
        except ValidationError:
            raise BourdainError("Invalid nutrient values in USDA response") from None


class FoodProvider(Protocol):
    def search(self, query: str) -> list[Food]: ...
    def food(self, fdc_id: int, *, refresh: bool = False) -> Food: ...


def http_get(endpoint: str, params: dict[str, str | int], api_key: str) -> Any:
    # Keep the key out of URLs, cache keys and exception messages.
    url = f"https://api.nal.usda.gov/fdc/v1/{endpoint}?{urlencode(params)}"
    request = Request(url, headers={"X-Api-Key": api_key, "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        content = response.read(10_000_001)
    if len(content) > 10_000_000:
        raise BourdainError("USDA response exceeds 10 MB")
    return json.loads(content)


class FDCClient:
    def __init__(
        self,
        root: Path,
        *,
        fetch: Callable[..., Any] = http_get,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root, self.fetch, self.sleep = root, fetch, sleep

    def _request(self, endpoint: str, params: dict[str, str | int]) -> Any:
        api_key = os.environ.get("FDC_API_KEY")
        if not api_key:
            raise BourdainError("Set FDC_API_KEY to fetch USDA nutrition data")
        for attempt in range(3):
            try:
                return self.fetch(endpoint, params, api_key)
            except HTTPError as exc:
                retryable = exc.code in {408, 429} or exc.code >= 500
                if not retryable or attempt == 2:
                    raise BourdainError(
                        f"USDA request failed (HTTP {exc.code}); "
                        "retry later or check the key"
                    ) from None
                try:
                    delay = float(exc.headers.get("Retry-After", 2**attempt))
                except (ValueError, TypeError):
                    delay = 2**attempt
                self.sleep(max(0, min(delay, 30)))
            except (URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise BourdainError("USDA connection failed; retry later") from None
                self.sleep(2**attempt)
            except (json.JSONDecodeError, UnicodeError):
                raise BourdainError("USDA returned invalid JSON") from None
        raise BourdainError("USDA request did not return data")

    def food(self, fdc_id: int, *, refresh: bool = False) -> Food:
        if not isinstance(fdc_id, int) or fdc_id <= 0:
            raise BourdainError("FDC ID must be a positive integer")
        path = safe_path(self.root, f".cache/fdc/{fdc_id}.json")
        data = (
            self._read(path)
            if path.exists() and not refresh
            else self._request(f"food/{fdc_id}", {})
        )
        result = self._food(data)
        if result.fdc_id != fdc_id:
            raise BourdainError("USDA food ID does not match the requested ID")
        result.macros()
        if refresh or not path.exists():
            atomic_write(path, result.model_dump_json(by_alias=True, indent=2).encode())
        return result

    def search(self, query: str) -> list[Food]:
        query = " ".join(query.split())
        if not query:
            raise BourdainError("USDA search query cannot be blank")
        digest = hashlib.sha256(query.casefold().encode()).hexdigest()
        path = safe_path(self.root, f".cache/fdc/search/{digest}.json")
        data = (
            self._read(path)
            if path.exists()
            else self._request("foods/search", {"query": query, "pageSize": 50})
        )
        if not isinstance(data, dict) or not isinstance(data.get("foods"), list):
            raise BourdainError("Invalid USDA search response")
        foods = [self._food(row) for row in data["foods"]]
        if not path.exists():
            atomic_write(
                path,
                json.dumps(
                    {"foods": [food.model_dump(by_alias=True) for food in foods]}
                ).encode(),
            )
        return sorted(
            foods, key=lambda food: (DATA_TYPES.get(food.data_type, 4), food.fdc_id)
        )

    @staticmethod
    def _food(data: Any) -> Food:
        try:
            return Food.model_validate(data)
        except ValidationError:
            raise BourdainError("Invalid USDA food response") from None

    @staticmethod
    def _read(path: Path) -> Any:
        try:
            return json.loads(path.read_bytes())
        except (ValueError, OSError):
            raise BourdainError(
                f"Invalid USDA cache: {path}; remove it to retry"
            ) from None


class FakeFDC:
    def __init__(
        self, foods: list[Food], searches: dict[str, list[int]] | None = None
    ) -> None:
        self.foods = {food.fdc_id: food for food in foods}
        self.searches = searches

    def search(self, query: str) -> list[Food]:
        ids = self.searches.get(query, []) if self.searches is not None else self.foods
        return [self.foods[identifier].model_copy(deep=True) for identifier in ids]

    def food(self, fdc_id: int, *, refresh: bool = False) -> Food:
        if fdc_id not in self.foods:
            raise BourdainError(f"Missing fixture food: {fdc_id}")
        return self.foods[fdc_id].model_copy(deep=True)
