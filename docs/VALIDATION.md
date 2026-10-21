# Live validation runbook

Implementation and offline checks are complete through P11. The checks below are
**pending**, not claimed passes. Use this runbook when starting live validation;
building the harness does not close the external acceptance items in ROADMAP.md.

## 1. Prepare a separate validation library and evidence folder

Use a new, explicitly named folder for all experiments. Keep keys in environment
variables and personal photos/receipts/export samples outside source control.
The following commands are examples to run during validation, not startup actions.

```sh
uv sync --locked
uv run bourdain init ./validation-kitchen
uv run bourdain --data ./validation-kitchen doctor
```

Configure that copy's `targets.yaml`: household servings, planned meal slots,
user-supplied macro ranges and fixed daily foods. Decide daily versus weekly
averages and the price currency. Confirm which Cronometer import/export features
are available in the actual account. Configure supported model names in
`bourdain.toml` after checking the account; defaults are not proof of availability.

For an existing validation library, take `backup ./validation-start.zip` with
`--data ./validation-kitchen`, then `restore ./validation-start.zip
./validation-restored` and run `doctor` on the restored copy. Check known raw
photos, YAML comments, purchase history and `exports/todoist` records. Capture
before/after health reports. Backup omits caches, so restored provider caches may
need refetching. Pause writers during snapshot creation. Keep the original archive.

## 2. Prepare independently reviewed expectations

Copy templates from [`examples/validation`](../examples/validation) to a private
`.validation-inputs` folder. The templates contain **placeholders**, not verified
food IDs, weights, receipt prices or recipe transcriptions. Replace every
placeholder before running; the FDC and mapping templates need more rows.
Put image paths relative to their manifest. Use selected page images for the
automated image checks (no unintentional whole-book PDF requests).

| Manifest environment variable | Required evidence | Acceptance |
| --- | --- | --- |
| `BOURDAIN_LIVE_PAGES` | Three cases named `two-page`, `two-recipes`, `handwritten`; exact titles, yield and raw lines | Correct recipe count, titles, servings and ingredient lines; P3.3 |
| `BOURDAIN_LIVE_FDC` | Exactly five independently verified IDs, descriptions and protein per 100 g | Typed retrieval agrees with the source; repeated lookup agrees; P5.1 |
| `BOURDAIN_LIVE_MAPPINGS` | Thirty distinct ingredients, including preparation variants; reviewed acceptable ID sets | Proposed IDs match approved sets; cases marked `review: required` stay flagged; P5.2 |
| `BOURDAIN_LIVE_INDEXES` | At least two contrasting layouts with category headings, cross references and multi-page entries | Exact recipe titles/printed pages; categories and cross references omitted; P10.1 |
| `BOURDAIN_LIVE_RECEIPTS` | Clear, repeated-row, discounted and incomplete/unit-ambiguous receipts | Raw rows, explicit package grams, quantity, final line total, currency and uncertainty match; unknown values remain null; P10.2 |
| `BOURDAIN_LIVE_PORTIONS` | Reviewed onion size, garlic clove/head where available, and a food with a non-unit edible fraction | Food/portion identity, original grams/count and final count weight agree; P10.10 |

The twenty unusual-name matching cases are already in `tests/test_live.py` (P4.2).
They are intentionally limited candidate sets; record real-library failures too.
For USDA portions, `expected_unit_g = gram_weight / count / edible_fraction`.
Verify the food's preparation and size before selecting a portion. If a needed
portion does not exist, record that limitation; use a confirmed manual measurement
instead of inventing an ID or density. Missing source amounts require a reviewed
count; existing amounts must agree. `portion_label` is the exact displayed label.

Receipt expectations are per printed product row, including repeated products.
Do not infer package grams from a product name without a printed mass, or assume
the currency of an ambiguous `$`. Resolve discounts explicitly in the manual
acceptance step. Do not count taxes, tender, refunds or basket discounts as foods.

## 3. Run opt-in API checks and retain evidence

Set `ANTHROPIC_API_KEY` and/or `FDC_API_KEY` in the process environment and the
manifest variables above. Optionally set `BOURDAIN_LIVE_VISION_MODEL` and
`BOURDAIN_LIVE_TEXT_MODEL`; the harness uses these overrides or the library's
default model names, not a personal data folder's config.

PowerShell example for one manifest:

```powershell
$env:BOURDAIN_LIVE_INDEXES = (Resolve-Path .validation-inputs/indexes.json).Path
```

Create a new `.validation-run` parent and use a **new run directory each time**
(`pytest --basetemp` clears an existing directory at that exact path):

```sh
uv run pytest -m live tests/test_live.py tests/test_live_readiness.py --basetemp .validation-run/api-001 --junitxml .validation-run/api-001.xml
```

Use `-k live_cookbook_indexes`, `-k live_receipt_rows`, `-k live_usda_portions`
or another test name for focused reruns. Missing keys/manifests produce skips;
**a skipped check is pending, never a pass**. All test writes go to temporary
libraries, not `BOURDAIN_DATA`. These calls may cost money. The harness never sends
Todoist tasks or automates Cronometer.

Keep the JUnit report and the relevant `.cache` JSON files under each test's
temporary folder. Record the date, model IDs, prompt version, fixture filenames
and expected/actual differences. The cache stores validated structured responses,
not authentication headers. Review and sanitize selected response examples before
promoting them into offline fixtures; mark them recorded rather than synthetic.
Retain failing outputs and fix causes before updating expected values.

