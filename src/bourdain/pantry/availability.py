"""Stable inventory identity used to detect stale optimized shopping decisions."""

import hashlib
import json

from bourdain.models import Pantry


def fingerprint(pantry: Pantry, identifiers: set[str]) -> str:
    rows = [
        item.model_dump(
            mode="json",
            include={"ingredient_id", "status", "qty_g", "expires", "acquired"},
        )
        for item in pantry.items
        if item.ingredient_id in identifiers
    ]
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
