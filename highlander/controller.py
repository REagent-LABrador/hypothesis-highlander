"""
The Highlander controller — the loop that ties it together:

  for generation in range(N):
      population = generate(archive, failures)      # LLM/offline mutation, seeded by elites+failures
      for genome in population:
          if seen(genome): drop (T0 dedup)          # don't re-spend eval budget on duplicates
          cascade_eval(genome)                       # cost-gated 4-tier fitness
          if fully-evaluated: archive.insert(genome) # MAP-Elites + Pareto
          else: failures.append(why it died)         # feed the next generation's generator

Small, deterministic, observable: an append-only run log, a fixed RNG seed, a bounded eval
budget, and per-generation metrics (coverage + Pareto size = the "it's learning" curve).
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

from .archive import Archive
from .genome import AXES
from .generator import generate
from .tiers import cascade, DEFAULT_TIERS, DEFAULT_GATES


class Highlander:
    def __init__(self, weights=None, gates=None, tiers=None, budget_units=None,
                 seed=42, log_path=None, use_llm=True):
        self.archive = Archive(weights)
        self.gates = gates or DEFAULT_GATES
        self.tiers = tiers or DEFAULT_TIERS
        self.budget = {"total": budget_units, "spent": 0.0} if budget_units else None
        self.rng = random.Random(seed)
        self.seed = seed
        self.seen: set[str] = set()          # dedup keys (T0)
        self.failures: list[dict] = []       # failure ledger (conditions the generator)
        self.history: list[dict] = []
        self.use_llm = use_llm
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, kind, payload):
        if self.log_path:
            with self.log_path.open("a") as fh:
                fh.write(json.dumps({"kind": kind, **payload}, default=str) + "\n")

    def run(self, generations=4, pop_size=16):
        for gen in range(generations):
            pop = generate(self.archive, self.failures, gen, pop_size, self.rng, use_llm=self.use_llm)
            n_dupe = n_eval = n_survived = 0
            for g in pop:
                if self.budget and self.budget["spent"] >= self.budget["total"]:
                    break
                key = g.dedup_key()
                if key in self.seen:                         # T0 dedup gate
                    n_dupe += 1
                    continue
                self.seen.add(key)
                cascade(g, self.tiers, self.gates, self.budget)
                n_eval += 1
                self._log("genome", {"gid": g.gid, "gen": gen, "cell": g.cell(),
                                     "scores": g.scores, "dropped_at": g.dropped_at,
                                     "reached_tier": g.reached_tier, "cost": g.eval_cost})
                if not g.dropped_at:                          # reached all tiers → archive it
                    self.archive.insert(g)
                    n_survived += 1
                else:
                    reason = g.detail.get(g.dropped_at, {}).get("rationale", "")
                    self.failures.append({"biomarker": g.biomarker_class(), "modality": g.modality,
                                          "dropped_at": g.dropped_at, "rationale": reason})
            st = self.archive.stats()
            rec = {"generation": gen, "proposed": len(pop), "deduped": n_dupe, "evaluated": n_eval,
                   "survived_to_archive": n_survived, "cells_filled": st["cells_filled"],
                   "pareto_front": st["pareto_front_size"], "best_roi": st["best_roi"],
                   "eval_cost_spent": round(self.budget["spent"], 1) if self.budget else None}
            self.history.append(rec)
            self._log("generation", rec)
        return self.result()

    def result(self):
        front = self.archive.pareto_front()
        return {
            "history": self.history,
            "pareto_front": [g.to_dict() for g in sorted(front, key=lambda x: -x.scores.get("roi", 0))],
            "grid": {f"{bm}×{mod}": bolds for (bm, mod), bolds in self.archive.grid_summary().items()},
            "top": [g.to_dict() for g in self.archive.top(8)],
            "stats": self.archive.stats(),
            "failure_ledger": self.failures,
            "seed": self.seed,
        }
