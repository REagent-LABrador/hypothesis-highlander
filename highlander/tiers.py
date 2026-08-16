"""Legacy/demo-only cost-tiered fitness cascade over local mock outputs.

This file must not be wired to real LABrador producer modules or terminal
orchestrator packets.  The production path is ``compare_packet_request``:
producers run upstream, the orchestrator freezes their outputs, and Highlander
compares those snapshots without calling a module or converting missing and
categorical results into plausible scalar values.

The historical cascade gates cheap-to-dear demo tiers so it can exercise the
archive and failure ledger without sibling repositories.

This node is SELF-CONTAINED (containerized): every tier here is a MOCK/lightweight stand-in behind
a clean interface, so the container runs with no dependency on sibling LABrador nodes. The old
tier-to-module analogy below is descriptive only and is not an integration instruction:

  plausibility  ← research-evidence-mapper      (Adapter A: graph findings → thesis Evidence[])
  roi           ← therapeutic-program-economics (replace _simple_rnpv with a `labrador analyze` call)
  recruitability← trial-recruitment-forecaster  (Adapter B: months → launch-delay)
  bio_reality   ← small-molecule-tractability-review (small-molecule only)

Do not replace these bodies with real calls.  Use the immutable production
packet consumer documented in COMPOSE.md.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Callable

from .adapters import (adapter_A_graph_to_evidence, adapter_B_months_to_launch_delay,
                       plausibility_from_evidence, recruitability_from_forecaster,
                       bio_from_tractability, roi_from_economics)


def _seeded_unit(*parts) -> float:
    h = hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


_LIT_SUPPORT = {"TNF": 0.95, "IL6R": 0.9, "JAK1": 0.9, "JAK": 0.9, "IL17": 0.85, "IL23": 0.85,
                "CD20": 0.8, "BTK": 0.6, "GM-CSF": 0.55, "TLR7": 0.4, "S1P": 0.5}
_BOLDNESS_PLAUS = {"supported": 0.9, "plausible": 0.7, "speculative": 0.45, "crazy": 0.2}
_BIO_TRACTABILITY = {"small_molecule": 0.7, "peptide": 0.6}


@dataclass
class Tier:
    name: str
    axis: str
    cost: float
    fn: Callable


# ── T1 plausibility ← research-evidence-mapper (via the REAL Adapter A) ──
def _mock_evidence_graph(g):
    supp = _LIT_SUPPORT.get(g.biomarker_class(), 0.35)
    papers = [{"id": "p1", "doi": f"10.1000/{g.biomarker_class()}.cohort", "study_type": "human_cohort"},
              {"id": "p2", "doi": f"10.1000/{g.biomarker_class()}.rct", "study_type": "clinical_trial"}]
    findings = [{"from": g.biomarker_class(), "how": g.target_direction, "to": g.indication, "says": "yes",
                 "quote": f"{g.biomarker} {g.target_direction} improves {g.indication}", "paper": "p2",
                 "confidence": supp, "is_own_result": True}]
    if g.boldness in ("speculative", "crazy"):
        findings.append({"from": g.biomarker_class(), "how": g.target_direction, "to": g.indication,
                         "says": "no", "quote": "no significant benefit observed", "paper": "p1",
                         "confidence": 0.5, "is_own_result": True})
    return {"papers": papers, "findings": findings}


def _plausibility(g):
    ev = adapter_A_graph_to_evidence(_mock_evidence_graph(g))
    g.evidence = ev
    score = plausibility_from_evidence(ev)
    return score, (f"{len(ev)} evidence items via Adapter A → net plausibility "
                   "[MOCK graph; real module: research-evidence-mapper]"), {"n_evidence": len(ev)}


# ── T2 ROI/rNPV — self-contained proxy (COMPOSE.md: replace with therapeutic-program-economics) ──
def _simple_rnpv(g):
    """Lightweight, dependency-free rNPV proxy so the container runs standalone. In the composed
    pipeline this is replaced by a call to the therapeutic-program-economics node (labrador analyze
    → rNPV JSON → roi_from_economics). Labeled a proxy; NOT decision-grade."""
    pos = {"phase1": 0.10, "phase2": 0.19, "phase3": 0.30, "filed": 0.90}.get(g.current_phase, 0.15)
    peak = g.prevalence_in_disease * 1.3e6 * 0.12 * 30_000 * 0.6      # eligible × share × net price × persistence (RA proxy)
    unrisked = peak * 0.55 * 6.0                                      # operating margin × PV multiplier
    cost = {"phase1": 320e6, "phase2": 300e6, "phase3": 250e6, "filed": 30e6}.get(g.current_phase, 300e6)
    return pos * unrisked - cost, pos


def _roi(g):
    enpv, pos = _simple_rnpv(g)
    score = roi_from_economics({"summary": {"p50_rnpv": enpv}})
    return score, (f"eNPV ${enpv/1e6:.0f}M, cumPoS {pos:.0%} "
                   "[SELF-CONTAINED PROXY; compose with therapeutic-program-economics — NOT_DECISION_GRADE]"), {"eNPV": enpv}


# ── T3 clinical recruitability ← trial-recruitment-forecaster (+ REAL Adapter B) ──
def _recruitability(g):
    prev = max(g.prevalence_in_disease, 0.02)
    base_months = 18.0 + 6.0 * math.log(1.0 / prev + 1.0)
    phase_mult = {"phase1": 0.8, "phase2": 1.0, "phase3": 1.4, "filed": 0.5}.get(g.current_phase, 1.0)
    months = base_months * phase_mult
    fc = {"simulatedMonthsToEnroll": round(months, 1), "score": max(0.0, min(1.0, 1.0 - (months - 12) / 48.0))}
    delay = adapter_B_months_to_launch_delay(months)
    g.detail["launch_delay"] = delay
    score = recruitability_from_forecaster(fc)
    return score, (f"prev {prev:.0%} → {months:.0f}mo enroll → launch delay {delay['launch_delay_years']}y "
                   f"(Adapter B, ${delay['value_lost_usd']/1e6:.0f}M lost) "
                   "[MOCK forecaster; real: trial-recruitment-forecaster]"), {"months": months, **delay}


# ── T4 biological reality ← small-molecule-tractability-review (small-molecule only) ──
def _mock_tractability(g):
    supp = _LIT_SUPPORT.get(g.biomarker_class(), 0.35)
    verdict = ("small_molecule_tractable" if supp >= 0.7 else
               "insufficient_evidence" if supp >= 0.45 else "not_tractable")
    return {"verdict": verdict, "axis_conflict": None,
            "tractability": {"cryptic_pocket_risk": "high" if g.boldness in ("speculative", "crazy") else "low",
                             "pocket_druggability": {"min": round(supp * 0.5, 2), "max": round(supp, 2)}}}


def _bio_reality(g):
    if g.modality == "small_molecule":
        score, rationale = bio_from_tractability(_mock_tractability(g))
        return score, rationale + " [MOCK review; real: small-molecule-tractability-review]", {"modality": g.modality}
    tract = _BIO_TRACTABILITY.get(g.modality, 0.5)
    bold_pen = {"supported": 1.0, "plausible": 0.9, "speculative": 0.7, "crazy": 0.5}.get(g.boldness, 0.8)
    return round(tract * bold_pen, 3), ("peptide developability proxy (Proto/ESMFold stub); "
                                        "tractability review is small-molecule-only"), {"modality": g.modality}


DEFAULT_TIERS = [
    Tier("plausibility", "plausibility", 1.0, _plausibility),
    Tier("roi", "roi", 8.0, _roi),
    Tier("recruitability", "recruitability", 15.0, _recruitability),
    Tier("bio_reality", "bio_reality", 40.0, _bio_reality),
]

# Gates sit INSIDE the mock score spreads (plausibility 0.51-0.71, roi 0.53-0.82) so they actually
# fire: weak hypotheses die cheap, the failure ledger fills, and the generator's failure
# conditioning is exercised end-to-end. Gates below every attainable score = dead code.
DEFAULT_GATES = {"plausibility": 0.55, "roi": 0.60, "recruitability": 0.30, "bio_reality": 0.0}


def cascade(g, tiers=None, gates=None, budget=None):
    """Run the gated cascade on one genome. Stops at the first tier below its gate or when budget
    is exhausted. Mutates g (scores/detail/eval_cost/reached_tier/dropped_at)."""
    tiers = tiers or DEFAULT_TIERS
    gates = gates or DEFAULT_GATES
    for i, t in enumerate(tiers, 1):
        if budget is not None and budget["spent"] + t.cost > budget["total"]:
            g.dropped_at = "budget"
            break
        score, rationale, detail = t.fn(g)
        g.scores[t.axis] = round(float(score), 4)
        g.detail[t.name] = {"score": round(float(score), 4), "rationale": rationale, **detail}
        g.eval_cost += t.cost
        g.reached_tier = i
        if budget is not None:
            budget["spent"] += t.cost
        if score < gates.get(t.axis, 0.0):
            g.dropped_at = t.name
            break
    return g
