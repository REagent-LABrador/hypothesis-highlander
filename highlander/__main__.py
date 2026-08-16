"""
The outbound invocation seam — how an orchestrator (COORDINATION.md §5) actually consumes
Highlander without editing library source inside the image:

    python -m highlander run --config config.json --generations 4 --seed 42 --out result.json
    docker run --rm hypothesis-highlander python -m highlander run --out - > result.json

Writes the full run result PLUS `pareto_theses`: each Pareto-front genome serialized as the
shared IndicationThesis contract (`to_thesis().to_json()`, null-free for the Zod boundary),
so downstream nodes can consume the winners directly. stdlib-only (argparse + json), keeping
the core dependency-light.

config.json keys (all optional; CLI flags override): {"gates": {axis: float}, "weights":
{axis: float}, "budget_units": float, "generations": int, "pop_size": int, "seed": int,
"use_llm": bool, "log_path": str}
"""
from __future__ import annotations

import argparse
import json
import sys

from .controller import Highlander


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="highlander",
                                description="Quality-diversity meta-search over drug-program hypotheses")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the search and write the result JSON")
    r.add_argument("--config", help="JSON config file (gates/weights/budget/seed/…)")
    r.add_argument("--generations", type=int)
    r.add_argument("--pop-size", type=int)
    r.add_argument("--seed", type=int)
    r.add_argument("--budget", type=float, help="total eval budget in cost units")
    r.add_argument("--llm", action="store_true", help="use the Claude mutation operator (default: offline)")
    r.add_argument("--log", help="append-only JSONL run-log path")
    r.add_argument("--out", default="-", help="result JSON path ('-' = stdout)")
    args = p.parse_args(argv)

    cfg = {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)

    hl = Highlander(
        weights=cfg.get("weights"),
        gates=cfg.get("gates"),
        budget_units=args.budget if args.budget is not None else cfg.get("budget_units", 2600),
        seed=args.seed if args.seed is not None else cfg.get("seed", 42),
        use_llm=bool(args.llm or cfg.get("use_llm", False)),   # offline by default: reproducible
        log_path=args.log or cfg.get("log_path"),
    )
    res = hl.run(generations=args.generations or cfg.get("generations", 4),
                 pop_size=args.pop_size or cfg.get("pop_size", 16))

    # the machine-readable deliverable: Pareto winners as the shared IndicationThesis contract
    res["pareto_theses"] = [g.to_thesis().to_json()
                            for g in sorted(hl.archive.pareto_front(),
                                            key=lambda x: -x.scores.get("roi", 0))]

    payload = json.dumps(res, indent=2, default=str)
    if args.out == "-":
        print(payload)
    else:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
