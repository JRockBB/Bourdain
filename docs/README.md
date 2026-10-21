# Bourdain

A personal, local-first meal planning toolkit: cookbook → recipes → macros → optimized weekly plan → grocery checklist.

Built to replace Mealime (shut down 21 Oct 2026) for one household, with the cookbooks already on the shelf, and with as little hand data entry as possible.

> Naming: the project is **Bourdain**; the Python package, CLI command and config file are lowercase `bourdain` (`bourdain.toml`, `$BOURDAIN_DATA`).

Implementation status: the core pipeline through Phase 9 is implemented,
including imports/review, USDA nutrition, the Cronometer text/CSV bridge, pantry
and purchase tracking, prices, optimized plans, the lean dial and mid-week replan.
EPUB import, freezer inventory/planning, library statistics and confirmed
count-based shopping quantities are also implemented.
External accuracy/format checks remain pending; offline fixtures do not establish
live service compatibility. See the [working CLI guide](../README.md)
and [roadmap acceptance checks](ROADMAP.md) for the boundary between implemented
features and verified external integrations.

---

## What it does

| Stage | Input | Output |
|---|---|---|
| **1. Ingest** | Phone photos of cookbook pages, PDFs/ebooks, recipe URLs, pasted text | Structured recipe files (YAML), flagged for review only where extraction is uncertain |
| **2. Normalize** | Raw ingredient lines ("2 lb bone-in chicken thighs, trimmed") | Canonical ingredients with gram weights, linked to a growing personal ingredient catalog |
| **3. Nutrition** | Canonical ingredients | Per-serving macros (USDA FoodData Central), plus a paste-ready block for Cronometer's recipe importer and a way to pull Cronometer's numbers back in |
| **4. Plan** | Recipe library, macro targets, pantry, prices, history | A weekly plan from a mixed-integer optimizer, with a single dial from **"use what I have / cheapest"** to **"maximum variety"** |
| **5. Shop** | The plan minus the pantry | A grocery list grouped by store section, rounded to real package sizes, exported as a Markdown checklist / Todoist CSV / plain text for Reminders |
| **6. Close the loop** | "Bought it", "cooked it" | Pantry and history updated, so next week's plan knows what's left |

## What it is not

- Not a phone app. The phone is used for two things only: taking photos of pages, and checking off a grocery list in whatever to-do app you already use.
- Not a nutrition tracker. Cronometer stays the tracker; Bourdain feeds it.
- Not a recipe-sharing service. All data stays on your machine, in plain files you can read, edit and version with git.

## Design principles

1. **Plain files are the database.** Recipes, the ingredient catalog, pantry and targets are YAML in a data folder (separate from this code repo). Hand-editable, diffable, greppable.
2. **Review by exception.** Every automated step (photo extraction, ingredient matching, nutrient lookup) records a confidence and flags. You only look at what's flagged.
3. **Each ingredient is mapped once.** The catalog is the long-lived asset; after the first few dozen recipes, most new recipes need zero manual work.
4. **Useful early.** A grocery list from a hand-picked week ships before the optimizer does.
5. **No fragile integrations.** Cronometer has no public API for individual users, so Bourdain integrates through its supported import/export features rather than scraping. (See `docs/ARCHITECTURE.md` → Cronometer.)

## Tech stack

Python 3.12+, managed with `uv`.

- CLI: `typer` + `rich`
- Models & validation: `pydantic` v2
- YAML that survives hand edits: `ruamel.yaml` (round-trip mode)
- Units: `pint`
- Fuzzy matching: `rapidfuzz`
- Recipe extraction from photos/PDF pages: Anthropic API (vision + structured output), behind an interface so it can be swapped
- URL import: `recipe-scrapers`
- PDF/ebook pages: `pymupdf`; HEIC photos: `pillow-heif`
- Nutrition: USDA FoodData Central API (free key), cached locally
- Optimization: `pulp` with the HiGHS solver (`highspy`) — MILP
- Tests: `pytest`; lint/format: `ruff`

Why Python over Julia or Rust: Julia's JuMP is the nicer optimization modeling language, but the other ~80% of this project (image handling, LLM SDKs, recipe scrapers, unit libraries, ingredient parsing) is best served in Python, and the MILP here is small enough that PuLP + HiGHS solves it in seconds.

## CLI

```bash
bourdain init ~/kitchen                      # create the data folder layout
bourdain ingest photos ~/kitchen/inbox/      # photos → recipe YAML (batch)
bourdain ingest url https://...              # web recipe
bourdain review                              # walk through only the flagged items
bourdain nutrition refresh                   # compute macros for all recipes
bourdain catalog portions onion              # inspect the confirmed USDA food's measures
bourdain catalog use-portion onion ID --unit medium --buy  # explicitly confirm one
bourdain cronometer export <recipe>          # paste-ready block for Cronometer's importer
bourdain cronometer import-nutrition f.csv --basis per-serving
bourdain plan --lean 0.2                     # 0 = thrifty/use-pantry … 1 = variety
bourdain plan --lock "fri dinner=out" --pin chana-masala
bourdain list --format md > groceries.md     # or: todoist-csv, text
bourdain bought groceries.md                 # checked items → pantry
bourdain cooked chana-masala                 # deduct from pantry, log history
```

## Repo layout (target)

```
bourdain/
├── README.md
├── AGENTS.md                # rules for coding agents
├── docs/
│   ├── ARCHITECTURE.md      # data model, modules, optimizer formulation
│   └── ROADMAP.md           # phased plan, agent-sized tasks, acceptance criteria
├── pyproject.toml
├── src/bourdain/
│   ├── cli.py
│   ├── config.py
│   ├── models/              # pydantic schemas
│   ├── store/               # YAML read/write, data-folder layout
│   ├── ingest/              # photos, pdf, url, text → RecipeDraft
│   ├── normalize/           # line parsing, catalog matching, unit → grams
│   ├── nutrition/           # FDC client + cache, per-serving macros
│   ├── cronometer/          # export paste blocks, import CSV
│   ├── plan/                # candidate filter, MILP model, presets
│   ├── grocery/             # aggregation, package rounding, exporters
│   └── pantry/              # pantry + history updates
└── tests/
    ├── fixtures/            # tiny recipe sets, recorded API responses
    └── ...
```

Your data lives elsewhere (default `~/kitchen`), ideally in its own private git repo:

```
~/kitchen/
├── bourdain.toml              # settings: household, meal slots, API keys via env
├── recipes/<book-slug>/<recipe-slug>.yaml
├── catalog/ingredients.yaml
├── catalog/fdc.yaml           # reviewed USDA mappings
├── nutrition/fdc/             # USDA snapshots for comparison with overrides
├── pantry.yaml
├── prices.yaml
├── targets.yaml
├── history.yaml
├── plans/2026-W43.yaml
├── inbox/                   # drop photos here
├── raw/                     # originals, kept for re-extraction
└── .cache/                  # FDC responses, extraction responses
```

## Status

Core implementation through Phase 9 is present. Live Anthropic/FDC checks, real
Cronometer export/import verification, manual Todoist import and hosted CI are
pending. Phase 10's multiple-package-size optimization, freezer portions,
EPUB import, statistics and shopping count units are also implemented;
its remaining features are future work. See [the root README](../README.md)
for setup and an offline planning demo, and [ROADMAP.md](ROADMAP.md) for acceptance
status.
