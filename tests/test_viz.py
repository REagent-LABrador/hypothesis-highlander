"""
RED→GREEN TDD for the visualization-backing functions (highlander/viz.py).
These are the pure, testable functions the Streamlit page renders: the MAP-Elites grid matrix,
the 2D Pareto set, the per-generation learning curve, and the per-axis leaders.
"""
from highlander.genome import Genome
from highlander.archive import Archive
from highlander import viz   # does not exist yet → RED


def _archive():
    a = Archive()
    def g(bm, mod, bold, sc):
        return Genome(biomarker=bm, modality=mod, boldness=bold, scores=sc)
    a.insert(g("TNF", "small_molecule", "supported",
               {"plausibility": .8, "roi": .5, "recruitability": .7, "bio_reality": .7}))   # comp .675
    a.insert(g("IL6R", "small_molecule", "plausible",
               {"plausibility": .6, "roi": .6, "recruitability": .6, "bio_reality": .6}))    # comp .60
    a.insert(g("TNF", "peptide", "crazy",
               {"plausibility": .3, "roi": .4, "recruitability": .5, "bio_reality": .5}))     # comp .425
    return a


def test_grid_matrix_shape_and_values():
    m = viz.grid_matrix(_archive(), "biomarker", "modality")
    assert set(m["rows"]) >= {"TNF", "IL6R"}
    assert set(m["cols"]) >= {"small_molecule", "peptide"}
    ri, ci = m["rows"].index("TNF"), m["cols"].index("small_molecule")
    assert abs(m["z"][ri][ci] - 0.675) < 1e-6            # champion composite for that cell
    ri2, ci2 = m["rows"].index("IL6R"), m["cols"].index("peptide")
    assert m["z"][ri2][ci2] is None                      # empty niche → None (renders blank)


def test_pareto_2d_is_nondominated():
    p = viz.pareto_2d(_archive(), "plausibility", "roi")
    labels = {pt["label"] for pt in p["pareto"]}
    # TNF(.8,.5) and IL6R(.6,.6) are mutually non-dominated; TNF-peptide(.3,.4) is dominated by both
    assert any("TNF/small_molecule" in l for l in labels)
    assert any("IL6R" in l for l in labels)
    assert all("peptide" not in l for l in labels)       # the dominated point is NOT on the front
    dom = {pt["label"] for pt in p["dominated"]}
    assert any("peptide" in l for l in dom)
    # each pareto point is genuinely non-dominated in 2D
    pts = [(pt["x"], pt["y"]) for pt in p["pareto"]]
    for x, y in pts:
        assert not any((ox >= x and oy >= y and (ox > x or oy > y)) for ox, oy in pts if (ox, oy) != (x, y))


def test_learning_curve_tracks_history():
    hist = [{"generation": 0, "cells_filled": 3, "pareto_front": 2, "best_roi": .5},
            {"generation": 1, "cells_filled": 5, "pareto_front": 3, "best_roi": .6}]
    lc = viz.learning_curve(hist)
    assert lc["generation"] == [0, 1]
    assert lc["coverage"] == [3, 5]
    assert lc["pareto"] == [2, 3]


def test_axis_leaders():
    al = viz.axis_leaders(_archive())
    assert abs(al["plausibility"]["score"] - 0.8) < 1e-9
    assert "TNF/small_molecule" in al["plausibility"]["label"]
