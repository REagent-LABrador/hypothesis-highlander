"""Minimal Highlander compatibility path for the current RA demo snapshot.

The demo orchestrator already exposes three biomarker records and nine hypothesis
branches through its snake-case ``/snapshot`` projection.  This module consumes
that projection as data; it does not call or modify the orchestrator or any
scientific module.

Only the native HypGen scientific metrics present in every embedded card
(``support``, ``novelty``, and ``testability``) enter Pareto comparison. The
projected ``plausibility`` field is display rank, not a scientific grade. Shared
recruitment, ROI, and tractability values are deliberately not ranked. The
production packet consumer remains a separate, stricter API.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import math
from numbers import Real
from typing import Any

from .packet_contracts import ContractError, canonical_json_sha256
from .packet_portfolio import (
    ComparisonPolicy,
    ObjectiveRule,
    PortfolioCandidate,
    PortfolioObservation,
    compare_packets,
)


RA_DEMO_RESULT_SCHEMA_VERSION = "highlander.ra-demo-result.v1"
RA_DEMO_POLICY_ID = "ra-demo-nine-branch-candidate-metrics.v1"
_SOURCE_SCHEMA_ID = "labrador.frontend-snapshot"
_NATIVE_HYPGEN_SCHEMA_ID = "https://labrador.dev/schemas/cards.schema.json"
_ASSOCIATION_BASES = frozenset({"SOURCE_PATH", "CONTEXT_ONLY"})
_SHARED_DOWNSTREAM_METRICS = (
    "programs[].metrics.rnpv",
    "programs[].metrics.positive",
    "programs[].metrics.impact",
    "programs[].metrics.recruit",
    "programs[].metrics.duration",
    "programs[].metrics.screens",
    "programs[].metrics.risk",
    "programs[].metrics.support",
    "programs[].metrics.occupancy",
    "programs[].metrics.convergence",
)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    return value


def _sequence(value: Any, path: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError(f"{path} must be an array")
    return tuple(value)


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{path} must be a non-empty string")
    return value.strip()


def _integer(value: Any, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{path} must be an integer >= {minimum}")
    return value


def _score(value: Any, path: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ContractError(f"{path} must be a number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ContractError(f"{path} must be finite") from error
    if not math.isfinite(result) or not 0.0 <= result <= maximum:
        raise ContractError(f"{path} must be finite and within 0..{maximum:g}")
    return result


def _timestamp(value: Any, path: str) -> str:
    result = _text(value, path)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError(f"{path} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{path} must include a UTC offset")
    return result


def _normalise_biomarkers(values: Any) -> tuple[dict[str, Any], ...]:
    biomarkers = _sequence(values, "snapshot.biomarkers")
    if len(biomarkers) != 3:
        raise ContractError("RA demo snapshot must contain exactly 3 biomarkers")

    normalised: list[dict[str, Any]] = []
    for index, raw in enumerate(biomarkers):
        item = _mapping(raw, f"snapshot.biomarkers[{index}]")
        normalised.append(
            {
                "slot": _integer(item.get("slot"), f"snapshot.biomarkers[{index}].slot"),
                "graphThingId": _text(
                    item.get("graph_thing_id"),
                    f"snapshot.biomarkers[{index}].graph_thing_id",
                ),
                "label": _text(
                    item.get("label"), f"snapshot.biomarkers[{index}].label"
                ),
                "summary": (
                    _text(item["summary"], f"snapshot.biomarkers[{index}].summary")
                    if item.get("summary") is not None
                    else None
                ),
            }
        )

    slots = [item["slot"] for item in normalised]
    thing_ids = [item["graphThingId"] for item in normalised]
    if sorted(slots) != [0, 1, 2]:
        raise ContractError("RA demo biomarker slots must be exactly 0, 1, and 2")
    if len(set(thing_ids)) != 3:
        raise ContractError("RA demo biomarker graph_thing_id values must be unique")
    return tuple(sorted(normalised, key=lambda item: item["slot"]))


def _normalise_hypotheses(
    values: Any, biomarkers: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], ...]:
    programs = _sequence(values, "snapshot.programs")
    if len(programs) != 9:
        raise ContractError("RA demo snapshot must contain exactly 9 hypotheses")

    biomarker_slots = {item["graphThingId"]: item["slot"] for item in biomarkers}
    normalised: list[dict[str, Any]] = []
    for index, raw in enumerate(programs):
        item = _mapping(raw, f"snapshot.programs[{index}]")
        projected_metrics = _mapping(
            item.get("metrics"), f"snapshot.programs[{index}].metrics"
        )
        biomarker_id = _text(
            item.get("biomarker_graph_thing_id"),
            f"snapshot.programs[{index}].biomarker_graph_thing_id",
        )
        if biomarker_id not in biomarker_slots:
            raise ContractError(
                f"snapshot.programs[{index}] references unknown biomarker {biomarker_id!r}"
            )
        biomarker_slot = _integer(
            item.get("biomarker_slot"),
            f"snapshot.programs[{index}].biomarker_slot",
        )
        if biomarker_slot != biomarker_slots[biomarker_id]:
            raise ContractError(
                f"snapshot.programs[{index}].biomarker_slot does not match its biomarker"
            )
        association_basis = _text(
            item.get("association_basis"),
            f"snapshot.programs[{index}].association_basis",
        ).upper()
        if association_basis not in _ASSOCIATION_BASES:
            raise ContractError(
                f"snapshot.programs[{index}].association_basis must be SOURCE_PATH or CONTEXT_ONLY"
            )
        station_payloads = _mapping(
            item.get("station_payloads"),
            f"snapshot.programs[{index}].station_payloads",
        )
        hypothesis_payload = _mapping(
            station_payloads.get("hypothesis"),
            f"snapshot.programs[{index}].station_payloads.hypothesis",
        )
        schema_version = _text(
            hypothesis_payload.get("schema_version"),
            f"snapshot.programs[{index}].station_payloads.hypothesis.schema_version",
        )
        if schema_version != "1.0":
            raise ContractError(
                f"snapshot.programs[{index}] HypGen schema_version must be '1.0'"
            )
        cards = _sequence(
            hypothesis_payload.get("hypotheses"),
            f"snapshot.programs[{index}].station_payloads.hypothesis.hypotheses",
        )
        source_hypothesis_id = _text(
            item.get("source_hypothesis_id"),
            f"snapshot.programs[{index}].source_hypothesis_id",
        )
        matches = [
            _mapping(card, f"snapshot.programs[{index}].native_card")
            for card in cards
            if isinstance(card, Mapping) and card.get("id") == source_hypothesis_id
        ]
        if len(matches) != 1:
            raise ContractError(
                f"snapshot.programs[{index}] must resolve exactly one native HypGen card "
                f"for {source_hypothesis_id!r}"
            )
        card = matches[0]
        native_metrics = _mapping(
            card.get("metrics"), f"snapshot.programs[{index}].native_card.metrics"
        )
        support = _score(
            native_metrics.get("support"),
            f"snapshot.programs[{index}].native_card.metrics.support",
            maximum=1.0,
        )
        novelty = _score(
            native_metrics.get("novelty"),
            f"snapshot.programs[{index}].native_card.metrics.novelty",
            maximum=1.0,
        )
        testability = _score(
            native_metrics.get("testability"),
            f"snapshot.programs[{index}].native_card.metrics.testability",
            maximum=1.0,
        )
        projected_evidence = _score(
            projected_metrics.get("evidence"),
            f"snapshot.programs[{index}].metrics.evidence",
            maximum=100.0,
        )
        if not math.isclose(projected_evidence, round(support * 100, 1), abs_tol=0.05):
            raise ContractError(
                f"snapshot.programs[{index}].metrics.evidence does not match native support"
            )
        status = _mapping(
            card.get("status"), f"snapshot.programs[{index}].native_card.status"
        )
        raw_verification = status.get("verification")
        verification = (
            "UNVERIFIED"
            if raw_verification is None
            else _text(
                raw_verification,
                f"snapshot.programs[{index}].native_card.status.verification",
            ).upper()
        )
        raw_flags = _sequence(
            status.get("flags"),
            f"snapshot.programs[{index}].native_card.status.flags",
        )
        flags = tuple(
            sorted(
                {
                    _text(
                        flag,
                        f"snapshot.programs[{index}].native_card.status.flags[{flag_index}]",
                    ).upper()
                    for flag_index, flag in enumerate(raw_flags)
                }
            )
        )
        native_output_sha256 = canonical_json_sha256(hypothesis_payload)
        native_card_sha256 = canonical_json_sha256(card)
        normalised.append(
            {
                "candidateId": _text(
                    item.get("id"), f"snapshot.programs[{index}].id"
                ),
                "sourceHypothesisId": source_hypothesis_id,
                "biomarkerGraphThingId": biomarker_id,
                "associationBasis": association_basis,
                "lane": _integer(
                    item.get("lane"), f"snapshot.programs[{index}].lane"
                ),
                "biomarkerSlot": biomarker_slot,
                "hypothesisSlot": _integer(
                    item.get("hypothesis_slot"),
                    f"snapshot.programs[{index}].hypothesis_slot",
                ),
                "label": _text(
                    item.get("label"), f"snapshot.programs[{index}].label"
                ),
                "support": support,
                "novelty": novelty,
                "testability": testability,
                "verification": verification,
                "flags": list(flags),
                "nativeHypGenOutputHash": f"sha256:{native_output_sha256}",
                "nativeCardHash": f"sha256:{native_card_sha256}",
                "revision": _text(
                    item.get("revision"), f"snapshot.programs[{index}].revision"
                ),
            }
        )

    candidate_ids = [item["candidateId"] for item in normalised]
    lanes = [item["lane"] for item in normalised]
    pairs = {
        (item["biomarkerGraphThingId"], item["sourceHypothesisId"])
        for item in normalised
    }
    source_ids = {item["sourceHypothesisId"] for item in normalised}
    expected_pairs = {
        (biomarker["graphThingId"], source_id)
        for biomarker in biomarkers
        for source_id in source_ids
    }
    if len(set(candidate_ids)) != 9:
        raise ContractError("RA demo hypothesis candidate IDs must be unique")
    if sorted(lanes) != list(range(9)):
        raise ContractError("RA demo hypothesis lanes must be exactly 0 through 8")
    if len(source_ids) != 3 or pairs != expected_pairs:
        raise ContractError(
            "RA demo hypotheses must form the complete 3 biomarker x 3 hypothesis cross-product"
        )

    slot_by_source: dict[str, int] = {}
    for item in normalised:
        source_id = item["sourceHypothesisId"]
        slot = item["hypothesisSlot"]
        if not 0 <= slot <= 2:
            raise ContractError("RA demo hypothesis_slot values must be within 0..2")
        previous = slot_by_source.setdefault(source_id, slot)
        if previous != slot:
            raise ContractError(
                f"source hypothesis {source_id!r} changes hypothesis_slot across biomarkers"
            )
    if sorted(slot_by_source.values()) != [0, 1, 2]:
        raise ContractError("RA demo source hypotheses must occupy slots 0, 1, and 2")
    native_output_hashes = {item["nativeHypGenOutputHash"] for item in normalised}
    if len(native_output_hashes) != 1:
        raise ContractError(
            "RA demo branches must embed the same immutable HypGen slate"
        )
    return tuple(sorted(normalised, key=lambda item: item["lane"]))


def _candidate(
    hypothesis: Mapping[str, Any], *, run_id: str, comparison_input_sha256: str
) -> PortfolioCandidate:
    basis = {
        "consumerProfile": RA_DEMO_POLICY_ID,
        "metricScale": "0..1",
        "sourceProjection": _SOURCE_SCHEMA_ID,
    }
    source_hash = hypothesis["nativeCardHash"]
    observations = tuple(
        PortfolioObservation(
            objective_id=objective_id,
            value=hypothesis[objective_id],
            direction="MAX",
            unit="score_0_1",
            basis=basis,
            evidence_basis="INFERRED",
            source_path=(
                "station_payloads.hypothesis.hypotheses"
                f"[id={hypothesis['sourceHypothesisId']}].metrics.{objective_id}"
            ),
            source_schema_id=_NATIVE_HYPGEN_SCHEMA_ID,
            source_schema_version="1.0",
            source_hash=source_hash,
            derived_policy_id=RA_DEMO_POLICY_ID,
            qualifiers=("RA_DEMO", "SOURCE_HYPOTHESIS_SPECIFIC"),
        )
        for objective_id in ("support", "novelty", "testability")
    )
    packet_body = {
        "schemaVersion": "highlander.ra-demo-candidate.v1",
        "runId": run_id,
        "comparisonInputSha256": comparison_input_sha256,
        "hypothesis": dict(hypothesis),
    }
    packet_hash = f"sha256:{canonical_json_sha256(packet_body)}"
    eligible_verification = hypothesis["verification"] in {"QUALIFIED", "VERIFIED"}
    verification_qualifier = (
        hypothesis["verification"]
        if hypothesis["verification"] in {"REJECTED", "UNVERIFIED", "BLOCKED"}
        else f"VERIFICATION:{hypothesis['verification']}"
    )
    exclusion_reasons = []
    if not eligible_verification:
        exclusion_reasons.append(
            f"HYPOTHESIS_VERIFICATION:{hypothesis['verification']}"
        )
    exclusion_reasons.extend(
        f"HYPOTHESIS_FLAG:{flag}" for flag in hypothesis["flags"]
    )
    return PortfolioCandidate(
        candidate_id=hypothesis["candidateId"],
        packet_revision_id=(
            f"{hypothesis['revision']}:{hypothesis['candidateId']}"
        ),
        packet_hash=packet_hash,
        run_id=run_id,
        observations=observations,
        qualifiers=(
            "RA_DEMO_COMPATIBILITY",
            "BIOMARKER_ASSOCIATION_NOT_SCORED",
            "SHARED_DOWNSTREAM_NOT_RANKED",
            "SHARED_SOURCE_CARD_ACROSS_BIOMARKERS",
            verification_qualifier,
            f"ASSOCIATION_BASIS:{hypothesis['associationBasis']}",
            f"BIOMARKER:{hypothesis['biomarkerGraphThingId']}",
            f"SOURCE_HYPOTHESIS:{hypothesis['sourceHypothesisId']}",
            *(f"HYPGEN_FLAG:{flag}" for flag in hypothesis["flags"]),
        ),
        exclusion_reasons=tuple(exclusion_reasons),
    )


def compare_ra_demo_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Compare all nine branches in a current RA demo ``/snapshot`` body.

    This compatibility mode is intentionally narrow.  It validates the 3x3 demo
    topology, compares only native HypGen support/novelty/testability,
    retains tied branch identities, and emits no winner.
    """

    root = _mapping(snapshot, "snapshot")
    run_id = _text(root.get("run_id"), "snapshot.run_id")
    updated_at = _timestamp(root.get("updated_at"), "snapshot.updated_at")
    last_event_id = _integer(root.get("last_event_id"), "snapshot.last_event_id")
    if root.get("highlander_ready") is not True:
        reason = root.get("highlander_blocked_reason")
        suffix = f": {reason}" if isinstance(reason, str) and reason.strip() else ""
        raise ContractError(f"snapshot is not ready for Highlander{suffix}")

    biomarkers = _normalise_biomarkers(root.get("biomarkers"))
    hypotheses = _normalise_hypotheses(root.get("programs"), biomarkers)
    comparison_input = {
        "schemaVersion": "highlander.ra-demo-comparison-input.v1",
        "runId": run_id,
        "updatedAt": updated_at,
        "lastEventId": last_event_id,
        "biomarkers": list(biomarkers),
        "hypotheses": list(hypotheses),
    }
    comparison_input_sha256 = canonical_json_sha256(comparison_input)
    candidates = tuple(
        _candidate(
            hypothesis,
            run_id=run_id,
            comparison_input_sha256=comparison_input_sha256,
        )
        for hypothesis in hypotheses
    )
    policy = ComparisonPolicy(
        policy_id=RA_DEMO_POLICY_ID,
        objectives=(
            ObjectiveRule("novelty", "MAX"),
            ObjectiveRule("support", "MAX"),
            ObjectiveRule("testability", "MAX"),
        ),
    )
    portfolio = compare_packets(
        candidates,
        policy,
        snapshot_id=f"ra-demo-{comparison_input_sha256[:20]}",
        created_at=updated_at,
    ).to_dict()
    comparison_by_id = {
        item["candidateId"]: item for item in portfolio["candidates"]
    }
    hypothesis_records = []
    for hypothesis in hypotheses:
        comparison = comparison_by_id[hypothesis["candidateId"]]
        hypothesis_records.append(
            {
                **dict(hypothesis),
                "comparisonStatus": comparison["comparisonStatus"],
                "comparisonGroupId": comparison.get("comparisonGroupId"),
                "incomparableReasons": comparison.get("incomparableReasons", []),
            }
        )

    return {
        "schemaVersion": RA_DEMO_RESULT_SCHEMA_VERSION,
        "mode": "RA_DEMO_MINIMAL",
        "runId": run_id,
        "sourceSnapshot": {
            "updatedAt": updated_at,
            "lastEventId": last_event_id,
            "comparisonInputSha256": f"sha256:{comparison_input_sha256}",
        },
        "scope": {
            "biomarkerCount": 3,
            "hypothesisCount": 9,
            "objectivePolicyId": RA_DEMO_POLICY_ID,
            "rankedObjectives": ["support", "novelty", "testability"],
            "displayRankFieldsNotUsed": [
                "programs[].metrics.plausibility",
                "station_payloads.hypothesis.hypotheses[].metrics.rank",
            ],
            "sharedDownstreamMetricsNotRanked": list(_SHARED_DOWNSTREAM_METRICS),
            "reason": (
                "The current RA demo supplies shared, non-candidate-specific "
                "recruitment, ROI, and tractability records; this compatibility "
                "mode does not use them to distinguish hypotheses."
            ),
        },
        "biomarkers": [dict(item) for item in biomarkers],
        "hypotheses": hypothesis_records,
        "portfolio": portfolio,
        "qualifiers": [
            "DEMO_ONLY",
            "NO_GLOBAL_WINNER",
            "NINE_HYPOTHESIS_BRANCHES",
            "SHARED_DOWNSTREAM_NOT_RANKED",
        ],
    }
