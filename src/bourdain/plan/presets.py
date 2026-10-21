"""The lean dial interpolates preferences; macro priority stays constant."""

from dataclasses import dataclass

from bourdain.errors import BourdainError

THRIFTY = {
    "cost": 10.0,
    "waste": 4.0,
    "pantry": 8.0,
    "macro": 10000.0,
    "repeat": 0.0,
    "novel": 0.0,
    "distinct": 0.0,
    "cooks": 5.0,
    "rating": 0.0,
}
VARIETY = {
    "cost": 1.0,
    "waste": 0.5,
    "pantry": 1.0,
    "macro": 10000.0,
    "repeat": 6.0,
    "novel": 3.0,
    "distinct": 8.0,
    "cooks": 0.5,
    "rating": 2.0,
}


@dataclass(frozen=True)
class Preferences:
    weights: dict[str, float]
    max_meals_per_recipe: int


def preferences(
    lean: float,
    *,
    overrides: dict[str, float] | None = None,
    max_meals: int | None = None,
) -> Preferences:
    if not 0 <= lean <= 1:
        raise BourdainError("Lean must be between 0 and 1")
    weights = {
        name: (1 - lean) * THRIFTY[name] + lean * VARIETY[name] for name in THRIFTY
    }
    for name, value in (overrides or {}).items():
        if name not in weights or value < 0:
            raise BourdainError(f"Unknown or negative planning weight: {name}")
        if name == "macro" and value != THRIFTY["macro"]:
            raise BourdainError("Macro priority is fixed; adjust targets instead")
        weights[name] = value
    return Preferences(weights, max_meals or round(6 - 4 * lean))
