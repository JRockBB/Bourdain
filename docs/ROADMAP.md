# Roadmap

Phased plan, broken into tasks sized for one coding-agent session each (roughly one PR). Each task lists what it depends on and how to know it's done. Work top to bottom; tasks within a phase can often run in parallel.

**Milestones that matter to the user**

| Milestone | After phase | What you can do |
|---|---|---|
| **M1 — Grocery list** | 2 | Type or paste a few recipes, pick a week by hand, get a checklist on your phone |
| **M2 — Cookbook in** | 4 | Photograph cookbook pages, review only what's flagged, recipes land in the library |
| **M3 — Macros** | 6 | Every recipe has per-serving macros; one paste per recipe gets it into Cronometer |
| **M4 — Optimized week** | 8 | `bourdain plan` builds a week that hits macro targets and uses the pantry |
| **M5 — The dial** | 9 | One setting slides from thrifty/use-what-I-have to variety |

---

## How to hand a task to a coding agent

Paste something like this, swapping in the task ID:

> Read `AGENTS.md`, `README.md` and `docs/ARCHITECTURE.md`, then implement task **P1.2** from `docs/ROADMAP.md`. Stay within its scope. Make the acceptance criteria pass, add tests, run `uv run pytest` and `uv run ruff check`, and tick the task's checkbox in the roadmap. If the spec is wrong or ambiguous, note it under "Spec changes" in your summary and update `ARCHITECTURE.md` in the same change.

---

## Phase 0 — Scaffold

- [ ] **P0.1 Project skeleton.**
  `uv init`, `pyproject.toml` (Python ≥ 3.12, deps from README tech stack, `bourdain` console script), `src/bourdain/` package with empty module folders from the README layout, `ruff` + `pytest` config, GitHub Actions CI running lint + tests.
  *Done when:* `uv run bourdain --help` prints a help screen; `uv run pytest` passes with one smoke test; CI is green.
  *Progress:* Scaffold implemented. Local CLI help, the offline smoke test, lint,
  formatting, and wheel/source builds pass on Python 3.12. GitHub Actions is
  configured for Windows and Linux; its first hosted run is still pending, so
  this checkbox remains open until CI is green.

- [x] **P0.2 Config.**
  `config.py`: load `bourdain.toml` from `--data`/`$BOURDAIN_DATA`/`~/kitchen`; pydantic settings model mirroring ARCHITECTURE §5; API keys only from env vars.
  *Done when:* defaults load with no file present; a malformed file gives a one-line readable error; test covers env-var precedence.
  *Depends on:* P0.1

## Phase 1 — Data model & storage

- [x] **P1.1 Pydantic models.**
  `models/`: `Recipe`, `IngredientLine`, `CatalogItem`, `PantryItem`, `Targets`, `Plan`, `HistoryEvent`, `RecipeDraft` exactly as ARCHITECTURE §3. Validation for slugs, non-negative quantities, ranges in targets.
  *Done when:* each example YAML in ARCHITECTURE §3 parses into its model (keep them as fixtures in `tests/fixtures/spec_examples/`).
  *Depends on:* P0.1

- [x] **P1.2 YAML store.**
  `store/`: read/write each file type with `ruamel.yaml` (preserves comments and key order so hand edits survive), atomic writes (temp file + rename), `bourdain init <path>` creating the data-folder layout and a commented `bourdain.toml`.
  *Done when:* round-trip of every fixture is byte-identical; `bourdain init` on an existing folder refuses unless `--force`; user comments in a recipe file survive a load/save.
  *Depends on:* P1.1

- [x] **P1.3 Locked/user fields.**
  Helper used by every automated writer: never overwrite a field whose value has `source: user` or `locked: true`.
  *Done when:* a test shows `nutrition refresh`-style updates skip locked fields.
  *Depends on:* P1.2

## Phase 2 — First useful output (M1)

Goal: a grocery list from a hand-picked week, before any photo or optimizer work.

