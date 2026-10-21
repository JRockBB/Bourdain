"""Command-line entry point for Bourdain."""

from collections.abc import Callable
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

import click
import typer
from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from bourdain.catalog import exclude_recipe, search_recipes, tag_recipe
from bourdain.config import Settings, load_settings
from bourdain.errors import BourdainError
from bourdain.grocery.aggregate import groceries
from bourdain.grocery.export import export_list
from bourdain.grocery.todoist import TodoistProvider
from bourdain.ingest.documents import DocumentExtractor
from bourdain.ingest.extractor import RecipeExtractor
from bourdain.ingest.text import from_text
from bourdain.ingest.url import from_url
from bourdain.library import import_draft
from bourdain.models import Recipe
from bourdain.normalize.matcher import CatalogMatcher
from bourdain.nutrition.fdc import FoodProvider
from bourdain.plan.manual import manual_plan
from bourdain.store import Repository, initialize

app = typer.Typer(
    name="bourdain",
    help="A local-first meal planning toolkit.",
    no_args_is_help=True,
    add_completion=False,
)


def friendly_errors(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except (BourdainError, OSError, ValidationError) as exc:
            message = (
                "Invalid input data" if isinstance(exc, ValidationError) else str(exc)
            )
            typer.echo(f"Error: {message}", err=True)
            raise typer.Exit(2) from None

    return wrapper


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    data: Annotated[
        Path | None, typer.Option(help="Data folder (or BOURDAIN_DATA). ")
    ] = None,
) -> None:
    """Expose the CLI command group for the library's pipeline stages."""
    ctx.obj = data


def repository(ctx: typer.Context) -> Repository:
    return Repository(load_settings(ctx.obj).paths.data)


@app.command("init")
@friendly_errors
def init_command(path: Path, force: bool = False) -> None:
    """Create a data folder; --force adds missing files without replacing edits."""
    typer.echo(f"Initialized {initialize(path, force=force)}")


@app.command("doctor")
@friendly_errors
def doctor_command(
    ctx: typer.Context, json_output: bool = typer.Option(False, "--json")
) -> None:
    """Check library integrity without changing files or calling APIs."""
    from bourdain.config import data_path
    from bourdain.doctor import diagnose

    report = diagnose(data_path(ctx.obj))
    if json_output:
        import json

        typer.echo(json.dumps(report.model_dump() | {"ok": report.ok}, indent=2))
    else:
        for item in report.findings:
            typer.echo(
                f"{item.severity.upper()} {item.path}: {item.message} [{item.code}]"
            )
        typer.echo(
            f"Checked {report.checked_files} data files; "
            f"HiGHS available: {report.solver_available}"
        )
        for key, present in report.credentials.items():
            typer.echo(f"{key}: {'set' if present else 'not set (optional)'}")
        typer.echo(
            "No integrity errors found" if report.ok else "Integrity errors found"
        )
    if not report.ok:
        raise typer.Exit(1)


@app.command("backup")
@friendly_errors
def backup_command(ctx: typer.Context, archive: Path) -> None:
    """Save a checksummed ZIP outside the library; never overwrite an archive."""
    from bourdain.backup import create_backup
    from bourdain.config import data_path

    result = create_backup(data_path(ctx.obj), archive)
    typer.echo(
        f"Backed up {result.files} files ({result.bytes} bytes) to {result.path}"
    )


@app.command("restore")
@friendly_errors
def restore_command(archive: Path, destination: Path) -> None:
    """Verify a backup and restore into a new folder; never merge existing data."""
    from bourdain.backup import restore_backup

    result = restore_backup(archive, destination)
    typer.echo(
        f"Restored {result.files} files to {result.path}; run doctor with --data there"
    )


ingest_app = typer.Typer(
    help="Import recipes from text, web pages, cookbook photos or PDFs.",
    no_args_is_help=True,
)
app.add_typer(ingest_app, name="ingest")


@ingest_app.command("text")
@friendly_errors
def ingest_text(
    ctx: typer.Context, file: str, book: str | None = None, llm_match: bool = False
) -> None:
    """Read a text file or '-' for stdin: title, Servings: N, ingredient lines."""
    settings = load_settings(ctx.obj)
    text = (
        typer.get_text_stream("stdin").read()
        if file == "-"
        else Path(file).read_text(encoding="utf-8-sig")
    )
    draft = from_text(text, store_steps=settings.ingest.store_steps)
    recipe = import_draft(
        Repository(settings.paths.data),
        draft,
        book=book,
        match_threshold=settings.ingest.match_threshold,
        matcher=make_matcher(settings) if llm_match else None,
    )
    typer.echo(f"Imported {recipe.id} ({recipe.review.status})")
    for flag in recipe.review.flags:
        typer.echo(f"Review: {flag}", err=True)


