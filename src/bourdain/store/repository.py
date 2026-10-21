"""Data-folder initialization and typed file access."""

from pathlib import Path

from pydantic import TypeAdapter

from bourdain.errors import BourdainError
from bourdain.models import Catalog, History, Pantry, Plan, Recipe, Targets
from bourdain.models.schemas import RecipeId
from bourdain.store.yaml import YamlDocument, atomic_write, create_yaml, load_yaml

DEFAULT_CONFIG = """# Bourdain settings. API keys belong only in environment variables.
[paths]
data = "." # The folder selected with --data or BOURDAIN_DATA is authoritative.

[llm]
provider = "anthropic"
model = "claude-sonnet-5-5"
text_model = "claude-haiku-5-5"
# Set ANTHROPIC_API_KEY in your environment.

[fdc]
# Set FDC_API_KEY in your environment.

[ingest]
store_steps = false
match_threshold = 85

[books]
# my-book = "My Cookbook Title"

[grocery]
section_order = ["produce", "meat", "dairy", "bakery", "pantry", "frozen", "other"]

[plan]
lean = 0.3
time_limit_s = 20
"""


def safe_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise BourdainError(f"Path escapes data folder: {relative}")
    return path


def initialize(root: Path, *, force: bool = False) -> Path:
    """Create missing files. Even --force preserves existing data and comments."""
    root = root.expanduser().resolve()
    if root.exists() and not force:
        raise BourdainError(
            f"Folder already exists: {root}; use --force to add missing files"
        )
    if root.exists() and not root.is_dir():
        raise BourdainError(f"Not a directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    for folder in ("recipes", "catalog", "plans", "inbox", "raw", ".cache"):
        safe_path(root, folder).mkdir(exist_ok=True)
    files = {
        "bourdain.toml": DEFAULT_CONFIG,
        "catalog/ingredients.yaml": "# Canonical ingredients and aliases.\n{}\n",
        "pantry.yaml": "# Use either status: have/low/out or qty_g.\nitems: []\n",
        "prices.yaml": "# Prices per ingredient and package size.\n{}\n",
        "targets.yaml": (
            "# Set household servings and optional macro targets here.\n"
            "planned_slots: [dinner]\ndays: [mon, tue, wed, thu, fri, sat, sun]\n"
            "eaters:\n  - name: me\n    servings_per_slot: 1\n    daily: null\n"
            "macro_mode: daily\n"
        ),
        "history.yaml": "# Append-only cooking events.\n[]\n",
        "freezer.yaml": (
            "# Prepared portions with explicitly chosen expiry dates.\nitems: []\n"
        ),
    }
    for name, content in files.items():
        try:
            atomic_write(
                safe_path(root, name), content.encode("utf-8"), overwrite=False
            )
        except FileExistsError:
            pass
    return root


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        from bourdain.store.transaction import recover

        recover(self.root)

    def path(self, relative: str) -> Path:
        return safe_path(self.root, relative)

    def catalog(self) -> YamlDocument[Catalog]:
        return load_yaml(self.path("catalog/ingredients.yaml"), Catalog)

    def pantry(self) -> YamlDocument[Pantry]:
        return load_yaml(self.path("pantry.yaml"), Pantry)

    def targets(self) -> YamlDocument[Targets]:
        return load_yaml(self.path("targets.yaml"), Targets)

    def history(self) -> YamlDocument[History]:
        return load_yaml(self.path("history.yaml"), History)

    def recipe_path(self, recipe_id: str) -> Path:
        recipe_id = TypeAdapter(RecipeId).validate_python(recipe_id)
        return self.path(f"recipes/{recipe_id}.yaml")

    def recipe(self, recipe_id: str) -> YamlDocument[Recipe]:
        doc = load_yaml(self.recipe_path(recipe_id), Recipe)
        if doc.model.id != recipe_id:
            raise BourdainError(f"Recipe id does not match its filename: {recipe_id}")
        return doc

    def recipes(self) -> dict[str, Recipe]:
        recipes = {}
        for path in sorted(self.path("recipes").rglob("*.yaml")):
            relative = path.relative_to(self.path("recipes")).with_suffix("").as_posix()
            recipes[relative] = self.recipe(relative).model
        return recipes

    def save_recipe(self, recipe: Recipe, *, automated: bool = True) -> Recipe:
        path = self.recipe_path(recipe.id)
        if path.exists():
            return self.recipe(recipe.id).save(recipe, automated=automated)
        create_yaml(path, recipe, Recipe)
        return recipe

    def plan(self, week: str) -> YamlDocument[Plan]:
        week = Plan(week=week).week
        return load_yaml(self.path(f"plans/{week}.yaml"), Plan)

    def save_plan(self, plan: Plan) -> None:
        path = self.path(f"plans/{plan.week}.yaml")
        if path.exists():
            raise BourdainError(
                f"Plan already exists: {plan.week}; edit the saved YAML to change it"
            )
        create_yaml(path, plan, Plan)
