"""Load local planning inputs and persist new or explicitly replanned weeks."""

from bourdain.config import Settings
from bourdain.errors import BourdainError
from bourdain.freezer import read_freezer
from bourdain.models import MealLock, Plan
from bourdain.plan.model import optimize
from bourdain.prices import price_document
from bourdain.store import Repository


def parse_locks(values: list[str]) -> list[MealLock]:
    result = []
    for value in values:
        try:
            when, identifier = value.split("=", 1)
            day, slot = when.strip().lower().split()
            result.append(MealLock(day=day, slot=slot, value=identifier.strip()))
        except ValueError:
            raise BourdainError(
                f"Use --lock 'fri dinner=out' or a recipe ID: {value}"
            ) from None
    return result


def automatic_plan(
    repo: Repository,
    settings: Settings,
    week: str,
    *,
    lean: float | None = None,
    locks: list[str] | None = None,
    pinned: list[str] | None = None,
    excluded: list[str] | None = None,
    ignore_macros: bool = False,
    replan_from: str | None = None,
    save: bool = True,
) -> Plan:
    week = Plan(week=week).week
    path = repo.path(f"plans/{week}.yaml")
    if save and path.exists() and replan_from is None:
        raise BourdainError(
            f"Plan already exists: {week}; use --replan-from to revise future days"
        )
    previous_doc = repo.plan(week) if replan_from else None
    previous = previous_doc.model if previous_doc else None
    combined = parse_locks(locks or [])
    explicit_times = {(lock.day, lock.slot) for lock in combined}
    if previous:
        combined += [
            lock
            for lock in previous.locks
            if (lock.day, lock.slot) not in explicit_times
        ]
    result = optimize(
        week,
        repo.recipes(),
        repo.catalog().model,
        repo.pantry().model,
        repo.targets().model,
        repo.history().model,
        price_document(repo).model,
        config=settings.plan,
        price_config=settings.prices,
        lean=lean,
        locks=combined,
        pinned=set(pinned or []),
        excluded=set(excluded or []),
        ignore_macros=ignore_macros,
        previous=previous,
        replan_from=replan_from,
        freezer=read_freezer(repo),
    )
    if save:
        if previous_doc:
            previous_doc.save(result, automated=False)
        else:
            repo.save_plan(result)
    return result