@ingest_app.command("url")
@friendly_errors
def ingest_url(
    ctx: typer.Context, url: str, book: str | None = None, llm_match: bool = False
) -> None:
    """Download a recipe page and extract its structured metadata."""
    settings = load_settings(ctx.obj)
    draft = from_url(url, store_steps=settings.ingest.store_steps)
    recipe = import_draft(
        Repository(settings.paths.data),
        draft,
        book=book,
        match_threshold=settings.ingest.match_threshold,
        matcher=make_matcher(settings) if llm_match else None,
    )
    typer.echo(f"Imported {recipe.id} ({recipe.review.status})")
    for flag in recipe.review.flags:
        typer.echo(f"Review: {flag}", err=True)


@app.command("plan")
@friendly_errors
def plan_command(
    ctx: typer.Context,
    manual: bool = False,
    add: Annotated[
        list[str] | None,
        typer.Option(help="Meal assignment, e.g. mon dinner=text/chili."),
    ] = None,
    week: str | None = None,
    lean: float | None = None,
    lock: Annotated[
        list[str] | None, typer.Option(help="Fix a meal, e.g. fri dinner=out.")
    ] = None,
    pin: Annotated[
        list[str] | None, typer.Option(help="Include this recipe at least once.")
    ] = None,
    exclude: Annotated[
        list[str] | None, typer.Option(help="Exclude this recipe for this plan.")
    ] = None,
    explain: bool = False,
    ignore_macros: bool = False,
    compare: str | None = None,
    replan_from: str | None = None,
) -> None:
    """Optimize a week, compare lean settings, or choose meals manually."""
    if not manual and not add:
        from bourdain.plan.service import automatic_plan

        settings = load_settings(ctx.obj)
        repo = Repository(settings.paths.data)
        week = week or date.today().strftime("%G-W%V")
        try:
            values = (
                [float(value.strip()) for value in compare.split(",")]
                if compare
                else [lean]
            )
        except ValueError:
            raise BourdainError("Use --compare 0,0.5,1") from None
        if compare:
            typer.echo("Lean\tCost\tDistinct\tPackages\tWaste g\tMacro slack")
        for value in values:
            plan = automatic_plan(
                repo,
                settings,
                week,
                lean=value,
                locks=lock,
                pinned=pin,
                excluded=exclude,
                ignore_macros=ignore_macros,
                replan_from=replan_from,
                save=not compare,
            )
            if compare:
                terms = plan.objective_breakdown
                typer.echo(
                    f"{plan.lean:g}\t{terms['cost']:.2f}\t{terms['distinct_recipes']:g}\t{sum(plan.package_counts.values())}\t{terms['waste_g_perishable']:.1f}\t{terms['macro_slack']:.4f}"
                )
            else:
                typer.echo(f"Saved plans/{plan.week}.yaml ({plan.solver_status})")
                for meal in plan.meals:
                    typer.echo(
                        f"{meal.day} {meal.slot}: {meal.recipe}"
                        + (" (leftovers)" if meal.leftover else "")
                    )
            for warning in plan.warnings:
                typer.echo(f"Warning: {warning}", err=True)
            if explain:
                for identifier, reasons in plan.explanations.items():
                    typer.echo(f"{identifier}: {'; '.join(reasons)}")
        return
    if (
        lean is not None
        or lock
        or pin
        or exclude
        or explain
        or ignore_macros
        or compare
        or replan_from
    ):
        raise BourdainError("Use optimizer options without --manual or --add")
    repo = repository(ctx)
    assignments = add or []
    if manual and not assignments:
        template = (
            "# Map meal times to recipe IDs (or out). Replace {} with entries:\n"
            "# mon dinner: text/my-recipe\n# fri dinner: out\n{}\n"
        )
        edited = click.edit(template, extension=".yaml")
        if edited is None:
            raise BourdainError("Plan editing cancelled")
        try:
            values = YAML(typ="safe").load(edited)
            if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()
            ):
                raise ValueError("expected a mapping of meal times to recipe IDs")
            assignments = [f"{key}={value}" for key, value in values.items()]
        except (YAMLError, ValueError) as exc:
            raise BourdainError(
                "Invalid plan template: map 'mon dinner' to a recipe ID"
            ) from exc
    week = week or date.today().strftime("%G-W%V")
    plan = manual_plan(week, assignments, repo.recipes(), repo.targets().model)
    repo.save_plan(plan)
    typer.echo(f"Saved plans/{plan.week}.yaml")


@app.command("list")
@friendly_errors
def list_command(
    ctx: typer.Context,
    format: Annotated[str, typer.Option(help="md, text, or todoist-csv")] = "md",
    week: str | None = None,
    include_optional: bool = False,
    output: Path | None = None,
) -> None:
    """Export groceries for a saved plan (latest week by default)."""
    from bourdain.freezer import read_freezer

    settings = load_settings(ctx.obj)
    repo = Repository(settings.paths.data)
    if week is None:
        plans = sorted(repo.path("plans").glob("*.yaml"))
        if not plans:
            raise BourdainError("No saved plans; create one with plan --add first")
        week = plans[-1].stem
    result = groceries(
        repo.plan(week).model,
        repo.recipes(),
        repo.catalog().model,
        repo.pantry().model,
        include_optional=include_optional,
        freezer=read_freezer(repo),
    )
    try:
        text = export_list(result, format, section_order=settings.grocery.section_order)
    except ValueError as exc:
        raise BourdainError(str(exc)) from exc
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}", err=True)
    if output:
        output.write_text(text, encoding="utf-8", newline="\n")
        typer.echo(f"Wrote {output}")
    else:
        typer.echo(text, nl=False)


