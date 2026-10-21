"""Conservative ingredient parsing; unresolved quantities carry review flags."""

import re
from fractions import Fraction

from bourdain.models import IngredientLine

ALIASES = {
    "g": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "ml": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "cup": "cup",
    "cups": "cup",
    "c": "cup",
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "tbsp": "tbsp",
    "tbsps": "tbsp",
    "tbs": "tbsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "tsp": "tsp",
    "tsps": "tsp",
    "fluid ounce": "fl oz",
    "fluid ounces": "fl oz",
    "fl oz": "fl oz",
    "pint": "pint",
    "pints": "pint",
    "quart": "quart",
    "quarts": "quart",
    "each": "each",
    "clove": "clove",
    "cloves": "clove",
    "can": "can",
    "cans": "can",
    "bunch": "bunch",
    "bunches": "bunch",
    "package": "package",
    "packages": "package",
    "slice": "slice",
    "slices": "slice",
    "sprig": "sprig",
    "sprigs": "sprig",
    "small": "small",
    "medium": "medium",
    "large": "large",
    "pinch": "pinch",
    "pinches": "pinch",
    "head": "head",
    "heads": "head",
    "bulb": "bulb",
    "bulbs": "bulb",
    "stalk": "stalk",
    "stalks": "stalk",
    "bag": "bag",
    "bags": "bag",
    "jar": "jar",
    "jars": "jar",
    "bottle": "bottle",
    "bottles": "bottle",
    "carton": "carton",
    "cartons": "carton",
    "piece": "piece",
    "pieces": "piece",
}
FRACTIONS = {
    "¼": "1/4",
    "½": "1/2",
    "¾": "3/4",
    "⅓": "1/3",
    "⅔": "2/3",
    "⅛": "1/8",
    "⅜": "3/8",
    "⅝": "5/8",
    "⅞": "7/8",
}
NUMBER = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?|\.\d+)"
QUANTITY = re.compile(
    rf"^({NUMBER})(?:\s*(?:[-–—]|to)\s*({NUMBER}))?(?=\s|[a-zA-Z(]|$)"
)
UNITS = re.compile(
    r"^("
    + "|".join(re.escape(k) for k in sorted(ALIASES, key=len, reverse=True))
    + r")\.?\s+(.*)$",
    re.IGNORECASE,
)
MASS_NOTE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*[- ]?\s*(kg|g|grams?|oz|ounces?)$", re.IGNORECASE
)


def _number(value: str) -> float:
    return sum(float(Fraction(part)) for part in value.split())


def _parentheses(text: str) -> tuple[str, list[str]]:
    """Keep nested preparation notes together and outside the ingredient name."""
    outside: list[str] = []
    notes: list[str] = []
    depth, start = 0, 0
    for index, character in enumerate(text):
        if character == "(":
            if depth == 0:
                start = index + 1
            depth += 1
        elif character == ")" and depth:
            depth -= 1
            if depth == 0:
                notes.append(text[start:index])
                outside.append(" ")
        elif depth == 0:
            outside.append(character)
    if depth:
        # An incomplete note is kept verbatim; never silently discard text.
        outside.append("(" + text[start:])
    return "".join(outside), notes


def parse_line(raw: str, *, group: str | None = None) -> IngredientLine:
    text = raw.strip().lstrip("•").strip()
    for character, fraction in FRACTIONS.items():
        text = text.replace(character, " " + fraction + " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ValueError("ingredient line is empty")
    flags: list[str] = []
    optional = bool(re.search(r"\boptional\b", text, re.IGNORECASE))
    text = re.sub(r"\boptional\b", "", text, flags=re.IGNORECASE).strip(" ,")
    text, notes = _parentheses(text)
    metric_g = None
    prep_notes = []
    for note in notes:
        for part in note.split(","):
            part = part.strip()
            mass = MASS_NOTE.fullmatch(part)
            if mass:
                factor = (
                    1000
                    if mass[2].lower() == "kg"
                    else 28.349523125
                    if mass[2].lower().startswith(("oz", "ounce"))
                    else 1
                )
                metric_g = float(mass[1]) * factor
            elif part and not re.fullmatch(r"\$\s*[\d.]+", part):
                prep_notes.append(part)
    text = re.sub(r"\s+", " ", text).strip(" ,")
    extras = re.split(
        r"\s*,?\s*\b(plus more for\b.*)", text, maxsplit=1, flags=re.IGNORECASE
    )
    prep_parts = extras[1:2] + prep_notes
    text = extras[0]
    to_taste = bool(
        re.search(
            r"\b(to taste|as needed|for serving)\b",
            text + " " + " ".join(prep_notes),
            re.IGNORECASE,
        )
    )
    text = re.sub(
        r"\s*,?\s*\b(to taste|as needed|for serving)\b", "", text, flags=re.IGNORECASE
    )
    qty = None
    qty_range = None
    match = QUANTITY.match(text)
    if match:
        try:
            first = _number(match[1])
            if match[2]:
                second = _number(match[2])
                if first > second:
                    flags.append("invalid_quantity_range")
                else:
                    qty_range = (first, second)
                    flags.append("quantity_range")
            else:
                qty = first
        except (ValueError, ZeroDivisionError):
            flags.append("invalid_quantity")
        text = text[match.end() :].strip()
    unit = None
    match_unit = UNITS.match(text)
    if match_unit:
        unit, text = ALIASES[match_unit[1].lower()], match_unit[2]
    elif qty is not None or qty_range:
        unit = "each"
    # Comma suffixes describe preparation, except identity qualifiers such as bone-in.
    parts = [p.strip() for p in text.split(",")]
    item_parts = [parts[0]]
    for part in parts[1:]:
        if re.search(r"\b(bone-in|boneless|skin-on|skinless)\b", part):
            item_parts.append(part)
        elif part:
            prep_parts.append(part)
    item = ", ".join(item_parts).strip(" ,")
    if not item:
        raise ValueError("ingredient name is missing")
    grams = metric_g
    if grams is not None and unit in {"can", "package"}:
        grams = grams * qty if qty is not None else None
    if to_taste and qty is None and qty_range is None and metric_g is None:
        grams = 0
    if qty is None and not qty_range and grams is None and not flags:
        flags.append("missing_quantity")
    return IngredientLine(
        raw=raw,
        qty=qty,
        qty_range=qty_range,
        unit=unit,
        item=item,
        prep=", ".join(prep_parts) or None,
        optional=optional,
        group=group,
        grams=grams,
        source="parser",
        confidence=1 if not flags else 0.5,
        flags=flags,
    )
