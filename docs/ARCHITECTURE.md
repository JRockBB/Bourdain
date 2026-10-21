# Architecture

This document is the technical spec. Coding agents should treat the schemas and interfaces here as the contract; if implementation reveals a better design, update this file in the same change.

---

## 1. Pipeline overview

```
 photos / pdf / url / text
          │
          ▼
   ┌─────────────┐   RecipeDraft (raw lines, servings, title, page refs, confidence)
   │   ingest    │──────────────────────────────────────────────┐
   └─────────────┘                                              │
          ▼                                                     │
   ┌─────────────┐   IngredientLine → canonical ingredient_id, grams
   │  normalize  │◄──── catalog/ingredients.yaml (grows over time)
   └─────────────┘
          ▼
   ┌─────────────┐   per-100g nutrients per ingredient → per-serving macros
   │  nutrition  │◄──── USDA FDC (cached) │ Cronometer CSV override
   └─────────────┘
          ▼
   recipes/*.yaml  (status: ok | needs_review)
          │
          ▼
   ┌─────────────┐   targets, pantry, prices, history, locks, lean dial
   │    plan     │──► plans/2026-W43.yaml
   └─────────────┘
          ▼
   ┌─────────────┐
   │   grocery   │──► groceries.md │ todoist.csv │ text
   └─────────────┘
          ▼
   ┌─────────────┐
   │   pantry    │◄── "bought" / "cooked" events → pantry.yaml, history.yaml
   └─────────────┘
```

Every stage reads and writes files in the data folder. Stages are independently runnable and idempotent: re-running `nutrition refresh` on an unchanged recipe changes nothing.

---

## 2. Internal conventions

- **Mass in grams, volume in millilitres, energy in kcal.** Convert at the edges only.
- **IDs are slugs** (`chicken-thigh-bone-in`, `my-book/skillet-lemon-chicken`). Stable, human-readable, used as filenames.
- **Every derived value records provenance**: `source` (e.g. `fdc`, `cronometer`, `llm`, `user`) and `confidence` (0–1) where applicable.
- **User edits win.** A field marked `locked: true` (or any value with `source: user`) is never overwritten by automated re-runs.

---

## 3. Data model (YAML on disk, pydantic in code)

### 3.1 Recipe — `recipes/<book>/<slug>.yaml`

```yaml
id: my-book/skillet-lemon-chicken
title: Skillet Lemon Chicken Thighs
source:
  kind: cookbook            # cookbook | url | text | manual
  book: my-book             # slug from bourdain.toml [books]
  page: 123                 # page ref so you can cook from the book
  url: null
  images: [raw/2026-10-21/IMG_0412.jpg]
servings: 4
time: {active_min: 20, total_min: 45}
tags: [dinner, chicken, weeknight, one-pan]   # LLM-suggested, user-editable
protein: chicken                              # main protein, used for variety
cuisine: mediterranean
keeps_days: 3                                 # leftovers safe for N days
freezes: false
ingredients:
  - raw: "2 lb bone-in, skin-on chicken thighs"
    qty: 2
    unit: lb
    item: chicken thighs, bone-in, skin-on
    prep: null
    optional: false
    group: null                # e.g. "For the sauce"
    ingredient_id: chicken-thigh-bone-in
    grams: 907
    match_confidence: 0.96
  - raw: "Salt and pepper to taste"
    qty: null
    unit: null
    item: salt and pepper
    ingredient_id: salt
    grams: 0                   # "to taste" → 0 g, treated as staple
    match_confidence: 1.0
steps: [...]                   # optional; see ingest.store_steps
nutrition:
  per_serving: {kcal: 512, protein_g: 38.1, fat_g: 34.0, carb_g: 9.2, fiber_g: 1.1}
  source: fdc                  # fdc | cronometer | user
  coverage: 0.98               # fraction of recipe grams with known nutrients
  computed_at: 2026-10-21T19:04:00
review:
  status: needs_review         # ok | needs_review
  flags: [low_match:shallot, missing_servings_guess]
```

### 3.2 Ingredient catalog — `catalog/ingredients.yaml`

The single most valuable file. Each entry is created once (mostly automatically) and reused by every recipe.

```yaml
chicken-thigh-bone-in:
  name: chicken thigh, bone-in, skin-on, raw
  aliases: [chicken thighs, bone-in chicken thighs, skin-on thighs]
  fdc_id: <fdc id>              # USDA FoodData Central id
  fdc_description: "..."        # cached for review
  edible_fraction: 0.72         # bones; nutrients apply to edible part
  density_g_per_ml: null        # needed for cup/tbsp → g
  unit_weights: {each: 170}     # "1 thigh", "1 medium onion", "1 clove"
  staple: false                 # staples (salt, oil, spices) never hit the list by default
  perishable: true
  shelf_life_days: 3            # fridge, from purchase
  buy:
    unit: lb
    package_g: [454, 907]       # sizes you actually see at your store
    section: meat               # grocery list grouping
    price_per_package: null     # filled from prices.yaml
```

### 3.3 Pantry — `pantry.yaml`

Two precision levels, because nobody weighs their rice:

```yaml
items:
  - ingredient_id: rice-jasmine
    status: have              # have | low | out   (coarse, for staples/dry goods)
  - ingredient_id: cilantro
    qty_g: 60                 # precise, auto-filled from "bought" events
    acquired: 2026-10-18
    expires: 2026-10-25       # from shelf_life_days unless overridden
```

Coarse items count as "available, assume enough" (`have`), "available, add to list as optional" (`low`), or "not available" (`out`).

### 3.4 Targets & household — `targets.yaml`

```yaml
planned_slots: [lunch, dinner]          # breakfast etc. handled as fixed foods
days: [mon, tue, wed, thu, fri, sat, sun]
eaters:
  - name: me
    servings_per_slot: 1.0
    daily:                               # [min, max]; null = unbounded
      kcal: [2200, 2500]
      protein_g: [150, null]
      fat_g: [null, 95]
      carb_g: [null, null]
      fiber_g: [30, null]
    fixed_daily:                         # eaten every day outside the plan
      kcal: 450
      protein_g: 40
  - name: partner
    servings_per_slot: 0.8
    daily: null                          # eats the plan, no constraints
macro_mode: daily                        # daily | weekly_average (looser, easier to satisfy)
```

### 3.5 Plan — `plans/<iso-week>.yaml`