push_app = typer.Typer(
    help="Push a grocery snapshot to a checklist app.", no_args_is_help=True
)
app.add_typer(push_app, name="push")


def make_todoist() -> TodoistProvider:
    from bourdain.grocery.todoist import TodoistClient

    return TodoistClient()


@push_app.command("todoist")
@friendly_errors
def push_todoist(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Todoist project ID.")],
    week: Annotated[str, typer.Option(help="Saved ISO week, e.g. 2026-W43.")],
    batch: str = "weekly",
    dry_run: bool = False,
    include_optional: bool = False,
) -> None:
    """Push once per project/week/batch; --dry-run previews without writes."""
    import json

    from bourdain.freezer import read_freezer
    from bourdain.grocery.todoist import push_groceries

    settings = load_settings(ctx.obj)
    repo = Repository(settings.paths.data)
    items = groceries(
        repo.plan(week).model,
        repo.recipes(),
        repo.catalog().model,
        repo.pantry().model,
        include_optional=include_optional,
        freezer=read_freezer(repo),
    )
    for warning in items.warnings:
        typer.echo(f"Warning: {warning}", err=True)
    result = push_groceries(
        repo,
        items,
        project,
        make_todoist(),
        batch=batch,
        dry_run=dry_run,
        section_order=settings.grocery.section_order,
    )
    if dry_run:
        typer.echo(json.dumps(result.preview, indent=2))
    else:
        typer.echo(
            f"Todoist: {result.sent} sent, {result.already_sent} already sent "
            f"({result.total} total)"
        )


def make_extractor(settings: Settings) -> RecipeExtractor:
    from bourdain.ingest.anthropic import AnthropicExtractor
    from bourdain.ingest.extractor import CachedExtractor

    return CachedExtractor(AnthropicExtractor(settings.llm.model), settings.paths.data)


def make_matcher(settings: Settings) -> CatalogMatcher:
    from bourdain.normalize.matcher import AnthropicMatcher

    return AnthropicMatcher(settings.llm.text_model, settings.paths.data)


def make_document_extractor(settings: Settings) -> DocumentExtractor:
    from bourdain.ingest.documents import CachedDocumentExtractor

    return CachedDocumentExtractor(settings.paths.data, settings.llm.model)


@ingest_app.command("index")
@friendly_errors
def ingest_index(
    ctx: typer.Context,
    source: Path,
    book: Annotated[str, typer.Option()],
    pages: str | None = None,
) -> None:
    """Extract cookbook index stubs from images or selected PDF index pages."""
    from bourdain.book_index import import_index

    settings = load_settings(ctx.obj)
    index = import_index(
        Repository(settings.paths.data),
        source,
        book,
        make_document_extractor(settings),
        page_numbers=parse_page_selection(pages),
    )
    typer.echo(
        f"Saved {len(index.entries)} index entries for {book}; use index ls {book}"
    )
    for warning in index.warnings:
        typer.echo(f"Warning: {warning}")


index_app = typer.Typer(
    help="Select cookbook index entries and list pages to photograph.",
    no_args_is_help=True,
)
app.add_typer(index_app, name="index")


def show_index(ctx: typer.Context, book: str, queue: bool, json_output: bool) -> None:
    import json

    from bourdain.book_index import index_rows

    rows = index_rows(repository(ctx), book, queue=queue)
    if json_output:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        status = (
            "captured"
            if row["captured"]
            else "selected"
            if row["selected"]
            else "unselected"
        )
        numbers = ",".join(str(page) for page in row["pages"]) or "unknown"
        typer.echo(f"{row['id']} | {row['title']} | printed pages {numbers} | {status}")
        if row["flags"]:
            typer.echo("  Review: " + ", ".join(row["flags"]))
    if queue:
        numbers = sorted({page for row in rows for page in row["pages"]})
        typer.echo("Pages to photograph: " + (", ".join(map(str, numbers)) or "none"))


