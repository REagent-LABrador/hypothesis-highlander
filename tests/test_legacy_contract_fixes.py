"""Focused regression tests for locked producer-schema compatibility in legacy/demo helpers."""
import pytest

from highlander.adapters import (
    adapter_A_graph_to_evidence,
    bio_from_tractability,
    plausibility_from_evidence,
    recruitability_from_forecaster,
    roi_from_economics,
)
from highlander.genome import Genome
from highlander.thesis import Evidence, IndicationThesis


def _locked_thesis(**updates):
    payload = {
        "id": "hyp-irak4-ra-001",
        "asset": {"modality": "small_molecule", "name": "IRAK4 inhibitor"},
        "target": {
            "symbol": "IRAK4",
            "direction": "inhibit",
            "uniprot_accession": "Q9NWZ3",
        },
        "disease": {"name": "rheumatoid arthritis"},
        "biomarker_population": {
            "marker": "IRAK4-high",
            "prevalence_in_disease": 0.22,
            "assay_available": False,
        },
        "endpoint": {"name": "ACR50", "type": "binary"},
        "mechanism": "Inhibit IRAK4 signaling in biomarker-positive disease",
        "mechanism_hypothesis": "orthosteric",
        "evidence": [{
            "claim": "No measurable change in the stated endpoint",
            "direction": "no_effect",
            "source": "PMID:33557356",
            "source_type": "publication",
            "strength": 0.6,
        }],
    }
    payload.update(updates)
    return payload


def test_locked_thesis_roundtrip_preserves_id_nested_accession_and_false_assay():
    thesis = IndicationThesis.from_json(_locked_thesis())
    thesis.validate()
    genome = Genome.from_thesis(thesis)
    roundtrip = genome.to_thesis().to_json()

    assert genome.gid == "hyp-irak4-ra-001"
    assert genome.uniprot_accession == "Q9NWZ3"
    assert genome.assay_available is False
    assert roundtrip["id"] == "hyp-irak4-ra-001"
    assert roundtrip["target"]["uniprot_accession"] == "Q9NWZ3"
    assert roundtrip["biomarker_population"]["assay_available"] is False
    assert roundtrip["evidence"][0]["direction"] == "no_effect"
    assert "uniprotAccession" not in roundtrip
    assert "biomarkerPopulation" not in roundtrip


def test_legacy_camel_case_thesis_is_rejected_at_current_public_boundary():
    payload = _locked_thesis()
    payload["biomarkerPopulation"] = payload.pop("biomarker_population")
    with pytest.raises(ValueError, match="biomarker_population"):
        IndicationThesis.from_json(payload)


def test_mapper_adapter_uses_only_doi_or_pmid_and_keeps_no_effect_neutral():
    graph = {
        "papers": [
            {"id": "p-local", "study_type": "animal"},
            {"id": "p-pmid", "pmid": "33557356", "study_type": "computational"},
            {"id": "p-doi", "doi": "https://doi.org/10.1000/example", "study_type": "animal"},
        ],
        "findings": [
            {"paper": "p-local", "says": "yes", "confidence": 0.9, "quote": "local only"},
            {"paper": "p-pmid", "says": "no_effect", "confidence": 0.8, "quote": "null"},
            {"paper": "p-doi", "says": "yes", "confidence": 0.7, "quote": "support"},
        ],
    }

    evidence = adapter_A_graph_to_evidence(graph)
    assert [(e.source, e.direction, e.sourceType) for e in evidence] == [
        ("PMID:33557356", "no_effect", "publication"),
        ("10.1000/example", "supports", "publication"),
    ]
    support_only = [evidence[1]]
    assert plausibility_from_evidence(evidence) == plausibility_from_evidence(support_only)


def test_economics_reads_locked_nested_p50_without_neutral_default():
    actual = roi_from_economics({"summary": {"p50_rnpv": -23_415_724.72}})
    assert actual == 0.485
    assert actual != 0.5


@pytest.mark.parametrize(
    "call",
    [
        lambda: plausibility_from_evidence([]),
        lambda: recruitability_from_forecaster({"simulatedMonthsToEnroll": 24}),
        lambda: bio_from_tractability({}),
        lambda: roi_from_economics({}),
        lambda: roi_from_economics({"p50_rnpv": -23_415_724.72}),
        lambda: roi_from_economics({"summary": {}}),
        lambda: roi_from_economics({"summary": {"p50_rnpv": "-23415724.72"}}),
        lambda: roi_from_economics({"summary": {"p50_rnpv": float("nan")}}),
    ],
)
def test_missing_or_nonfinite_outputs_fail_closed_instead_of_becoming_numbers(call):
    with pytest.raises(ValueError):
        call()


def test_no_effect_is_valid_and_neutral_in_legacy_plausibility():
    support = Evidence("support", "supports", "10.1000/support", "publication", 0.8)
    no_effect = Evidence("null", "no_effect", "PMID:1", "publication", 1.0)
    no_effect.validate()
    assert plausibility_from_evidence([support, no_effect]) == plausibility_from_evidence([support])