```yaml
week: 2026-W43
lean: 0.2
cook_events:            # what you actually cook
  - {recipe: my-book/chana-masala, day: mon, slot: dinner, batches: 1}
meals:                  # what you eat, incl. leftovers
  - {day: mon, slot: dinner, recipe: my-book/chana-masala, leftover: false}
  - {day: tue, slot: lunch,  recipe: my-book/chana-masala, leftover: true}
locks:
  - {day: fri, slot: dinner, value: out}
daily_macros: {mon: {kcal: 2340, protein_g: 152, ...}, ...}
objective_breakdown: {cost: 61.20, waste_g_perishable: 140, variety_penalty: 3, macro_slack: 0}
```

### 3.6 History — `history.yaml`

Append-only log of `cooked` events (date, recipe, batches, optional rating 1–5). Drives "not recently" variety and lets the optimizer favor recipes you rated well.

```yaml
- date: 2026-10-21
  recipe: my-book/chana-masala
  batches: 1
  rating: 4
```

### 3.7 Drafts and first-workflow clarifications

`RecipeDraft` has `title`, nullable `servings`, `source`, `time`, `ingredients`,
`steps`, `tags`, a field-name → confidence map, and review `flags`. An ingredient
retains its verbatim `raw` text. Unknown servings stay `null`; manual planning
requires the user to supply servings first. Unknown nutrient values also stay
`null`, including in `Macros` (sodium is represented by `sodium_mg`).

Ingredients additionally support `qty_range: [min, max]` and `flags`. Ranges have
`qty: null` until reviewed. Parenthetical weights are authoritative totals for
volume measurements and per-package weights for `can`/`package` counts. Volume
units use US customary cup/tbsp/tsp values. Densities and unit weights are never
invented. Catalog stubs have `source: parser`, `confidence: 0`, and keep triggering
review until corrected. No perishability, edible fraction, or density is guessed.

Catalog items, ingredient lines, and nutrition objects accept `source`,
`confidence`, and a strict boolean `locked`. Recipes also accept `locked`,
`excluded`, `extraction_reviewed` and `user_fields` (a list of protected recipe
field names). Whole-object markers protect the object, including replacement or
deletion via an ancestor. Automated edits conservatively preserve a whole list
when it contains a protected object. Explicit user edits may opt out of protection.
`user_fields` protects selected scalar fields without blocking independent stages
such as nutrition refresh. Automated writers cannot remove that protection list.
Catalog entries also support `user_fields`. Confirming a count weight protects
`unit_weights` (and `buy` when chosen), preserving other unreviewed provenance.
`unit_weight_sources` optionally records a confirmed USDA source per count unit:
`source: fdc`, `fdc_id`, food `description`, `portion_id`, `portion_description`,
`amount`, `portion_grams` and `edible_fraction`. It is protected together with
`unit_weights`. The saved count weight equals
`portion_grams / amount / edible_fraction`; this snapshot is not an instruction
to refetch or silently update accepted values. Model-generated catalog proposals
cannot supply source records or mark their own fields as user-confirmed.

Manual cook events may use fractional `batches`: household servings divided by
recipe yield. Each assigned meal is a fresh cook event; explicit leftover
allocation remains optimizer work. The grocery list consumes only cook events.
Plans additionally support `package_counts`, `package_sizes_g`, `package_purchases`,
`pantry_usage_g` and `ingredient_needs_g` keyed by catalog ID. `pantry_fingerprint`
records the
relevant inventory; changed stock or recipe quantities require replanning before
export. `eater_daily_macros` contains every eater; `daily_macros` retains the first
eater's values for compatibility. `macro_slack`, `warnings`, `explanations`,
`solver_status` and nullable `solver_gap` expose solve quality. `Meal.cook_day`
identifies the source cook event. `replan_from`, `recipe_servings` and
`household_servings` preserve the assumptions needed for mid-week replanning.

`package_purchases` stores the exact selected mix as a list of
`{package_g, count, unit_price, currency}` per ingredient. Counts are positive,
sizes unique, and prices/currency are snapshots from planning time. An empty
list records zero purchases. `package_counts` is the total number of packages
across sizes. `package_sizes_g` is retained for a single size and omitted for
mixed purchases; it never holds an average package size. Legacy plans without
`package_purchases` still use their original count/size pair. Validation rejects
disagreement between the old summary fields and the detailed purchase mix.
Purchases may also carry `unit` (e.g. `medium`), a confirmed count label for that
exact package weight. This is a snapshot; a mixed bag is never relabeled as one
onion. `RecipeSource.kind` additionally accepts `epub`, with `document` pointing
to the archived original and `section` identifying its chapter/recipe.

The example ellipses in §3 are illustrative, not literal schema values. Executable
fixtures replace them with complete values; the unknown example FDC ID is `null`.
All IDs are validated slugs and paths are checked to stay inside the data folder.
Unknown model fields are rejected to catch editing mistakes.

No-op YAML saves preserve bytes exactly. Changed fields are patched into the
round-trip document so comments, quotes, and key order survive. Writes publish a
flushed temporary file atomically. A document changed since loading is rejected
before saving. `init --force` creates missing files only and never replaces edits.

Multi-file pantry/history changes use `store.transaction`: an OS lock, a durable
`.transaction.json` journal and atomic file replacement. Opening a repository
rolls an interrupted transaction forward. Unexpected concurrent edits cause an
error instead of overwriting data; the journal is retained for recovery.

### 3.8 Nutrition mappings, snapshots and prices

`catalog/fdc.yaml` maps ingredient IDs to a proposed or confirmed `fdc_id`,
`description`, `data_type`, `flags`, `source`, `confidence` and `locked`. A sidecar
keeps automated proposals separate from user-protected catalog entries. Explicit
confirmation also fills the catalog's legacy FDC fields. An existing catalog ID
remains usable without a sidecar. `nutrition/fdc/<recipe-id>.yaml` retains computed
FDC nutrition even when active recipe nutrition is a locked Cronometer override.

`prices.yaml` maps each ingredient ID to a list of package prices:

```yaml
carrot:
  - {package_g: 1000, price: 2.50, currency: USD, source: user}
```

The optimizer considers every maintained package price and may buy a combination
of sizes. It minimizes the configured cost/waste objective for actual ingredient
needs after pantry use; the lowest price per gram does not always yield the
cheapest basket. Duplicate size entries use their lowest maintained price.
When no maintained prices exist, all catalog sizes receive visible per-section
estimates; an unknown size uses a warned 1 g bulk unit. Maintained price entries
define the available options when present. Mixed currencies require correction.
The library's `quote()` helper still returns one unit-price estimate;
`package_options()` exposes all choices used by the optimizer.

`Pantry.applied_purchases` maps purchase IDs to recorded grams. `HistoryEvent`
accepts an optional `event_id` for retry-safe explicit cooking operations.

### 3.9 Prepared freezer inventory — `freezer.yaml`

