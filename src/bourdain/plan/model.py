"""Meal/cook allocation MILP with expiry-aware stock and lexicographic macros."""

import math
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

import pulp

from bourdain.config import PlanConfig, PricesConfig
from bourdain.errors import BourdainError
from bourdain.models import (
    Catalog,
    CookEvent,
    Freezer,
    History,
    Macros,
    Meal,
    MealLock,
    PackagePurchase,
    Pantry,
    Plan,
    Recipe,
    Targets,
)
from bourdain.normalize.units import shopping_unit
from bourdain.pantry.availability import fingerprint
from bourdain.plan.candidates import DAYS, history_scores, select_candidates
from bourdain.plan.presets import preferences
from bourdain.prices import Prices, package_options


def _number(value: Any) -> float:
    result = pulp.value(value)
    return float(result) if result is not None else 0.0


def _complete_fixed_values(problem: pulp.LpProblem) -> None:
    # PuLP does not export unused columns. Fixed diagnostic variables still occur
    # in problem.variables(), so set their known value before consistency checks.
    for value in problem.variables():
        if (
            value.varValue is None
            and value.lowBound is not None
            and value.lowBound == value.upBound
        ):
            value.varValue = value.lowBound


def _past_sources(
    plan: Plan, recipes: dict[str, Recipe], household: float, cutoff: int
) -> dict[tuple[str, str], int]:
    """Recover serving allocations for older plans that lack meal.cook_day."""
    available: dict[tuple[str, int], float] = defaultdict(float)
    for event in plan.cook_events:
        day = DAYS.index(event.day)
        if day < cutoff:
            yield_ = recipes[event.recipe].servings
            if yield_ is None:
                raise BourdainError(f"Frozen recipe has no servings: {event.recipe}")
            available[event.recipe, day] += event.batches * yield_
    sources = {}
    for meal in sorted(plan.meals, key=lambda meal: DAYS.index(meal.day)):
        day = DAYS.index(meal.day)
        if day >= cutoff or meal.freezer_lot or meal.freezer_portions:
            continue
        possible = [
            cook_day
            for (identifier, cook_day), amount in available.items()
            if identifier == meal.recipe
            and 0 <= day - cook_day <= recipes[identifier].keeps_days
            and amount + 1e-7 >= household
        ]
        if meal.cook_day is not None:
            possible = [
                candidate
                for candidate in possible
                if candidate == DAYS.index(meal.cook_day)
            ]
        if not possible:
            raise BourdainError(
                f"Frozen meal has no usable cook event: {meal.day} {meal.slot}"
            )
        chosen = min(possible)
        available[meal.recipe, chosen] -= household
        sources[meal.day, meal.slot] = chosen
    return sources


