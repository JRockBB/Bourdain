"""Read-only diagnostics: no Repository, recovery, sidecars, API calls or solves."""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from bourdain.book_index import BookIndex
from bourdain.config import load_settings
from bourdain.cronometer.bridge import ExportHistory
from bourdain.errors import BourdainError
from bourdain.freezer import recipe_signature
from bourdain.grocery.aggregate import groceries
from bourdain.grocery.todoist import PushBatch
from bourdain.models import (
    Catalog,
    Freezer,
    History,
    Nutrition,
    Pantry,
    Plan,
    Recipe,
    Targets,
)
from bourdain.models.schemas import Model
from bourdain.normalize.units import unit_weight
from bourdain.nutrition.service import Mappings
from bourdain.prices import Prices
from bourdain.receipts import Receipt
from bourdain.store.inventory import inventory, portable_name
from bourdain.store.yaml import load_yaml

CORE = {
    "catalog/ingredients.yaml": Catalog,
    "pantry.yaml": Pantry,
    "prices.yaml": Prices,
    "targets.yaml": Targets,
    "history.yaml": History,
}
OPTIONAL = {
    "freezer.yaml": Freezer,
    "catalog/fdc.yaml": Mappings,
    "cronometer_exports.yaml": ExportHistory,
}


class Finding(Model):
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str


class HealthReport(Model):
    findings: list[Finding] = Field(default_factory=list)
    checked_files: int = 0
    credentials: dict[str, bool] = Field(default_factory=dict)
    solver_available: bool = False

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)


def solver_available() -> bool:
    try:
        import pulp

        return bool(pulp.HiGHS(msg=False).available())
    except (ImportError, OSError):
        return False


