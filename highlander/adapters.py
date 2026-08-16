"""
Interop adapters between LABrador modules — including the TWO UNOWNED adapters from
COORDINATION.md §5, plus normalizers that turn each module's native output into a [0,1] tier
score for the Highlander search (MAP-Elites / Pareto) WITHOUT violating the module's contract.

Module → Highlander tier map:
  research-evidence-mapper   → plausibility   (Adapter A: graph findings → thesis Evidence[])
  small-molecule-tractability→ bio_reality    (verdict+axes → search-only scalar; axes preserved)
  trial-recruitment-forecaster→ recruitability (uses its native `score`; months → launch delay)
  therapeutic-program-economics→ roi          (Adapter B feeds launch delay; rNPV → score)
"""
from __future__ import annotations

import math

from .thesis import Evidence

# ── Adapter A: evidence-mapper graph → thesis Evidence[] (COORDINATION §5, unowned) ──
_STUDY_STRENGTH = {"meta_analysis": 0.9, "clinical_trial": 0.8, "human_cohort": 0.6,
                   "animal": 0.4, "test_tube": 0.3, "computational": 0.2, "review": 0.3}
_STUDY_TO_SOURCETYPE = {"clinical_trial": "trial", "computational": "simulation"}   # else → publication


def adapter_A_graph_to_evidence(graph: dict) -> list[Evidence]:
    """Map the evidence mapper's knowledge graph (findings + papers) into thesis Evidence[].
    Convention for the mapper's `no_effect` (thesis has only supports|contradicts): map to
    `contradicts` with a "[no_effect]" note and reduced strength — NOT a silent third value."""
    papers = {p["id"]: p for p in graph.get("papers", [])}
    out: list[Evidence] = []
    for f in graph.get("findings", []):
        says = f.get("says", "yes")
        if says == "yes":
            direction, tag = "supports", ""
        elif says == "no":
            direction, tag = "contradicts", ""
        else:                                   # no_effect
            direction, tag = "contradicts", " [no_effect]"
        p = papers.get(f.get("paper"), {})
        study = p.get("study_type", "test_tube")
        strength = 0.5 * _STUDY_STRENGTH.get(study, 0.3) + 0.5 * float(f.get("confidence", 0.5))
        if not f.get("is_own_result", True):    # background/citing another's work → weaker
            strength *= 0.5
        if says == "no_effect" or f.get("hedged"):
            strength *= 0.7
        out.append(Evidence(
            claim=(f.get("quote") or f"{f.get('from')} {f.get('how')} {f.get('to')}: {says}") + tag,
            direction=direction,
            source=p.get("doi") or p.get("id") or f.get("paper", "unknown"),
            sourceType=_STUDY_TO_SOURCETYPE.get(study, "publication"),
            strength=round(min(1.0, max(0.0, strength)), 3),
        ))
    return out


# ── Adapter B: forecaster months → economics launch-delay (COORDINATION §5, unowned) ──
def adapter_B_months_to_launch_delay(simulated_months: float, target_months: float = 24.0,
                                     value_lost_per_delay_year: float = 5.06e6) -> dict:
    """Forecaster `simulatedMonthsToEnroll` → economics launch-delay input.
    NOT score→PoS (category error) and NOT months→stage_durations (only shifts cost timing) — the
    value-bearing slot the economics engine already prices is the launch delay in years."""
    delay_years = max(0.0, (float(simulated_months) - float(target_months)) / 12.0)
    return {"launch_delay_years": round(delay_years, 2),
            "value_lost_usd": round(delay_years * value_lost_per_delay_year),
            "basis": f"({simulated_months}mo − {target_months}mo target)/12"}


# ── Normalizers: native module output → [0,1] tier score for the QD search ──
def plausibility_from_evidence(evidence: list) -> float:
    """Net supports-vs-contradicts, strength-weighted, squashed to [0,1]."""
    if not evidence:
        return 0.3
    net = sum((e.strength if e.direction == "supports" else -e.strength) for e in evidence)
    return round(1.0 / (1.0 + math.exp(-net)), 3)


def recruitability_from_forecaster(out: dict) -> float:
    """Prefer the forecaster's native `score` (already 0-1); else derive from months."""
    if out.get("score") is not None:
        return round(float(out["score"]), 3)
    months = out.get("simulatedMonthsToEnroll", 36)
    return round(max(0.0, min(1.0, 1.0 - (months - 12) / 48.0)), 3)


def bio_from_tractability(review: dict) -> tuple[float, str]:
    """Search-only scalar from the tractability review. The module DELIBERATELY reports two
    non-averaged axes and no overall number; we do NOT fabricate one for the module — we derive a
    scalar for ranking only and preserve both axes in the rationale."""
    verdict = review.get("verdict", "insufficient_evidence")
    base = {"small_molecule_tractable": 0.8, "insufficient_evidence": 0.4, "not_tractable": 0.15}.get(verdict, 0.4)
    if review.get("tractability", {}).get("cryptic_pocket_risk") == "high":
        base -= 0.15
    if review.get("axis_conflict"):
        base -= 0.05
    score = round(max(0.0, min(1.0, base)), 3)
    tr = review.get("tractability", {})
    dr = tr.get("pocket_druggability", {})
    rationale = (f"verdict={verdict}; druggability {dr.get('min')}–{dr.get('max')}; "
                 f"cryptic={tr.get('cryptic_pocket_risk')}; axis_conflict={bool(review.get('axis_conflict'))} "
                 f"[search-only scalar — module reports two non-averaged axes]")
    return score, rationale


def roi_from_economics(out: dict) -> float:
    """Economics rNPV P50 → [0,1] via the same sigmoid the ROI tool uses ($0→0.5, +$400M→0.73)."""
    p50 = out.get("p50_rnpv", out.get("eNPV", 0.0))
    return round(1.0 / (1.0 + math.exp(-(p50 / 1e6) / 400.0)), 3)
