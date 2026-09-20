"""Unified position view with explicit source provenance and T+1 inventory."""

from __future__ import annotations

from typing import Any


def merge_positions(
    manual_positions: dict[str, dict[str, Any]] | None,
    paper_positions: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for code, raw in (manual_positions or {}).items():
        item = dict(raw or {})
        quantity = int(item.get("quantity") or 0)
        sellable = min(quantity, int(item.get("sellable") if item.get("sellable") is not None else quantity))
        merged[str(code)] = {
            **item,
            "quantity": quantity,
            "sellable": sellable,
            "locked_t1": max(0, quantity - sellable),
            "position_sources": ["manual"],
            "source_inventory": {
                "manual": {"quantity": quantity, "sellable": sellable, "cost": item.get("cost")}
            },
        }

    for raw in paper_positions or []:
        code = str(raw.get("symbol") or raw.get("code") or "").strip()
        if not code:
            continue
        paper_qty = int(raw.get("quantity") or 0)
        paper_sellable = min(paper_qty, int(raw.get("sellable") or 0))
        current = merged.get(code) or {}
        quantity = max(int(current.get("quantity") or 0), paper_qty)
        sellable = min(quantity, max(int(current.get("sellable") or 0), paper_sellable))
        inventories = dict(current.get("source_inventory") or {})
        inventories["paper"] = {
            "quantity": paper_qty,
            "sellable": paper_sellable,
            "cost": raw.get("avg_cost") or raw.get("cost"),
        }
        merged[code] = {
            **current,
            "quantity": quantity,
            "sellable": sellable,
            "locked_t1": max(0, quantity - sellable),
            "cost": raw.get("avg_cost") or raw.get("cost") or current.get("cost"),
            "paper_quantity": paper_qty,
            "paper_sellable": paper_sellable,
            "position_sources": list(dict.fromkeys(list(current.get("position_sources") or []) + ["paper"])),
            "source_inventory": inventories,
        }
    return merged

