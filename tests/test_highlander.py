"""Regression tests for the Highlander loop. Run: .venv/bin/python -m pytest highlander/tests -q"""
from highlander.genome import Genome
from highlander.archive import Archive
from highlander.tiers import cascade, DEFAULT_TIERS, DEFAULT_GATES
from highlander.controller import Highlander


def test_pareto_dominance():
    a = Genome(biomarker="TNF", scores={"plausibility": 0.8, "roi": 0.6, "recruitability": 0.7, "bio_reality": 0.7})
    b = Genome(biomarker="X", scores={"plausibility": 0.5, "roi": 0.5, "recruitability": 0.6, "bio_reality": 0.6})
    assert a.dominates(b) and not b.dominates(a)
    c = Genome(biomarker="Y", scores={"plausibility": 0.9, "roi": 0.3, "recruitability": 0.7, "bio_reality": 0.7})
    assert not a.dominates(c) and not c.dominates(a)   # non-dominated trade-off


def test_cost_cascade_gates_before_expensive_tiers():
    """A hypothesis that fails plausibility must NOT reach the $$$ bio-reality tier."""
    g = Genome(biomarker="TLR7", modality="peptide", boldness="crazy", route="iv_infusion")
    cascade(g, DEFAULT_TIERS, DEFAULT_GATES)
    if g.scores["plausibility"] < DEFAULT_GATES["plausibility"]:
        assert g.dropped_at == "plausibility"
        assert "bio_reality" not in g.scores            # dear tier never spent
        assert g.reached_tier == 1


def test_budget_bounds_spend():
    hl = Highlander(seed=1, budget_units=200, use_llm=False)
    hl.run(generations=3, pop_size=20)
    assert hl.budget["spent"] <= hl.budget["total"] + 64   # never exceeds budget (+ one in-flight tier)


def test_archive_illumination_grows():
    hl = Highlander(seed=3, budget_units=2000, use_llm=False)
    res = hl.run(generations=4, pop_size=16)
    cov = [h["cells_filled"] for h in res["history"]]
    assert cov[-1] > cov[0]                              # coverage grows across generations
    assert res["stats"]["pareto_front_size"] >= 1
    # Pareto front is genuinely non-dominated
    front = hl.archive.pareto_front()
    for g in front:
        assert not any(o.dominates(g) for o in front if o.gid != g.gid)


def test_dedup_prevents_rescoring():
    hl = Highlander(seed=5, budget_units=3000, use_llm=False)
    res = hl.run(generations=4, pop_size=16)
    assert sum(h["deduped"] for h in res["history"]) > 0   # duplicates are caught, not re-evaluated
