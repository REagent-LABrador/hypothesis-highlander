"""
IndicationThesis — the SHARED pipeline contract, mirrored from LABrador's thesis.ts (the Zod
schema the whole team composes on). Highlander speaks this so a genome IS a candidate thesis the
real nodes consume: the hypothesis node emits one, the evidence mapper + tractability review
enrich it, the recruitment forecaster scores it, and the economics node values it.

Field names are kept camelCase to match thesis.ts exactly, so `to_json()` round-trips to the
JSON the TypeScript/Python nodes already accept. Two additions come from the ratification agenda
in COORDINATION.md §6 (needed by Rafal's tractability node): `uniprotAccession` and
`mechanismHypothesis`. Evidence.direction is the thesis's supports|contradicts; the mapper's
`no_effect` is handled in adapters.py by convention (not a silent third value).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

MODALITY = {"antibody", "small_molecule", "peptide", "oligonucleotide", "cell_therapy", "other"}
DIRECTION = {"inhibit", "activate", "degrade", "block", "modulate"}
ENDPOINT_TYPE = {"continuous", "binary", "time_to_event"}
EVIDENCE_DIRECTION = {"supports", "contradicts"}
SOURCE_TYPE = {"trial", "publication", "database", "simulation"}
# ratification add (Rafal's tractability node uses this to choose which chains form the site)
MECHANISM_HYPOTHESIS = {"orthosteric", "allosteric", "oligomer_destabilisation", "unknown"}


@dataclass
class Evidence:
    """One traceable claim. `source` must be a real identifier (NCT/PMID/DOI/accession)."""
    claim: str
    direction: str          # supports | contradicts
    source: str
    sourceType: str         # trial | publication | database | simulation
    strength: float         # 0 = anecdote, 1 = randomised human outcome

    def validate(self) -> "Evidence":
        assert self.direction in EVIDENCE_DIRECTION, f"bad evidence direction {self.direction}"
        assert self.sourceType in SOURCE_TYPE, f"bad sourceType {self.sourceType}"
        assert 0.0 <= self.strength <= 1.0
        assert self.source, "evidence needs a real source id (won/lost inspectability here)"
        return self


@dataclass
class IndicationThesis:
    id: str
    asset: dict                 # {modality (MODALITY), name, sponsor?}
    target: dict                # {symbol, direction (DIRECTION)}
    disease: dict               # {name, subtype?}
    biomarkerPopulation: dict   # {marker, prevalenceInDisease 0-1, assayAvailable}
    endpoint: dict              # {name, type (ENDPOINT_TYPE), expectedEffectSize?}
    mechanism: str              # free-text causal story target→pathway→cell→phenotype
    evidence: list = field(default_factory=list)          # list[Evidence]
    tissue: str | None = None
    uncertainty: float | None = None                       # 0 determined … 1 pure speculation
    # ── ratification additions (COORDINATION.md §6) ──
    uniprotAccession: str | None = None                    # Rafal's join key for tractability
    mechanismHypothesis: str = "unknown"                   # drives chain selection in tractability

    def validate(self) -> "IndicationThesis":
        assert self.asset.get("modality") in MODALITY, f"bad modality {self.asset.get('modality')}"
        assert self.target.get("direction") in DIRECTION, f"bad target direction"
        assert self.endpoint.get("type") in ENDPOINT_TYPE, f"bad endpoint type"
        p = self.biomarkerPopulation.get("prevalenceInDisease")
        assert p is None or 0.0 <= p <= 1.0, "prevalenceInDisease out of [0,1]"
        assert self.mechanismHypothesis in MECHANISM_HYPOTHESIS
        for e in self.evidence:
            (e if isinstance(e, Evidence) else Evidence(**e)).validate()
        return self

    def to_json(self) -> dict:
        # OMIT unset optionals rather than emitting null: zod's .optional() accepts a missing key
        # (undefined) but REJECTS null — `tissue: null` is a hard parse failure at the TS boundary.
        d = {k: v for k, v in asdict(self).items() if v is not None}
        d["evidence"] = [asdict(e) if isinstance(e, Evidence) else e for e in self.evidence]
        return d

    def to_json_str(self) -> str:
        return json.dumps(self.to_json(), indent=2)

    @staticmethod
    def from_json(d: dict) -> "IndicationThesis":
        d = dict(d)
        d["evidence"] = [Evidence(**e) if isinstance(e, dict) else e for e in d.get("evidence", [])]
        return IndicationThesis(**{k: v for k, v in d.items()})
