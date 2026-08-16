"""
Regression tests for the adversarially-verified review fixes (see FABLE review):
H1 input guards, H2 bounded generator, H4 Pareto-over-ledger, H5 diverse parent pool,
H6 null-free thesis JSON, H10 gates that fire, C1 CLI seam.
"""
import json
import random

from highlander.genome import Genome
from highlander.archive import Archive
from highlander.generator import (_mk, _parent_pool, generate_offline,
                                  SEED_BIOMARKERS, MODALITIES)
from highlander.controller import Highlander
from highlander.__main__ import main as cli_main


# ── H1: adversarial LLM output must not crash a paid run ──
def test_whitespace_biomarker_does_not_crash():
    assert Genome(biomarker=" ").biomarker_class() == "?"
    assert Genome(biomarker="").biomarker_class() == "?"
    assert Genome(biomarker="tnf alpha").biomarker_class() == "TNF"


def test_mk_coerces_nonstring_llm_fields():
    g = _mk(" ", "small_molecule", "plausible", 0, hypothesis={"not": "a string"})
    assert isinstance(g.hypothesis, str) and g.hypothesis
    g.dedup_key()                                   # must not raise
    assert g.biomarker_class() == "?"


# ── H2: an all-dead failure set must not hang or zero-out the generator ──
def _dead_everything():
    return [{"biomarker": b, "modality": m, "dropped_at": "roi"}
            for b in SEED_BIOMARKERS for m in MODALITIES]


def test_seed_generation_survives_total_deadset():
    out = generate_offline(Archive(), _dead_everything(), 0, 8, random.Random(0))
    assert len(out) == 8                             # fallback ignores the dead-set, never proposes zero


def test_crossover_generation_terminates_on_total_deadset():
    ar = Archive()
    for i, bm in enumerate(SEED_BIOMARKERS[:3]):
        g = Genome(biomarker=bm, scores={"plausibility": .6, "roi": .6, "recruitability": .6, "bio_reality": .6})
        ar.insert(g)
    out = generate_offline(ar, _dead_everything(), 2, 6, random.Random(1))
    assert len(out) == 6                             # bounded loop + seed-space fallback, no hang


# ── H4: the front is computed over the ledger, not composite-selected champions ──
def test_pareto_front_includes_champion_evicted_genome():
    ar = Archive()
    best_roi = Genome(biomarker="TNF", hypothesis="roi-max",
                      scores={"plausibility": .3, "roi": .95, "recruitability": .3, "bio_reality": .3})
    balanced = Genome(biomarker="TNF", hypothesis="balanced",
                      scores={"plausibility": .8, "roi": .5, "recruitability": .8, "bio_reality": .8})
    ar.insert(best_roi)
    ar.insert(balanced)                              # evicts best_roi from the (TNF,sm,plausible) cell
    assert ar.cells[balanced.cell()].gid == balanced.gid
    front = {g.gid for g in ar.pareto_front()}
    assert best_roi.gid in front and balanced.gid in front   # both non-dominated → both on the front
    assert ar.best_on("roi").gid == best_roi.gid             # axis leader survives eviction too


def test_pareto_front_dedupes_identical_score_vectors():
    ar = Archive()
    for h in ("a", "b", "c"):
        ar.insert(Genome(biomarker="JAK1", hypothesis=h,
                         scores={"plausibility": .7, "roi": .7, "recruitability": .7, "bio_reality": .7}))
    assert len(ar.pareto_front()) == 1               # mock ties must not balloon the front


# ── H5: parent pool spans all niches, not the composite top-8 ──
def test_parent_pool_keeps_bold_niches():
    ar = Archive()
    for i, bm in enumerate(SEED_BIOMARKERS[:9]):     # 9 strong 'supported' elites fill a top-8
        ar.insert(Genome(biomarker=bm, boldness="supported",
                         scores={"plausibility": .9, "roi": .9, "recruitability": .9, "bio_reality": .9}))
    crazy = Genome(biomarker="TLR7", boldness="crazy",
                   scores={"plausibility": .2, "roi": .2, "recruitability": .2, "bio_reality": .2})
    ar.insert(crazy)
    pool = _parent_pool(ar)
    assert any(g.boldness == "crazy" for g in pool)  # top(8) would exclude it; elites() must not


# ── H6: thesis JSON is null-free (zod .optional() rejects null) ──
def test_thesis_json_has_no_nulls():
    j = Genome(biomarker="TNF").to_thesis().to_json()        # uniprot unset → key OMITTED, not null
    assert None not in j.values()
    assert "uniprotAccession" not in j
    assert "uniprotAccession" not in j["target"]
    j2 = Genome(biomarker="TNF", uniprot_accession="P01375").to_thesis().to_json()
    assert "uniprotAccession" not in j2
    assert j2["target"]["uniprot_accession"] == "P01375"


# ── H10: gates sit inside the score spread, so the cascade actually kills ──
def test_gates_fire_and_feed_the_failure_ledger():
    hl = Highlander(seed=7, budget_units=3000, use_llm=False)
    hl.run(generations=2, pop_size=24)
    assert hl.failures                                        # deaths happen
    assert {f["dropped_at"] for f in hl.failures} & {"plausibility", "roi"}
    assert hl.archive.ledger                                  # and survivors still exist


# ── C1: the CLI seam writes consumable JSON with contract-valid theses ──
def test_cli_writes_result_with_pareto_theses(tmp_path):
    out = tmp_path / "result.json"
    rc = cli_main(["run", "--generations", "1", "--pop-size", "10", "--seed", "3",
                   "--budget", "1500", "--out", str(out)])
    assert rc == 0
    d = json.loads(out.read_text())
    assert d["history"] and "pareto_theses" in d
    for th in d["pareto_theses"]:
        assert None not in th.values()                        # null-free at the TS boundary
        assert th["target"]["symbol"]


def test_cli_config_file_sets_gates(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"gates": {"plausibility": 0.99, "roi": 0.0,
                                         "recruitability": 0.0, "bio_reality": 0.0},
                               "generations": 1, "pop_size": 8, "budget_units": 800}))
    out = tmp_path / "r.json"
    cli_main(["run", "--config", str(cfg), "--out", str(out)])
    d = json.loads(out.read_text())
    assert d["pareto_theses"] == []                           # 0.99 plausibility gate kills everything
    assert d["failure_ledger"]