- [x] **P2.1 Rule-based ingredient line parser.**
  `normalize/parse.py`: "1 1/2 cups (200 g) all-purpose flour, sifted" → qty, unit, item, prep, optional. Handle fractions, unicode fractions (½), ranges ("2–3"), parenthetical metric, "to taste", "plus more for…", "divided".
  *Done when:* ≥ 50 golden test lines pass (put them in `tests/fixtures/ingredient_lines.yaml`).
  *Depends on:* P1.1

- [x] **P2.2 Unit → grams (basic).**
  `normalize/units.py` with `pint`: mass directly; volume via catalog density; counts via `unit_weights`. Missing data → `grams: null` + flag, never a guess.
  *Done when:* property tests for round-trips; table tests for cups/tbsp/tsp/oz/lb/g/kg/ml/each/clove/can.
  *Depends on:* P2.1

- [x] **P2.3 Text & URL ingest.**
  `bourdain ingest text <file|->` and `bourdain ingest url <url>` → recipe YAML via `recipe-scrapers` and P2.1. Catalog matching at this stage is exact-alias only; unknown items create stub catalog entries flagged for review.
  *Done when:* recorded HTML fixtures for 3 sites produce correct recipes offline.
  *Depends on:* P1.2, P2.1

- [x] **P2.4 Manual plan + grocery list.**
  `bourdain plan --manual` opens a plan template in `$EDITOR` (or accepts `--add "mon dinner=<recipe>"`), scales recipes to servings, aggregates ingredients across recipes, subtracts pantry `have` staples, groups by section.
  *Done when:* a 3-recipe fixture week produces the expected golden `groceries.md`.
  *Depends on:* P2.2, P2.3

- [ ] **P2.5 Exporters.**
  `bourdain list --format md|text|todoist-csv`. Markdown `- [ ]` checklist; text one item per line (pastes into Apple Reminders as separate items); Todoist CSV with section rows.
  *Done when:* golden-file tests per format; Todoist CSV verified by importing once by hand (note the result in the PR).
  *Progress:* All three exporters are implemented and covered by golden tests.
  Todoist's documented CSV columns and section rows are used. The manual import
  acceptance check is pending; this checkbox stays open until it is verified.
  *Depends on:* P2.4

## Phase 3 — Photo & PDF ingest

- [x] **P3.1 Image loading.**
  `ingest/images.py`: JPEG/PNG/HEIC (`pillow-heif`), EXIF auto-rotate, downscale, content hash; PDF page rendering via `pymupdf`. Originals copied to `raw/<date>/`.
  *Done when:* fixtures of each type load; rotated photo comes out upright.
  *Depends on:* P0.1

- [x] **P3.2 Extractor interface + fake.**
  `RecipeExtractor` protocol (ARCHITECTURE §4.1), `FakeExtractor` reading fixture JSON, response cache keyed by image hash + prompt version.
  *Done when:* pipeline tests run end-to-end with the fake and no network.
  *Depends on:* P1.1

- [ ] **P3.3 Anthropic extractor.**
  Vision call with a strict JSON schema (tool use / structured output) returning `RecipeDraft`s with per-field confidence; prompt stored in `ingest/prompts/` with a version string; model name from config. Retries with backoff; clear error if `ANTHROPIC_API_KEY` is missing.
  *Done when:* `pytest -m live` extracts 3 real fixture pages (two-page recipe, one page with two recipes, a handwritten note) correctly; recorded responses saved as fixtures for the offline suite.
  *Progress:* Structured extractor, versioned prompt, safe retries, validation,
  confidence flags and cache are implemented and fake-client tests pass. A live
  manifest-driven check is available; real pages, credentials and captured live
  responses are still needed. Hand-authored fixtures are explicitly labeled.
  *Depends on:* P3.1, P3.2