def optimize(
    week: str,
    recipes: dict[str, Recipe],
    catalog: Catalog,
    pantry: Pantry,
    targets: Targets,
    history: History,
    prices: Prices,
    *,
    config: PlanConfig | None = None,
    price_config: PricesConfig | None = None,
    lean: float | None = None,
    locks: list[MealLock] | None = None,
    pinned: set[str] | None = None,
    excluded: set[str] | None = None,
    ignore_macros: bool = False,
    previous: Plan | None = None,
    replan_from: str | None = None,
    freezer: Freezer | None = None,
) -> Plan:
    from bourdain.freezer import fingerprint as freezer_fingerprint
    from bourdain.freezer import recipe_signature

    started = time.monotonic()
    config, price_config = config or PlanConfig(), price_config or PricesConfig()
    lean = config.lean if lean is None else lean
    result = Plan(week=week, lean=lean)
    monday = date.fromisocalendar(int(week[:4]), int(week[-2:]), 1)
    settings = preferences(
        lean, overrides=config.weights, max_meals=config.max_meals_per_recipe
    )
    weights, pinned = settings.weights, pinned or set()
    cutoff = 0
    if replan_from is not None:
        if replan_from not in DAYS or previous is None or previous.week != week:
            raise BourdainError(
                "Replanning needs an existing plan for the same week and a valid day"
            )
        cutoff = DAYS.index(replan_from)
        result.replan_from = replan_from
    elif previous is not None:
        raise BourdainError("Supply replan_from when passing a previous plan")
    freezer = freezer or Freezer()
    freezer_lots = {}
    for lot in freezer.items:
        if (
            lot.servings <= 0
            or lot.expires < monday + timedelta(days=cutoff)
            or lot.frozen_on > monday + timedelta(days=6)
        ):
            continue
        if (
            lot.recipe not in recipes
            or recipe_signature(recipes[lot.recipe]) != lot.recipe_signature
        ):
            result.warnings.append(
                f"Freezer lot {lot.id}: recipe quantities or nutrition changed; "
                "excluded from planning"
            )
            continue
        if recipes[lot.recipe].freezes:
            freezer_lots[lot.id] = lot
    times = [
        (day, slot)
        for day in sorted(targets.days, key=DAYS.index)
        for slot in targets.planned_slots
    ]
    time_set = set(times)
    lock_map: dict[tuple[str, str], str] = {}
    for lock in locks or []:
        when = lock.day, lock.slot
        if when not in time_set:
            raise BourdainError(
                f"Lock refers to a disabled meal: {lock.day} {lock.slot}"
            )
        if when in lock_map and lock_map[when] != lock.value:
            raise BourdainError(f"Conflicting locks: {lock.day} {lock.slot}")
        lock_map[when] = lock.value
    frozen_meals = {}
    past_freezer = {}
    frozen_cooks: dict[tuple[str, int], float] = defaultdict(float)
    if previous is not None:
        for meal in previous.meals:
            if DAYS.index(meal.day) < cutoff:
                if (meal.day, meal.slot) not in time_set:
                    raise BourdainError("Keep past meal slots enabled when replanning")
                frozen_meals[meal.day, meal.slot] = meal.recipe
                if meal.freezer_lot or meal.freezer_portions:
                    past_freezer[meal.day, meal.slot] = meal.freezer_portions or {
                        meal.freezer_lot: sum(
                            eater.servings_per_slot for eater in targets.eaters
                        )
                    }
        for when in times:
            if DAYS.index(when[0]) < cutoff:
                value = frozen_meals.get(when, "out")
                if when in lock_map and lock_map[when] != value:
                    raise BourdainError("A new lock would change a frozen past meal")
                lock_map[when] = value
        for event in previous.cook_events:
            if DAYS.index(event.day) < cutoff:
                if event.recipe not in recipes:
                    raise BourdainError(
                        f"Frozen cook references a missing recipe: {event.recipe}"
                    )
                frozen_cooks[event.recipe, DAYS.index(event.day)] += event.batches
    mandatory = pinned | {
        value
        for when, value in lock_map.items()
        if value != "out" and DAYS.index(when[0]) >= cutoff
    }
    selection = select_candidates(
        recipes,
        catalog,
        pantry,
        targets,
        history,
        today=monday + timedelta(days=cutoff),
        pinned=mandatory,
        excluded=excluded,
        limit=config.candidate_limit,
        ignore_macros=ignore_macros,
        preferred={lot.recipe for lot in freezer_lots.values()},
    )
    future_ids = set(selection.recipes)
    selected = dict(selection.recipes)
    for identifier, _ in frozen_cooks:
        selected[identifier] = recipes[identifier]
    for identifier in frozen_meals.values():
        if identifier not in recipes:
            raise BourdainError(f"Past meal references a missing recipe: {identifier}")
        selected[identifier] = recipes[identifier]
    if not selected and any(lock_map.get(when) != "out" for when in times):
        reasons = Counter(
            reason for values in selection.excluded.values() for reason in values
        )
        detail = "; ".join(f"{count} {reason}" for reason, count in reasons.items())
        raise BourdainError(f"No eligible recipes. {detail}")
    household = sum(eater.servings_per_slot for eater in targets.eaters)
    if (
        previous
        and previous.household_servings is not None
        and abs(previous.household_servings - household) > 1e-7
    ):
        raise BourdainError(
            "Keep household servings unchanged when freezing past meals"
        )
    for identifier, _ in frozen_cooks:
        if (
            identifier in previous.recipe_servings
            and previous.recipe_servings[identifier] != recipes[identifier].servings
        ):
            raise BourdainError(f"Servings changed for a frozen recipe: {identifier}")
    result.household_servings = household
    past_sources = (
        _past_sources(previous, recipes, household, cutoff) if previous else {}
    )
    problem = pulp.LpProblem("bourdain_week", pulp.LpMinimize)
    serial = 0

    def variable(
        prefix: str, *, kind: str = "Continuous", upper: float | None = None
    ) -> pulp.LpVariable:
        nonlocal serial
        serial += 1
        return problem.add_variable(
            f"{prefix}_{serial}", lowBound=0, upBound=upper, cat=kind
        )

    anchor = variable("anchor", upper=0)
    problem += anchor == 0
    relax = {
        name: variable("diagnostic", upper=0)
        for name in (
            "recipe meal limits",
            "repeat cooking",
            "cook event budget",
            "weekday time budget",
            "pins",
            "meal locks",
        )
    }
    for protein in config.max_per_protein:
        relax[f"protein cap: {protein}"] = variable("diagnostic", upper=0)
    x, c, batches, used, allocation = {}, {}, {}, {}, {}
    freezer_allocation = {}
    by_meal: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for identifier, recipe in selected.items():
        used[identifier] = variable("used", kind="Binary")
        for day in range(7):
            c[identifier, day] = variable("cook", kind="Binary")
            batches[identifier, day] = variable(
                "batch",
                kind="Continuous" if day < cutoff else "Integer",
                upper=max(
                    config.max_batches_per_cook, frozen_cooks.get((identifier, day), 0)
                ),
            )
            problem += (
                batches[identifier, day]
                <= max(
                    config.max_batches_per_cook, frozen_cooks.get((identifier, day), 0)
                )
                * c[identifier, day]
            )
            if day >= cutoff:
                problem += batches[identifier, day] >= c[identifier, day]
            else:
                amount = frozen_cooks.get((identifier, day), 0)
                problem += batches[identifier, day] == amount
                problem += c[identifier, day] == int(amount > 0)
        for when in times:
            day, slot = when
            past = DAYS.index(day) < cutoff
            allowed = (
                frozen_meals.get(when) == identifier
                if past
                else identifier in future_ids
                and (slot in recipe.tags or identifier in mandatory)
            )
            if not allowed or lock_map.get(when) == "out":
                continue
            x[identifier, when] = variable("meal", kind="Binary")
            by_meal[when].append(x[identifier, when])
            if when in lock_map:
                if past:
                    problem += x[identifier, when] == int(lock_map[when] == identifier)
                elif lock_map[when] == identifier:
                    problem += x[identifier, when] >= 1 - relax["meal locks"]
                else:
                    problem += x[identifier, when] <= relax["meal locks"]
            sources = []
            if past and when in past_freezer:
                z = variable("past_freezer", kind="Binary")
                sources.append(z)
                for lot_id, amount in past_freezer[when].items():
                    portions = variable("past_portions", upper=amount)
                    problem += portions == amount * z
                    freezer_allocation[lot_id, when] = portions
            elif not past:
                portions = []
                for lot in freezer_lots.values():
                    if (
                        lot.recipe == identifier
                        and lot.frozen_on
                        <= monday + timedelta(days=DAYS.index(day))
                        <= lot.expires
                    ):
                        z = variable("freezer_portions", upper=household)
                        freezer_allocation[lot.id, when] = z
                        portions.append(z)
                if portions:
                    use_freezer = variable("freezer_meal", kind="Binary")
                    sources.append(use_freezer)
                    problem += pulp.lpSum(portions) == household * use_freezer
            for cook_day in range(
                max(0, DAYS.index(day) - recipe.keeps_days), DAYS.index(day) + 1
            ):
                if past and (when in past_freezer or past_sources[when] != cook_day):
                    continue
                z = variable("allocation", kind="Binary")
                allocation[identifier, cook_day, when] = z
                sources.append(z)
                problem += z <= c[identifier, cook_day]
            problem += pulp.lpSum(sources) == x[identifier, when]
        meal_vars = [
            value for (recipe_id, _), value in x.items() if recipe_id == identifier
        ]
        problem += pulp.lpSum(meal_vars) >= used[identifier]
        problem += pulp.lpSum(meal_vars) <= len(times) * used[identifier]
        prior_meals = sum(value == identifier for value in frozen_meals.values())
        problem += (
            pulp.lpSum(meal_vars)
            <= max(settings.max_meals_per_recipe, prior_meals)
            + len(times) * relax["recipe meal limits"]
        )
        if identifier in pinned:
            problem += used[identifier] >= 1 - relax["pins"]
        if not config.allow_repeat_cook:
            prior_cooks = sum(
                1
                for (recipe_id, _), amount in frozen_cooks.items()
                if recipe_id == identifier and amount > 0
            )
            problem += (
                pulp.lpSum(c[identifier, day] for day in range(7))
                <= max(1, prior_cooks) + 7 * relax["repeat cooking"]
            )
        for day in range(7):
            servings = pulp.lpSum(
                value * household
                for (recipe_id, cook_day, _), value in allocation.items()
                if recipe_id == identifier and cook_day == day
            )
            problem += servings <= (recipe.servings or 0) * batches[identifier, day]
            if day >= cutoff:
                # Do not cook extra whole batches merely to earn pantry credit.
                problem += (
                    batches[identifier, day]
                    <= servings / (recipe.servings or 1)
                    + (1 - 1e-5) * c[identifier, day]
                )
                problem += c[identifier, day] <= pulp.lpSum(
                    value
                    for (recipe_id, cook_day, when), value in allocation.items()
                    if recipe_id == identifier
                    and cook_day == day
                    and DAYS.index(when[0]) == day
                )
    for lot in freezer_lots.values():
        problem += (
            pulp.lpSum(
                value
                for (lot_id, when), value in freezer_allocation.items()
                if lot_id == lot.id and DAYS.index(when[0]) >= cutoff
            )
            <= lot.servings
        )
    for when in times:
        if lock_map.get(when) == "out":
            continue
        if not by_meal[when]:
            raise BourdainError(
                f"No eligible recipe for {when[0]} {when[1]}; check tags and exclusions"
            )
        problem += pulp.lpSum(by_meal[when]) == 1
    for protein, limit in config.max_per_protein.items():
        prior = sum(
            selected[identifier].protein == protein
            for identifier in frozen_meals.values()
        )
        problem += (
            pulp.lpSum(
                value
                for (identifier, _), value in x.items()
                if selected[identifier].protein == protein
            )
            <= max(limit, prior) + len(times) * relax[f"protein cap: {protein}"]
        )
    if config.max_cooks is not None:
        previous_count = (
            len(
                [
                    event
                    for event in previous.cook_events
                    if DAYS.index(event.day) < cutoff
                ]
            )
            if previous
            else 0
        )
        problem += (
            pulp.lpSum(value for (identifier, day), value in c.items() if day >= cutoff)
            <= max(0, config.max_cooks - previous_count)
            + len(times) * relax["cook event budget"]
        )
    if config.weekday_active_minutes is not None:
        for day in range(max(cutoff, 0), 5):
            for identifier, recipe in selected.items():
                if recipe.time.active_min is None:
                    problem += c[identifier, day] == 0
            problem += (
                pulp.lpSum(
                    (selected[identifier].time.active_min or 0)
                    * batches[identifier, day]
                    for identifier in selected
                )
                <= config.weekday_active_minutes
                + sum(
                    (recipe.time.active_min or 0) * config.max_batches_per_cook
                    for recipe in selected.values()
                )
                * relax["weekday time budget"]
            )
    quantities: dict[str, dict[str, float]] = {}
    for identifier, recipe in selected.items():
        amounts: dict[str, float] = defaultdict(float)
        for line in recipe.ingredients:
            if (
                not line.optional
                and line.ingredient_id in catalog
                and line.grams is not None
            ):
                amounts[line.ingredient_id] += line.grams
        quantities[identifier] = dict(amounts)
    ingredients = sorted({item for amounts in quantities.values() for item in amounts})
    packages, need, pantry_use, waste, quotes, purchased_g = {}, {}, {}, {}, {}, {}
    pantry_reward = 0
    possible_warnings: dict[str, list[str]] = defaultdict(list)
    for item_id in ingredients:
        item = catalog[item_id]
        options = package_options(
            item_id,
            item,
            prices,
            per_kg=price_config.section_per_kg,
            currency=price_config.currency,
        )
        quotes[item_id] = options
        possible_warnings[item_id].extend(
            option.warning for option in options if option.warning
        )
        daily_need = {
            day: pulp.lpSum(
                quantities[identifier].get(item_id, 0) * batches[identifier, day]
                for identifier in selected
            )
            for day in range(cutoff, 7)
        }
        need[item_id] = pulp.lpSum(daily_need.values())
        limit = (
            max([amounts.get(item_id, 0) for amounts in quantities.values()] or [0])
            * config.max_batches_per_cook
            * max(1, len(times))
        )
        for option_index, option in enumerate(options):
            packages[item_id, option_index] = variable(
                "packages", kind="Integer", upper=math.ceil(limit / option.package_g)
            )
        purchased_g[item_id] = pulp.lpSum(
            option.package_g * packages[item_id, option_index]
            for option_index, option in enumerate(options)
        )
        lots = [stock for stock in pantry.items if stock.ingredient_id == item_id]
        per_day_use: dict[int, list[Any]] = defaultdict(list)
        all_use = []
        for lot in lots:
            if lot.status == "out":
                continue
            lot_vars = []
            for day in range(cutoff, 7):
                actual = monday + timedelta(days=day)
                if (lot.expires and lot.expires < actual) or (
                    lot.acquired and lot.acquired > actual
                ):
                    continue
                if lot.qty_g is None and lot.status not in {"have", "low"}:
                    continue
                value = variable(
                    "stock", upper=lot.qty_g if lot.qty_g is not None else limit
                )
                lot_vars.append(value)
                all_use.append(value)
                per_day_use[day].append(value)
                urgency = (
                    1 + 7 / (1 + max(0, (lot.expires - actual).days))
                    if lot.expires
                    else 1
                )
                if lot.qty_g is not None:
                    pantry_reward += urgency * value / 1000
                if lot.status == "low" and lot.qty_g is None:
                    possible_warnings[item_id].append(
                        f"{item_id}: low unweighed stock assumed sufficient; "
                        "check pantry"
                    )
            if lot.qty_g is not None:
                problem += pulp.lpSum(lot_vars) <= lot.qty_g
        if item.staple and not lots:
            for day in range(cutoff, 7):
                value = variable("staple", upper=limit)
                all_use.append(value)
                per_day_use[day].append(value)
        for day in range(cutoff, 7):
            problem += pulp.lpSum(per_day_use[day]) <= daily_need[day]
        pantry_use[item_id] = pulp.lpSum(all_use)
        problem += pantry_use[item_id] + purchased_g[item_id] >= need[item_id]
        waste[item_id] = purchased_g[item_id] + pantry_use[item_id] - need[item_id]
    slack, macro_values, scales = {}, {}, {}
    if not ignore_macros:
        for eater in targets.eaters:
            for nutrient, (lower, upper) in (eater.daily or {}).items():
                if lower is None and upper is None:
                    continue
                if targets.macro_mode == "weekly_average":
                    for identifier in set(frozen_meals.values()):
                        nutrition = selected[identifier].nutrition
                        if (
                            nutrition is None
                            or getattr(nutrition.per_serving, nutrient) is None
                        ):
                            raise BourdainError(
                                f"Frozen recipe lacks {nutrient} for a "
                                f"weekly-average replan: {identifier}"
                            )
                groups = (
                    [[day] for day in targets.days if DAYS.index(day) >= cutoff]
                    if targets.macro_mode == "daily"
                    else [list(targets.days)]
                )
                for days in groups:
                    if not days:
                        continue
                    label = (
                        days[0] if targets.macro_mode == "daily" else "weekly_average"
                    )
                    value = eater.fixed_daily.get(nutrient, 0) + pulp.lpSum(
                        variable_
                        * eater.servings_per_slot
                        * (
                            getattr(
                                selected[identifier].nutrition.per_serving, nutrient
                            )
                            if selected[identifier].nutrition
                            else 0
                        )
                        for (identifier, when), variable_ in x.items()
                        if when[0] in days
                    ) / len(days)
                    macro_values[eater.name, label, nutrient] = value
                    for side, bound in (("min", lower), ("max", upper)):
                        if bound is None:
                            continue
                        key = f"{eater.name}:{label}:{nutrient}:{side}"
                        slack[key] = variable("macro_slack")
                        scales[key] = max(1, abs(bound))
                        if side == "min":
                            problem += value + slack[key] >= bound
                        else:
                            problem += value - slack[key] <= bound
    normalized_slack = pulp.lpSum(value / scales[key] for key, value in slack.items())
    incumbent = None
    first = None
    if slack:
        problem += normalized_slack
        first = problem.solve(
            pulp.HiGHS(
                msg=False,
                threads=1,
                timeLimit=max(0.1, config.time_limit_s / 2),
                gapRel=0,
                random_seed=0,
            )
        )
        _complete_fixed_values(problem)
        if not first.has_solution:
            raise BourdainError(
                (
                    _infeasible_message(
                        config, selected, times, settings.max_meals_per_recipe
                    )
                    if first.status_str == "Infeasible"
                    else ""
                )
                + _diagnose(problem, relax, first, normalized_slack)
            )
        best_slack = _number(normalized_slack)
        problem += normalized_slack <= best_slack + 1e-7
        if first.status_str != "Optimal":
            result.warnings.append(
                "Macro search reached its limit; best fit is not proven"
            )
        incumbent = {
            value.name: value.varValue
            for value in problem.variables()
            if value.varValue is not None
        }
    cost = pulp.lpSum(
        quotes[item][option_index].price * value
        for (item, option_index), value in packages.items()
    )
    perishables = pulp.lpSum(
        value / 1000
        for item, value in waste.items()
        if catalog[item].perishable is True
    )
    repeat = pulp.lpSum(x.values()) - pulp.lpSum(used.values())
    novelty = pulp.lpSum(
        history_scores(identifier, history, monday + timedelta(days=cutoff))[0] * value
        for identifier, value in used.items()
    )
    rating = pulp.lpSum(
        history_scores(identifier, history, monday + timedelta(days=cutoff))[1] * value
        for identifier, value in used.items()
    )
    cooks = pulp.lpSum(value for (identifier, day), value in c.items() if day >= cutoff)
    freezer_reward = pulp.lpSum(
        value
        / household
        * (
            1
            + 7
            / (
                1
                + max(
                    0,
                    (
                        freezer_lots[lot_id].expires
                        - (monday + timedelta(days=DAYS.index(when[0])))
                    ).days,
                )
            )
        )
        for (lot_id, when), value in freezer_allocation.items()
        if DAYS.index(when[0]) >= cutoff
    )
    objective = (
        weights["cost"] * cost
        + weights["waste"] * perishables
        - weights["pantry"] * (pantry_reward + freezer_reward)
        + weights["macro"] * normalized_slack
        + weights["repeat"] * repeat
        - weights["novel"] * novelty
        - weights["distinct"] * pulp.lpSum(used.values())
        + weights["cooks"] * cooks
        - weights["rating"] * rating
    )
    problem.setObjective(objective)
    remaining = max(0.1, config.time_limit_s - (time.monotonic() - started))
    stats = problem.solve(
        pulp.HiGHS(
            msg=False, threads=1, timeLimit=remaining, gapRel=0.01, random_seed=0
        )
    )
    _complete_fixed_values(problem)
    fallback = False
    if not stats.has_solution or not problem.valid(1e-5):
        if incumbent is not None:
            problem.assignVarsVals(incumbent)
            fallback = True
            result.warnings.append(
                "Preference solve found no incumbent; "
                "returning the feasible macro-stage plan"
            )
        else:
            raise BourdainError(
                (
                    _infeasible_message(
                        config, selected, times, settings.max_meals_per_recipe
                    )
                    if stats.status_str == "Infeasible"
                    else ""
                )
                + _diagnose(problem, relax, stats, normalized_slack)
            )
    result.solver_status = "Macro incumbent" if fallback else stats.status_str
    result.solver_gap = (
        stats.gap_rel
        if not fallback and stats.gap_rel is not None and math.isfinite(stats.gap_rel)
        else None
    )
    if stats.status_str != "Optimal":
        result.warnings.append(f"Solver returned a feasible plan at {stats.status_str}")
    result.locks = [
        MealLock(day=day, slot=slot, value=value)
        for (day, slot), value in sorted(
            lock_map.items(),
            key=lambda entry: (
                DAYS.index(entry[0][0]),
                targets.planned_slots.index(entry[0][1]),
            ),
        )
    ]
    for when in times:
        chosen = [
            identifier
            for identifier in selected
            if (identifier, when) in x and _number(x[identifier, when]) > 0.5
        ]
        if not chosen:
            continue
        identifier = chosen[0]
        freezer_portions = {
            lot_id: _number(value)
            for (lot_id, meal_time), value in freezer_allocation.items()
            if meal_time == when and _number(value) > 1e-7
        }
        freezer_source = (
            next(iter(freezer_portions)) if len(freezer_portions) == 1 else None
        )
        source = next(
            (
                day
                for (recipe_id, day, meal_time), value in allocation.items()
                if recipe_id == identifier
                and meal_time == when
                and _number(value) > 0.5
            ),
            None,
        )
        result.meals.append(
            Meal(
                day=when[0],
                slot=when[1],
                recipe=identifier,
                cook_day=DAYS[source] if source is not None else None,
                leftover=freezer_source is not None
                or (source is not None and source < DAYS.index(when[0])),
                freezer_lot=freezer_source,
                freezer_portions=freezer_portions,
            )
        )
    if previous:
        result.cook_events.extend(
            event.model_copy(deep=True)
            for event in previous.cook_events
            if DAYS.index(event.day) < cutoff
        )
    for (identifier, day), value in c.items():
        if day >= cutoff and _number(value) > 0.5:
            slot = next(
                meal.slot
                for meal in result.meals
                if meal.recipe == identifier
                and meal.day == DAYS[day]
                and meal.cook_day == DAYS[day]
                and meal.freezer_lot is None
            )
            result.cook_events.append(
                CookEvent(
                    recipe=identifier,
                    day=DAYS[day],
                    slot=slot,
                    batches=round(_number(batches[identifier, day])),
                )
            )
    result.cook_events.sort(
        key=lambda event: (
            DAYS.index(event.day),
            targets.planned_slots.index(event.slot),
            event.recipe,
        )
    )
    for meal in result.meals:
        meal.leftover = not any(
            event.recipe == meal.recipe
            and event.day == meal.day
            and event.slot == meal.slot
            and meal.cook_day == meal.day
            for event in result.cook_events
        )
    for identifier in ingredients:
        if _number(need[identifier]) <= 1e-7:
            continue
        count_unit = shopping_unit(catalog[identifier])
        purchases = [
            PackagePurchase(
                package_g=option.package_g,
                count=round(_number(packages[identifier, option_index])),
                unit_price=option.price,
                currency=price_config.currency,
                unit=(
                    count_unit[0]
                    if count_unit and abs(count_unit[1] - option.package_g) < 1e-7
                    else None
                ),
            )
            for option_index, option in enumerate(quotes[identifier])
            if _number(packages[identifier, option_index]) > 0.5
        ]
        result.package_purchases[identifier] = purchases
        result.package_counts[identifier] = sum(
            purchase.count for purchase in purchases
        )
        if len(purchases) <= 1:
            result.package_sizes_g[identifier] = (
                purchases[0].package_g if purchases else quotes[identifier][0].package_g
            )
        result.pantry_usage_g[identifier] = max(0, _number(pantry_use[identifier]))
        result.ingredient_needs_g[identifier] = max(0, _number(need[identifier]))
        result.warnings.extend(
            warning
            for warning in possible_warnings[identifier]
            if result.package_counts[identifier] > 0 or "unweighed" in warning
        )
        if catalog[identifier].perishable is None:
            result.warnings.append(
                f"{identifier}: perishability unknown; excluded from waste penalty"
            )
    result.macro_slack = {
        key: max(0, _number(value))
        for key, value in slack.items()
        if _number(value) > 1e-5
    }
    for key, amount in result.macro_slack.items():
        eater, day, nutrient, side = key.rsplit(":", 3)
        achieved = _number(macro_values[eater, day, nutrient])
        result.warnings.append(
            f"{eater}, {day}: {nutrient} = {achieved:g}; "
            f"{'below minimum' if side == 'min' else 'above maximum'} by {amount:g}"
        )
    if ignore_macros:
        result.warnings.append("Macro targets were explicitly ignored")
    for eater in targets.eaters:
        daily = {}
        for day in targets.days:
            values = {}
            meals = [meal for meal in result.meals if meal.day == day]
            for name in Macros.model_fields:
                contributions = [
                    getattr(selected[meal.recipe].nutrition.per_serving, name)
                    if selected[meal.recipe].nutrition
                    else None
                    for meal in meals
                ]
                values[name] = (
                    None
                    if any(value is None for value in contributions)
                    else eater.fixed_daily.get(name, 0)
                    + sum(contributions) * eater.servings_per_slot
                )
            daily[day] = Macros(**values)
        result.eater_daily_macros[eater.name] = daily
    result.daily_macros = result.eater_daily_macros[targets.eaters[0].name]
    for (lot_id, when), value in freezer_allocation.items():
        if DAYS.index(when[0]) >= cutoff and _number(value) > 1e-7:
            result.freezer_usage[lot_id] = result.freezer_usage.get(
                lot_id, 0
            ) + _number(value)
    if result.freezer_usage:
        result.freezer_fingerprint = freezer_fingerprint(
            freezer, set(result.freezer_usage)
        )
    result.objective_breakdown = {
        "cost": _number(cost),
        "waste_g_perishable": _number(perishables) * 1000,
        "pantry_used_g": sum(result.pantry_usage_g.values()),
        "freezer_servings_used": sum(result.freezer_usage.values()),
        "macro_slack": _number(normalized_slack),
        "repeated_meals": _number(repeat),
        "distinct_recipes": sum(_number(value) > 0.5 for value in used.values()),
        "cook_events": _number(cooks),
        "weighted_total": _number(objective),
    }
    for identifier, value in used.items():
        if _number(value) <= 0.5:
            continue
        recipe = selected[identifier]
        if recipe.servings is not None:
            result.recipe_servings[identifier] = recipe.servings
        count = sum(meal.recipe == identifier for meal in result.meals)
        events = sum(event.recipe == identifier for event in result.cook_events)
        reasons = [f"{count} meal(s), {events} cook event(s)"]
        frozen_count = sum(
            meal.recipe == identifier
            and bool(meal.freezer_lot or meal.freezer_portions)
            for meal in result.meals
        )
        if frozen_count:
            reasons.append(f"{frozen_count} meal(s) from recorded freezer portions")
        shared = [
            item
            for item in quantities[identifier]
            if result.pantry_usage_g.get(item, 0) > 0
        ]
        if shared:
            reasons.append("Uses pantry ingredients: " + ", ".join(shared))
        if recipe.nutrition and recipe.nutrition.per_serving.protein_g is not None:
            reasons.append(
                f"{recipe.nutrition.per_serving.protein_g:g} g protein per serving"
            )
        result.explanations[identifier] = reasons
    result.warnings = list(dict.fromkeys(result.warnings))
    result.pantry_fingerprint = fingerprint(pantry, set(result.package_counts))
    return result