`Freezer.items` contains unique `FreezerLot`s: `id`, `recipe`, remaining `servings`,
`frozen_on`, explicitly chosen `expires`, `recipe_servings` and a
`recipe_signature`. The signature records the ingredient quantities/IDs, optional
flags, yield and nutrition used when frozen; timestamp/provenance-only updates do
not invalidate it. Changed quantities or nutrients exclude a lot from planning
with a warning. No storage lifetime is inferred. Recipes must have `freezes: true`
and a known yield to add portions. Exhausted/expired lots are not scheduled.

`Freezer.consumed` maps retry IDs to lot/servings/date records. Plans store
`freezer_usage` for future servings and a `freezer_fingerprint`; each meal stores
`freezer_portions` per lot. `freezer_lot` is a convenience for single-lot meals.
Prepared meals have no `cook_day` and create no ingredient demand. Households may
combine portions of the same recipe from multiple lots. Existing data folders
without `freezer.yaml` behave as empty; only a mutation creates the missing file.

---

## 4. Modules

### 4.1 `ingest` — getting recipes in with minimal typing

All sources produce a `RecipeDraft`; normalization is a separate stage.

**Photos / PDF pages** (the main path for cookbooks)

1. Load images (JPEG/PNG/HEIC), auto-rotate from EXIF, downscale to ~1600 px long edge. PDFs: render pages with `pymupdf`.
2. **Group pages into recipes.** Many recipes span two pages, and one page can hold two recipes. Send pages in order with a small sliding window; the model returns, for each page, which recipe(s) it starts/continues. A user can force grouping by putting files in subfolders (`inbox/batch1/recipe-a/`).
3. **Extract** with a vision model using a strict JSON schema (tool use / structured output): title, servings/yield, times, ingredient lines verbatim + parsed (qty, unit, item, prep, optional, group), steps (optional), headnote tags, page number if visible, and a per-field confidence.
4. Keep the verbatim `raw` line always; parsed fields can be redone later without re-photographing.
5. Cache every model response keyed by image hash + prompt version, so re-runs are free and tests are deterministic.

The extractor sits behind:

```python
class RecipeExtractor(Protocol):
    def extract(self, pages: list[PageImage], hints: ExtractHints) -> list[RecipeDraft]: ...
```

Implementations: `AnthropicExtractor` (default; model name configurable in `bourdain.toml`), `FakeExtractor` (tests, reads fixtures).

**Implemented extraction contract.** `PageImage` holds the archived relative path,
one-based physical page number, dimensions, prepared JPEG bytes, content hash and
optional EXIF capture time. `ExtractHints` carries `book`, `store_steps`, a forced
group name and previously seen titles. Extractors also expose `cache_identity`,
including the model and prompt version/hash. The cache key includes ordered image
hashes/references and all hints; corrupt caches report an error rather than making
an unexpected paid retry. Only successful, validated responses are cached.

Drafts include `page_refs` identifying their actual input pages. References must
belong to the supplied pages. Persisted `source.images` holds archived paths;
`source.image_pages` maps each path to its physical PDF page indices. Printed book
numbers remain in `source.page`/`source.pages`. Original bytes are copied once,
under `raw/<date>/<sha256>.<extension>`, and reused across repeat imports. Prepared
API images omit EXIF/GPS metadata. PDFs accept up to 100 selected pages per file;
inputs are capped at 100 MB and images default to a 1600-pixel long edge.

Filename order is natural; EXIF order is explicit and requires timestamps on all
photos. Each directory defines a group, and overlapping two-page windows never
cross a group boundary. Same-title drafts merge only when their source references
overlap. Conflicting yields remain null and flagged. Disjoint same-title recipes
are rejected to prevent overwriting; import them with different book namespaces.
Subfolders containing multiple detected recipes retain a review flag.

`AnthropicExtractor` uses the SDK's structured `messages.parse` API with a versioned
prompt in `ingest/prompts/`. Transport/rate-limit/server errors retry with bounded
backoff; refusals, truncated output and invalid schemas do not become recipes.
Missing confidence is flagged. Model-derived catalog IDs and grams are discarded;
explicit weights in raw text are recovered by the deterministic parser. No API
keys or SDK error payloads are written to cache or shown in errors.

**Settings that reduce effort**

- `ingest.store_steps = false`: skip instructions entirely and keep the page reference — you cook from the book anyway. Fewer tokens, less to review.
- `ingest.book` default per inbox subfolder, so book/page metadata isn't typed per recipe.
- **Index-first mode**: photograph a book's index/table of contents, select recipe stubs, then photograph only the chosen printed pages (details below).

**URLs**: `recipe-scrapers` (schema.org Recipe), falling back to the LLM on page text.

**Text (first workflow)**: paste or file; a title, optional `Servings: N`, and one
ingredient per line. Optional `Ingredients:` and group headings are recognized;
`Instructions:` ends the ingredient list. Extraction is deterministic and uses
the rule-based parser. The LLM fallback remains later work. URL ingestion first
uses `recipe-scrapers` and schema.org metadata; missing metadata produces a clear
error and a suggestion to use text import until that fallback is implemented.

**EPUB**: `ingest.epub.extract_epub` reads the OCF container/package and XHTML
spine order without extracting ZIP members onto the filesystem or fetching remote
resources. It supports Recipe JSON-LD records (including graphs/multiple recipes)
and conservative titled XHTML sections with explicit Ingredients/Method headings.
Servings remain null when absent/ambiguous; ordinary prose never becomes a recipe
without an ingredient section. Unknown chapters are reported, duplicate recipe
titles rejected, and original bytes archived once with content hashes. Recipe
source references show the archive member and recipe ordinal during review.

Limits: 100 MB archive, 200 MB expanded, 5000 ZIP entries, 5 MB per read member,
500 spine chapters, 1000 recipes. Paths must resolve inside the archive; encrypted
text, XML entities and unsupported encodings fail explicitly. Font obfuscation
does not block unencrypted text. Image-only pages and layouts without recognizable
recipe structure need text/photo/PDF import. No LLM extraction is implied.

**Index-first workflow (P10.1).** `book_index.py` stores `BookIndex` documents in
`indexes/<book>.yaml`. Entries are separate `IndexStub`s, not incomplete Recipe
objects: title, printed recipe pages, source index-image references, confidence,
flags, selection and an optional explicit recipe link. Stable entry IDs hash
normalized title plus page numbers; repeated mentions merge by that identity.
Unknown pages/low confidence stay flagged. Index categories/cross references
are excluded by the prompt; accuracy of that distinction awaits real indexes.
`ingest index` reuses image preparation and groups at most two pages per request.
All extracted rows validate before the index document is changed. It never
guesses ingredient data, printed-to-PDF offsets or recipe servings.

