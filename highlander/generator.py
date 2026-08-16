"""
The mutation operator — proposes the next generation of hypotheses, conditioned on the elite
archive AND a failure ledger (FunSearch's trick: the generator reads what died and why, and
proposes offspring that avoid known dead ends). This is where "learning from each run" lives —
cheap archive-conditioning, no surrogate model required.

Two backends:
  • Claude (if ANTHROPIC_API_KEY set) — free-text hypotheses with a constrained JSON schema.
  • Offline sampler (default fallback) — deterministic seed space + crossover/mutation of elites,
    so the loop runs with zero keys for the demo.
"""
from __future__ import annotations

import json
import os
import random

from .genome import Genome, BOLDNESS

# RA-relevant seed targets with the IndicationThesis fields the pipeline needs
# {uniprot, target direction, biomarker prevalence in RA, mechanism_hypothesis for tractability}
BIOMARKER_META = {
    "TNF":    {"uniprot": "P01375", "direction": "inhibit", "prev": 1.00, "mech": "oligomer_destabilisation"},
    "IL6R":   {"uniprot": "P08887", "direction": "inhibit", "prev": 1.00, "mech": "orthosteric"},
    "JAK1":   {"uniprot": "P23458", "direction": "inhibit", "prev": 1.00, "mech": "orthosteric"},
    "IL17":   {"uniprot": "Q16552", "direction": "inhibit", "prev": 0.60, "mech": "orthosteric"},
    "IL23":   {"uniprot": "Q9NPF7", "direction": "inhibit", "prev": 0.50, "mech": "orthosteric"},
    "CD20":   {"uniprot": "P11836", "direction": "block",   "prev": 1.00, "mech": "orthosteric"},
    "BTK":    {"uniprot": "Q06187", "direction": "inhibit", "prev": 1.00, "mech": "orthosteric"},
    "GM-CSF": {"uniprot": "P04141", "direction": "inhibit", "prev": 0.70, "mech": "orthosteric"},
    "TLR7":   {"uniprot": "Q9NYK1", "direction": "modulate", "prev": 0.40, "mech": "allosteric"},
    "S1P":    {"uniprot": "P21453", "direction": "modulate", "prev": 0.60, "mech": "orthosteric"},
}
SEED_BIOMARKERS = list(BIOMARKER_META)
MODALITIES = ["small_molecule", "peptide"]
ROUTES = {"small_molecule": ["oral"], "peptide": ["sc_injectable", "iv_infusion"]}
_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _molecule_for(modality: str) -> str:
    return "CC1CCN(CC1N(C)c1ncnc2[nH]ccc12)C(=O)CC#N" if modality == "small_molecule" else "GEEFTGVVPILVELDGDANQ"


def _mk(biomarker, modality, boldness, gen, parents=None, hypothesis="", route=None):
    # coerce LLM-provided fields defensively: a non-str hypothesis or whitespace biomarker from a
    # parsed JSON reply must degrade to a template, never crash the run downstream (dedup_key/gid)
    biomarker = str(biomarker or "?").strip() or "?"
    hypothesis = str(hypothesis).strip() if hypothesis else ""
    route = route or ROUTES[modality][0]
    m = BIOMARKER_META.get(biomarker.upper(), {"uniprot": "", "direction": "inhibit", "prev": 0.5, "mech": "unknown"})
    return Genome(biomarker=biomarker, modality=modality, route=route, boldness=boldness,
                  molecule=_molecule_for(modality), current_phase="phase1", generation=gen,
                  parent_ids=parents or [],
                  hypothesis=hypothesis or f"{m['direction'].title()} {biomarker} via a {modality} for RA ({boldness}).",
                  moa={"target": biomarker, "axis": "immune", "boldness": boldness},
                  target_direction=m["direction"], uniprot_accession=m["uniprot"],
                  mechanism_hypothesis=m["mech"], prevalence_in_disease=m["prev"])


def _anthropic_key() -> str:
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not k and os.path.exists(_ENV):
        for line in open(_ENV):
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                k = line.split("=", 1)[1].strip()
    return k


def _parent_pool(archive):
    """H5: parents come from ALL cells, not the top-N composite. Every mock/real tier tends to
    punish boldness monotonically, so a composite top-8 contains zero speculative/crazy parents
    and ±1 boldness mutation can never reach 'crazy' — elitist selection would collapse exactly
    the diversity the MAP-Elites archive exists to preserve."""
    return archive.elites()


