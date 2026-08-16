# Composing Highlander with the real LABrador nodes

Highlander runs standalone (every tier is a labeled mock) so it never blocks on a sibling node. To
compose it with the real pipeline you replace **one function body per tier** and keep the adapter. No
other file changes; nothing in the sibling nodes changes.

## The tier contract
Every tier is:
```python
def evaluate(genome) -> tuple[float, str, dict]:
    # score in [0,1], a human rationale, and a detail dict for the audit log
```
`DEFAULT_TIERS` in `highlander/tiers.py` lists them cheap→dear with per-tier `cost` and `gate`. The
cascade stops at the first tier below its gate (logged in the failure ledger) — that is what lets you
enumerate 10^4 hypotheses but only pay for full evaluation on ~10^2.

## Tier → node wiring

### 1. plausibility ← `research-evidence-mapper`
- **Node output consumed:** a knowledge graph `{papers[], findings[]}` (see the mapper's `SCHEMA.md`).
- **Adapter A** (`adapter_A_graph_to_evidence`): `findings[] → thesis.Evidence[]`. Convention: `no_effect`
  maps to `contradicts` with a `[no_effect]` note and reduced strength — **not** a silent third value.
- **Swap:** replace `_mock_evidence_graph(g)` with the mapper's real graph for `g.to_thesis()`.

### 2. roi ← `therapeutic-program-economics`  ← (this is the node to consume, NOT Claude's local rNPV)
- **Node output consumed:** the economics result JSON, e.g. `{"p50_rnpv": <float>, ...}`.
- **Adapter:** `roi_from_economics(result)` → sigmoid to [0,1].
- **Swap:** replace `_simple_rnpv(g)` with a call to the economics node — build a `ProgramInput` from
  `g.to_thesis()` (modality enum is `SMALL_MOLECULE|PEPTIDE`; map other thesis modalities → peptide and
  flag `NOT_DECISION_GRADE`), run `labrador analyze` (or import the node), pass its result to the adapter.
- The shipped `_simple_rnpv` is a **labeled standalone proxy** so the container runs with no economics
  node present. It is deliberately generic (modality/phase/prevalence only) and is not decision-grade.

### 3. recruitability ← `trial-recruitment-forecaster`
- **Node output consumed:** `{"simulatedMonthsToEnroll": <float>, "score": <0..1>}`.
- **Adapter B** (`adapter_B_months_to_launch_delay`): months-over-target → **launch-delay years**, which
  the economics node already prices via `value_lost_per_launch_delay_year`. Deliberately NOT `score→PoS`
  (a category error) and NOT `months→stage_durations` (only shifts cost timing) — the two mis-wirings
  `COORDINATION.md §5` warns about.
- **Swap:** replace `_recruitability`'s mock forecast with the forecaster's real output for `g.to_thesis()`.

### 4. in-silico validation (`bio_reality`) ← `small-molecule-tractability-review`
- **Node output consumed (small molecule):** `{verdict, tractability{pocket_druggability{min,max}, ...},
  axis_conflict}`. The review **refuses to emit an overall score**; we honor that — `bio_from_tractability`
  derives only a *search-only* scalar and keeps both axes in the rationale (never averages them).
- **Peptide:** the tractability review is small-molecule-only, so peptides use an ESMFold/Proto
  developability proxy behind the same interface.
- **Swap:** replace `_mock_tractability(g)` with the review node's real output for `g.uniprot_accession`
  + `g.mechanism_hypothesis`.

## What "learning across module outputs" means concretely
- **Archive (`archive.py`):** MAP-Elites keeps the best genome per `(biomarker-class × modality ×
  boldness)` cell, plus a Pareto front over the module-output scores. This is the memory.
- **Failure ledger:** every genome dropped by a gate is recorded with *which* module output killed it.
- **Generator (`generator.py`):** the next generation is conditioned on the current elites **and** the
  failure ledger (FunSearch-style) — so the search moves toward regions the module outputs reward and
  away from ones they punish. That cross-run credit assignment is the "learning," and it needs no
  surrogate model.

## Anti-reward-hacking (the one correctness risk to respect)
The generator is an LLM. If the **plausibility** tier were *also* vibes-based LLM scoring, the loop would
optimize for plausible-sounding nonsense. Plausibility must stay grounded in the evidence mapper's real
findings (Adapter A over `research-evidence-mapper` output) — the mock says so loudly and the real wiring
must preserve it.