@index_app.command("ls")
@friendly_errors
def index_list(
    ctx: typer.Context,
    book: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List extracted titles, printed recipe pages and capture status."""
    show_index(ctx, book, False, json_output)


@index_app.command("queue")
@friendly_errors
def index_queue(
    ctx: typer.Context,
    book: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List selected recipe pages still needing capture (printed page numbers)."""
    show_index(ctx, book, True, json_output)


@index_app.command("select")
@friendly_errors
def index_select(
    ctx: typer.Context,
    book: str,
    entry: str,
    title: str | None = None,
    pages: str | None = None,
    remove: bool = False,
) -> None:
    """Confirm a title/page entry and select it, optionally correcting OCR fields."""
    from bourdain.book_index import select_entry

    result = select_entry(
        repository(ctx),
        book,
        entry,
        selected=not remove,
        title=title,
        pages=parse_page_selection(pages),
    )
    typer.echo(f"{'Selected' if result.selected else 'Deselected'} {result.title}")


@index_app.command("link")
@friendly_errors
def index_link(ctx: typer.Context, book: str, entry: str, recipe: str) -> None:
    """Link an index entry to its imported recipe when titles differ."""
    from bourdain.book_index import link_entry

    link_entry(repository(ctx), book, entry, recipe)
    typer.echo(f"Linked {entry} to {recipe}")


def import_pages(
    ctx: typer.Context,
    source: Path,
    book: str,
    order: str,
    pages: list[int] | None = None,
    llm_match: bool = False,
) -> None:
    from bourdain.ingest.batch import extract_path
    from bourdain.ingest.extractor import ExtractHints

    settings = load_settings(ctx.obj)
    repo = Repository(settings.paths.data)
    # Confirm the initialized catalog before copying originals or contacting the API.
    repo.catalog()
    hints = ExtractHints(book=book, store_steps=settings.ingest.store_steps)
    drafts = extract_path(
        source,
        repo.root,
        make_extractor(settings),
        hints,
        order=order,
        page_numbers=pages,
    )
    matcher = make_matcher(settings) if llm_match else None
    for draft in drafts:
        recipe = import_draft(
            repo,
            draft,
            book=book,
            match_threshold=settings.ingest.match_threshold,
            matcher=matcher,
        )
        typer.echo(f"Imported {recipe.id} ({recipe.review.status})")
    if not drafts:
        typer.echo("No recipes found in the supplied pages")


@ingest_app.command("photos")
@friendly_errors
def ingest_photos(
    ctx: typer.Context,
    source: Path,
    book: Annotated[str, typer.Option(help="Cookbook slug.")],
    order: str = "filename",
    llm_match: bool = False,
) -> None:
    """Extract a photo or directory; subfolders separate recipe groups."""
    import_pages(ctx, source, book, order, llm_match=llm_match)


def parse_page_selection(value: str | None) -> list[int] | None:
    if value is None:
        return None
    pages = []
    try:
        for part in value.split(","):
            bounds = part.strip().split("-")
            if len(bounds) == 1:
                pages.append(int(bounds[0]))
            elif len(bounds) == 2:
                start, end = map(int, bounds)
                if end < start or end - start > 99:
                    raise ValueError
                pages.extend(range(start, end + 1))
            else:
                raise ValueError
        if (
            not pages
            or min(pages) < 1
            or len(pages) > 100
            or len(set(pages)) != len(pages)
        ):
            raise ValueError
    except ValueError:
        raise BourdainError(
            "Use up to 100 unique PDF page numbers, e.g. --pages 3-5,8"
        ) from None
    return pages


@ingest_app.command("pdf")
@friendly_errors
def ingest_pdf(
    ctx: typer.Context,
    source: Path,
    book: Annotated[str, typer.Option(help="Cookbook slug.")],
    pages: str | None = None,
    llm_match: bool = False,
) -> None:
    """Extract a PDF, optionally selecting one-based pages with --pages 3-5,8."""
    if source.suffix.lower() != ".pdf":
        raise BourdainError("ingest pdf requires a PDF file")
    import_pages(
        ctx, source, book, "filename", parse_page_selection(pages), llm_match=llm_match
    )


@ingest_app.command("epub")
@friendly_errors
def ingest_epub(
    ctx: typer.Context,
    source: Path,
    book: Annotated[str, typer.Option(help="Cookbook slug.")],
    llm_match: bool = False,
) -> None:
    """Import unencrypted EPUB recipes with structured metadata or clear headings."""
    from bourdain.ingest.epub import extract_epub

    settings = load_settings(ctx.obj)
    repo = Repository(settings.paths.data)
    repo.catalog()
    result = extract_epub(
        source, repo.root, book=book, store_steps=settings.ingest.store_steps
    )
    matcher = make_matcher(settings) if llm_match else None
    for draft in result.drafts:
        recipe = import_draft(
            repo,
            draft,
            book=book,
            match_threshold=settings.ingest.match_threshold,
            matcher=matcher,
        )
        typer.echo(f"Imported {recipe.id} ({recipe.review.status})")
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}", err=True)


recipes_app = typer.Typer(
    help="Browse and organize the recipe library.", no_args_is_help=True
)
app.add_typer(recipes_app, name="recipes")


def print_recipes(recipes: list[Recipe]) -> None:
    typer.echo("ID\tTitle\tStatus\tExcluded")
    for recipe in recipes:
        typer.echo(
            f"{recipe.id}\t{recipe.title}\t{recipe.review.status}\t{recipe.excluded}"
        )


@recipes_app.command("ls")
@friendly_errors
def recipes_ls(
    ctx: typer.Context,
    tag: str | None = None,
    book: str | None = None,
    protein: str | None = None,
    status: str | None = None,
) -> None:
    """List recipes with optional tag, book, protein, and review-status filters."""
    print_recipes(
        search_recipes(
            repository(ctx).recipes(),
            tag=tag,
            book=book,
            protein=protein,
            status=status,
        )
    )