def _stratified_elites(archive, per_bold=2):
    """A boldness-stratified elite sample (best per niche level) for conditioning the LLM."""
    by = {}
    for g in archive.elites():
        by.setdefault(g.boldness, []).append(g)
    out = []
    for bold in BOLDNESS:
        ranked = sorted(by.get(bold, []), key=lambda g: g.composite(archive.weights), reverse=True)
        out.extend(ranked[:per_bold])
    return out


def generate_claude(archive, failures, gen, k):
    """LLM proposals conditioned on elites + failures. Returns [] on any error (caller falls back)."""
    key = _anthropic_key()
    if not key:
        return []
    try:
        import anthropic
        elites = "; ".join(f"{g.biomarker}/{g.modality}/{g.boldness} (roi={g.scores.get('roi',0):.2f})"
                           for g in _stratified_elites(archive))
        fails = "; ".join(f"{f['biomarker']}/{f['modality']} died at {f['dropped_at']}" for f in failures[-8:])
        prompt = (
            f"You are the mutation operator in an evolutionary search for RA drug-program hypotheses. "
            f"Propose {k} NEW, DIVERSE hypotheses (vary biomarker, modality, and boldness from "
            f"'supported' to 'crazy'). Avoid known dead-ends.\n"
            f"Current elites: {elites or 'none yet'}\nFailure ledger: {fails or 'none yet'}\n"
            f"Reply as a JSON array of objects: "
            f'{{"biomarker","modality"(small_molecule|peptide),"boldness"(supported|plausible|speculative|crazy),'
            f'"hypothesis"(one sentence, mechanistic)}}. JSON only.')
        msg = anthropic.Anthropic(api_key=key).messages.create(
            model="claude-sonnet-5", max_tokens=1500,
            messages=[{"role": "user", "content": prompt}])
        # extract the TEXT block (sonnet may emit a ThinkingBlock first)
        txt = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        arr = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
        out = []
        for o in arr[:k]:
            mod = "peptide" if str(o.get("modality", "")).lower().startswith("pep") else "small_molecule"
            bold = o.get("boldness", "plausible")
            bold = bold if bold in BOLDNESS else "plausible"
            out.append(_mk(str(o.get("biomarker", "?")).upper(), mod, bold, gen,
                           hypothesis=o.get("hypothesis", "")))
        return out
    except Exception:  # noqa: BLE001
        return []


def generate_offline(archive, failures, gen, k, rng):
    """Deterministic seed space (gen 0) + crossover/mutation of elites (gen>0), avoiding failures."""
    dead = {(f["biomarker"], f["modality"]) for f in failures if f["dropped_at"] in ("plausibility", "roi")}
    out = []
    if gen == 0 or not archive.elites():
        combos = [(b, m, bold) for b in SEED_BIOMARKERS for m in MODALITIES for bold in BOLDNESS]
        # if EVERY seed pair is dead, ignore the dead-set rather than proposing nothing
        live = [c for c in combos if (c[0], c[1]) not in dead] or combos
        rng.shuffle(live)
        for b, m, bold in live[:k]:
            out.append(_mk(b, m, bold, gen))
        return out
    elites = _parent_pool(archive)                 # all niches, not composite top-8 (H5)
    for _ in range(50 * k):                        # bounded: an all-dead dead-set must not hang (H2)
        if len(out) >= k:
            break
        a, b = rng.choice(elites), rng.choice(elites)
        biomarker = a.biomarker
        modality = b.modality
        # mutate boldness up/down to explore the frontier
        idx = BOLDNESS.index(a.boldness) if a.boldness in BOLDNESS else 1
        bold = BOLDNESS[max(0, min(len(BOLDNESS) - 1, idx + rng.choice([-1, 0, 1])))]
        # occasional fresh exploration into an unseen biomarker
        if rng.random() < 0.3:
            biomarker = rng.choice(SEED_BIOMARKERS)
        if (biomarker, modality) in dead:
            continue
        out.append(_mk(biomarker, modality, bold, gen, parents=[a.gid, b.gid]))
    if not out:
        # every crossover hit the dead-set — explore the raw seed space instead of stalling
        combos = [(b, m, bold) for b in SEED_BIOMARKERS for m in MODALITIES for bold in BOLDNESS]
        rng.shuffle(combos)
        out = [_mk(b, m, bold, gen) for b, m, bold in combos[:k]]
    return out


def generate(archive, failures, gen, k, rng, use_llm=True):
    if use_llm:
        g = generate_claude(archive, failures, gen, k)
        if g:
            return g
    return generate_offline(archive, failures, gen, k, rng)
