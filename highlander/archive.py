"""
MAP-Elites archive + Pareto front — the "portfolio, not champion" deliverable.

Two views on the same population:
  • MAP-Elites grid — one champion per behavior cell (biomarker-class × modality × boldness).
    This ILLUMINATES the space: you keep the best idea in every biological niche, including the
    "crazy" ones, and coverage growing per generation is the "it's exploring" signal.
  • Pareto front — the non-dominated set across the four non-commensurable axes (plausibility,
    ROI, recruitability, bio-reality). This is what BD actually wants: the trade-off frontier,
    not a scalar winner. The front advancing per generation is the "it's learning" signal.

The archive is an append-only ledger (every fully-evaluated genome kept) for provenance.
"""
from __future__ import annotations

from .genome import Genome, AXES


class Archive:
    def __init__(self, weights: dict | None = None):
        self.cells: dict[tuple, Genome] = {}   # behavior cell -> champion (by composite)
        self.ledger: list[Genome] = []          # every fully-evaluated genome (append-only)
        self.weights = weights or {a: 0.25 for a in AXES}

    def insert(self, g: Genome) -> bool:
        """Insert a fully-evaluated genome. Returns True if it became (or replaced) a cell champion."""
        self.ledger.append(g)
        c = g.cell()
        cur = self.cells.get(c)
        if cur is None or g.composite(self.weights) > cur.composite(self.weights):
            self.cells[c] = g
            return True
        return False

    def elites(self) -> list[Genome]:
        return list(self.cells.values())

    def pareto_front(self) -> list[Genome]:
        """Non-dominated cell champions across all four axes."""
        el = self.elites()
        return [g for g in el if not any(o.dominates(g) for o in el if o.gid != g.gid)]

    def coverage(self) -> int:
        return len(self.cells)

    def best_on(self, axis: str):
        el = [g for g in self.elites() if axis in g.scores]
        return max(el, key=lambda g: g.scores[axis]) if el else None

    def top(self, n: int = 5) -> list[Genome]:
        return sorted(self.elites(), key=lambda g: g.composite(self.weights), reverse=True)[:n]

    def grid_summary(self) -> dict:
        """Occupancy per (biomarker_class × modality) with the boldness of the champion."""
        grid = {}
        for (bm, mod, bold), g in self.cells.items():
            grid.setdefault((bm, mod), {})[bold] = round(g.composite(self.weights), 3)
        return grid

    def stats(self) -> dict:
        front = self.pareto_front()
        return {"cells_filled": self.coverage(), "pareto_front_size": len(front),
                "evaluated_total": len(self.ledger),
                "best_roi": (self.best_on("roi").scores.get("roi") if self.best_on("roi") else None),
                "pareto_gids": [g.gid for g in front]}