@recipes_app.command("search")
@friendly_errors
def recipes_search(
    ctx: typer.Context,
    query: str,
    tag: str | None = None,
    book: str | None = None,
    protein: str | None = None,
    status: str | None = None,
) -> None:
    """Search recipe names, IDs, ingredients and tags."""
    print_recipes(
        search_recipes(
            repository(ctx).recipes(),
            query,
            tag=tag,
            book=book,
            protein=protein,
            status=status,
        )
    )


@recipes_app.command("show")
@friendly_errors
def recipes_show(ctx: typer.Context, recipe: str) -> None:
    """Print a recipe's original YAML, including source-page references."""
    repo = repository(ctx)
    repo.recipe(recipe)
    typer.echo(repo.recipe_path(recipe).read_text(encoding="utf-8"), nl=False)


@recipes_app.command("tag")
@friendly_errors
def recipes_tag(
    ctx: typer.Context, recipe: str, tag: str, remove: bool = False
) -> None:
    """Add a tag, or remove it with --remove."""
    updated = tag_recipe(repository(ctx), recipe, tag, remove=remove)
    typer.echo(f"{updated.id}: {', '.join(updated.tags)}")


@recipes_app.command("exclude")
@friendly_errors
def recipes_exclude(ctx: typer.Context, recipe: str, include: bool = False) -> None:
    """Exclude a recipe from planning; --include restores it."""
    updated = exclude_recipe(repository(ctx), recipe, excluded=not include)
    typer.echo(f"{updated.id}: excluded={updated.excluded}")


@app.command("review")
@friendly_errors
def review_command(
    ctx: typer.Context, recipe: str | None = None, nutrition: bool = False
) -> None:
    """Review flagged fields; confirmed ingredient names teach the catalog."""
    from bourdain.review import review_session

    if nutrition:
        if recipe:
            raise BourdainError(
                "Nutrition mappings belong to catalog items; omit --recipe"
            )
        from bourdain.nutrition.review import review_mappings

        count = review_mappings(repository(ctx), make_fdc(load_settings(ctx.obj)))
    else:
        count = review_session(repository(ctx), recipe_id=recipe)
    typer.echo(f"Reviewed {count} item(s)")


def make_fdc(settings: Settings) -> FoodProvider:
    from bourdain.nutrition.fdc import FDCClient

    return FDCClient(settings.paths.data)


nutrition_app = typer.Typer(
    help="Map USDA foods, calculate macros and compare sources.", no_args_is_help=True
)
app.add_typer(nutrition_app, name="nutrition")


@nutrition_app.command("map")
@friendly_errors
def nutrition_map(ctx: typer.Context, ingredient: str | None = None) -> None:
    """Propose USDA mappings for unmapped catalog ingredients."""
    from bourdain.nutrition.service import map_catalog

    settings = load_settings(ctx.obj)
    mappings = map_catalog(
        Repository(settings.paths.data), make_fdc(settings), ingredient=ingredient
    )
    for identifier, mapping in mappings.items():
        if ingredient is None or ingredient == identifier:
            typer.echo(
                f"{identifier}\t{mapping.fdc_id}\t{mapping.description}\t"
                f"{', '.join(mapping.flags)}"
            )


@nutrition_app.command("set")
@friendly_errors
def nutrition_set(
    ctx: typer.Context,
    ingredient: str,
    fdc_id: int,
    edible_fraction: float | None = None,
) -> None:
    """Explicitly choose a USDA food, optionally setting the edible fraction."""
    from bourdain.nutrition.service import confirm_mapping

    settings = load_settings(ctx.obj)
    confirm_mapping(
        Repository(settings.paths.data),
        ingredient,
        make_fdc(settings).food(fdc_id),
        edible_fraction=edible_fraction,
    )
    typer.echo(f"Mapped {ingredient} to USDA {fdc_id}")


@nutrition_app.command("refresh")
@friendly_errors
def nutrition_refresh(ctx: typer.Context, recipe: str | None = None) -> None:
    """Calculate per-serving USDA macros while preserving locked nutrition."""
    from bourdain.nutrition.service import refresh

    settings = load_settings(ctx.obj)
    repo = Repository(settings.paths.data)
    for identifier, nutrition in refresh(
        repo, make_fdc(settings), recipe_id=recipe
    ).items():
        active = repo.recipe(identifier).model.nutrition
        saved = (
            "active"
            if active == nutrition
            else "comparison only; recipe/nutrition protected"
        )
        typer.echo(f"{identifier}\tcoverage={nutrition.coverage:.1%}\t{saved}")


@nutrition_app.command("diff")
@friendly_errors
def nutrition_diff(ctx: typer.Context) -> None:
    """Show USDA/Cronometer differences greater than ten percent."""
    from bourdain.nutrition.service import differences

    for identifier, nutrient, fdc, cronometer, difference in differences(
        repository(ctx)
    ):
        typer.echo(
            f"{identifier}\t{nutrient}\tUSDA={fdc:g}\tCronometer={cronometer:g}\t{difference:.1%}"
        )


