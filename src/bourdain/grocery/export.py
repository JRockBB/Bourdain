"""Markdown, Reminders-friendly text, and Todoist CSV exporters."""

import csv
import io
import json
from collections.abc import Iterable
from typing import Protocol

from bourdain.config import SECTION_ORDER
from bourdain.grocery.aggregate import GroceryItem, GroceryList


class Exporter(Protocol):
    def export(self, groceries: GroceryList, section_order: list[str]) -> str: ...


def _text(value: str) -> str:
    return " ".join(value.split())


def _label(item: GroceryItem) -> str:
    if item.check and not item.buy_g:
        quantity = "check pantry"
    elif item.package_purchases:
        quantity = " + ".join(
            (
                f"{purchase.count} × {purchase.unit} @ ~{purchase.package_g:g} g each"
                if purchase.unit
                else f"{purchase.count} × {purchase.package_g:g} g package"
            )
            for purchase in item.package_purchases
        )
    elif item.packages is not None:
        quantity = f"{item.packages} × {item.package_g:g} g package"
    else:
        quantity = f"{item.buy_g:g} g"
    context = ", ".join(_text(name) for name in item.recipes)
    return f"{_text(item.name)} — {quantity} ({context})"


def sections(
    groceries: GroceryList, order: list[str]
) -> Iterable[tuple[str, list[GroceryItem]]]:
    keys = {item.section for item in groceries.items if not item.check}
    for name in sorted(
        keys, key=lambda key: (order.index(key) if key in order else len(order), key)
    ):
        yield (
            name,
            sorted(
                [
                    item
                    for item in groceries.items
                    if item.section == name and not item.check
                ],
                key=lambda item: item.name.casefold(),
            ),
        )
    checked = sorted(
        [item for item in groceries.items if item.check],
        key=lambda item: item.name.casefold(),
    )
    if checked:
        yield "Also check", checked


class MarkdownExporter:
    def export(self, groceries: GroceryList, section_order: list[str]) -> str:
        lines = [f"# Groceries — {groceries.week}", ""]
        for warning in groceries.warnings:
            lines.extend([f"> {_text(warning)}", ""])
        for section, items in sections(groceries, section_order):
            lines.extend([f"## {section.capitalize()}", ""])
            for item in items:
                metadata = ""
                if item.buy_g > 0:
                    data = {
                        "purchase_id": f"{groceries.week}:{item.ingredient_id}",
                        "ingredient_id": item.ingredient_id,
                        "grams": item.buy_g,
                    }
                    metadata = (
                        " <!-- bourdain:"
                        + json.dumps(data, separators=(",", ":"))
                        + " -->"
                    )
                lines.append(f"- [ ] {_label(item)}{metadata}")
            lines.append("")
        if not groceries.items:
            lines.extend(["Nothing to buy.", ""])
        return "\n".join(lines)


class TextExporter:
    def export(self, groceries: GroceryList, section_order: list[str]) -> str:
        return "".join(
            f"{_label(item)}\n"
            for _, items in sections(groceries, section_order)
            for item in items
        )


class TodoistExporter:
    def export(self, groceries: GroceryList, section_order: list[str]) -> str:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["TYPE", "CONTENT", "DESCRIPTION", "PRIORITY", "INDENT"])
        for section, items in sections(groceries, section_order):
            writer.writerow(["section", section.capitalize(), "", "", ""])
            for item in items:
                writer.writerow(["task", _label(item), "; ".join(item.notes), 4, 1])
        return stream.getvalue()


EXPORTERS: dict[str, Exporter] = {
    "md": MarkdownExporter(),
    "text": TextExporter(),
    "todoist-csv": TodoistExporter(),
}


def export_list(
    groceries: GroceryList,
    format: str = "md",
    *,
    section_order: list[str] | None = None,
) -> str:
    if format not in EXPORTERS:
        raise ValueError(f"Unknown format: {format}")
    return EXPORTERS[format].export(
        groceries, SECTION_ORDER if section_order is None else section_order
    )