- [x] **P3.4 Page grouping.**
  Batch `bourdain ingest photos <dir>`: order by filename/EXIF time, sliding window so recipes spanning pages are merged and multi-recipe pages are split; subfolders force grouping; `--book <slug>` sets source for the batch.
  *Done when:* fixture batch of 7 pages → 5 recipes with correct page ranges.
  *Depends on:* P3.3

## Phase 4 — Catalog matching & review (M2)

- [x] **P4.1 Fuzzy catalog matching.**
  `rapidfuzz` over names + aliases, threshold from config; confirmed matches saved as new aliases.
  *Done when:* golden tests for common variants ("scallions"/"green onions", "EVOO"/"olive oil").
  *Depends on:* P2.3

- [ ] **P4.2 LLM fallback for matching and new catalog entries.**
  Below threshold: LLM picks from top-k or proposes a new `CatalogItem` (aliases, staple, perishable, shelf life, section, unit weights, density). Everything it writes is `source: llm` and flagged.
  *Done when:* fake-LLM tests for both branches; live test on 20 unusual ingredients.
  *Progress:* Both proposal branches, cached Anthropic matching and `--llm-match`
  are implemented with offline tests. The opt-in 20-ingredient live check is
  implemented but has not run with credentials; this checkbox remains open.
  *Depends on:* P4.1, P3.2

- [x] **P4.3 `bourdain review`.**
  Interactive `rich` TUI that walks only flagged items, one at a time: shows the source image crop/page beside the parsed data; keys to accept, edit inline, pick another catalog match, or skip. Accepting clears the flag and teaches the catalog.
  *Done when:* scripted-input test drives a review session; reviewing a flagged ingredient once makes the same phrasing unflagged in a second recipe.
  *Depends on:* P4.2

- [x] **P4.4 Library commands.**
  `bourdain recipes ls|show|search|tag|exclude`, filters by tag/book/protein/status.
  *Done when:* table tests on a fixture library.
  *Depends on:* P1.2

## Phase 5 — Nutrition

- [ ] **P5.1 FDC client + cache.**
  Search and fetch by id, data-type preference (Foundation, SR Legacy, then Branded), on-disk cache, rate-limit handling, `FDC_API_KEY` from env.
  *Done when:* offline tests on recorded responses; live test fetches 5 foods.
  *Progress:* Typed client, validated caches, bounded retries and secret-safe errors
  are implemented with synthetic transport fixtures. A five-food manifest-driven
  live test is ready; live verification and captured responses remain deferred.
  *Depends on:* P0.2

- [ ] **P5.2 Catalog → FDC mapping.**
  For catalog items without `fdc_id`: search FDC, rank candidates (LLM tie-break allowed), store choice + description, flag if low confidence. Reviewable in `bourdain review`.
  *Done when:* 30 common ingredients map to sensible FDC entries (golden list).
  *Progress:* Ranking, preparation penalties, a protected mapping sidecar,
  explicit assignment and `review --nutrition` are implemented. Thirty synthetic
  name cases pass; real FDC IDs still need verification before closing this task.
  *Depends on:* P5.1, P4.3

- [x] **P5.3 Recipe macros.**
  `bourdain nutrition refresh [--recipe]`: per-serving kcal/protein/fat/sat fat/carb/fiber/sugar/sodium, `edible_fraction`, `coverage`; low coverage flags the recipe.
  *Done when:* 3 fixture recipes match hand-calculated values within 2%.
  *Verified:* Three hand-calculated cases, conservative missing-data handling,
  repeat-safe snapshots and protection of user/Cronometer overrides pass offline.
  *Depends on:* P5.2

## Phase 6 — Cronometer bridge (M3)

- [ ] **P6.1 Paste-block export.**
  `bourdain cronometer export <recipe>|--all-new`: title, servings, one line per ingredient in grams with plain names; batch mode writes `cronometer_queue.md` and copies the next block to the clipboard; tracks which recipes have been exported.
  *Done when:* golden output; manually pasting 3 blocks into Cronometer's Recipe Importer matches all ingredients (note results in PR — tune name wording if not).
  *Progress:* Golden paste output, edible grams, export hashes, queue file and
  optional clipboard copy are implemented. The three manual imports are deferred.
  *Depends on:* P5.3