## 4. Exercise the human-facing workflows

Use the explicit `--data ./validation-kitchen` prefix throughout.

| Workflow | Steps and acceptance evidence |
| --- | --- |
| Photo/PDF review | Import real pages and selected physical PDF pages. Confirm printed-page references, continuation merging, source previews, yields and raw lines. Correct a match/weight, repeat ingest, and check that accepted edits survive. Record review time and flags per recipe. |
| Index capture | `ingest index`, `index ls`, `index select`, `index queue`; correct an entry and repeat import. Capture queued **printed** pages separately from physical PDF indices. Verify captured detection and `index link` when a title differs. |
| EPUB | Import one structured metadata book and one clearly headed XHTML book. Compare order, recipe count, title, yield and ingredient text to the source. Record unsupported layouts without treating skipped chapters as successful imports. |
| Receipt review | `prices receipt`, `prices review`; verify raw/source rows, explicitly fill unknown sizes/currency, and resolve discounts. `prices accept` must produce final line total / package count. Repeat acceptance and import; ensure no extra price changes or pantry additions. |
| Counts and groceries | Confirm a real onion portion with `catalog portions` / `use-portion --buy`. Combine a gram recipe with a medium-onion recipe and pantry stock. Check total grams and whole-item rounding by hand. Confirm garlic clove recipe units can use a head shopping unit. Change a source food/fraction in the validation copy and confirm `doctor` flags the stale conversion. |
| USDA nutrition | Verify edible fractions and at least three full recipes against independent arithmetic, within the existing 2% target. Check incomplete values stay unknown and protected values survive refresh. |
| Cronometer paste (P6.1) | Export three real blocks, manually paste into the account's Recipe Importer, and record each ingredient/name/quantity match and actual yield. Fix wording where needed. |
| Cronometer CSV (P6.2) | Save an actual account export. Record headers, layout, units and serving basis. Import with explicit `--recipe` and `--basis`; compare eight nutrient fields and check that refresh preserves the override. If unsupported, retain a sanitized sample and implement that observed layout. |
| Todoist CSV (P2.5) | Export a real list with sections, mixed packages and Unicode names; manually import into a separate test project. Check section structure, task count and readable quantities. |
| Shop/cook/freezer | Export Markdown, mark known purchases, run `bought` twice with the same IDs, and confirm stock increases once. Record a cook with an explicit event ID, freezer servings and expiry; retry and check history/stock once. Allocate portions next week, consume with a retry ID and check no duplicate raw ingredient demand. |
| Planner | Try lean 0, 0.5 and 1, locks, an out meal, a pinned recipe, leftovers and an infeasible macro case. Review macro slack/warnings, per-eater totals and exact package counts. Change stock and verify stale saved lists require replanning. Replan midweek and check past meals remain fixed. |

### Direct Todoist delivery (P10.3)

Use a dedicated test project and an explicitly chosen project ID. Set
`TODOIST_API_TOKEN` in the environment only. Start with a saved plan containing
a small nonempty grocery list and review:

```sh
uv run bourdain --data ./validation-kitchen push todoist --project PROJECT_ID --week 2026-W43 --dry-run
uv run bourdain --data ./validation-kitchen push todoist --project PROJECT_ID --week 2026-W43
uv run bourdain --data ./validation-kitchen push todoist --project PROJECT_ID --week 2026-W43
```

Substitute the test week and project ID. Compare displayed tasks against the
preview, then confirm the repeated push adds none. Check week, store section,
quantities, Unicode and descriptions. Record project/task counts, not the token.
Mark one remote task complete and confirm pantry stays unchanged. Change the
local list: the same batch must refuse; review existing remote tasks before using
a new `--batch`. Inspect retained UUID/acknowledgment state and confirm it survives
backup/restore. Do not delete it to force retries. Offline tests cover partial
network failure and lost acknowledgments; a live fault test is optional and must
reuse the original batch. Remote cleanup is manual.

## 5. Benchmark the real 80-candidate library

Prepare at least 80 eligible, reviewed recipes and 14 enabled meal times (seven
days, lunch and dinner). Keep actual household constraints, prices and usable
inventory. `stats --json` explains exclusions. Snapshot this validation library
to a new archive, then run:

```sh
uv run python examples/benchmark_library.py ./validation-80.zip --week 2026-W43 --output ./benchmark-001.json
```

The script restores into a disposable directory, verifies eligibility and meal
count, sets the candidate limit to 80, and runs lean 0/0.5/1 without saving plans.
It makes no API calls. Record hardware, versions, configured time limit, elapsed
time, incumbent status/gap, macro slack and warnings. Timing includes planning
input reads, excludes backup restore and grocery checking, and is therefore a
conservative check of the **≤10-second solve** target. Repeat with a new output
filename to distinguish cold and warm runs. A timeout incumbent must be reviewed
for quality; a quick fallback or infeasible result is not an accepted benchmark.

## 6. Record acceptance and close only proven items

Copy [VALIDATION_REPORT.md](VALIDATION_REPORT.md) to the private evidence folder.
Mark each row pass/fail/pending with artifact paths and a concrete result. A
blocked account feature or absent portion is evidence of a limitation, not a pass.
After fixes, rerun affected offline/live cases and preserve the earlier failure.
Run final lint, format, offline tests and package build. Run hosted CI on both
Windows and Ubuntu once a remote repository is configured; local success is not
hosted CI acceptance. Update only the roadmap checkboxes whose external criteria
now have evidence. Review all remaining warnings before using the real workflow.
