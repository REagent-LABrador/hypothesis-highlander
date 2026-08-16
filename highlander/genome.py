"""
The Genome — the unit that gets enumerated, mutated, evaluated, and archived.

A genome is a STRUCTURED HYPOTHESIS (biomarker × mechanism × modality × boldness), not a
sequence — so it carries a biological idea plus everything the fitness cascade needs. Scores
are per-axis in [0,1]; provenance (per-tier rationale + parent lineage + eval cost) travels
with it, mirroring the ROI tool's observability keystone.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict

# "well-supported → crazy" is a FIRST-CLASS behavior axis (MAP-Elites keeps a champion in each
# boldness niche, so speculative-but-high-upside ideas are illuminated, not discarded).
BOLDNESS = ["supported", "plausible", "speculative", "crazy"]
AXES = ["plausibility", "roi", "recruitability", "bio_reality"]


@dataclass
class Genome:
    biomarker: str                      # e.g. "IL6R", "JAK1", "TNF", "BTK"
    modality: str = "small_molecule"    # small_molecule | peptide
    route: str = "oral"                 # oral | sc_injectable | iv_infusion
    indication: str = "rheumatoid arthritis"
    moa: dict = field(default_factory=dict)   # mechanism-of-action representation (from lit module)
    hypothesis: str = ""                 # the free-text hypothesis (generator output)
    boldness: str = "plausible"          # supported | plausible | speculative | crazy
    molecule: str = ""                   # SMILES / peptide seq proxy (optional; drives COGS tier)
    current_phase: str = "phase1"
    # ── IndicationThesis fields (so a genome IS the shared LABrador contract) ──
    target_direction: str = "inhibit"    # inhibit|activate|degrade|block|modulate
    uniprot_accession: str = ""          # Rafal's tractability join key
    mechanism_hypothesis: str = "unknown"  # orthosteric|allosteric|oligomer_destabilisation|unknown
    prevalence_in_disease: float = 0.4   # biomarkerPopulation.prevalenceInDisease (recruitment's key input)
    assay_available: bool = True          # required by the locked IndicationThesis contract
    endpoint_name: str = "ACR20 response"
    endpoint_type: str = "binary"        # continuous|binary|time_to_event
    tissue: str = "synovium"
    evidence: list = field(default_factory=list)   # list[thesis.Evidence] (filled by adapter A)
    scores: dict = field(default_factory=dict)     # axis -> [0,1]
    detail: dict = field(default_factory=dict)     # tier -> {value, rationale, source}
    parent_ids: list = field(default_factory=list)
    generation: int = 0
    eval_cost: float = 0.0               # accumulated eval cost (relative units)
    reached_tier: int = 0                # highest tier evaluated (0 = dropped at dedup)
    dropped_at: str = ""                 # "" if fully evaluated, else the gate that killed it
    gid: str = ""

    def __post_init__(self):
        if not self.gid:
            key = f"{self.biomarker}|{self.modality}|{self.route}|{self.hypothesis}|{self.boldness}"
            self.gid = hashlib.sha1(key.encode()).hexdigest()[:10]

    # ── MAP-Elites behavior descriptor (about the HYPOTHESIS, not the outcome — avoids reward hacking) ──
    def biomarker_class(self) -> str:
        # guard whitespace-only / non-str input (adversarial LLM output must not crash a paid run)
        s = str(self.biomarker or "").strip().upper()
        return s.split()[0] if s else "?"

    def cell(self) -> tuple:
        return (self.biomarker_class(), self.modality, self.boldness)

    def dedup_key(self) -> str:
        return f"{self.biomarker_class()}|{self.modality}|{self.route}|{self.hypothesis[:80].strip().lower()}"

    # ── selection helpers ──
    def composite(self, weights: dict) -> float:
        return sum(weights.get(a, 0.0) * self.scores.get(a, 0.0) for a in AXES)

    def dominates(self, other: "Genome", axes=AXES) -> bool:
        """Pareto dominance: >= on all axes and > on at least one (only over evaluated axes)."""
        common = [a for a in axes if a in self.scores and a in other.scores]
        if not common:
            return False
        ge = all(self.scores[a] >= other.scores[a] for a in common)
        gt = any(self.scores[a] > other.scores[a] for a in common)
        return ge and gt

    def to_dict(self) -> dict:
        return asdict(self)

    # ── interop: emit the shared IndicationThesis contract the LABrador nodes consume ──
    def to_thesis(self):
        from .thesis import IndicationThesis
        _unc = {"supported": 0.2, "plausible": 0.45, "speculative": 0.7, "crazy": 0.9}
        target = {"symbol": self.biomarker, "direction": self.target_direction}
        if self.uniprot_accession:
            target["uniprotAccession"] = self.uniprot_accession
        return IndicationThesis(
            id=self.gid,
            asset={"modality": self.modality, "name": self.molecule or f"{self.biomarker}-{self.modality}"},
            target=target,
            disease={"name": self.indication},
            biomarkerPopulation={"marker": self.biomarker,
                                 "prevalenceInDisease": self.prevalence_in_disease,
                                 "assayAvailable": self.assay_available},
            endpoint={"name": self.endpoint_name, "type": self.endpoint_type},
            mechanism=self.hypothesis or f"Modulate {self.biomarker} for {self.indication}",
            evidence=list(self.evidence),
            tissue=self.tissue,
            uncertainty=_unc.get(self.boldness, 0.5),
            mechanismHypothesis=self.mechanism_hypothesis,
        )

    @staticmethod
    def from_thesis(t, boldness="plausible", generation=0, molecule="", current_phase="phase1"):
        return Genome(
            biomarker=t.target["symbol"], modality=t.asset["modality"],
            route=("oral" if t.asset["modality"] == "small_molecule" else "sc_injectable"),
            indication=t.disease["name"], hypothesis=t.mechanism, boldness=boldness,
            molecule=molecule or (t.asset.get("name") or ""), current_phase=current_phase,
            target_direction=t.target["direction"],
            uniprot_accession=t.target.get("uniprotAccession") or "",
            mechanism_hypothesis=t.mechanismHypothesis,
            prevalence_in_disease=t.biomarkerPopulation.get("prevalenceInDisease", 0.4),
            assay_available=t.biomarkerPopulation["assayAvailable"],
            endpoint_name=t.endpoint.get("name", ""), endpoint_type=t.endpoint.get("type", "binary"),
            tissue=t.tissue, evidence=list(t.evidence), generation=generation, gid=t.id,
        )
