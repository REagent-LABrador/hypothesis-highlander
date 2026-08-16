"""
Visualization-backing functions (pure, tested) for the Highlander Streamlit page.

Separated from rendering so the "it's learning" story is data we can unit-test:
  - grid_matrix   : MAP-Elites illumination heatmap (best composite per behavior cell)
  - pareto_2d     : the non-dominated trade-off set on any two axes (the BD deliverable)
  - learning_curve: coverage + Pareto size per generation (the learning signal)
  - axis_leaders  : the champion on each individual axis
"""
from __future__ import annotations

from .genome import AXES


def _dim(g, dim: str):
    return {"biomarker": g.biomarker_class(), "modality": g.modality, "boldness": g.boldness}[dim]


def _label(g) -> str:
    return f"{g.biomarker}/{g.modality}/{g.boldness}"


def grid_matrix(archive, row_dim: str = "biomarker", col_dim: str = "modality") -> dict:
    """2D matrix of the best champion composite per (row_dim, col_dim) cell; None where empty."""
    elites = archive.elites()
    rows = sorted({_dim(g, row_dim) for g in elites})
    cols = sorted({_dim(g, col_dim) for g in elites})
    z = [[None for _ in cols] for _ in rows]
    label = [["" for _ in cols] for _ in rows]
    for g in elites:
        r, c = rows.index(_dim(g, row_dim)), cols.index(_dim(g, col_dim))
        comp = g.composite(archive.weights)
        if z[r][c] is None or comp > z[r][c]:
            z[r][c] = round(comp, 3)
            label[r][c] = _label(g)
    return {"rows": rows, "cols": cols, "z": z, "label": label,
            "row_dim": row_dim, "col_dim": col_dim}


def pareto_2d(archive, x_axis: str, y_axis: str) -> dict:
    """Split cell champions into the 2D non-dominated set (front) and the dominated rest."""
    pts = [{"x": g.scores[x_axis], "y": g.scores[y_axis], "label": _label(g)}
           for g in archive.elites() if x_axis in g.scores and y_axis in g.scores]

    def dominated(p) -> bool:
        return any((q["x"] >= p["x"] and q["y"] >= p["y"] and (q["x"] > p["x"] or q["y"] > p["y"]))
                   for q in pts if q is not p)

    return {"x_axis": x_axis, "y_axis": y_axis,
            "pareto": [p for p in pts if not dominated(p)],
            "dominated": [p for p in pts if dominated(p)]}


def learning_curve(history: list) -> dict:
    """Per-generation series for the learning-curve chart."""
    return {"generation": [h["generation"] for h in history],
            "coverage": [h["cells_filled"] for h in history],
            "pareto": [h["pareto_front"] for h in history],
            "best_roi": [(h.get("best_roi") or 0.0) for h in history]}


def axis_leaders(archive) -> dict:
    """The best champion on each axis independently."""
    out = {}
    for axis in AXES:
        cand = [g for g in archive.elites() if axis in g.scores]
        if cand:
            g = max(cand, key=lambda x: x.scores[axis])
            out[axis] = {"label": _label(g), "score": g.scores[axis]}
    return out