cronometer_app = typer.Typer(
    help="Export paste blocks and import nutrition CSV files.", no_args_is_help=True
)
app.add_typer(cronometer_app, name="cronometer")


@cronometer_app.command("export")
@friendly_errors
def cronometer_export(
    ctx: typer.Context,
    recipe: Annotated[str | None, typer.Argument()] = None,
    all_new: bool = False,
) -> None:
    """Print one paste block, or save a queue and copy its first block."""
    from bourdain.cronometer.bridge import export_recipes

    text, copied = export_recipes(repository(ctx), recipe_id=recipe, all_new=all_new)
    typer.echo(text, nl=False)
    if all_new:
        typer.echo(
            "First block copied to clipboard"
            if copied
            else "Use the saved queue if clipboard copying is unavailable",
            err=True,
        )


@cronometer_app.command("import-nutrition")
@friendly_errors
def cronometer_import(
    ctx: typer.Context,
    csv: Path,
    basis: Annotated[str, typer.Option(help="per-serving, full-recipe, or per-100g")],
    recipe: str | None = None,
    recipe_weight_g: float | None = None,
) -> None:
    """Import CSV macros with an explicit serving basis and lock them."""
    from bourdain.cronometer.bridge import import_nutrition

    for identifier in import_nutrition(
        repository(ctx),
        csv,
        basis=basis,
        recipe_id=recipe,
        recipe_weight_g=recipe_weight_g,
    ):
        typer.echo(f"Imported and locked nutrition: {identifier}")


pantry_app = typer.Typer(help="Inspect and update pantry stock.", no_args_is_help=True)
app.add_typer(pantry_app, name="pantry")


@pantry_app.command("ls")
@friendly_errors
def pantry_ls(ctx: typer.Context) -> None:
    """List precise lots and coarse stock, labeling expired entries."""
    for item in repository(ctx).pantry().model.items:
        expired = " EXPIRED" if item.expires and item.expires < date.today() else ""
        quantity = f"{item.qty_g:g} g" if item.qty_g is not None else item.status
        typer.echo(f"{item.ingredient_id}\t{quantity}\texpires={item.expires}{expired}")


@pantry_app.command("set")
@friendly_errors
def pantry_set(
    ctx: typer.Context,
    ingredient: str,
    status: Annotated[str | None, typer.Argument()] = None,
    qty_g: float | None = None,
    expires: str | None = None,
) -> None:
    """Replace an ingredient's stock with have/low/out or --qty-g."""
    from bourdain.pantry.service import set_stock

    try:
        expiry = date.fromisoformat(expires) if expires else None
    except ValueError:
        raise BourdainError("Expiry must be YYYY-MM-DD") from None
    set_stock(repository(ctx), ingredient, status=status, qty_g=qty_g, expires=expiry)
    typer.echo(f"Updated pantry: {ingredient}")


@pantry_app.command("edit")
@friendly_errors
def pantry_edit(ctx: typer.Context) -> None:
    """Edit pantry YAML in the configured editor, validating before saving."""
    from bourdain.models import Pantry

    document = repository(ctx).pantry()
    edited = click.edit(document.path.read_text(encoding="utf-8"), extension=".yaml")
    if edited is None:
        return
    try:
        model = Pantry.model_validate(YAML(typ="safe").load(edited))
    except YAMLError:
        raise BourdainError("Invalid pantry YAML") from None
    document.save(model, automated=False)
    typer.echo("Saved pantry")


@app.command("bought")
@friendly_errors
def bought_command(
    ctx: typer.Context, groceries: Path, purchase_id: str | None = None
) -> None:
    """Add checked grocery items once; use a new purchase ID for a second trip."""
    from bourdain.pantry.service import bought

    added = bought(repository(ctx), groceries, purchase_id=purchase_id)
    typer.echo(f"Recorded {len(added)} purchase(s)")


@app.command("cooked")
@friendly_errors
def cooked_command(
    ctx: typer.Context,
    recipe: str,
    batches: float = 1,
    rating: int | None = None,
    event_id: str | None = None,
    include_optional: bool = False,
    freeze_servings: float = 0,
    freezer_expires: str | None = None,
) -> None:
    """Deduct weighed stock and record a cooking event with an optional rating."""
    from bourdain.pantry.service import cooked

    expiry = parse_date(freezer_expires) if freezer_expires else None

    for warning in cooked(
        repository(ctx),
        recipe,
        batches=batches,
        rating=rating,
        event_id=event_id,
        include_optional=include_optional,
        freeze_servings=freeze_servings,
        freezer_expires=expiry,
    ):
        typer.echo(f"Warning: {warning}", err=True)
    typer.echo(f"Recorded cooking: {recipe}")


prices_app = typer.Typer(
    help="Set package prices used by the planner.", no_args_is_help=True
)
app.add_typer(prices_app, name="prices")