- [ ] **P6.2 Import Cronometer nutrition.**
  `bourdain cronometer import-nutrition <csv>`: parse Cronometer's recipe CSV export, map to a recipe (by name, confirm if ambiguous), write `nutrition.source: cronometer` and lock.
  *Done when:* fixture CSV import works; locked values survive `nutrition refresh`.
  *Progress:* Conservative wide/vertical CSV readers, explicit serving basis,
  unit conversion, ambiguity checks and locked overrides pass synthetic tests.
  An actual Cronometer export still needs to confirm format compatibility, so
  this checkbox remains open.
  *Depends on:* P6.1, P1.3

- [x] **P6.3 Compare report.**
  `bourdain nutrition diff`: recipes where FDC and Cronometer differ by > 10% on any macro — usually a bad match worth reviewing.
  *Depends on:* P6.2

## Phase 7 — Pantry, prices & history

- [x] **P7.1 Pantry commands.**
  `bourdain pantry ls|set|edit`, coarse (`have/low/out`) and precise (`qty_g`, `expires`) items, expiry handling.
  *Depends on:* P1.2

- [x] **P7.2 Close the loop.**
  `bourdain bought <groceries.md>` (checked items → pantry with package grams and expiry) and `bourdain cooked <recipe> [--batches] [--rating]` (deduct, log history).
  *Done when:* bought → cooked → pantry quantities match a golden sequence.
  *Verified:* Purchase/cooking quantities, retries, expired lots, coarse inventory
  and recovery after a simulated split pantry/history write pass offline.
  *Depends on:* P7.1, P2.5

- [x] **P7.3 Prices.**
  `prices.yaml` per catalog item + package size; `bourdain prices set`; missing prices fall back to a per-section default with a warning so cost terms never silently zero out.
  *Depends on:* P1.2

## Phase 8 — Optimizer v1 (M4)

- [x] **P8.1 Candidate filter.**
  ARCHITECTURE §4.5 pre-filter with a scoring function; deterministic given inputs.
  *Depends on:* P5.3, P7.1

- [x] **P8.2 MILP core.**
  `plan/model.py`: variables and constraints 1–8 from §4.5 with PuLP + HiGHS; objective = cost + waste − pantry use + macro slack; time limit + 1% gap.
  *Done when:* tiny-world tests (§6) pass, including: macros satisfied when feasible; slack reported when not; leftovers never exceed keep window; locks respected.
  *Verified:* Tiny-world constraints, expiry-aware lot allocation, batch capacity,
  same-day leftovers, two eaters, weekly-average macros and timeout fallback pass.
  Macro fit is optimized first, then preferences without worsening achieved fit.
  *Depends on:* P8.1, P7.3

- [x] **P8.3 `bourdain plan` CLI + plan file.**
  Writes `plans/<iso-week>.yaml` with meals, cook events, daily macros, objective breakdown; `--lock`, `--pin`, `--exclude`; grocery list uses the solver's package counts.
  *Done when:* end-to-end fixture: library of 20 recipes → plan → list in under 10 s.
  *Verified:* The 20-recipe/14-meal fixture meets the 10-second check locally.
  CLI persistence and exact solver package quantities pass. The broader 80-recipe
  architecture performance target remains a real-library benchmark to run later.
  *Depends on:* P8.2, P2.5

- [x] **P8.4 Explain & infeasibility.**
  `--explain` per-recipe reasons; on infeasibility, report the binding constraint in plain language ("protein ≥ 150 g/day isn't reachable with lunch+dinner from these 34 recipes; best is 131 g").
  *Depends on:* P8.3

## Phase 9 — The lean dial (M5)