def diagnose(root: Path) -> HealthReport:
    root = root.expanduser().resolve()
    report = HealthReport(
        credentials={
            key: bool(os.environ.get(key))
            for key in ("ANTHROPIC_API_KEY", "FDC_API_KEY", "TODOIST_API_TOKEN")
        },
        solver_available=solver_available(),
    )

    def add(code: str, path: str, message: str, *, warning: bool = False) -> None:
        report.findings.append(
            Finding(
                severity="warning" if warning else "error",
                code=code,
                path=path,
                message=message,
            )
        )

    if not root.is_dir():
        add("missing_library", ".", "Data folder does not exist; run init first")
        return report
    files = set()
    for name, problem in inventory(root):
        if problem:
            add("unsafe_path", name, problem)
        else:
            files.add(name)
    if ".transaction.json" in files:
        add(
            "pending_transaction",
            ".transaction.json",
            "Interrupted transaction needs recovery; no recovery was attempted",
        )
    if "bourdain.toml" in files or not (root / "bourdain.toml").exists():
        try:
            load_settings(root)
        except BourdainError:
            add(
                "invalid_config", "bourdain.toml", "Invalid or unreadable configuration"
            )
    if not report.solver_available:
        add(
            "solver_unavailable",
            ".",
            "HiGHS is unavailable; install project dependencies",
        )

    documents: dict[str, Any] = {}
    for name in sorted(files | CORE.keys()):
        schema = CORE.get(name, OPTIONAL.get(name))
        for prefix, candidate in (
            ("recipes/", Recipe),
            ("plans/", Plan),
            ("indexes/", BookIndex),
            ("receipts/", Receipt),
            ("nutrition/fdc/", Nutrition),
            ("exports/todoist/", PushBatch),
        ):
            if name.startswith(prefix) and name.endswith(".yaml"):
                schema = candidate
                break
        if schema is None:
            continue
        if name not in files:
            add("missing_file", name, "Required library file is missing or unsafe")
            continue
        report.checked_files += 1
        try:
            documents[name] = load_yaml(root / name, schema).model
        except BourdainError:
            add("invalid_data", name, "Cannot load file: invalid schema or YAML")

    catalog = documents.get("catalog/ingredients.yaml")
    recipes: dict[str, Recipe] = {}

    def reference(identifier: str, collection: Any, path: str, kind: str) -> None:
        if collection is not None and identifier not in collection:
            add("broken_reference", path, f"Unknown {kind}: {identifier}")

    def source(ref: str, path: str) -> None:
        name = ref.split("#", 1)[0]
        if not portable_name(name) or name not in files:
            add("missing_source", path, f"Missing or unsafe archived source: {name}")

    for name, model in documents.items():
        if not isinstance(model, Recipe):
            continue
        identifier = name.removeprefix("recipes/").removesuffix(".yaml")
        if model.id != identifier:
            add("identity_mismatch", name, "Recipe ID does not match its filename")
        recipes[identifier] = model
        for line in model.ingredients:
            if line.ingredient_id:
                reference(line.ingredient_id, catalog, name, "ingredient")
        for ref in set(model.source.images) | set(model.source.image_pages):
            source(ref, name)
        if model.source.document:
            source(model.source.document, name)
    for identifier, item in (catalog or {}).items():
        for unit in item.unit_weight_sources:
            if unit_weight(item, unit) is None:
                add(
                    "stale_conversion",
                    "catalog/ingredients.yaml",
                    f"Reconfirm {identifier}: {unit} portion conversion",
                    warning=True,
                )

    pantry = documents.get("pantry.yaml")
    for stock in pantry.items if pantry else []:
        reference(stock.ingredient_id, catalog, "pantry.yaml", "ingredient")
    for event in documents.get("history.yaml", []):
        reference(event.recipe, recipes, "history.yaml", "recipe")
    for name in ("catalog/fdc.yaml", "prices.yaml"):
        for identifier in documents.get(name, {}):
            reference(identifier, catalog, name, "ingredient")
    for identifier in documents.get("cronometer_exports.yaml", {}):
        reference(identifier, recipes, "cronometer_exports.yaml", "recipe")
    freezer = documents.get("freezer.yaml", Freezer())
    for lot in freezer.items:
        reference(lot.recipe, recipes, "freezer.yaml", "recipe")
        if lot.recipe in recipes and lot.recipe_signature != recipe_signature(
            recipes[lot.recipe]
        ):
            add(
                "stale_freezer",
                "freezer.yaml",
                f"Recipe changed for lot {lot.id}",
                warning=True,
            )
    for record in freezer.consumed.values():
        reference(
            record.lot, {lot.id for lot in freezer.items}, "freezer.yaml", "freezer lot"
        )

    for name, model in documents.items():
        if isinstance(model, Plan):
            if name != f"plans/{model.week}.yaml":
                add("identity_mismatch", name, "Plan week does not match its filename")
            for identifier in (
                {row.recipe for row in [*model.meals, *model.cook_events]}
                | {lock.value for lock in model.locks}
                | model.recipe_servings.keys()
            ):
                if identifier != "out":
                    reference(identifier, recipes, name, "recipe")
            for identifier in (
                model.package_counts.keys()
                | model.ingredient_needs_g.keys()
                | model.pantry_usage_g.keys()
            ):
                reference(identifier, catalog, name, "ingredient")
            for identifier in model.freezer_usage:
                reference(
                    identifier, {lot.id for lot in freezer.items}, name, "freezer lot"
                )
            if catalog is not None and pantry is not None:
                try:
                    groceries(model, recipes, catalog, pantry, freezer=freezer)
                except BourdainError:
                    add(
                        "unusable_plan",
                        name,
                        "Plan cannot produce a current list; check recipe quantities "
                        "and pantry/freezer changes, then replan",
                        warning=True,
                    )
        elif isinstance(model, BookIndex):
            if name != f"indexes/{model.book}.yaml":
                add("identity_mismatch", name, "Index book does not match its filename")
            for entry in model.entries.values():
                if entry.recipe_id:
                    reference(entry.recipe_id, recipes, name, "recipe")
                    if (
                        entry.recipe_id in recipes
                        and recipes[entry.recipe_id].source.book != model.book
                    ):
                        add(
                            "index_book_mismatch",
                            name,
                            "Linked recipe belongs to another book",
                        )
                for ref in entry.index_refs:
                    source(ref, name)
        elif isinstance(model, Receipt):
            if name != f"receipts/{model.id}.yaml":
                add("identity_mismatch", name, "Receipt ID does not match its filename")
            for ref in model.page_refs:
                source(ref, name)
            for entry in model.entries.values():
                for identifier in (
                    entry.ingredient,
                    entry.accepted.ingredient if entry.accepted else None,
                ):
                    if identifier:
                        reference(identifier, catalog, name, "ingredient")
                for ref in entry.observed.page_refs:
                    source(ref, name)
        elif name.startswith("nutrition/fdc/"):
            reference(
                name.removeprefix("nutrition/fdc/").removesuffix(".yaml"),
                recipes,
                name,
                "recipe",
            )
        elif isinstance(model, PushBatch):
            ids = [command.get("uuid") for command in model.commands]
            if (
                any(not isinstance(value, str) or not value for value in ids)
                or len(set(str(value) for value in ids)) != len(ids)
                or not set(model.completed) <= set(str(value) for value in ids)
            ):
                add(
                    "invalid_retry_state",
                    name,
                    "Todoist command IDs or acknowledgments are inconsistent",
                )
    for rows in documents.get("prices.yaml", {}).values():
        for price in rows:
            if price.receipt_ref:
                receipt_id, _, row_id = price.receipt_ref.partition("/")
                receipt = documents.get(f"receipts/{receipt_id}.yaml")
                if (
                    not isinstance(receipt, Receipt)
                    or row_id not in receipt.entries
                    or receipt.entries[row_id].accepted is None
                ):
                    add(
                        "broken_reference",
                        "prices.yaml",
                        "Receipt price refers to a missing or unaccepted row",
                    )
    report.findings.sort(key=lambda item: (item.path, item.code, item.message))
    return report
