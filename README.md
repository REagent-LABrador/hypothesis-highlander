# hypothesis-highlander — the meta-search that sits on top of the LABrador pipeline

Highlander is **not another evaluation module**. It is the layer *above* the four LABrador nodes.
It fixes a **common clinical indication** (RA is the running example), **enumerates many biomarkers ×
many hypotheses**, runs each hypothesis through the existing nodes, **learns across all those runs**,
and converges on a small Pareto front of **novel, non-obvious program hypotheses** — the ones no single
module would surface on its own.

```
                      one disease  (RA)  ─ the common theme
                              │
        ┌─────────────────────┴─────────────────────┐
        │   ENUMERATE:  N biomarkers × M hypotheses   │   (generator, archive-conditioned)
        └─────────────────────┬─────────────────────┘
                              │  each hypothesis ↓
   ┌──────────────┬───────────┴───────────┬──────────────────────┐
   │  plausibility│         ROI            │  recruitability      │  in-silico validation
   │  (evidence)  │  (economics rNPV)      │  (trial forecaster)  │  (tractability / fold)
   └──────────────┴───────────┬───────────┴──────────────────────┘
                              │  scores + rationale per module output
                              ▼
        LEARN ACROSS RUNS:  MAP-Elites archive (champion per niche)
                            + Pareto front + failure ledger
                              │  conditions the next generation
                              ▼
        CONVERGE →  Pareto front of novel hypotheses  (+ illuminated grid)
```

## Module-agnostic by design (this is the point)

Highlander **does not embed any module's logic and hard-codes no specific ROI engine.** Each evaluation
is a thin **consumer of a node's output contract** — one function `evaluate(genome) -> (score∈[0,1],
rationale, detail)`. Swap the real node in by pointing the tier at that node's output JSON; nothing else
changes. The four tiers map 1:1 to the LABrador nodes:

| Highlander tier | LABrador node it consumes | Reads (the node's output) | → tier score |
|---|---|---|---|
| plausibility | `research-evidence-mapper` | knowledge-graph findings/links | **Adapter A** → `thesis.Evidence[]` → net supports/contradicts |
| roi | `therapeutic-program-economics` | rNPV result (`p50_rnpv`, …) | `roi_from_economics()` → sigmoid |
| recruitability | `trial-recruitment-forecaster` | `simulatedMonthsToEnroll`, `score` | `recruitability_from_forecaster()`; **Adapter B** → launch-delay |
| in-silico (`bio_reality`) | `small-molecule-tractability-review` | verdict + druggability axes (small-mol); ESMFold/Proto proxy (peptide) | `bio_from_tractability()` → search-only scalar (axes preserved) |

So the container ships each tier with a **clearly-labeled mock/proxy** so it runs standalone with zero
dependency on the sibling nodes; in the composed pipeline you replace each tier body with a call to the
real node and keep the same adapter. **There is no import of any teammate's node and no import of
Claude's local `rnpv_copilot`** — verified in CI (`tests/test_no_hardcoded_dep.py`).

## Why a Pareto front, not one winner
The axes are **non-commensurable** — the highest-ROI hypothesis is rarely the most recruitable or the
most biologically real. A scalar winner throws away exactly the trade-off a program team needs. And a
single survivor starves the learning signal and collapses to a local optimum. MAP-Elites keeps a
champion in **every niche** (including the deliberately "crazy" ones), so the search *illuminates* the
space instead of tunnelling. The deliverable is the **Pareto front + the grid**.

## The shared contract
A genome **is** a candidate `IndicationThesis` (mirrored from the team's `thesis.ts` in
`highlander/thesis.py`, incl. the ratification additions `uniprotAccession` + `mechanismHypothesis`).
`Genome.to_thesis()` emits the exact JSON the nodes consume; `Genome.from_thesis()` reads it back.
The two unowned adapters from `COORDINATION.md §5` live in `highlander/adapters.py` (see `COMPOSE.md`).

## Files
- `highlander/genome.py` — one hypothesis (biomarker × direction × modality × **boldness**) ⇄ `IndicationThesis`.
- `highlander/thesis.py` — Python mirror of the shared `IndicationThesis` contract (validate / to_json / from_json).
- `highlander/adapters.py` — the module-output adapters (A: graph→Evidence; B: months→launch-delay; + normalizers).
- `highlander/tiers.py` — the 4-tier **cost-gated cascade**; each tier consumes one node's output (mock default).
- `highlander/archive.py` — MAP-Elites grid + Pareto front + append-only failure ledger (**the learning**).
- `highlander/generator.py` — mutation operator conditioned on elites + failures (Claude, or deterministic offline).
- `highlander/controller.py` — the loop, dedup, cost budget, provenance log, per-generation metrics.
- `highlander/viz.py` + `highlander/app.py` — the "it's learning" Streamlit page (grid, Pareto, learning curve).
- `highlander/demo.py` — offline end-to-end (no keys, no sibling nodes).
- `COMPOSE.md` — how to swap each mock tier for the real node; the two adapters' contracts.

## Run
```bash
# THE SEAM: machine-readable run for an orchestrator — result JSON + Pareto winners as
# IndicationThesis (null-free for the Zod boundary). Config overrides gates/weights/budget.
python -m highlander run --config config.json --generations 4 --seed 42 --out result.json
# offline pretty-printed demo, no keys, no sibling nodes — the "it's learning" story
python -m highlander.demo
# Claude mutation operator (reads ANTHROPIC_API_KEY from the env; never committed)
python -m highlander.demo --llm
# live dashboard
streamlit run highlander/app.py
# containerized
docker build -t hypothesis-highlander . && docker run --rm hypothesis-highlander
docker run --rm hypothesis-highlander python -m highlander run --out - > result.json
```

## Containerization / isolation guarantees
- Own `managed/hypothesis-highlander/` dir; **touches no other node**. Own `pyproject.toml` + `Dockerfile`.
- Core loop is **stdlib-only**; `numpy/plotly/streamlit` are viz-only, `anthropic` is a lazy optional import.
- `.dockerignore` + `.gitignore` exclude any `.env`/secrets; a CI test asserts no hardcoded sibling/`rnpv_copilot` import.

## Honest status
Real and running: the loop, MAP-Elites + Pareto (computed over the full ledger, not just cell
champions), the cost-gated cascade (gates sit inside the mock score spreads, so weak hypotheses
really die and the failure ledger really fills), the shared-thesis contract, the two adapters, the
Claude generator, the CLI seam (`python -m highlander run`), and the Streamlit page.
**Mocked behind the tier interface** (pending wiring to the real nodes, and clearly labelled as such):
the four evaluation bodies. Swapping in a real node is a one-function change per tier (`COMPOSE.md`).
The ROI axis is intentionally *not* driven by Claude's local ROI tool — it consumes the
`therapeutic-program-economics` node's output like every other tier.

**Learning-signal honesty:** with the mocks in place, every score is a pure function of the
behavior cell (biomarker-class × modality × boldness) — the free-text hypothesis never touches a
tier. So the demo's learning curve demonstrates the *mechanics* (gating, illumination, frontier
growth, failure-conditioning), **not** content-level learning. Real learning signal begins the
moment the evidence mapper / economics / forecaster / tractability nodes replace the mocks.
