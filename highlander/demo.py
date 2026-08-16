#!/usr/bin/env python3
"""
Offline end-to-end demo of the Hypothesis Highlander loop (no API key needed).
    ../.venv/bin/python -m highlander.demo   [--llm]   (from repo root, or run this file)
The payoff: MAP-Elites coverage + Pareto front growing per generation = "it's learning".
"""
from __future__ import annotations

import sys

from highlander.controller import Highlander


def money(x):
    return f"${x/1e9:.2f}B" if abs(x) >= 1e9 else f"${x/1e6:.0f}M"


def main():
    use_llm = "--llm" in sys.argv
    # budget ≈ 40 full cascades (1+8+15+40=64 units each); cheap drops cost far less → 10^2 partials
    hl = Highlander(seed=42, budget_units=2600, use_llm=use_llm, log_path="highlander/runs/demo.jsonl")
    res = hl.run(generations=4, pop_size=18)

    print("\n" + "=" * 92)
    print(f"  Hypothesis Highlander — {'Claude' if use_llm else 'offline'} generator, RA-anchored")
    print("=" * 92)
    print(f"\n{'gen':>3} {'proposed':>9} {'deduped':>8} {'evaluated':>10} {'archived':>9} "
          f"{'cells':>6} {'pareto':>7} {'best_roi':>9} {'cost':>7}")
    for h in res["history"]:
        print(f"{h['generation']:>3} {h['proposed']:>9} {h['deduped']:>8} {h['evaluated']:>10} "
              f"{h['survived_to_archive']:>9} {h['cells_filled']:>6} {h['pareto_front']:>7} "
              f"{(h['best_roi'] or 0):>9.2f} {h['eval_cost_spent']:>7}")

    print("\n[MAP-Elites grid — champion composite by biomarker×modality, per boldness niche]")
    for cell, bolds in sorted(res["grid"].items()):
        print(f"  {cell:<22} " + "  ".join(f"{b}:{s}" for b, s in bolds.items()))

    print("\n[Pareto front — the non-dominated trade-off set (BD's actual deliverable)]")
    print(f"  {'biomarker':<10}{'modality':<16}{'bold':<12}{'plaus':>6}{'roi':>6}{'recruit':>8}{'bio':>6}")
    for g in res["pareto_front"]:
        s = g["scores"]
        print(f"  {g['biomarker']:<10}{g['modality']:<16}{g['boldness']:<12}"
              f"{s.get('plausibility',0):>6.2f}{s.get('roi',0):>6.2f}"
              f"{s.get('recruitability',0):>8.2f}{s.get('bio_reality',0):>6.2f}")

    print("\n[Best-in-axis champions]")
    st = hl.archive.stats()
    for axis in ["plausibility", "roi", "recruitability", "bio_reality"]:
        b = hl.archive.best_on(axis)
        if b:
            print(f"  {axis:<15} {b.biomarker}/{b.modality}/{b.boldness}  ({axis}={b.scores[axis]:.2f})")

    print(f"\n[Failure ledger — {len(res['failure_ledger'])} deaths fed back to the generator]")
    from collections import Counter
    by = Counter(f"{f['dropped_at']}" for f in res["failure_ledger"])
    print("  drops by gate: " + ", ".join(f"{k}={v}" for k, v in by.items()))
    print(f"\n{res['stats']['cells_filled']} niches illuminated · Pareto front {res['stats']['pareto_front_size']} "
          f"· reproducible from seed={res['seed']} + highlander/runs/demo.jsonl\n")


if __name__ == "__main__":
    main()