- [x] **P9.1 Variety terms.**
  Add repeat penalty, novelty (from history), distinct-recipe bonus, per-protein caps, rating term.
  *Depends on:* P8.2

- [x] **P9.2 Presets + `--lean`.**
  `plan/presets.py` thrifty and variety presets, linear interpolation, overridable weights in `bourdain.toml`.
  *Done when:* on a fixture world, lean=0 reuses a pantry perishable and buys fewer packages; lean=1 has ≥ N distinct recipes and no recipe more than twice; macros identical in both.
  *Depends on:* P9.1, P7.2

- [x] **P9.3 Compare mode.**
  `bourdain plan --compare 0,0.5,1`: side-by-side table of cost, distinct recipes, packages, perishable waste, macro fit — so the dial's effect is visible before committing.
  *Depends on:* P9.2

- [x] **P9.4 Mid-week replan.**
  `--replan-from <day>` freezes the past, uses current pantry.
  *Depends on:* P9.2

## Phase 10 — Later / nice to have

- [x] **P10.1 Index-first ingest.** Photograph an index/TOC, retain reviewable
  recipe stubs, select/correct entries and generate a printed-page capture queue.
  *Verified:* Offline tests cover source binding, cache reuse, unknown-page
  blocking, protected corrections on reimport, ordinary recipe capture detection,
  explicit links and CLI selection/queue JSON. Stubs stay outside planning.
  Real index/category extraction accuracy remains deferred.
- [x] **P10.2 Receipt photo price proposals.** Extract item rows and explicitly
  confirm package size, quantity, final paid total, currency and catalog identity.
  *Verified:* Offline tests cover repeated rows, no premature price/pantry writes,
  incomplete/discounted/foreign-currency review, protected-price replacement,
  retries preserving newer edits, CLI flow and recovery after an interrupted
  price/receipt commit. Real receipt OCR accuracy remains deferred.
- [x] **P10.3 Direct Todoist push.** Preview and push a saved grocery snapshot to
  an explicit project using persisted UUIDs and individual success tracking.
  *Verified:* Offline tests cover dry-run isolation, repeated pushes, changed and
  empty snapshots, explicit new batches, partial failures, lost acknowledgments,
  100-command chunking, sanitized retries, secret exclusion and CLI export.
  This is a one-way snapshot; account acceptance and a Microsoft To Do adapter
  remain deferred. Remote completion does not mutate pantry stock.
- [x] **P10.4 Multiple package sizes per ingredient in the MILP.**
  Optimize all maintained size/price options, persist the exact purchase mix and
  carry it through grocery exports and `bought`. Keep legacy single-size plans
  readable and preserve the macro-first objective.
  *Done when:* tiny-world cases choose the cheapest small pack or mixed purchase,
  pantry stock reduces the purchase correctly, saved plans round-trip, all three
  grocery formats show the mix and purchase retries preserve exact grams.
  *Verified:* Eight offline cases cover small/mixed/zero purchases, pantry
  subtraction, duplicate prices, schema consistency, saved-plan/CLI export,
  metadata mismatch rejection and repeat-safe purchase totals. The existing
  single-size planning and grocery regressions also pass.
- [x] **P10.5 Batch-cook / freezer portions across weeks.**
  Explicit freezer lots, add/eat/list commands, `cooked --freeze-servings`,
  optimizer allocation without duplicate ingredients, expiry/capacity checks,
  saved per-meal portions and replan support.
  *Verified:* Offline cases cover multi-lot household portions, exact grocery
  omission, current-stock replanning, changed-recipe exclusion, consumption retries
  and recovery of an interrupted pantry/history/freezer transaction. Freezing
  extra batches is explicitly recorded; future freezer stock is not inferred.
