"""Legacy/demo-only scalar adapters for the original mock Highlander search.

These functions are retained solely so ``python -m highlander run`` and the
historical demo remain reproducible.  They are not the production LABrador
composition seam and must never consume orchestrator packet snapshots.  In
particular, their [0,1] normalizers are search heuristics, not producer-native
scientific objectives.  Production integrations use ``packet_adapters`` and
``packet_consumer``, which preserve raw values, missingness, and categorical
tractability without scalar imputation.

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
_STUDY_TO_SOURCETYPE = {"clinical_trial": "trial"}   # all other paper findings remain publications


def _required_finite_number(value, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is required and must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} is required and must be a finite number")
    return number


def _paper_source(paper: dict) -> str | None:
    """Return only an inspectable DOI/PMID; local mapper row IDs are not evidence sources."""
    doi = str(paper.get("doi") or "").strip()
    if doi.lower().startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    if doi.startswith("10."):
        return doi
    pmid = str(paper.get("pmid") or "").strip()
    if pmid.isdigit():
        return f"PMID:{pmid}"
    return None


def adapter_A_graph_to_evidence(graph: dict) -> list[Evidence]:
    """Map the evidence mapper's knowledge graph (findings + papers) into thesis Evidence[].
    `no_effect` remains distinct and neutral. Findings without DOI/PMID provenance are dropped
    rather than laundering a local mapper row ID into the cross-module evidence contract."""
    papers = {p["id"]: p for p in graph.get("papers", []) if p.get("id")}
    out: list[Evidence] = []
    for f in graph.get("findings", []):
        says = f.get("says")
        if says == "yes":
            direction = "supports"
        elif says == "no":
            direction = "contradicts"
        elif says == "no_effect":
            direction = "no_effect"
        else:
            raise ValueError(f"unsupported mapper finding direction: {says!r}")
        p = papers.get(f.get("paper"), {})
        source = _paper_source(p)
        if source is None:
            continue
        study = p.get("study_type")
        if study not in _STUDY_STRENGTH:
            raise ValueError(f"unsupported or missing study_type: {study!r}")
        confidence = _required_finite_number(f.get("confidence"), "finding.confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("finding.confidence must be in [0,1]")
        strength = 0.5 * _STUDY_STRENGTH[study] + 0.5 * confidence
        if not f.get("is_own_result", True):    # background/citing another's work → weaker
            strength *= 0.5
        if says == "no_effect" or f.get("hedged"):
            strength *= 0.7
        out.append(Evidence(
            claim=f.get("quote") or f"{f.get('from')} {f.get('how')} {f.get('to')}: {says}",
            direction=direction,
            source=source,
            sourceType=_STUDY_TO_SOURCETYPE.get(study, "publication"),
            strength=round(strength, 3),
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
        raise ValueError("evidence is required; missing evidence has no numeric plausibility")
    weights = {"supports": 1.0, "contradicts": -1.0, "no_effect": 0.0}
    try:
        net = sum(weights[e.direction] * _required_finite_number(e.strength, "evidence.strength")
                  for e in evidence)
    except KeyError as exc:
        raise ValueError(f"unsupported evidence direction: {exc.args[0]!r}") from exc
    return round(1.0 / (1.0 + math.exp(-net)), 3)


def recruitability_from_forecaster(out: dict) -> float:
    """Use the forecaster's required native score; missing output is not imputed from months."""
    score = _required_finite_number(out.get("score"), "score")
    if not 0.0 <= score <= 1.0:
        raise ValueError("score must be in [0,1]")
    return round(score, 3)


def bio_from_tractability(review: dict) -> tuple[float, str]:
    """Return a legacy mock-search scalar; never use this in packet comparison.

    The tractability producer deliberately reports separate axes and no native
    scalar.  This historical demo heuristic collapses missing/conflicted states
    and therefore is intentionally excluded from the production consumer.
    """
    verdict = review.get("verdict")
    bases = {"small_molecule_tractable": 0.8, "insufficient_evidence": 0.4, "not_tractable": 0.15}
    if verdict not in bases:
        raise ValueError(f"recognized tractability verdict is required, got {verdict!r}")
    base = bases[verdict]
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
    summary = out.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("economics summary is required")
    p50 = _required_finite_number(summary.get("p50_rnpv"), "summary.p50_rnpv")
    scaled = (p50 / 1e6) / 400.0
    # Numerically stable sigmoid for adversarial but finite producer values.
    if scaled >= 0:
        score = 1.0 / (1.0 + math.exp(-scaled))
    else:
        exp_scaled = math.exp(scaled)
        score = exp_scaled / (1.0 + exp_scaled)
    return round(score, 3)
