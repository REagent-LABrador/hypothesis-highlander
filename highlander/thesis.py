"""Canonical public snake_case IndicationThesis boundary.

The legacy search keeps its internal Python attribute names, but every serialized
thesis is validated against the immutable platform contract consumed by the
current clinical station.  CamelCase producer payloads are no longer accepted at
this boundary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator

MODALITY = {"antibody", "small_molecule", "peptide", "oligonucleotide", "cell_therapy", "other"}
DIRECTION = {"inhibit", "activate", "degrade", "block", "modulate"}
ENDPOINT_TYPE = {"continuous", "binary", "time_to_event"}
EVIDENCE_DIRECTION = {"supports", "contradicts", "no_effect"}
SOURCE_TYPE = {"trial", "publication", "database", "simulation"}
# ratification add (Rafal's tractability node uses this to choose which chains form the site)
MECHANISM_HYPOTHESIS = {"orthosteric", "allosteric", "oligomer_destabilisation", "unknown"}

_CONTRACT_PATH = Path(__file__).with_name("contracts") / "indication-thesis.schema.json"
_WIRE_VALIDATOR = Draft202012Validator(json.loads(_CONTRACT_PATH.read_text()))


def validate_indication_thesis_wire(value: dict) -> dict:
    """Return ``value`` or raise a concise error for the canonical wire contract."""

    errors = sorted(_WIRE_VALIDATOR.iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise ValueError(f"IndicationThesis wire contract at {path}: {first.message}")
    return value


@dataclass
class Evidence:
    """One traceable claim. `source` must be a real identifier (NCT/PMID/DOI/accession)."""
    claim: str
    direction: str          # supports | contradicts | no_effect
    source: str
    sourceType: str         # trial | publication | database | simulation
    strength: float         # 0 = anecdote, 1 = randomised human outcome

    def validate(self) -> "Evidence":
        assert self.direction in EVIDENCE_DIRECTION, f"bad evidence direction {self.direction}"
        assert self.sourceType in SOURCE_TYPE, f"bad sourceType {self.sourceType}"
        assert 0.0 <= self.strength <= 1.0
        assert self.source, "evidence needs a real source id (won/lost inspectability here)"
        return self

    def to_wire(self) -> dict:
        return {
            "claim": self.claim,
            "direction": self.direction,
            "source": self.source,
            "source_type": self.sourceType,
            "strength": self.strength,
        }

    @staticmethod
    def from_wire(value: dict) -> "Evidence":
        return Evidence(
            claim=value["claim"],
            direction=value["direction"],
            source=value["source"],
            sourceType=value["source_type"],
            strength=value["strength"],
        )


@dataclass
class IndicationThesis:
    id: str
    asset: dict                 # {modality (MODALITY), name, sponsor?}
    target: dict                # {symbol, direction (DIRECTION), uniprotAccession?}
    disease: dict               # {name, subtype?}
    biomarkerPopulation: dict   # {marker, prevalenceInDisease 0-1, assayAvailable}
    endpoint: dict              # {name, type (ENDPOINT_TYPE), expectedEffectSize?}
    mechanism: str              # free-text causal story target→pathway→cell→phenotype
    evidence: list = field(default_factory=list)          # list[Evidence]
    tissue: str | None = None
    uncertainty: float | None = None                       # 0 determined … 1 pure speculation
    mechanismHypothesis: str = "unknown"                   # drives chain selection in tractability
    asOfDate: str | None = None

    @property
    def uniprotAccession(self) -> str | None:
        """Read-only compatibility alias; canonical JSON keeps the accession under `target`."""
        return self.target.get("uniprotAccession")

    def validate(self) -> "IndicationThesis":
        assert self.asset.get("modality") in MODALITY, f"bad modality {self.asset.get('modality')}"
        assert self.target.get("direction") in DIRECTION, f"bad target direction"
        assert self.endpoint.get("type") in ENDPOINT_TYPE, f"bad endpoint type"
        assert isinstance(self.biomarkerPopulation.get("assayAvailable"), bool), (
            "assayAvailable must be boolean"
        )
        p = self.biomarkerPopulation.get("prevalenceInDisease")
        assert p is None or 0.0 <= p <= 1.0, "prevalenceInDisease out of [0,1]"
        assert self.mechanismHypothesis in MECHANISM_HYPOTHESIS
        for e in self.evidence:
            (e if isinstance(e, Evidence) else Evidence(**e)).validate()
        return self

    def to_json(self) -> dict:
        self.validate()
        target = {
            "symbol": self.target["symbol"],
            "direction": self.target["direction"],
        }
        accession = self.target.get("uniprotAccession")
        if accession is not None:
            target["uniprot_accession"] = accession
        biomarker = {
            "marker": self.biomarkerPopulation["marker"],
            "prevalence_in_disease": self.biomarkerPopulation["prevalenceInDisease"],
            "assay_available": self.biomarkerPopulation["assayAvailable"],
        }
        endpoint = {
            "name": self.endpoint["name"],
            "type": self.endpoint["type"],
        }
        if self.endpoint.get("expectedEffectSize") is not None:
            endpoint["expected_effect_size"] = self.endpoint["expectedEffectSize"]
        evidence = [
            item.to_wire() if isinstance(item, Evidence) else Evidence(**item).to_wire()
            for item in self.evidence
        ]
        wire = {
            "id": self.id,
            "asset": self.asset,
            "target": target,
            "disease": self.disease,
            "biomarker_population": biomarker,
            "endpoint": endpoint,
            "mechanism": self.mechanism,
            "mechanism_hypothesis": self.mechanismHypothesis,
            "evidence": evidence,
        }
        if self.tissue is not None:
            wire["tissue"] = self.tissue
        if self.uncertainty is not None:
            wire["uncertainty"] = self.uncertainty
        if self.asOfDate is not None:
            wire["as_of_date"] = self.asOfDate
        return validate_indication_thesis_wire(wire)

    def to_json_str(self) -> str:
        return json.dumps(self.to_json(), indent=2)

    @staticmethod
    def from_json(d: dict) -> "IndicationThesis":
        wire = validate_indication_thesis_wire(dict(d))
        target = dict(wire["target"])
        accession = target.pop("uniprot_accession", None)
        if accession is not None:
            target["uniprotAccession"] = accession
        biomarker = wire["biomarker_population"]
        endpoint_wire = wire["endpoint"]
        endpoint = {"name": endpoint_wire["name"], "type": endpoint_wire["type"]}
        if "expected_effect_size" in endpoint_wire:
            endpoint["expectedEffectSize"] = endpoint_wire["expected_effect_size"]
        return IndicationThesis(
            id=wire["id"],
            asset=dict(wire["asset"]),
            target=target,
            disease=dict(wire["disease"]),
            biomarkerPopulation={
                "marker": biomarker["marker"],
                "prevalenceInDisease": biomarker["prevalence_in_disease"],
                "assayAvailable": biomarker["assay_available"],
            },
            endpoint=endpoint,
            mechanism=wire["mechanism"],
            evidence=[Evidence.from_wire(item) for item in wire.get("evidence", [])],
            tissue=wire.get("tissue"),
            uncertainty=wire.get("uncertainty"),
            mechanismHypothesis=wire.get("mechanism_hypothesis") or "unknown",
            asOfDate=wire.get("as_of_date"),
        )
