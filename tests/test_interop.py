"""
Interoperability tests: the shared IndicationThesis contract + the two LABrador adapters.
These guard that Highlander speaks the pipeline's language and that the unowned adapters behave.
"""
from highlander.genome import Genome
from highlander.thesis import IndicationThesis, Evidence
from highlander.adapters import (adapter_A_graph_to_evidence, adapter_B_months_to_launch_delay,
                                 plausibility_from_evidence)
from highlander.controller import Highlander


def test_genome_emits_valid_thesis():
    g = Genome(biomarker="TNF", modality="small_molecule", target_direction="inhibit",
               uniprot_accession="P01375", mechanism_hypothesis="oligomer_destabilisation",
               prevalence_in_disease=1.0, endpoint_type="binary")
    t = g.to_thesis()
    assert isinstance(t, IndicationThesis)
    t.validate()                                   # matches thesis.ts contract (modality/direction/endpoint/prevalence)
    assert t.target["symbol"] == "TNF"
    assert t.target["uniprotAccession"] == "P01375"
    assert "uniprotAccession" not in t.to_json()
    assert t.mechanismHypothesis == "oligomer_destabilisation"


def test_thesis_json_roundtrip():
    g = Genome(biomarker="JAK1", modality="small_molecule")
    t = g.to_thesis()
    back = IndicationThesis.from_json(t.to_json())
    assert back.to_json() == t.to_json()


def test_adapter_A_maps_findings_to_evidence():
    graph = {"papers": [{"id": "p2", "doi": "10.1/rct", "study_type": "clinical_trial"},
                        {"id": "p1", "doi": "10.1/cohort", "study_type": "human_cohort"}],
             "findings": [{"from": "TNF", "how": "inhibit", "to": "RA", "says": "yes",
                           "quote": "TNF inhibition improves RA", "paper": "p2", "confidence": 0.9, "is_own_result": True},
                          {"from": "TNF", "how": "inhibit", "to": "RA", "says": "no_effect",
                           "quote": "no change observed", "paper": "p1", "confidence": 0.6, "is_own_result": True}]}
    ev = adapter_A_graph_to_evidence(graph)
    assert len(ev) == 2
    for e in ev:
        e.validate()                                # produces contract-valid Evidence
    assert ev[0].direction == "supports" and ev[0].source == "10.1/rct"
    # The locked thesis schema preserves no_effect as a distinct, neutral direction.
    assert ev[1].direction == "no_effect"
    assert ev[1].strength < ev[0].strength          # no_effect / weaker study → lower strength
    assert plausibility_from_evidence(ev) == plausibility_from_evidence(ev[:1])


def test_adapter_B_months_to_launch_delay():
    slow = adapter_B_months_to_launch_delay(48, target_months=24)   # 24mo over target → 2y delay
    assert slow["launch_delay_years"] == 2.0
    assert slow["value_lost_usd"] > 0
    fast = adapter_B_months_to_launch_delay(18, target_months=24)   # under target → no delay
    assert fast["launch_delay_years"] == 0.0
    assert fast["value_lost_usd"] == 0


def test_loop_populates_thesis_evidence_and_launch_delay():
    hl = Highlander(seed=11, budget_units=1500, use_llm=False)
    hl.run(generations=2, pop_size=12)
    # at least one archived genome carries adapter-A evidence and an adapter-B launch delay
    archived = hl.archive.ledger
    assert any(g.evidence for g in archived)                       # adapter A ran
    assert any("launch_delay" in g.detail for g in archived)       # adapter B ran
    # and every archived genome still emits a valid thesis
    for g in archived[:5]:
        g.to_thesis().validate()