`index select` explicitly confirms/corrects title and printed pages and protects
the entry from reimports. `index ls` lists stubs; `index queue` lists selected,
uncaptured entries and deduplicated printed pages to photograph. Entries count
as captured when a same-title recipe has ingredients, belongs to the same book
and overlaps the printed pages, or through an explicit same-book `index link`.
Capture does not imply review/nutrition readiness. Ordinary photo/PDF ingestion
remains the recipe import path. No index stub enters planning/statistics counts.

`ingest/documents.py` provides a typed `DocumentExtractor` boundary and a cached
Anthropic implementation shared by indexes and receipts. Prompt, response schema,
model and image references/content hashes determine the cache key. Page references
must belong to the actual inputs; unknown references and invalid responses are
not cached. Corrupt caches fail explicitly instead of causing a paid retry.
The existing recipe extractor shares only the bounded structured-image transport.
Credentials come from the environment; images sent to the API omit EXIF metadata.

### 4.2 `normalize` — ingredient lines → catalog + grams

For each `IngredientLine`:

1. **Parse** (already done by the extractor; a rule-based parser is the fallback for text/URL sources).
2. **Match** `item` to the catalog: exact alias → `rapidfuzz` token-set ratio over names+aliases → if best score < threshold, ask the LLM to pick from top-k candidates *or* propose a new catalog entry.
3. **New catalog entries** are created with LLM-proposed fields (name, aliases, staple, perishable, shelf life, section, unit weights, density) and a proposed FDC match (see 4.3), all with `source: llm` and flagged for one-time review.
4. **Convert to grams** with `pint`: mass units directly; volume via `density_g_per_ml`; counts via `unit_weights` (`each`, `clove`, `bunch`, `can`, `medium`, `large`…); "to taste"/"for serving" → 0 g and staple.
5. Record `match_confidence`; below threshold → recipe flag.

Learned matches are saved as aliases, so the same phrasing never needs review twice.

Count units include sizes and physical units such as each, clove, head, bulb,
stalk, can, jar, bottle and bag. `catalog unit-weight ID UNIT GRAMS [--buy]`
confirms ingredient-specific weights; `--buy` chooses `buy.unit`. When no package
sizes/prices exist, that confirmed count weight becomes a whole-item option in
the optimizer and manual grocery rounding. Unconfirmed model proposals are not
shopping counts. Ingredient amounts remain grams; count-based purchases display
their approximate per-item weight, round upward after pantry subtraction and
record an estimated-weight note when bought. Volume still requires density.

**Matching and review clarifications.** Exact names/aliases run first; fuzzy scores
use token-set ratio with `ingest.match_threshold` (0–100). Fuzzy matches always
request one confirmation because token subsets can conceal meaningful modifiers.
Scores within three points are ambiguous and leave the catalog ID unresolved.
Synonyms such as scallions/green onions or EVOO/olive oil are learned aliases,
not semantic knowledge supplied by fuzzy string similarity.

LLM fallback is explicitly enabled with ingest `--llm-match` (or an injected
`CatalogMatcher` in Python). Without it, unmatched ingredients become parser
stubs. The matcher selects one of at most five candidate IDs or proposes a new
catalog item. Proposals are validated and cached by model, prompt, ingredient and
candidate contents. New properties are marked `source: llm`; all model matches
are flagged. The matcher cannot change an existing catalog item or supply FDC IDs.
Suggested weights/densities remain visibly unreviewed until confirmed.

`review` displays only flagged ingredients/recipe fields using Rich panels and a
small terminal rendering of the source page. Accept confirms the displayed line
and catalog properties, requires a resolved ID and grams, learns the alias, and
sets `source: user`/`locked: true` on the line. An accepted alias takes precedence
over old unreviewed stubs; those stubs remain on disk to preserve other references.
Conflicts with confirmed aliases require an explicit catalog edit. Shared flags
remain until all affected lines have been reviewed. Recipe-field confirmation
sets `extraction_reviewed` and protects title, servings, source, time, steps and
tags via `user_fields`. Nutrition remains independently refreshable. Automated
re-import preserves protected ingredient lists and accepted extraction fields,
and retains nutrition review flags. Explicit review edits use the round-trip
store with `automated=False`. `review --nutrition` is a separate mapping session
so accepting extraction never clears nutrient uncertainty.

### 4.3 `nutrition`

**Default source for planning: USDA FoodData Central (FDC).** The active recipe
nutrition may instead be an explicit user/Cronometer override. The `FoodProvider`
interface supplies search and food details; `FDCClient` uses an environment-only
key, bounded retry/backoff and validated disk caches. Details live in
`.cache/fdc/<fdc_id>.json`, searches in `.cache/fdc/search/<query-hash>.json`.
Cache hits require no key; corrupt cache files raise an error. Keys are sent in
headers and excluded from URLs, cache content and error messages.

`Food.food_portions` retains the API's `foodPortions` records (ID, amount,
gramWeight, modifier, portionDescription and measureUnit). Missing portions stay
null, including in older caches; no source amount is assumed. `food(id,
refresh=True)` explicitly refetches details and atomically replaces the cache only
after response validation. Normal reads, nutrition calculations and confirmations
continue to reuse cached details.

`nutrition/portions.py` exposes `portion_food` and `use_portion`; the CLI presents
them as `catalog portions INGREDIENT [--refresh] [--json]` and
`catalog use-portion INGREDIENT PORTION_ID --unit medium [--buy] [--count N]
[--replace]`. A catalog FDC ID must already be confirmed. Listing never changes
catalog fields. Selection explicitly confirms a portion's food/preparation/size
and its count label; it requires a positive edible fraction and preserves the
source snapshot. USDA portions are edible weights, divided by count and the
catalog edible fraction to obtain recipe-basis grams. Missing source counts need
`--count`; a provided count must agree with a known source amount. Duplicate IDs,
invalid weights and mass/volume portions cannot become item counts. Existing
measurements require explicit replacement; matching retries are byte-stable.
No density or generic size alias is inferred. Changes to the catalog food ID,
edible fraction or sourced weight invalidate that count for new conversions and
shopping; existing recipe grams remain explicit saved values. Manual unit-weight
edits clear provenance for that unit. Live portion coverage and actual Cronometer
serving exports remain deferred acceptance checks.

Mapping ranks names/aliases and penalizes preparation mismatches such as
raw/cooked or salted/unsalted. Within comparable score bands it prefers
Foundation, SR Legacy, Survey and then Branded. Weak or close matches require
review. `nutrition map` proposes, `review --nutrition` confirms, and
`nutrition set ID FDC_ID --edible-fraction N` assigns explicitly. Food IDs and
edible fractions must be reviewed; edible fraction is never silently assumed 1.

