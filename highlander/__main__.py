"""Highlander command-line entry points.

``compare`` is the production packet consumer. ``ra-demo`` is a deliberately
minimal compatibility surface for the current 3-biomarker/9-hypothesis demo
snapshot. ``run`` is retained only as a standalone legacy/demo search over
explicitly mocked tier outputs.
"""
from __future__ import annotations

import argparse
import json
import sys

from .controller import Highlander
from .packet_consumer import MAX_REQUEST_BYTES, compare_packet_request, strict_json_loads
from .packet_contracts import ContractError
from .ra_demo import compare_ra_demo_snapshot


def _write_json(value, destination: str) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    if destination == "-":
        print(payload)
        return
    with open(destination, "w", encoding="utf-8") as fh:
        fh.write(payload + "\n")
    print(f"wrote {destination}", file=sys.stderr)


def _run_legacy(args) -> int:
    cfg = {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)

    hl = Highlander(
        weights=cfg.get("weights"),
        gates=cfg.get("gates"),
        budget_units=args.budget if args.budget is not None else cfg.get("budget_units", 2600),
        seed=args.seed if args.seed is not None else cfg.get("seed", 42),
        use_llm=bool(args.llm or cfg.get("use_llm", False)),
        log_path=args.log or cfg.get("log_path"),
    )
    res = hl.run(
        generations=args.generations or cfg.get("generations", 4),
        pop_size=args.pop_size or cfg.get("pop_size", 16),
    )

    # Legacy/demo output only. Production comparisons never invoke this search.
    res["pareto_theses"] = [
        genome.to_thesis().to_json()
        for genome in sorted(
            hl.archive.pareto_front(), key=lambda item: -item.scores.get("roi", 0)
        )
    ]
    _write_json(res, args.out)
    return 0


def _compare(args) -> int:
    try:
        if args.request == "-":
            raw_text = sys.stdin.read(MAX_REQUEST_BYTES + 1)
            raw = raw_text.encode("utf-8")
        else:
            with open(args.request, "rb") as fh:
                raw = fh.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ContractError("request exceeds the configured byte limit")
        request = strict_json_loads(raw, "comparison request")
        result = compare_packet_request(request)
    except (ContractError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"highlander compare: {error}", file=sys.stderr)
        return 2
    _write_json(result.to_dict(), args.out)
    return 0


def _ra_demo(args) -> int:
    try:
        if args.snapshot == "-":
            raw_text = sys.stdin.read(MAX_REQUEST_BYTES + 1)
            raw = raw_text.encode("utf-8")
        else:
            with open(args.snapshot, "rb") as fh:
                raw = fh.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ContractError("snapshot exceeds the configured byte limit")
        snapshot = strict_json_loads(raw, "RA demo snapshot")
        result = compare_ra_demo_snapshot(snapshot)
    except (ContractError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"highlander ra-demo: {error}", file=sys.stderr)
        return 2
    _write_json(result, args.out)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="highlander",
        description="Immutable LABrador packet comparison and legacy demo search",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser(
        "compare",
        help="validate locked module packets and emit a versioned Pareto snapshot",
    )
    c.add_argument(
        "--request",
        default="-",
        help="comparison-request JSON path ('-' = stdin)",
    )
    c.add_argument("--out", default="-", help="result JSON path ('-' = stdout)")

    d = sub.add_parser(
        "ra-demo",
        help="compare the current 3-biomarker/9-hypothesis RA demo snapshot",
    )
    d.add_argument(
        "--snapshot",
        default="-",
        help="orchestrator /snapshot JSON path ('-' = stdin)",
    )
    d.add_argument("--out", default="-", help="result JSON path ('-' = stdout)")

    r = sub.add_parser(
        "run",
        help="run the legacy mock search (demo only; not production comparison)",
    )
    r.add_argument("--config", help="JSON config file (gates/weights/budget/seed/…)")
    r.add_argument("--generations", type=int)
    r.add_argument("--pop-size", type=int)
    r.add_argument("--seed", type=int)
    r.add_argument("--budget", type=float, help="total eval budget in cost units")
    r.add_argument("--llm", action="store_true", help="use the Claude mutation operator (default: offline)")
    r.add_argument("--log", help="append-only JSONL run-log path")
    r.add_argument("--out", default="-", help="result JSON path ('-' = stdout)")
    args = p.parse_args(argv)
    if args.cmd == "compare":
        return _compare(args)
    if args.cmd == "ra-demo":
        return _ra_demo(args)
    return _run_legacy(args)


if __name__ == "__main__":
    raise SystemExit(main())
