# AGENTS.md — rules for coding agents

You are working on **Bourdain**, a personal meal-planning CLI. Read `README.md` (purpose), `docs/ARCHITECTURE.md` (the contract) and `docs/ROADMAP.md` (the task list) before writing code.

## Scope

- Implement **one roadmap task per change**, identified by its ID (e.g. `P4.2`). Don't start adjacent tasks; list follow-ups in your summary instead.
- `ARCHITECTURE.md` is the source of truth for schemas, file layout and module boundaries. If you need to deviate, update `ARCHITECTURE.md` in the same change and say why under **Spec changes** in your summary.
- Tick the task's checkbox in `ROADMAP.md` when its acceptance criteria pass.

## Tooling

```bash
uv sync                      # install
uv run pytest                # tests (offline, must pass)
uv run pytest -m live        # opt-in, hits real APIs; needs keys in env
uv run ruff check --fix && uv run ruff format
uv run bourdain --help
```

- Python ≥ 3.12, type hints everywhere, pydantic v2 models.
- Add dependencies with `uv add`; prefer the libraries already named in the README. Ask (in your summary) before adding a heavy dependency.

## Hard rules

1. **Never touch the user's data folder** (`~/kitchen` or `$BOURDAIN_DATA`) from tests or development. Tests use `tmp_path` and fixtures only.
2. **No network in the default test suite.** LLM and FDC calls go through interfaces with fakes and recorded fixtures. Live tests are marked `@pytest.mark.live`.
3. **No secrets in files.** API keys come only from `ANTHROPIC_API_KEY` and `FDC_API_KEY`. Never log them.
4. **User edits win.** Automated writers must respect `locked: true` and `source: user` (use the helper from P1.3).
5. **Preserve hand-edited YAML.** Use the store layer (ruamel round-trip) — never `yaml.dump` a user file directly.
6. **Never guess quantities silently.** If grams, nutrients or matches are uncertain, write `null`/low confidence and add a review flag.
7. **No Cronometer scraping or browser automation.** Integrate only via paste blocks and its CSV exports (see ARCHITECTURE §4.4).
8. **Internal units:** grams, millilitres, kcal. Convert only at the edges (parsing input, printing output).

## Code layout

Keep module boundaries from the README: `ingest` produces `RecipeDraft`s and knows nothing about nutrition; `plan` reads models and never calls external APIs; `grocery` consumes a `Plan`; the CLI (`cli.py`) is a thin layer over library functions that can be called from Python directly.

## Tests

- Every task adds tests that encode its acceptance criteria.
- Prefer golden files for text outputs (`tests/golden/`), with a `--update-golden` flag.
- Optimizer tests use tiny worlds where the optimum is known by hand.

## Summary format (end of each change)

```
Task: P#.#
What changed: ...
How to verify: commands + expected output
Spec changes: none | ...
Follow-ups: ...
```
