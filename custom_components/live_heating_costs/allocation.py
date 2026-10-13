"""Whole-house merit order with local-heating constraints and existing headroom."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EligibleSource:
    source_id: str
    capacity_kw: float
    local_capacity_kw: float
    cost: float | None
    current_kw: float | None = 0.0
    open_house: bool = True
    response_minutes: float = 0.0
    # Expected direct/local delivery over the same horizon as remaining demand.
    # A dual-purpose or slow source's split cannot be inferred from total input.
    current_local_kw: float | None = None


def allocate(
    total_kw: float,
    local_kw: float | None,
    sources: list[EligibleSource],
    *,
    additional: bool = False,
) -> dict:
    """Reserve local demand first; unknown delivery cannot create extra headroom.

    In additional mode ``total_kw`` is already the whole-house *remaining*
    requirement, while ``local_kw`` is the full local requirement. Subtract
    identifiable committed local delivery before partitioning that remainder.
    This removes current heat from demand once and from each source's available
    capacity once, rather than requesting the full local load a second time.

    Slow sources cannot be assumed to deliver newly requested heat in the next
    hour. Their committed output belongs in the thermal fabric continuation;
    new dispatch waits for an identified source-specific response model.
    """
    if not math.isfinite(total_kw) or total_kw < 0:
        raise ValueError("invalid total demand")
    if local_kw is None:
        return {"allocations": [], "uncovered_kw": None, "reason": "local_requirement_unknown"}
    if not math.isfinite(local_kw) or local_kw < 0:
        raise ValueError("invalid local demand")
    if len({s.source_id for s in sources}) != len(sources):
        raise ValueError("duplicate heat source")
    valid = []
    committed_local = {}
    for s in sources:
        if not all(
            math.isfinite(v) and v >= 0
            for v in (s.capacity_kw, s.local_capacity_kw, s.response_minutes)
        ):
            raise ValueError("invalid capacity or response")
        if s.local_capacity_kw > s.capacity_kw:
            raise ValueError("local capacity exceeds total capacity")
        if s.current_kw is not None and (not math.isfinite(s.current_kw) or s.current_kw < 0):
            raise ValueError("invalid current heat")
        if s.current_local_kw is not None and (
            not math.isfinite(s.current_local_kw)
            or s.current_local_kw < 0
            or s.current_kw is None
            or s.current_local_kw > s.current_kw
        ):
            raise ValueError("invalid committed local heat")
        if s.current_local_kw is not None:
            # A fresh operating-limit/capacity change can remove future
            # headroom while measured committed heat is still being delivered.
            committed_local[s.source_id] = s.current_local_kw
        elif s.current_kw == 0 or s.local_capacity_kw == 0:
            committed_local[s.source_id] = 0.0
        elif not s.open_house and s.response_minutes == 0:
            committed_local[s.source_id] = s.current_kw
        else:
            committed_local[s.source_id] = None
        if s.cost is not None and math.isfinite(s.cost):
            valid.append(s)
    if additional and local_kw > 0 and any(v is None for v in committed_local.values()):
        return {
            "allocations": [],
            "uncovered_kw": None,
            "reason": "local_remaining_unknown",
            "total_kw": total_kw,
            "local_kw": None,
            "local_requirement_kw": local_kw,
            "committed_local_kw": None,
        }
    local_committed = sum(value or 0.0 for value in committed_local.values())
    local_remaining = max(local_kw - local_committed, 0.0) if additional else local_kw
    ranked = sorted(valid, key=lambda s: (s.cost, s.source_id))
    capacity = {}
    for s in ranked:
        capacity[s.source_id] = (
            max(0, s.capacity_kw - s.current_kw)
            if additional and s.current_kw is not None
            else 0
            if additional
            else s.capacity_kw
        )
        if additional and s.response_minutes > 0:
            capacity[s.source_id] = 0
    remaining_local = local_remaining
    remaining_open = max(0.0, total_kw - local_remaining)
    allocations = []
    for group in ("local", "open_house"):
        for s in ranked:
            if group == "local":
                # The direct local split and total output constrain different
                # capacities. Total current heat was already removed above.
                local_cap = (
                    max(0, s.local_capacity_kw - (committed_local[s.source_id] or 0))
                    if additional
                    else s.local_capacity_kw
                )
                value = min(remaining_local, local_cap, capacity[s.source_id])
                remaining_local -= value
            else:
                value = min(remaining_open, capacity[s.source_id]) if s.open_house else 0
                remaining_open -= value
            if value > 0:
                allocations.append({"source_id": s.source_id, "group": group, "kw": value})
                capacity[s.source_id] -= value
    return {
        "allocations": allocations,
        "uncovered_kw": remaining_local + remaining_open,
        "reason": None,
        "total_kw": max(total_kw, local_remaining),
        "local_kw": local_remaining,
        "local_requirement_kw": local_kw,
        "committed_local_kw": local_committed if additional else None,
    }