- [ ] Simple local web view (static HTML of the week + list) — only if the CLI proves annoying.
- [x] **P10.7 EPUB ingest without photos.**
  Import unencrypted structured Recipe metadata and clearly headed XHTML in spine
  order; archive originals and retain chapter references for review.
  *Verified:* Synthetic books cover multiple recipes, unknown yields, repeat
  import, source references, unsafe paths and encrypted text rejection. Real
  cookbook layouts remain an external validation check.
- [x] **P10.8 Library statistics.**
  `stats [--json]` reports backlog, coverage, missing quantities and target-based
  eligibility, with read-only empty-library and CLI checks.
- [x] **P10.9 Count-based shopping units.**
  Confirm ingredient weights with `catalog unit-weight --buy`; aggregate mass
  and count recipes, subtract pantry stock and round whole shopping items.
  *Verified:* A 170 g line plus one confirmed 150 g medium onion totals 320 g;
  manual and optimized shopping choose three onions (450 g estimated), exports
  show the count and `bought` records those grams. Unknown weights/densities stay
  unresolved. Catalog field protection preserves independent review provenance.
- [x] **P10.10 Reviewed USDA portion conversions.**
  Retain source portions in the FDC cache, list without accepting, and explicitly
  select count weights with food/portion provenance. Account for the confirmed
  edible fraction, preserve existing values, and support explicit cache refresh.
  *Verified:* Synthetic offline cases cover multi-item amounts, edible-weight
  adjustment, count rounding in groceries, missing amounts, invalid/volume
  rejection, protected provenance, stale food/fraction handling, replacement,
  byte-stable retries, old caches and failed-refresh preservation. Real USDA
  portion coverage and Cronometer serving-export compatibility remain deferred.

---

## Phase 11 — Validation readiness

- [x] **P11.1 Read-only library health check.** `doctor [--json]` checks schemas,
  identities, source and cross-file references, stale portion/freezer/plan data,
  configuration and local solver availability. Report pending transactions without
  recovering them; report credential presence only. Continue past damaged files.
  *Verified:* Seven offline cases cover missing/legacy libraries, multiple damaged
  files, secret-safe configuration output, stale data, cross-file/source links,
  unsafe paths, identity/retry errors and byte-stable pending-transaction inspection.
- [x] **P11.2 Portable backup and separate-folder restore.** Preserve original
  bytes, sources and retry/history records with checksums. Reject incomplete,
  unsafe or changing snapshots; never overwrite an archive or existing destination.
  *Verified:* Nineteen offline cases cover byte-preserving recovery, secret/cache
  exclusion, purchase history and Todoist deduplication after restore, pending and
  changing snapshots, output refusal, malformed archives, traversal, collisions,
  links, duplicates and CLI usage. No actual user library was accessed.
- [x] **P11.3 Live validation preparation.** Provide a complete acceptance runbook,
  input manifests and report template for APIs, account workflows, recovery and the
  80-recipe benchmark. Keep all live execution explicitly deferred.
  *Verified:* Runbook/report templates and six manifest templates are provided.
  Four additional opt-in API tests cover indexes, receipts, portions and mapping;
  the backup-based benchmark requires 80 eligible recipes and 14 meal times.
  Three offline harness cases verify unknown-value failure detection, input
  preflight and disposable benchmark isolation with a fake solver. Live accuracy,
  real account behavior, real-library timing and hosted CI remain pending.

## Estimated effort for the user (not the agents)

Rough expectations for the "don't spend hours on data entry" goal, to be validated in Phase 3–4:

- **Per recipe from a cookbook:** 1–2 phone photos; ~0–60 s of review if anything is flagged.
- **First ~30 recipes:** most review time goes into the catalog (new ingredients, FDC matches). Each ingredient is reviewed once.
- **After ~50 recipes:** most new recipes should need no review at all. Track `flagged per recipe` in `bourdain stats` to confirm.
- **Cronometer:** one paste + save per recipe, only for recipes you actually want in Cronometer.
- **Weekly:** `bourdain plan`, glance at the plan, `bourdain list`, check items off in the store, `bourdain bought`.
