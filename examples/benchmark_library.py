"""Time 80 eligible candidates x 14 meal times using a disposable backup restore."""

import argparse
import json
import platform
import tempfile
import time
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any

from bourdain.backup import restore_backup
from bourdain.config import load_settings
from bourdain.errors import BourdainError
from bourdain.freezer import read_freezer
from bourdain.grocery.aggregate import groceries
from bourdain.models import Plan
from bourdain.plan.candidates import select_candidates
from bourdain.plan.service import automatic_plan
from bourdain.store import Repository


def benchmark(archive: Path, week: str) -> dict[str, Any]:
    week = Plan(week=week).week
    with tempfile.TemporaryDirectory(prefix="bourdain-benchmark-") as temporary:
        root = Path(temporary) / "library"
        restore_backup(archive, root)
        repo = Repository(root)
        settings = load_settings(root)
        targets = repo.targets().model
        if len(targets.days) * len(targets.planned_slots) != 14:
            raise BourdainError("Benchmark needs 14 enabled meal times in targets.yaml")
        recipes, catalog = repo.recipes(), repo.catalog().model
        pantry, history = repo.pantry().model, repo.history().model
        monday = date.fromisocalendar(int(week[:4]), int(week[-2:]), 1)
        selected = select_candidates(
            recipes, catalog, pantry, targets, history, today=monday, limit=80
        )
        if len(selected.recipes) != 80:
            raise BourdainError(
                "Benchmark needs at least 80 eligible recipes; review stats first"
            )
        settings.plan.candidate_limit = 80
        trials = []
        for lean in (0.0, 0.5, 1.0):
            start = time.perf_counter()
            plan = automatic_plan(repo, settings, week, lean=lean, save=False)
            elapsed = time.perf_counter() - start
            if len(plan.meals) != 14:
                raise BourdainError("Benchmark did not produce all 14 meals")
            groceries(plan, recipes, catalog, pantry, freezer=read_freezer(repo))
            trials.append(
                {
                    "lean": lean,
                    "planning_seconds": elapsed,
                    "within_10_seconds": elapsed <= 10,
                    "meals": len(plan.meals),
                    "solver_status": plan.solver_status,
                    "solver_gap": plan.solver_gap,
                    "macro_slack": plan.macro_slack,
                    "objective": plan.objective_breakdown,
                    "warnings": plan.warnings,
                }
            )
        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "bourdain": version("bourdain"),
            "highspy": version("highspy"),
            "week": week,
            "candidates": len(selected.recipes),
            "meal_times": 14,
            "plan_config": settings.plan.model_dump(),
            "targets": targets.model_dump(mode="json"),
            "timing_scope": (
                "automatic_plan including input reads; "
                "excludes restore and grocery check"
            ),
            "trials": trials,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--week", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new report filename")
    try:
        report = benchmark(args.archive, args.week)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
    except (BourdainError, OSError, ValueError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(f"Wrote benchmark evidence to {args.output}")