@prices_app.command("set")
@friendly_errors
def prices_set(
    ctx: typer.Context,
    ingredient: str,
    package_g: float,
    price: float,
    currency: str = "USD",
) -> None:
    """Set a package price: INGREDIENT PACKAGE_G PRICE."""
    from bourdain.prices import set_price

    set_price(repository(ctx), ingredient, package_g, price, currency=currency)
    typer.echo(f"Saved price: {ingredient} {package_g:g} g = {price:g} {currency}")


@prices_app.command("receipt")
@friendly_errors
def prices_receipt(ctx: typer.Context, source: Path, pages: str | None = None) -> None:
    """Extract one receipt into proposals without changing prices or pantry stock."""
    from bourdain.receipts import import_receipt

    settings = load_settings(ctx.obj)
    result = import_receipt(
        Repository(settings.paths.data),
        source,
        make_document_extractor(settings),
        page_numbers=parse_page_selection(pages),
    )
    typer.echo(
        f"Saved {result.id}: {len(result.entries)} rows; use prices review {result.id}"
    )


@prices_app.command("receipts")
@friendly_errors
def prices_receipts(ctx: typer.Context) -> None:
    """List saved receipt proposal IDs."""
    from bourdain.receipts import receipt_document

    repo = repository(ctx)
    for path in sorted(repo.path("receipts").glob("*.yaml")):
        receipt = receipt_document(repo, path.stem).model
        pending = sum(
            not row.accepted and not row.ignored for row in receipt.entries.values()
        )
        typer.echo(
            f"{receipt.id} | {receipt.store or 'unknown store'} | {pending} pending"
        )


