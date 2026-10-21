# Bourdain

A personal, local-first meal planning toolkit: cookbook recipes, nutrition,
weekly plans, and grocery checklists, with hand-editable files as the database.

## Status

Import recipes from text, URLs, cookbook photos or PDFs, review flagged fields,
browse the library, calculate USDA macros, exchange nutrition with Cronometer,
optimize a week, and update stock from shopping/cooking. The lean dial adjusts
cost versus variety; compare settings before saving and replan future days while
preserving the past. These stages also have callable Python library functions.

The main implementation now extends through Phase 9. Offline tests cover the
integrated workflow, including tiny optimizer worlds and crash recovery. Real
Anthropic/USDA accuracy, actual Cronometer CSV/paste compatibility, manual Todoist
import and hosted CI remain external acceptance checks. Multiple package sizes
from Phase 10 are also supported, along with EPUB import, freezer portions,
library statistics and count-based shopping quantities with reviewed USDA portion
weights. Index-first cookbook capture, receipt price proposals and one-way
Todoist push are implemented too; real images and account behavior still need
external acceptance checks.

The final readiness pass adds a read-only health check, checksummed backup/restore,
and a complete live acceptance runbook with manifest-driven checks and a disposable
80-candidate benchmark. These tools prepare validation; live checks remain pending.

- [Project overview and planned CLI](docs/README.md)
- [Architecture and data contracts](docs/ARCHITECTURE.md)
- [Roadmap and acceptance criteria](docs/ROADMAP.md)
- [Live validation runbook](docs/VALIDATION.md)
- [Coding-agent instructions](docs/AGENTS.md)