def _diagnose(
    problem: pulp.LpProblem,
    relax: dict[str, pulp.LpVariable],
    stats: Any,
    macro_slack: Any,
) -> str:
    if stats.status_str != "Infeasible":
        if stats.has_solution:
            return "Solver result failed consistency checks; no plan was saved."
        return (
            f"Solver stopped at {stats.status_str} without a usable incumbent; "
            "increase the time limit."
        )
    for value in relax.values():
        value.upBound = 1
    problem.setObjective(pulp.lpSum(relax.values()) + 1e-6 * macro_slack)
    diagnostic = problem.solve(
        pulp.HiGHS(msg=False, threads=1, timeLimit=2, gapRel=0.01, random_seed=0)
    )
    if diagnostic.has_solution:
        binding = [name for name, value in relax.items() if _number(value) > 1e-6]
        if binding:
            return (
                " A diagnostic solve can make a plan by relaxing: "
                + ", ".join(binding)
                + "."
            )
    return (
        " Also check batch yields and missing active times; "
        "food availability and frozen meals were not relaxed."
    )


def _infeasible_message(
    config: PlanConfig,
    recipes: dict[str, Recipe],
    times: list[tuple[str, str]],
    max_meals: int,
) -> str:
    details = [
        f"{len(recipes)} eligible recipes for {len(times)} meal slots",
        f"at most {max_meals} meals per recipe",
    ]
    if not config.allow_repeat_cook:
        details.append(
            "each recipe may be cooked only once, within its leftover window"
        )
    if config.max_cooks:
        details.append(f"at most {config.max_cooks} cook events")
    if config.max_per_protein:
        details.append("protein-category caps are active")
    if config.weekday_active_minutes:
        details.append("weekday cooking-time limits are active")
    return (
        "No feasible plan under the locks and limits: "
        + "; ".join(details)
        + ". Relax a limit, add recipes, or enable repeat cooking."
    )