@prices_app.command("review")
@friendly_errors
def prices_review(
    ctx: typer.Context,
    receipt: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show source text, package quantities, totals and unresolved receipt fields."""
    from bourdain.receipts import receipt_document

    result = receipt_document(repository(ctx), receipt).model
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
        return
    for ref in result.page_refs:
        typer.echo(f"Source: {ref}")
    for warning in result.warnings:
        typer.echo(f"Warning: {warning}")
    for identifier, row in result.entries.items():
        observed = row.observed
        status = "accepted" if row.accepted else "ignored" if row.ignored else "pending"
        typer.echo(f"{identifier} ({status}): {observed.raw}")
        typer.echo(
            f"  {row.ingredient or 'unmatched'} | package {observed.package_g} g "
            f"| quantity {observed.quantity} | line total {observed.line_total} "
            f"{observed.currency or 'unknown currency'}"
        )
        if row.flags and status == "pending":
            typer.echo("  Review: " + ", ".join(row.flags))
    typer.echo(
        "Use prices accept RECEIPT ROW with corrections, or prices ignore RECEIPT ROW."
    )


@prices_app.command("accept")
@friendly_errors
def prices_accept(
    ctx: typer.Context,
    receipt: str,
    row: str,
    ingredient: str | None = None,
    package_g: float | None = None,
    quantity: float | None = None,
    line_total: float | None = None,
    currency: str | None = None,
    resolve_discount: bool = False,
    replace: bool = False,
) -> None:
    """Confirm a receipt row's final paid total and publish its per-package price."""
    from bourdain.receipts import accept_price

    settings = load_settings(ctx.obj)
    result = accept_price(
        Repository(settings.paths.data),
        receipt,
        row,
        ingredient=ingredient,
        package_g=package_g,
        quantity=quantity,
        line_total=line_total,
        currency=currency,
        expected_currency=settings.prices.currency,
        resolve_discount=resolve_discount,
        replace=replace,
    )
    typer.echo(
        f"Confirmed {result.ingredient}: {result.package_g:g} g = "
        f"{result.line_total / result.quantity:g} {result.currency}"
    )


@prices_app.command("ignore")
@friendly_errors
def prices_ignore(ctx: typer.Context, receipt: str, row: str) -> None:
    """Ignore a non-food or unusable receipt row without changing prices."""
    from bourdain.receipts import ignore_row

    ignore_row(repository(ctx), receipt, row)
    typer.echo(f"Ignored {row}")


catalog_app = typer.Typer(help="Confirm ingredient measurements.", no_args_is_help=True)
app.add_typer(catalog_app, name="catalog")


@catalog_app.command("unit-weight")
@friendly_errors
def catalog_unit_weight(
    ctx: typer.Context, ingredient: str, unit: str, grams: float, buy: bool = False
) -> None:
    """Confirm grams per item; --buy also chooses this shopping count unit."""
    from bourdain.catalog import set_unit_weight

    set_unit_weight(repository(ctx), ingredient, unit, grams, buy=buy)
    typer.echo(f"Confirmed {ingredient}: 1 {unit} is approximately {grams:g} g")


@catalog_app.command("portions")
@friendly_errors
def catalog_portions(
    ctx: typer.Context,
    ingredient: str,
    refresh: bool = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show USDA portion weights; --refresh explicitly refetches food details."""
    import json

    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    from bourdain.nutrition.portions import portion_food

    settings = load_settings(ctx.obj)
    repo = Repository(settings.paths.data)
    food = portion_food(repo, make_fdc(settings), ingredient, refresh=refresh)
    if json_output:
        typer.echo(json.dumps(food.model_dump(by_alias=True), indent=2))
        return
    console = Console()
    console.print(Text(f"USDA {food.fdc_id}: {food.description} ({food.data_type})"))
    if not food.food_portions:
        typer.echo(
            "No portion data in this response. If this is an older cache, "
            "retry with --refresh and FDC_API_KEY."
            if food.food_portions is None
            else "USDA returned no portions for this food. Use a measured unit-weight."
        )
        return
    table = Table("ID", "Portion description", "Source amount", "Edible grams")
    for portion in food.food_portions:
        table.add_row(
            str(portion.id) if portion.id is not None else "unknown",
            Text(portion.label or "undescribed"),
            f"{portion.amount:g}" if portion.amount is not None else "confirm count",
            f"{portion.gram_weight:g}"
            if portion.gram_weight is not None
            else "unknown",
        )
    console.print(table)
    item = repo.catalog().model[ingredient]
    typer.echo(f"Catalog edible fraction: {item.edible_fraction}")
    typer.echo(
        "Review the food and portion description, then use catalog use-portion "
        "INGREDIENT ID --unit medium [--buy]. No catalog weights changed."
    )


@catalog_app.command("use-portion")
@friendly_errors
def catalog_use_portion(
    ctx: typer.Context,
    ingredient: str,
    portion_id: int,
    unit: Annotated[str, typer.Option(help="Confirmed count label, e.g. medium.")],
    count: Annotated[
        float | None, typer.Option(help="Count only when omitted by the source.")
    ] = None,
    buy: bool = False,
    replace: bool = False,
) -> None:
    """Confirm one USDA count portion, preserving its source with the measurement."""
    from bourdain.nutrition.portions import use_portion

    settings = load_settings(ctx.obj)
    grams = use_portion(
        Repository(settings.paths.data),
        make_fdc(settings),
        ingredient,
        portion_id,
        unit,
        count=count,
        buy=buy,
        replace=replace,
    )
    typer.echo(
        f"Confirmed {ingredient}: 1 {unit} is approximately {grams:g} g "
        "on the catalog's recipe-weight basis; USDA source saved."
    )


@app.command("stats")
@friendly_errors
def stats_command(
    ctx: typer.Context, json_output: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Show review backlog, nutrient coverage and eligibility for current targets."""
    import json

    from rich.console import Console
    from rich.table import Table

    from bourdain.stats import summarize

    report = summarize(repository(ctx))
    if json_output:
        typer.echo(json.dumps(report, indent=2))
        return
    table = Table("Metric", "Value")
    for key, value in report.items():
        if isinstance(value, (int, float)):
            table.add_row(
                key.replace("_", " ").capitalize(),
                f"{value:.3g}" if isinstance(value, float) else str(value),
            )
    Console().print(table)
    for reason, count in report["blocking_reasons"].items():
        typer.echo(f"{count} recipe(s): {reason}")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BourdainError("Date must be YYYY-MM-DD") from None


freezer_app = typer.Typer(
    help="Record prepared portions for future weeks.", no_args_is_help=True
)
app.add_typer(freezer_app, name="freezer")


@recipes_app.command("freezable")
@friendly_errors
def recipe_freezable(ctx: typer.Context, recipe: str, disable: bool = False) -> None:
    """Confirm whether you want this recipe eligible for freezing."""
    document = repository(ctx).recipe(recipe)
    document.model.freezes = not disable
    document.save(automated=False)
    typer.echo(f"Freezable: {not disable}")


@freezer_app.command("ls")
@friendly_errors
def freezer_ls(ctx: typer.Context) -> None:
    from bourdain.freezer import read_freezer

    typer.echo("Lot\tRecipe\tServings\tExpires\tStatus")
    for lot in read_freezer(repository(ctx)).items:
        status = (
            "expired"
            if lot.expires < date.today()
            else "empty"
            if lot.servings == 0
            else "available"
        )
        typer.echo(f"{lot.id}\t{lot.recipe}\t{lot.servings:g}\t{lot.expires}\t{status}")


@freezer_app.command("add")
@friendly_errors
def freezer_add(
    ctx: typer.Context,
    recipe: str,
    servings: float,
    expires: Annotated[str, typer.Option(help="Your chosen expiry date, YYYY-MM-DD.")],
    lot: str | None = None,
) -> None:
    """Record already-cooked portions; use cooked --freeze-servings for new cooking."""
    from bourdain.freezer import add_portions

    result = add_portions(
        repository(ctx), recipe, servings, parse_date(expires), lot_id=lot
    )
    typer.echo(f"Added freezer lot: {result.id}")


@freezer_app.command("eat")
@friendly_errors
def freezer_eat(
    ctx: typer.Context, lot: str, servings: float, event_id: str | None = None
) -> None:
    """Deduct eaten portions; --event-id makes a retry safe."""
    from bourdain.freezer import eat_portions

    eat_portions(repository(ctx), lot, servings, event_id=event_id)
    typer.echo(f"Recorded {servings:g} serving(s) eaten from {lot}")