## Development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked
uv run bourdain --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build --no-build-isolation
```

## Health checks and recovery

```sh
uv run bourdain --data ./demo-kitchen doctor
uv run bourdain --data ./demo-kitchen doctor --json
uv run bourdain --data ./demo-kitchen backup ./kitchen-backup.zip
uv run bourdain restore ./kitchen-backup.zip ./restored-kitchen
uv run bourdain --data ./restored-kitchen doctor
```

`doctor` reads without repairing files, recovering transactions or calling APIs.
It checks schemas, references, archived sources, stale conversions/plans and local
solver availability. It reports credential presence without values; absent API
keys are optional. Exit status is 1 for integrity errors, 0 otherwise. Warnings
can include historical plans whose inventory has since changed.

Backups preserve original file bytes, comments, raw sources, receipts, pantry and
history, and Todoist/Cronometer retry records. They include other ordinary user
files, excluding `.cache`, `.git`, `__pycache__`, `.transaction.lock` and `.env*`.
Environment variables are never serialized. The ZIP has a versioned manifest
with SHA-256 checksums. Keep the archive outside the library; its parent must
exist and its filename must be new. Pause other writers while making a snapshot.
Pending transactions, links/junctions and detected concurrent changes stop backup.

Restore checks every checksum and path before creating a **new** destination;
it never merges into an existing folder. Its parent must already exist. Limits
are 100,000 files and 20 GiB uncompressed. Caches can be repopulated when needed;
empty directories and filesystem permissions/timestamps are not preserved.
If a filesystem failure interrupts publication, inspect the partial destination
and retry into another new folder. Run `doctor` on the restored copy before use.

## Try the local workflow

These commands create a separate demo folder. Run them from this repository:

```sh
uv run bourdain init ./demo-kitchen
uv run bourdain --data ./demo-kitchen ingest text examples/tomato-beans.txt
uv run bourdain --data ./demo-kitchen plan --week 2026-W43 --add "mon dinner=text/tomato-beans" --add "fri dinner=out"
uv run bourdain --data ./demo-kitchen list --output groceries.md
uv run bourdain --data ./demo-kitchen list --format text
uv run bourdain --data ./demo-kitchen list --format todoist-csv --output groceries.csv
```

Set `BOURDAIN_DATA` to avoid repeating `--data`. The precedence is `--data`, then
`BOURDAIN_DATA`, then `~/kitchen`. `--data` is a global option and goes before the
command. `init` only creates files at the path you give it; `init --force` adds
missing files without replacing edits. Use `--output` for UTF-8 exports on Windows.

Text recipes start with a title, then `Servings: 2`, then one ingredient per line.
`Ingredients:` is optional. A heading such as `For the sauce:` groups ingredients.
`Instructions:` ends the ingredient list; instructions are saved only when
`ingest.store_steps = true`. Use `ingest text -` to read stdin or `ingest url URL`
to fetch recipe metadata from a web page. Pages without metadata report an error.

New ingredients create catalog stubs and review flags. Edit
`catalog/ingredients.yaml` to add aliases, `buy.section`, `buy.package_g`, densities,
and count weights. Mass quantities work immediately; cups, cans, cloves, and other
counts need the relevant catalog information. Volumes use US customary measures.
Ambiguous quantities remain unresolved. A grocery export identifies unresolved
ingredients so you can supply grams before shopping.

### Grams, onions and shopping counts

Mass units (`g`, `kg`, `oz`, `lb`) convert directly. Counts (`each`, `medium`,
`clove`, `head`, `can`, etc.) use ingredient-specific `unit_weights`; volumes use
`density_g_per_ml`. No onion size or cup density is guessed. After an onion
catalog entry exists, confirm a measurement once and select it for shopping:

```sh
uv run bourdain --data ./demo-kitchen catalog unit-weight onion medium 150 --buy
```

Here **150 g is an illustrative weight you replace with your own confirmed
value**. Then `1 medium onion` contributes 150 g. A recipe needing 170 g plus
another needing one medium onion totals 320 g; after pantry subtraction, a
320 g shortfall becomes **3 medium onions**, approximately 450 g. Lists show
`3 × medium @ ~150 g each`, and `bought` records the estimated weight with a note.
The optimizer prices and rounds whole items too. Configured bags/packages retain
their package quantities; a package equal to the confirmed count weight can use
the count label. Garlic may have a `clove` conversion while its shopping unit is
`head`, each with its own confirmed weight.

If the weight or density is missing, the recipe remains flagged. After adding
it, `review` can retry conversion on the unresolved line. Confirming a weight
protects that catalog field without accepting unrelated model suggestions.

You can also look up portion weights from the ingredient's confirmed USDA food:

```sh
uv run bourdain --data ./demo-kitchen catalog portions onion
# Replace PORTION_ID with the ID of the matching medium-onion portion shown above:
uv run bourdain --data ./demo-kitchen catalog use-portion onion PORTION_ID --unit medium --buy
```

The lookup uses `FDC_API_KEY` for uncached foods and works without a key when
cached. It displays the full food and portion descriptions without changing the
catalog. First confirm the food and edible fraction with `review --nutrition` or
`nutrition set`. Select the matching preparation and size explicitly; USDA foods
do not all have every size. USDA portion weights describe edible food, so the
saved weight is `portion grams / portion count / edible_fraction`, consistent
with the catalog's recipe-weight basis. With fraction `1`, no adjustment applies.

`use-portion` saves the food ID, description, original portion and edible fraction
alongside the conversion. A source without an amount requires an explicit
`--count`; existing measurements require `--replace`. Mass/volume portions cannot
be turned into whole-item counts, and densities are not inferred. A later change
to the food ID, edible fraction or sourced weight stops its use for new count
conversions and shopping counts until reconfirmed. Previously saved recipe grams
stay unchanged. `unit-weight` explicitly replaces a sourced weight with a manual
measurement and clears that unit's source record.

For an old cache without portion data, `catalog portions onion --refresh`
explicitly refetches details; a failed fetch preserves the old cache. `--json`
returns the USDA-shaped food details for library clients. The offline portion
tests use synthetic data; real food/portion accuracy remains a live acceptance
check. Cronometer serving conversions still need a real export sample before a
compatible importer can be implemented; there is no connected account access.

## Import and review cookbook pages

With `ANTHROPIC_API_KEY` set in your environment:

```sh
uv run bourdain --data ./demo-kitchen ingest photos ./cookbook-photos --book my-book
uv run bourdain --data ./demo-kitchen ingest pdf ./cookbook.pdf --book my-book --pages 3-5,8
uv run bourdain --data ./demo-kitchen review
uv run bourdain --data ./demo-kitchen recipes ls --book my-book --status needs_review
uv run bourdain --data ./demo-kitchen recipes search soup
uv run bourdain --data ./demo-kitchen recipes show my-book/carrot-soup
uv run bourdain --data ./demo-kitchen recipes tag my-book/carrot-soup dinner
uv run bourdain --data ./demo-kitchen recipes exclude my-book/carrot-soup
```

JPEG, PNG and HEIC photos are rotated upright and resized before extraction;
original files are preserved under `raw/<date>/` with content hashes as names.
Filenames use natural order (`page2` before `page10`); `--order exif` requires capture
times on every photo. Subfolders separate groups. Overlapping page windows merge
continuations while retaining printed page numbers and physical PDF page indices.
Identical extraction requests reuse `.cache/extraction` responses.

`review` visits flagged fields and displays a small source-page preview alongside
parsed data. Use `a` to accept the displayed ingredient and catalog properties,
`e` to edit inline, `m` to choose another catalog ID, `s` to skip, or `q` to quit.
Unknown grams must be supplied before acceptance. Confirmed names become aliases;
accepted lines are marked `source: user` and locked. Confirmed recipe fields are
listed in `user_fields`, preserving those edits while allowing nutrition to be
calculated. A manually set recipe `locked: true` still protects the entire recipe.
Narrow the session with `review --recipe ID`.

Exact names and confirmed aliases match locally. Fuzzy matches use
`ingest.match_threshold` and remain flagged until confirmed. Add `--llm-match` to
any ingest command to enable cached Anthropic proposals below that threshold.
Proposals remain flagged, including suggested densities and count weights; they
never overwrite existing catalog properties. Keys are read only from the
environment. Configure vision and matching models under `[llm]`.

Recipe listing/search also filter by `--tag`, `--protein`, and `--status`.
Use `recipes tag ID TAG --remove` to remove a tag and `recipes exclude ID --include`
to restore an excluded recipe. Recipe IDs remain stable when titles are reviewed.

### EPUB books and library readiness

```sh
uv run bourdain --data ./demo-kitchen ingest epub ./cookbook.epub --book my-book
uv run bourdain --data ./demo-kitchen stats
uv run bourdain --data ./demo-kitchen stats --json
```

EPUB import follows the book's reading order, archives the original, and records
chapter/recipe references for review. It supports Recipe JSON metadata and XHTML
recipes with a title and explicit `Ingredients` heading, plus optional servings
and instructions. Chapters without recognizable recipes are reported. Encrypted
text and image-only/unstructured layouts need another ingest path; no text is
sent to an API unless you request `--llm-match` for ingredient matching.

`stats` reports review backlog, flags per recipe, complete nutrition, average
coverage, missing quantities/matches, freezer portions and eligibility for your
current targets. Eligibility means the input filters pass, not that all weekly
constraints are feasible. It reads files without changing recipe data.

## Select recipes from a cookbook index

```sh
uv run bourdain --data ./demo-kitchen ingest index ./index-photos --book my-book
# Or select the physical PDF pages containing the index:
uv run bourdain --data ./demo-kitchen ingest index ./cookbook.pdf --book my-book --pages 300-304
uv run bourdain --data ./demo-kitchen index ls my-book
uv run bourdain --data ./demo-kitchen index select my-book ENTRY_ID
uv run bourdain --data ./demo-kitchen index queue my-book
```

Replace `ENTRY_ID` with the ID displayed by `index ls`. Extraction uses
`ANTHROPIC_API_KEY`, caches successful structured responses, archives originals
and flags uncertain titles/pages. Stubs live in `indexes/<book>.yaml`, separate
from complete recipes, so they cannot enter planning. Correct an entry while
selecting it with `--title "Corrected title" --pages 42-43`; `--remove` deselects.
Confirmed entries survive repeat extraction. `index ls` and `queue` support JSON.

The capture queue lists **printed recipe pages**, while ingest `--pages` selects
**physical PDF pages**. No offset is assumed. Photograph the selected recipe
pages and use the existing `ingest photos` or `ingest pdf` workflow. A same-title
recipe in the same book with overlapping printed pages marks an entry captured.
When the imported title differs, use `index link my-book ENTRY_ID RECIPE_ID`.
Category headings/cross references are excluded by the extraction prompt; actual
cookbook index layouts still require live accuracy checks.

## Nutrition and Cronometer

With `FDC_API_KEY` set in the environment:

```sh
uv run bourdain --data ./demo-kitchen nutrition map
uv run bourdain --data ./demo-kitchen review --nutrition
uv run bourdain --data ./demo-kitchen nutrition refresh
uv run bourdain --data ./demo-kitchen cronometer export text/tomato-beans
uv run bourdain --data ./demo-kitchen cronometer export --all-new
uv run bourdain --data ./demo-kitchen cronometer import-nutrition recipe.csv --recipe text/tomato-beans --basis full-recipe
uv run bourdain --data ./demo-kitchen nutrition diff
```

USDA search/details are cached in `.cache/fdc/`. Proposals are stored separately in
`catalog/fdc.yaml` so accepted catalog properties stay protected. Review uncertain
matches and supply an `edible_fraction` for each ingredient: `1` means the recorded
grams are entirely edible. An explicit alternative is `nutrition set INGREDIENT
FDC_ID --edible-fraction 1`. Unknown weights, edible fractions or nutrient values
remain flagged; missing nutrients do not become zero.

`nutrition refresh` saves an independent USDA calculation under `nutrition/fdc/`
and updates unlocked recipe nutrition. Cronometer imports lock the active values;
the independent calculation remains available to `nutrition diff`. Cache entries
are reused until removed explicitly; refresh recalculates recipes from that cache.

Cronometer export produces ingredient grams and names for manual pasting. Batch
export saves `cronometer_queue.md`, attempts to copy its first block, and records
content hashes so changed recipes reappear. CSV import supports named nutrient
columns or `Nutrient,Amount,Unit` rows. Specify `--basis per-serving`, `full-recipe`,
or `per-100g`; the last also requires `--recipe-weight-g` for one recipe. Units are
converted explicitly, and ambiguous names require `--recipe`. These conservative
CSV layouts are tested with synthetic examples; a real account export still needs
to be checked. There is no Cronometer account automation.

## Plan, shop and record cooking

Edit `targets.yaml` to set household servings and enabled meal slots. The default
is one eater and dinner only, with no macro constraints. Manual plans scale batches
to those servings. `plan --manual` opens an assignment template in your editor;
`--add` can be repeated. `plan` without either option runs the optimizer. Eligible
recipes need reviewed ingredients, known servings, nutrition coverage of at least
95%, and a tag matching a planned slot. `--ignore-macros` explicitly bypasses the
nutrition requirement. `list` uses the latest saved week, or `--week`.

```sh
uv run bourdain --data ./demo-kitchen pantry set carrots --qty-g 500 --expires 2026-10-28
uv run bourdain --data ./demo-kitchen prices set carrots 1000 2.50
uv run bourdain --data ./demo-kitchen plan --compare 0,0.5,1
uv run bourdain --data ./demo-kitchen plan --lean 0.2 --lock "fri dinner=out" --pin text/tomato-beans --explain
uv run bourdain --data ./demo-kitchen list --output groceries.md
# Check purchased items in groceries.md, then:
uv run bourdain --data ./demo-kitchen bought groceries.md
uv run bourdain --data ./demo-kitchen cooked text/tomato-beans --batches 1 --rating 4
uv run bourdain --data ./demo-kitchen plan --replan-from wed
```

The solver first minimizes macro deviations, then cost/variety preferences without
worsening that fit. Unmet targets are reported in nutrient units. Leftovers refer
to actual cook events within `keeps_days`; batches cannot be added solely to earn
pantry credit. Recipe repeat limits, protein-category caps and optional effort
budgets are configurable under `[plan]`. Prices default to USD; estimated prices
and missing package sizes produce warnings, and can be overridden in `[prices]`.

Record each package size separately to let the optimizer compare whole purchases:

```sh
uv run bourdain --data ./demo-kitchen prices set carrots 250 2.00
uv run bourdain --data ./demo-kitchen prices set carrots 1000 5.00
```

For a 1,250 g shortfall, those prices allow one of each package for $7 instead of
two large packages for $10. The solver considers all maintained sizes and can
trade cost against perishable surplus using the lean settings. Without maintained
prices, it estimates every catalog size and reports that assumption.

`--compare` writes no plan. Normal saves refuse an existing week; `--replan-from`
updates it explicitly, freezes earlier meals/cooks and uses current pantry stock.
Grocery export then includes only future cook events. Keep household portions and
the servings of already-cooked recipes unchanged during a replan. Changed stock
or recipe quantities require replanning before exporting an optimized checklist.

Pantry entries accept `qty_g` or coarse `have`/`low`/`out` status. Expired stock is
ignored with a warning. Staples are omitted, with low staples in “Also check”.
Optional ingredients are omitted unless `list --include-optional` is supplied.
Where package sizes are configured, manual lists minimize surplus using one size
per ingredient, breaking ties by fewer packages. Optimized lists use the exact
package mix chosen by the solver, including combinations of sizes. Optional
ingredients were not modeled by the optimizer; use a manual plan when explicitly
including them.

`pantry ls` labels expired entries; `pantry edit` opens validated YAML in your
editor. `pantry set` replaces an ingredient's lots, while `bought` adds a purchase
lot with its known acquisition date and configured shelf life. Grocery Markdown
contains hidden IDs and quantities for repeat-safe purchase recording. A second
trip for the same week's item needs `bought --purchase-id NEW_ID`. If editing a
quantity by hand, update/remove the hidden metadata too. Checked “check pantry”
items need an explicit quantity or `pantry set` instead of an inferred purchase.

`cooked` consumes earliest-expiring weighed stock first and leaves unweighed stock
as coarse status. Unrecorded consumption is reported. `--event-id ID` makes a
cooking retry idempotent; a normal invocation records a new event. Pantry/history
writes use a recovery journal so an interruption cannot permanently split the
deduction from its history record. Keep any `.transaction.json` until recovery.

### Carry cooked portions into future weeks

```sh
uv run bourdain --data ./demo-kitchen recipes freezable text/tomato-beans
uv run bourdain --data ./demo-kitchen cooked text/tomato-beans --batches 2 --freeze-servings 2 --freezer-expires 2026-12-01 --event-id beans-batch-1
uv run bourdain --data ./demo-kitchen freezer ls
# Or record portions that were cooked previously:
uv run bourdain --data ./demo-kitchen freezer add text/tomato-beans 2 --expires 2026-12-01 --lot beans-leftovers
uv run bourdain --data ./demo-kitchen freezer eat beans-leftovers 1 --event-id monday-dinner
```

Choose the expiry date yourself; the example date is not a storage-life estimate.
`cooked --freeze-servings` deducts the full batch ingredients and records the
reserved servings together. `freezer add` records existing food without deducting
raw ingredients again. Use `freezer eat` when consuming prepared portions.

Automatic plans can use recorded portions across weeks, combine portions from
several lots, and prefer earlier expiry. They buy ingredients only for fresh cook
events. Planning/comparison does not consume stock: log what you actually eat.
The saved meal's `freezer_portions` identifies lots and serving amounts. Replanning
preserves earlier meals and uses current freezer balances. Changed stock requires
replanning before grocery export. If recipe quantities, yield or nutrition change
after freezing, that lot is excluded with a warning to avoid using new values for
old food. Extra batches for freezing are explicitly recorded with `cooked`;
the optimizer does not assume unrecorded future freezer stock.

## Propose prices from a receipt

```sh
uv run bourdain --data ./demo-kitchen prices receipt ./receipt.jpg
uv run bourdain --data ./demo-kitchen prices receipts
uv run bourdain --data ./demo-kitchen prices review RECEIPT_ID
uv run bourdain --data ./demo-kitchen prices accept RECEIPT_ID line-1
```

Receipt import uses the same image preparation and Anthropic boundary. Pass a
single photo or a PDF of one receipt (at most ten selected pages). Proposals in
`receipts/` retain raw item text, source references and suggested catalog matches;
importing does not change prices or pantry stock. Review the source, then accept
each usable line. For missing or misread fields, supply `--ingredient beans
--package-g 500 --quantity 2 --line-total 6.00 --currency USD`. These values are an
illustration: package grams describe **one package**, quantity is its package
count, and line total is the final amount paid for that row. The stored package
price is line total divided by quantity. An ambiguous `$` stays unresolved.

Use `--resolve-discount` only after confirming the item's final discounted total;
unassigned basket discounts require that review. Currency must match the configured
price currency, and updating an existing package price requires `--replace`.
`prices ignore RECEIPT_ID ROW` skips unsuitable rows. Reimporting the same source
preserves decisions, and repeating acceptance cannot undo later manual price
edits. Price and acceptance writes use the recovery journal. Returns, unweighed
count/volume purchases and unclear package sizes need manual handling; weights
are never inferred from a product name. Receipt accuracy remains unvalidated on
real photos.

## Push groceries to Todoist

```sh
uv run bourdain --data ./demo-kitchen push todoist --project PROJECT_ID --week 2026-W43 --dry-run
# Set TODOIST_API_TOKEN in your environment, then send the reviewed snapshot:
uv run bourdain --data ./demo-kitchen push todoist --project PROJECT_ID --week 2026-W43
```

`--dry-run` prints the task payloads without a token, network access or export
state. Tasks preserve package/count labels, recipe context, notes and warnings,
with the week and store section in each title. Provide the destination project
ID explicitly. Credentials stay in the environment and are sent only in the
authorization header.

This is a **one-way snapshot push**. Command UUIDs are saved before sending under
`exports/todoist/`; successful tasks are acknowledged individually. Retry the same
command after a partial failure or interruption: pending commands retain their
UUIDs for Todoist's deduplication, and confirmed successes are skipped. Requests
contain at most 100 tasks. Keep the export state to preserve retry behavior.

If the list changes after staging a batch, the command stops. Review the existing
Todoist tasks before explicitly starting a separate export with `--batch second-trip`.
It does not reconcile remote edits, completion or deletions, and it does not mark
pantry purchases. Continue using `bought` for stock. Microsoft To Do remains a
future adapter; real Todoist account acceptance is deferred. The transport follows
the [Todoist API v1 Sync command contract](https://developer.todoist.com/api/v1/).

## Try automatic planning without keys

This creates clearly labeled synthetic data in a new folder:

```sh
uv run python examples/planning_demo.py ./planner-demo
uv run bourdain --data ./planner-demo plan --compare 0,0.5,1
uv run bourdain --data ./planner-demo plan --lean 0 --explain
uv run bourdain --data ./planner-demo list --output demo-groceries.md
```

The demo's ingredient quantities and macros are illustrative test values.

Objects with `locked: true` or `source: user` survive automated updates. Save a
typed document with `automated=False` only for an intentional user edit.

```python
from pathlib import Path
from bourdain.ingest.text import from_text
from bourdain.library import import_draft
from bourdain.store import Repository