Per recipe: sum `grams × edible_fraction × nutrient_per_g`, divided by servings.
Optional ingredients and explicit zero-gram lines are omitted. Missing values
remain null, including a macro missing from any contributing ingredient.
`coverage` is the fraction of nonoptional ingredient grams backed by all eight
nutrients, known edible fractions and accepted mappings; unknown grams make
coverage zero. Missing servings leave per-serving values null. Coverage below
0.95 adds `nutrition:*` review flags and prevents macro-constrained planning.
`--ignore-macros` bypasses nutrient requirements but still requires resolved
ingredient quantities and other review issues to be fixed.

FDC amounts are per 100 g. Energy preference is specific Atwater (2048), standard
kcal (1008), then general Atwater (2047); nutrient units are checked explicitly.
Refresh keeps an independent FDC snapshot, preserves timestamps on unchanged
results, and respects locked/user/Cronometer nutrition and recipe protection.
`nutrition diff` compares the snapshot with active Cronometer macros and reports
relative differences greater than 10%, using Cronometer as the denominator.

Nutrients carried: kcal, protein, fat, saturated fat, carbs, fiber, sugar, sodium. Micronutrients are out of scope (that's what Cronometer is for).

### 4.4 `cronometer` — integrating without an API

Facts this design is built on (checked October 2026):

- Cronometer offers no public API for individual users; API access goes to select partners/enterprise.
- Its "Import from JSON" for custom foods only accepts files previously exported from Cronometer; recipe JSON depends on Cronometer's internal food/measure IDs.
- Cronometer Gold's **Recipe Importer** accepts a URL *or pasted ingredient text* and matches ingredients itself.
- A custom recipe can be exported to CSV/JSON; per-ingredient nutrition can be obtained by adding the recipe to the diary, using "Explode Recipe", and exporting servings.

So Bourdain provides two commands:

- `bourdain cronometer export <recipe|--all-new>` → a paste-ready block per recipe (title, servings, one clean line per ingredient in grams, e.g. `907 g chicken thigh, bone-in, raw`). Grams + plain names give Cronometer's matcher the easiest job. Batch mode writes `cronometer_queue.md` with one section per recipe and copies the first to the clipboard; you paste, save in Cronometer, move on.
- `bourdain cronometer import-nutrition <export.csv> --basis per-serving` → explicitly make Cronometer's numbers canonical with `nutrition.source: cronometer` and lock them.

Paste blocks use edible grams and the confirmed FDC description when available;
unresolved grams, edible fractions or servings stop export. Export records content
hashes in `cronometer_exports.yaml`, so changed recipes reappear in `--all-new`.
Clipboard failure leaves the text available in the terminal/queue file.

CSV input currently supports wide named nutrient columns and vertical
`Nutrient,Amount,Unit` rows, with explicit g/mg/kJ conversion. The caller must
select `per-serving`, `full-recipe` or `per-100g`; the last requires a recipe
weight and one recipe. Names must match uniquely or the user supplies `--recipe`.
All rows are validated before writes, duplicate recipe records are rejected,
and missing nutrients remain null and flagged. These layouts have synthetic
fixture coverage; compatibility with an actual Cronometer export remains an
external acceptance check, alongside manually pasting three recipes.

Browser automation of Cronometer is explicitly **out of scope** (brittle, and likely against its terms). Revisit only if Cronometer ships a personal API.

> Assumption to confirm: the user has Cronometer Gold. Without it, the export still works for manual recipe entry, but the paste importer isn't available.

### 4.5 `plan` — the optimizer

A mixed-integer linear program built with PuLP, solved with HiGHS.

**Pre-filter** (keeps the model small and fast): recipes with resolved ingredients,
known yield, cleared review flags, nutrition coverage ≥ 0.95 and required macros,
tagged for a planned slot, not excluded; then top ~80 by pantry overlap, rating
and novelty. Scores and ties are deterministic. Pinned/locked recipes bypass the
size limit and slot-tag filter, but cannot bypass unresolved data or exclusions.

**Sets**
- `R` candidate recipes, `D` days, `S` planned slots, `T = D×S` meal times, `I` ingredients appearing in `R`.
- `T_lock` meal times fixed by the user (eating out, or a pinned recipe).

**Decision variables**
- `x[r,t] ∈ {0,1}` — recipe `r` is eaten at meal time `t`.
- `c[r,d] ∈ {0,1}` — recipe `r` is cooked on day `d`; `b[r,d] ∈ ℤ≥0` gives batches, bounded by `max_batches_per_cook` (default 4).
- `a[r,d,t] ∈ {0,1}` — meal `t` receives its household portions from cook day `d`, within the keep window.
- `p[i,k] ∈ ℤ≥0` — packages of ingredient `i` in available size/price option `k` to buy.
- `u[lot,d] ≥ 0` — grams of a pantry lot used on cook day `d`, after acquisition and before expiry, bounded by the lot's quantity.
- `y[r] ∈ {0,1}` — recipe `r` used at all this week.
- slack variables `s⁻[e,d,n], s⁺[e,d,n] ≥ 0` for each eater `e`, day `d`, nutrient `n`.

**Constraints**
1. *Every unlocked meal time gets exactly one recipe:* `Σ_r x[r,t] = 1`.
2. *Leftovers come from a cook event within its keep window:* servings eaten of `r` from day `d'` to `d' + keeps_days` ≤ `yield_r × c[r,d']` summed appropriately. Concretely: for each `r` and each day `d`, cumulative servings eaten through `d` ≤ cumulative servings cooked through `d`, and servings eaten on `d` must come from a cook within `keeps_days` (implement with per-cook-event assignment variables if the cumulative form is too loose).
   Servings per meal time = `Σ_e servings_per_slot[e]`.
3. *Ingredient need:* `need[i] = Σ_{r,d} qty_g[r,i] × b[r,d]`.
4. *Supply:* `u[i] + Σ_k package_g[i,k] × p[i,k] ≥ need[i]`, `u[i] ≤ pantry_g[i]`, `u[i] ≤ need[i]`. (`have` staples → `pantry_g = ∞`; `out` → 0.)
5. *Macros (per constrained eater, per day or weekly average):*
   `lo[e,n] − s⁻ ≤ fixed[e,n] + Σ_{r,t∈day} x[r,t] × spp[e] × macro[r,n] ≤ hi[e,n] + s⁺`.
6. *Variety hard limits:* `Σ_t x[r,t] ≤ max_meals_per_recipe` (interpolated from 6 to 2 with lean, unless configured); per main protein `≤ max_per_protein`; no recipe cooked twice in the week unless `allow_repeat_cook`.
7. *Link:* `x[r,t] ≤ y[r]`, `c[r,d] ≤ y[r]`.
8. *Locks and pins:* fixed `x` values; pinned recipe ⇒ `y[r] = 1`.
9. *Effort (optional):* at most `K` cook events per week; active minutes on each weekday ≤ budget. Unknown active time prevents cooking that recipe on a budgeted day.

Each new cook must serve a meal on its cook day. Batches are the minimum whole
number needed by their allocated portions, preventing extra cooking solely for
pantry credit. A later meal from that event is marked as a leftover even on the
same day. Supply usage cannot exceed ingredient need on that cook day. Coarse
stock assumes enough; `low` produces a check warning. Unrecorded staples are
assumed available, consistent with grocery omission. Optional ingredients are
excluded from the model.

**Objective (minimize)**

```
  w_cost    × Σ_i,k price[i,k] × p[i,k]                      # money spent
+ w_waste   × Σ_i perishable[i] × leftover_g[i] / scale     # bought-but-unused perishables
− w_pantry  × Σ_i urgency[i] × u[i] / scale                # use up what you have, expiring first
+ w_macro   × Σ slack                                       # macro deviation (kept large)
+ w_repeat  × Σ_r (meals_of_r − 1)⁺                         # eating the same thing again
− w_novel   × Σ_r novelty[r] × y[r]                         # not cooked in last N weeks
− w_distinct× Σ_r y[r]                                      # number of distinct recipes
+ w_cooks   × Σ c                                           # cooking effort
− w_rating  × Σ_r rating[r] × y[r]
```

where `leftover_g[i] = Σ_k package_g[i,k]×p[i,k] + u[i] − need[i]` (non-negative by constraint 4), `urgency[i]` rises as the pantry item nears `expires`, and `novelty[r]` is days since last cooked, capped.

**The "lean" dial.** One user-facing number `lean ∈ [0,1]`:

| | lean = 0 (thrifty) | lean = 0.5 | lean = 1 (variety) |
|---|---|---|---|
| `w_cost`, `w_waste`, `w_pantry` | high | medium | low |
| `w_repeat`, `w_novel`, `w_distinct` | ~0 | medium | high |
| `max_meals_per_recipe` | 6 | 4 | 2 |
| `w_cooks` | high (batch cook) | medium | low |

Weights interpolate linearly between the presets in `plan/presets.py`; all except
macro priority may be overridden. Optimization runs in two stages: minimize
slack normalized by each target bound, then optimize the preference objective
without worsening the achieved fit. Macro priority remains fixed at 10000 for
reporting. Raw deviations and achieved values are exposed in nutrient units.
The time budget is shared across stages, with a 1% relative gap in the preference
stage. If that stage cannot return a usable incumbent, retain the first stage's
feasible solution and warn. A time-limited macro incumbent is explicitly labeled;
optimal nutrient fit is not claimed without proof.

Because packages are integers, thrifty mode naturally clusters recipes that share a perishable (the rest of the cilantro bunch, the other half of the cabbage) — this is the main mechanism for saving money, not just raw prices.

**Re-planning mid-week:** `bourdain plan --replan-from wed` freezes earlier meals,
out locks and cook events, retains their remaining leftovers, and solves future
days from current pantry stock. Past ingredient purchases are excluded. Changing
household portions or an already-cooked recipe's yield requires correction before
replanning. Previous locks remain unless explicitly replaced. Plan saves otherwise
refuse an existing week; `--compare 0,0.5,1` never writes a plan.

**Output & explainability:** the plan includes per-eater macros, objective terms,
solver status/gap, macro deviations, exclusions and selection reasons. `--explain`
prints pantry overlap and protein categories. On proven infeasibility a separate,
bounded diagnostic solve identifies conflicting constraint families (such as cook
budget, repeats, protein caps, locks or pins). Its relaxed result is never saved as
a plan. Timeouts without an incumbent are reported separately from infeasibility.

**Performance target:** ≤ 10 s solve for 80 candidates × 14 meal times on a laptop; use a time limit + MIP gap of 1% and return the incumbent.

**Freezer allocation:** a meal may instead draw its household servings from
recorded lots of that recipe. Continuous per-lot serving variables are bounded
by remaining inventory and explicit availability dates; their sum equals the
household portions times a binary freezer-meal choice. The meal selects either
prepared portions or a fresh cook source. Existing freezer recipes still pass
normal eligibility checks and are retained outside the candidate-size cutoff.
Earlier-expiring portions receive a bonus under the pantry-use weight. Nutrition
priority remains the first solve stage. Planning never consumes inventory.

Replanning preserves past freezer allocations, while future decisions use current
balances. Grocery export verifies the referenced freezer lots/signatures as well
as pantry stock. Additional batches for a future freezer inventory are explicitly
logged with `cooked`; the optimizer never invents unrecorded prepared portions.

### 4.6 `grocery`

1. `need[i]` from the plan's cook events, minus pantry use `u[i]`.
2. Use the solver's `p[i,k]` decisions (so the list matches the saved package mix).
3. Staples are omitted; staples with `status: low` appear in an "Also check" section.
4. Group by `buy.section`, sort sections by a store order in `bourdain.toml`.
5. Each line: human quantity + context, e.g. `- [ ] Cilantro — 1 bunch (chana masala, fish tacos)`.

**Exporters** (pluggable `Exporter` interface):
- `md` — GitHub/Obsidian-style `- [ ]` checklist (default).
- `todoist-csv` — Todoist's CSV import format, one section per store section.
- `text` — one item per line; pasting multiple lines into Apple Reminders creates one reminder per line.
- `push todoist` — one-way API snapshot push; Microsoft To Do remains a future adapter.

Manual plans use `--add "mon dinner=book/recipe"` or `plan --manual`, which opens
an editor with a YAML mapping from meal times to recipe IDs (or `out`). The
template is validated before saving a normal Plan. Existing plan files are not
overwritten. A missing ingredient mapping or unresolved non-staple weight stops
export with the recipe and raw line to fix. Optional ingredients require explicit
opt-in. Precise pantry quantities are subtracted once after aggregation; coarse
`have` means enough and coarse `low` appears in “Also check”. Expired quantities
are ignored with a warning. Manual lists choose the package size yielding least
surplus, then fewest packages. Optimized lists use exact saved package decisions,
including zero purchases; changed pantry fingerprints, recipe needs or insufficient
remaining stock stop export and require a replan. Optional-ingredient opt-in is
limited to manual plans because optimized quantities exclude them.

All formats display mixed purchases explicitly, for example
`1 × 250 g package + 1 × 1000 g package`. Markdown purchase lines carry a hidden
JSON comment containing ingredient ID, purchase ID (`week:ingredient`) and total
purchased grams. Visible quantities must sum to the metadata total. `bought`
accepts mixed-size lines with or without metadata and adds one inventory lot for
their total grams. Plain text and Todoist exports omit metadata.

The Todoist exporter uses the documented TYPE/CONTENT/DESCRIPTION/PRIORITY/INDENT
columns, lowercase `section`/`task` rows, priority 4, and indent 1. Format reference:
https://www.todoist.com/help/articles/208821185-Project-templates . A manual import
is still required before claiming that acceptance criterion is complete.

**Direct Todoist push (P10.3).** `grocery/todoist.py` exposes an injectable
`TodoistProvider`, `TodoistClient` and `push_groceries`. The CLI requires explicit
project/week and supports `--dry-run`, `--batch` and `--include-optional` (subject
to the usual plan constraints). Preview reads the saved plan and produces task
payloads without credentials, writes or requests. Each task carries its week,
store section, existing quantity/context label, notes and warnings.

An immutable snapshot is stored under `exports/todoist/<identity-hash>.yaml`, keyed
by project/week/batch. Persist command UUIDs and temporary IDs before sending;
Todoist API v1 `/sync` accepts at most 100 `item_add` commands per request. A process
lock guards staging, send and acknowledgment. Save individual successful commands
before reporting partial failures; resume only pending commands using the same
UUIDs. A lost local acknowledgment is safe to retry under Todoist's documented
command-idempotency contract. Keep state files; removing them loses deduplication.
Failures use sanitized messages and bounded retry/backoff. `TODOIST_API_TOKEN`
is read only from the environment and sent only in an authorization header.

Changed snapshots require a new explicit batch after reviewing existing tasks;
this includes a changed-to-empty list. This adapter does not update/delete remote
tasks, reconcile manual edits/completions, or record pantry purchases. Existing
`bought` remains explicit. Real account/project acceptance is still deferred.

### 4.7 `pantry`

- `bourdain bought <list.md>` — checked items (`- [x]`) are added to the pantry with `qty_g = package_g × count`, `acquired = today`, `expires = today + shelf_life_days`.
- `bourdain cooked <recipe> [--batches N] [--rating 4]` — deducts ingredient grams from precise pantry items (floored at 0), appends to history.
- `bourdain pantry set <ingredient> have|low|out` and `bourdain pantry edit` (opens YAML in `$EDITOR`).
- Expired stock is ignored by planning, groceries and cooking, and labeled by `pantry ls`. Read-only planning does not rewrite protected inventory.

`pantry set` replaces all lots for an ingredient; `bought` adds a dated purchase
lot. Replaying the same purchase ID is a no-op; conflicting quantities error.
Use `--purchase-id` for a second shopping trip. Handwritten checked lines need
unambiguous catalog names and explicit grams/package quantities. Coarse check
items do not imply a purchase amount. `cooked` consumes earliest-expiring lots
first, reports any unrecorded balance, and leaves unweighed stock coarse. Each
normal invocation records a new event; `--event-id` makes retries idempotent.
Pantry/history are committed together through the recovery journal.

`cooked --freeze-servings N --freezer-expires DATE` also stages freezer portions
in that transaction, bounded by the actual cooked yield. `HistoryEvent` records
`freeze_servings` and `freezer_expires` so retry details must agree. `freezer add`
records previously cooked food without another raw-ingredient deduction;
`freezer eat LOT SERVINGS --event-id ID` consumes it once. Explicit IDs enable
retries; ordinary calls create new events. `freezer ls` displays balances/expiry.

### 4.8 `stats`

`summarize(repo)` and `bourdain stats [--json]` report review backlog, flags per
recipe, complete eight-nutrient profiles, mean coverage, missing gram weights,
unmatched lines, expired pantry entries, dated freezer portions and per-book
counts. Planning readiness uses the same eligibility filter with current targets,
without candidate truncation or a solver call. It does not prove a feasible week.
Statistics do not mutate the library or create missing optional sidecars.

---

### 4.9 Receipt price proposals (P10.2)

`receipts.py` imports one receipt photo or up to ten selected PDF pages through
`DocumentExtractor`. `receipts/<source-hash>.yaml` retains store/date (when known),
input page references, warnings and numbered rows. Each row preserves observed
raw text, description, per-package mass, package quantity, final line total,
currency, confidence and discount uncertainty. Missing values stay null. Exact
catalog names/aliases or unambiguous fuzzy candidates supply suggestions only.
Repeated item rows remain separate. Reimporting identical archived input reuses
the stored receipt, preserving review decisions without another API call.

`prices receipt`, `receipts`, `review`, `accept` and `ignore` expose that workflow.
Acceptance explicitly confirms/corrects the catalog identity and all price inputs.
It requires positive package mass/quantity, a nonnegative line total and a currency
matching configured prices. Unknown currency (including ambiguous `$`) is not
assumed. Discount ambiguity requires explicit resolution; no inferred count/volume
weights or automatic allocation of basket discounts. Existing package prices need
`--replace`. Saved price equals line total / package quantity, with `source: user`,
`locked: true` and optional `receipt_ref: <receipt-id>/<row-id>` on `PackagePrice`.
Other package sizes are preserved. Manual `prices set` clears that provenance.

Price and row acceptance commit together using the existing recovery journal.
An identical acceptance retry is a no-op even after a newer manual price edit;
conflicting retries error. Ignored rows cannot be accepted. Extraction, review
and confirmation never update pantry stock. Raw source remains archived for
inspection. Offline fixtures exercise mechanics, not real receipt OCR accuracy.

### 4.10 Library health check (P11.1)

`doctor.diagnose(root)` and `doctor [--json]` inventory ordinary files without
following symlinks or junctions, validate core and optional schemas independently,
and check recipe/plan/index/receipt identities, catalog/recipe/source links,
receipt price provenance and Todoist acknowledgment IDs. Missing optional sidecars
remain absent. Stale USDA conversions, freezer signatures and plans that cannot
produce current groceries are warnings, including historical plans whose stock
has since changed. Broken references, invalid files/configuration and an unavailable
local HiGHS installation are errors. Review backlog remains the role of `stats`.

Diagnostics never construct `Repository`, recover transactions, create a lock,
solve a plan or contact a provider. A pending `.transaction.json` is reported as an
error without modification. Credentials appear only as presence booleans; missing
optional keys do not fail the check. Exit status is 0 without errors, 1 with
findings at error severity (JSON includes `ok`). An unreadable file does not stop
inspection of the others. This is a point-in-time inspection; pause writers for a
consistent report. It does not certify nutrition/OCR accuracy or remote services.

### 4.11 Backup and restore (P11.2)

`backup.create_backup(root, archive)` / `backup ARCHIVE` take a streamed ZIP
snapshot outside the data folder. `manifest.json` contains format
`bourdain-backup`, version 1, UTC creation time and each relative file's byte count
and SHA-256; payloads live under `data/`. All ordinary files are included except
`.cache`, `.git`, `__pycache__`, `.transaction.lock` and `.env*` at any depth.
Preserve comments, sources, receipt decisions, purchase/cooking IDs, freezer
consumption, Cronometer export hashes and Todoist UUID/acknowledgment state exactly.
Do not parse/reserialize data or read environment credentials into the archive.

Require the initialized core files. Reject pending transaction journals, links,
junctions, special files and nonportable/colliding names. Hold the transaction
lock while streaming and recheck file contents plus inventory before an exclusive
archive publish. Other writers must be paused; this detects ordinary concurrent
changes but is not a filesystem snapshot against arbitrary concurrent mutation.
The lock file may be created; no library documents are changed or recovered.

`restore_backup(archive, destination)` / `restore ARCHIVE DESTINATION` validate
version, exact entry membership, paths, regular-file types, sizes and hashes in a
temporary sibling directory before exclusively creating the destination. Reject
ZIP traversal, absolute/drive/backslash names, Windows reserved names, links,
case/parent collisions, duplicates, encrypted entries and missing/extra payloads.
Never overwrite an archive or an existing destination, even an empty directory.
Both output parents must exist. Limits: 100,000 files, 20 GiB uncompressed,
20 MiB manifest. Publication uses exclusive hard links like the YAML store.
If disk failure interrupts publication, a partial new destination may remain;
the command fails and a retry must choose a fresh destination. No repair or journal
replay occurs during restore. Empty directories, metadata and caches are omitted;
run `doctor` on the restored copy to assess semantic health separately from byte
integrity. Checksums detect corruption, not a maliciously rewritten manifest.

## 5. Configuration — `bourdain.toml`

Select the root by `--data` > `BOURDAIN_DATA` > `~/kitchen`, and read
`bourdain.toml` there. The selected root is authoritative: `[paths].data` is
accepted as descriptive metadata but cannot redirect writes to another folder.
No file is required to load defaults. Credentials are environment-only properties
and never appear in serialized settings; file-based key fields are rejected.
The model names below are configurable defaults from the planning spec; their
availability and accuracy still require the deferred live acceptance checks.

```toml
[paths]
data = "~/kitchen"

[llm]
provider = "anthropic"
model = "claude-sonnet-5-5"     # vision-capable; cheaper models for text-only steps
text_model = "claude-haiku-5-5"
# API key from ANTHROPIC_API_KEY env var — never in files

[fdc]
# key from FDC_API_KEY env var

[ingest]
store_steps = false
match_threshold = 85

[books]
my-book = "My Cookbook Title"

[grocery]
section_order = ["produce", "meat", "dairy", "bakery", "pantry", "frozen", "other"]

[plan]
lean = 0.3
time_limit_s = 20
candidate_limit = 80
allow_repeat_cook = false
max_batches_per_cook = 4
# max_meals_per_recipe = 4    # otherwise interpolated from lean
# max_cooks = 5
# weekday_active_minutes = 45

[plan.max_per_protein]
# chicken = 4

[plan.weights]
# cost = 5.0                 # available: cost, waste, pantry, repeat, novel,
#                           # distinct, cooks, rating; macro stays fixed

[prices]
currency = "USD"

[prices.section_per_kg]
# produce = 4.0              # override visible fallback estimates
```

---

## 6. Testing strategy

- **No network in tests.** LLM and FDC calls go through interfaces with fakes that read recorded fixtures in `tests/fixtures/`.
- **Golden files** for YAML round-trip, normalization (raw line → grams), grocery output.
- **Optimizer tests** use tiny hand-built worlds (4–6 recipes, 2 days) where the optimal answer is known, e.g. "with lean=0 and cilantro in pantry expiring tomorrow, the cilantro recipe is chosen".
- **Property tests** (`hypothesis`) for unit conversion and grocery aggregation (never lists negative amounts; total grams conserved).
- An opt-in `pytest -m live` suite that hits real APIs, skipped by default.

**Validation preparation (P11.3).** `docs/VALIDATION.md` maps every deferred
external criterion to inputs, commands and evidence; `VALIDATION_REPORT.md` is
an unfilled report template. `examples/validation/*.example.json` contain explicit
placeholders. The opt-in harness adds typed index, receipt, portion and 30-food
mapping manifests, rejects empty inputs and uses only pytest temporary folders.
Vision/text model overrides are environment variables for this harness alone.
No automatic Todoist delivery or Cronometer account access is part of these tests.
Manual account steps remain in the runbook; skipped tests remain pending.

`examples/benchmark_library.py` restores an explicit backup into a disposable
directory, requires 80 eligible candidates and 14 meal times, and records lean
0/0.5/1 planning elapsed time, status/gap, macro slack, settings and versions.
It never saves plans or calls a provider. Timing includes planning input reads
and excludes restore and grocery verification. Offline harness checks use fakes;
they are not evidence of real-image accuracy or real-library performance.

---

## 7. Household setup and deferred external validation

Implementation uses configurable defaults (one eater, dinner, no macro bounds,
USD) so these preferences do not block development. Resolve them when setting up
the real data folder. Do not infer personal targets from synthetic fixtures.

1. Cronometer Gold — confirmed? (affects 4.4)
2. Household: how many eaters, and does only one have macro targets?
3. Which slots are planned (dinner only? lunch + dinner?), and what is "fixed" (breakfast, shakes)?
4. Daily vs weekly-average macro targets.
5. Prices: maintain `prices.yaml` by hand or review receipt-photo price proposals?
6. Primary grocery checklist app (decides which exporter to polish first).

## References

- Cronometer custom recipes & recipe import: https://support.cronometer.com/hc/en-us/articles/360018510311
- Cronometer JSON import limitation (forum, CEO reply): https://forums.cronometer.com/discussion/comment/3110
- Cronometer API availability (forum thread): https://forums.cronometer.com/discussion/3365/any-chance-a-public-api-for-cronometer-will-be-available
- USDA FoodData Central API: https://fdc.nal.usda.gov/api-guide
- USDA portion/edible-weight documentation: https://fdc.nal.usda.gov/Foundation_Foods_Documentation/
- USDA API schema (FoodPortion): https://api.nal.usda.gov/fdc/v1/json-spec?api_key=DEMO_KEY
- Todoist API v1 Sync commands and UUID idempotency: https://developer.todoist.com/api/v1/
- EPUB package, container and reading order: https://www.w3.org/TR/epub-33/
- PuLP: https://coin-or.github.io/pulp/ · HiGHS: https://highs.dev
- recipe-scrapers: https://github.com/hhursev/recipe-scrapers