repo = Repository(Path("demo-kitchen"))
draft = from_text("Rice\nServings: 2\n150 g rice")
recipe = import_draft(repo, draft)
print(recipe.id)
```

`uv.lock` pins the dependencies for reproducible installs. The package uses a
`src/bourdain/` layout, with separate modules for each pipeline stage. It can also
be invoked with `uv run python -m bourdain --help`.

The default test run excludes tests marked `live` and blocks socket connections.
Fixtures include reduced recipe metadata from three public sites. Tests use
temporary data directories, golden grocery outputs, and property checks for mass
conversion and ingredient conservation. `uv run pytest --update-golden` explicitly
updates the expected export files. Live integration tests can be selected with
`uv run pytest -m live`; credentials come only from `ANTHROPIC_API_KEY` and
`FDC_API_KEY`. Cookbook live tests also require a real-page manifest, described in
[the extraction fixture notes](tests/fixtures/extraction/README.md). Live tests
skip when prerequisites are absent; no live accuracy result is claimed from fakes.
The five-food FDC check uses the verified manifest described in
[the nutrition fixture notes](tests/fixtures/nutrition/README.md).

GitHub Actions runs lint, formatting, offline tests, CLI help, and package builds
on Windows and Linux, following the [uv CI guide](https://docs.astral.sh/uv/guides/integration/github/).

Personal recipes and pantry data belong in a separate directory, such as
`~/kitchen` or `$BOURDAIN_DATA`. Installation and CLI help do not create or modify
that directory.
