"""Fail-closed adapters from locked LABrador outputs to Highlander observations.

These adapters validate only the runtime slices Highlander consumes.  They do
not modify producer schemas, call producer code, invent missing values, or
collapse categorical scientific findings into convenience scores.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping, Sequence

from .packet_contracts import (
    AdaptedModuleResult,
    ContractError,
    HypothesisCandidate,
    ModulePacket,
    ObjectiveObservation,
    canonical_json_bytes,
)
from .vendor.tractability_validator import validate_dossier


MAPPER = "research-evidence-mapper"
HYPOTHESIS_GENERATOR = "hypothesis-generator"
RECRUITMENT = "trial-recruitment-forecaster"
ECONOMICS = "therapeutic-program-economics"
TRACTABILITY = "small-molecule-tractability-review"

_HYPOTHESIS_OBJECTIVES = (
    ("support", "support", "MAX"),
    ("novelty", "novelty", "MAX"),
    ("testability", "testability", "MAX"),
    ("contradiction_risk", "contradiction_risk", "MIN"),
)
_HYPGEN_VERIFICATION_VERDICTS = {"verified", "qualified", "unverified", "rejected"}
_HYPGEN_GATE_STATUSES = {"pass", "warn", "fail", "skip"}
_HYPGEN_ISSUE_SEVERITIES = {"error", "warning"}
_HYPGEN_INTEGRITY_GATES = {"structure", "citations"}
_TRACTABILITY_VERDICTS = {
    "small_molecule_tractable",
    "not_tractable",
    "insufficient_evidence",
}
_TRACTABILITY_BASES = {"retrieved_precedent", "computed_tractability", "both", "none"}
_ECONOMICS_DECISION_GRADES = {"DECISION_GRADE", "NOT_DECISION_GRADE"}
_ECONOMICS_RECOMMENDATIONS = {
    "ADVANCE",
    "ADVANCE_WITH_EVIDENCE_GATE",
    "OPTION_OR_PARTNER",
    "HOLD",
    "STOP",
    "NOT_DECISION_GRADE",
}
_ECONOMICS_WARNING_SEVERITIES = {"INFO", "WARNING", "ERROR"}
_ECONOMICS_SIMULATION_RANGES = {
    "price_multiplier",
    "patient_multiplier",
    "gross_to_net_shift",
    "persistence_multiplier",
    "development_cost_multiplier",
    "launch_delay_years",
    "loe_retention_multiplier",
}
_LOCKED_PRODUCER_CODE_VERSION = "5131cd109bef1f9eebe0b109a04a0fcb98908454"
_ADAPTER_VERSION = "packet-adapters.v1"
_SHA256_URI_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TRACTABILITY_VALIDATOR_SHA256 = (
    "fc84771646932cc9d570ea82fda912686d05579da5d576c288b007e5430d7490"
)
_LOCKED_SCHEMAS = {
    MAPPER: ("EvidenceGraph", "1.1"),
    HYPOTHESIS_GENERATOR: ("hyp_gen.schema.Slate", "locked-5131cd1"),
    RECRUITMENT: ("RecruitabilityResult", "locked-5131cd1"),
    ECONOMICS: ("labrador_roi.engine.AnalysisResult", "1.3.0"),
    TRACTABILITY: ("small-molecule-tractability-dossier", "locked-5131cd1"),
}


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractError(f"{path} must be an array")
    return value


def _text(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{path} must be a string")
    if not allow_empty and not value.strip():
        raise ContractError(f"{path} must be a non-empty string")
    return value


def _text_sequence(value: Any, path: str) -> Sequence[Any]:
    """Validate a locked ``string[]`` without normalising or copying it."""

    items = _sequence(value, path)
    for index, item in enumerate(items):
        _text(item, f"{path}[{index}]")
    return items


def _integer(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{path} must be at least {minimum}")
    return value


def _number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{path} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise ContractError(f"{path} must be finite")
    if minimum is not None and out < minimum:
        raise ContractError(f"{path} must be at least {minimum}")
    if maximum is not None and out > maximum:
        raise ContractError(f"{path} must be at most {maximum}")
    return out


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{path} must be a boolean")
    return value


def _only_fields(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ContractError(
            f"{path} contains fields forbidden by the locked schema: "
            + ",".join(unexpected)
        )


def _qualifiers(packet: ModulePacket, *extra: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            [packet.evidence_basis.upper(), *packet.qualifiers, *(item.upper() for item in extra)]
        )
    )


def _result(
    packet: ModulePacket,
    *,
    objectives: tuple[ObjectiveObservation, ...] = (),
    candidate: HypothesisCandidate | None = None,
    details: Mapping[str, Any] | None = None,
    qualifiers: tuple[str, ...] | None = None,
    missing_reasons: tuple[str, ...] = (),
    quarantined: bool = False,
    quarantine_reasons: tuple[str, ...] = (),
) -> AdaptedModuleResult:
    return AdaptedModuleResult(
        run_id=packet.run_id,
        module_id=packet.module_id,
        hypothesis_id=packet.hypothesis_id,
        attempt_id=packet.attempt_id,
        packet_sha256=packet.result_sha256,
        attempt_record={
            "runId": packet.run_id,
            "hypothesisId": packet.hypothesis_id,
            "moduleId": packet.module_id,
            "attemptId": packet.attempt_id,
            "executionStatus": packet.execution_status,
            "executionReason": packet.execution_reason,
            "evidenceBasis": packet.evidence_basis,
            "nativeSchemaId": packet.native_schema_id,
            "nativeSchemaVersion": packet.native_schema_version,
            "producerCodeVersion": packet.producer_code_version,
            "adapterVersion": packet.adapter_version,
            "inputRawSha256": packet.input_raw_sha256,
            "outputRawSha256": packet.output_raw_sha256,
            "outputCanonicalSha256": packet.output_canonical_sha256,
            "envelopeCanonicalSha256": packet.envelope_canonical_sha256,
            "inputArtifactRef": packet.input_artifact_ref,
            "outputArtifactRef": packet.output_artifact_ref,
            "executionArtifactRef": packet.execution_artifact_ref,
            "executionArtifactRawSha256": packet.execution_artifact_raw_sha256,
            "dependsOn": packet.dependencies,
            "subject": packet.subject.to_dict(),
        },
        objectives=objectives,
        candidate=candidate,
        details=details or {},
        raw_payload=packet.payload,
        qualifiers=qualifiers or _qualifiers(packet),
        missing_reasons=missing_reasons,
        quarantined=quarantined,
        quarantine_reasons=quarantine_reasons,
    )


def _preflight(packet: ModulePacket, expected_module: str) -> AdaptedModuleResult | None:
    if packet.module_id != expected_module:
        return _result(
            packet,
            qualifiers=_qualifiers(packet, "QUARANTINED"),
            quarantined=True,
            quarantine_reasons=(
                f"module_id_mismatch:expected={expected_module},actual={packet.module_id}",
            ),
        )
    expected_schema_id, expected_schema_version = _LOCKED_SCHEMAS[expected_module]
    schema_mismatches: list[str] = []
    if packet.native_schema_id != expected_schema_id:
        schema_mismatches.append(
            "native_schema_id_mismatch:"
            f"expected={expected_schema_id},actual={packet.native_schema_id}"
        )
    if packet.native_schema_version != expected_schema_version:
        schema_mismatches.append(
            "native_schema_version_mismatch:"
            f"expected={expected_schema_version},actual={packet.native_schema_version}"
        )
    if schema_mismatches:
        return _result(
            packet,
            qualifiers=_qualifiers(packet, "QUARANTINED", "SCHEMA_MISMATCH"),
            quarantined=True,
            quarantine_reasons=tuple(schema_mismatches),
        )
    version_mismatches: list[str] = []
    if packet.producer_code_version != _LOCKED_PRODUCER_CODE_VERSION:
        version_mismatches.append(
            "producer_code_version_mismatch:"
            f"expected={_LOCKED_PRODUCER_CODE_VERSION},"
            f"actual={packet.producer_code_version}"
        )
    if packet.adapter_version != _ADAPTER_VERSION:
        version_mismatches.append(
            "adapter_version_mismatch:"
            f"expected={_ADAPTER_VERSION},actual={packet.adapter_version}"
        )
    if version_mismatches:
        return _result(
            packet,
            qualifiers=_qualifiers(packet, "QUARANTINED", "VERSION_MISMATCH"),
            quarantined=True,
            quarantine_reasons=tuple(version_mismatches),
        )
    if not packet.succeeded:
        return _result(
            packet,
            qualifiers=_qualifiers(packet, packet.execution_status),
            missing_reasons=(f"execution_status:{packet.execution_status}",),
        )
    return None


def _malformed(packet: ModulePacket, error: ContractError) -> AdaptedModuleResult:
    return _result(
        packet,
        qualifiers=_qualifiers(packet, "SCHEMA_ERROR"),
        missing_reasons=(f"malformed_native_output:{error}",),
    )


def _observation(
    packet: ModulePacket,
    *,
    objective_id: str,
    kind: str,
    direction: str,
    raw_value: float | str,
    unit: str | None,
    uncertainty: Mapping[str, Any],
    basis: Mapping[str, Any],
    source_path: str,
    qualifiers: tuple[str, ...],
) -> ObjectiveObservation:
    return ObjectiveObservation(
        objective_id=objective_id,
        kind=kind,
        direction=direction,
        raw_value=raw_value,
        unit=unit,
        uncertainty=uncertainty,
        basis=basis,
        source_path=source_path,
        native_schema_id=packet.native_schema_id,
        native_schema_version=packet.native_schema_version,
        packet_sha256=packet.output_canonical_sha256,
        evidence_basis=packet.evidence_basis,
        qualifiers=qualifiers,
    )


def adapt_mapper(packet: ModulePacket) -> AdaptedModuleResult:
    """Preserve graph coverage and three-way evidence semantics.

    A mapper graph is run-scoped rather than hypothesis-scoped, so it never
    creates a candidate objective.  Hypothesis-specific scientific support is
    consumed from the locked Hypothesis Generator Slate instead.
    """

    early = _preflight(packet, MAPPER)
    if early is not None:
        return early
    try:
        payload = _mapping(packet.payload, "payload")
        graph_id = _text(payload.get("graph_id"), "payload.graph_id")
        graph_round = _integer(payload.get("round"), "payload.round", minimum=0)
        native_status = _text(payload.get("status"), "payload.status").lower()
        coverage = _mapping(payload.get("coverage"), "payload.coverage")
        papers = _sequence(payload.get("papers"), "payload.papers")
        findings = _sequence(payload.get("findings"), "payload.findings")

        directions = {"yes": 0, "no": 0, "no_effect": 0}
        for index, item in enumerate(findings):
            finding = _mapping(item, f"payload.findings[{index}]")
            says = _text(finding.get("says"), f"payload.findings[{index}].says")
            if says not in directions:
                raise ContractError(
                    f"payload.findings[{index}].says must be yes, no, or no_effect"
                )
            directions[says] += 1

        mismatch: list[str] = []
        if packet.subject.graph_id is not None and packet.subject.graph_id != graph_id:
            mismatch.append("graph_id_mismatch")
        if packet.subject.graph_round is not None and packet.subject.graph_round != graph_round:
            mismatch.append("graph_round_mismatch")
        qualifiers = list(_qualifiers(packet))
        if bool(coverage.get("truncated")):
            qualifiers.append("TRUNCATED")
        if native_status != "ok":
            qualifiers.append(native_status.upper())
        details = {
            "graphId": graph_id,
            "graphRound": graph_round,
            "nativeStatus": native_status,
            "error": payload.get("error"),
            "coverage": coverage,
            "directionCounts": directions,
            "papers": papers,
            "findings": findings,
        }
        if mismatch:
            return _result(
                packet,
                details=details,
                qualifiers=tuple(dict.fromkeys([*qualifiers, "QUARANTINED"])),
                quarantined=True,
                quarantine_reasons=tuple(mismatch),
            )
        missing = () if native_status == "ok" else (f"native_status:{native_status}",)
        return _result(
            packet,
            details=details,
            qualifiers=tuple(dict.fromkeys(qualifiers)),
            missing_reasons=missing,
        )
    except ContractError as error:
        return _malformed(packet, error)


def _validate_hypgen_issue(value: Any, path: str) -> Mapping[str, Any]:
    """Validate the locked ``ValidationIssue`` model without rewriting it."""

    issue = _mapping(value, path)
    _only_fields(issue, {"code", "detail", "severity"}, path)
    _text(issue.get("code"), f"{path}.code", allow_empty=True)
    _text(issue.get("detail"), f"{path}.detail", allow_empty=True)
    severity = issue.get("severity", "warning")
    if not isinstance(severity, str) or severity not in _HYPGEN_ISSUE_SEVERITIES:
        raise ContractError(f"{path}.severity is outside the locked union")
    return issue


def _validate_hypgen_verification(
    value: Any, path: str
) -> Mapping[str, Any]:
    """Validate the decision-bearing ``Verification`` model and its invariants.

    The nested shape mirrors the locked Pydantic models.  The extra coherence
    checks mirror ``hyp_gen.verify._verdict`` and its stop-on-halting-failure
    loop so a syntactically valid but relabelled verdict cannot become eligible.
    """

    verification = _mapping(value, path)
    _only_fields(verification, {"verdict", "gates", "halted_at"}, path)
    verdict = _text(verification.get("verdict"), f"{path}.verdict")
    if verdict not in _HYPGEN_VERIFICATION_VERDICTS:
        raise ContractError(f"{path}.verdict is outside the locked union")

    gates = _sequence(verification.get("gates", ()), f"{path}.gates")
    validated_gates: list[tuple[str, str, bool]] = []
    for index, item in enumerate(gates):
        gate_path = f"{path}.gates[{index}]"
        gate = _mapping(item, gate_path)
        _only_fields(gate, {"name", "status", "summary", "issues", "halting"}, gate_path)
        name = _text(gate.get("name"), f"{gate_path}.name", allow_empty=True)
        status = _text(gate.get("status"), f"{gate_path}.status")
        if status not in _HYPGEN_GATE_STATUSES:
            raise ContractError(f"{gate_path}.status is outside the locked union")
        _text(gate.get("summary", ""), f"{gate_path}.summary", allow_empty=True)
        gate_issues = _sequence(gate.get("issues", ()), f"{gate_path}.issues")
        for issue_index, issue in enumerate(gate_issues):
            _validate_hypgen_issue(issue, f"{gate_path}.issues[{issue_index}]")
        halting = _boolean(gate.get("halting", False), f"{gate_path}.halting")
        validated_gates.append((name, status, halting))

    halted_at = verification.get("halted_at")
    if halted_at is not None:
        halted_at = _text(halted_at, f"{path}.halted_at", allow_empty=True)
        halt_indexes = [
            index
            for index, (name, status, halting) in enumerate(validated_gates)
            if status == "fail" and halting
        ]
        if (
            len(halt_indexes) != 1
            or validated_gates[halt_indexes[0]][0] != halted_at
        ):
            raise ContractError(
                f"{path}.halted_at must identify exactly one failed halting gate"
            )
        halt_index = halt_indexes[0]
        if any(
            status != "skip" or halting
            for _, status, halting in validated_gates[halt_index + 1 :]
        ):
            raise ContractError(
                f"{path}.gates after halted_at must be non-halting skips"
            )
    elif any(status == "fail" and halting for _, status, halting in validated_gates):
        raise ContractError(
            f"{path}.halted_at is required when a halting gate fails"
        )

    if halted_at in _HYPGEN_INTEGRITY_GATES:
        expected_verdict = "rejected"
    elif halted_at is not None or any(
        status == "fail" for _, status, _ in validated_gates
    ):
        expected_verdict = "unverified"
    elif any(status in {"warn", "skip"} for _, status, _ in validated_gates):
        expected_verdict = "qualified"
    else:
        expected_verdict = "verified"
    if verdict != expected_verdict:
        raise ContractError(
            f"{path}.verdict {verdict!r} is inconsistent with locked gate semantics; "
            f"expected {expected_verdict!r}"
        )
    return verification


def _candidate_has_blocking_issue(candidate: HypothesisCandidate) -> bool:
    """Apply the locked error-severity blocking rule, including gate issues."""

    if any(issue.get("severity", "warning") == "error" for issue in candidate.issues):
        return True
    if candidate.verification is None:
        return False
    for gate in candidate.verification.get("gates", ()):
        if any(issue.get("severity", "warning") == "error" for issue in gate.get("issues", ())):
            return True
    return False


def _candidate_from_slate(
    hypothesis: Mapping[str, Any], graph_id: str, graph_round: int
) -> HypothesisCandidate:
    scores = _mapping(hypothesis.get("scores"), "hypothesis.scores")
    scientific_scores: dict[str, float] = {}
    for name, value in scores.items():
        scientific_scores[name] = _number(value, f"hypothesis.scores.{name}")
    path = _sequence(hypothesis.get("path", ()), "hypothesis.path")
    evidence = _mapping(hypothesis.get("evidence", {}), "hypothesis.evidence")
    caveats = _sequence(hypothesis.get("caveats", ()), "hypothesis.caveats")
    verification_value = hypothesis.get("verification")
    verification = (
        None
        if verification_value is None
        else _validate_hypgen_verification(
            verification_value, "hypothesis.verification"
        )
    )
    issues = _sequence(hypothesis.get("issues", ()), "hypothesis.issues")
    validated_issues = tuple(
        _validate_hypgen_issue(item, f"hypothesis.issues[{index}]")
        for index, item in enumerate(issues)
    )
    return HypothesisCandidate(
        source_id=_text(hypothesis.get("id"), "hypothesis.id"),
        graph_id=graph_id,
        graph_round=graph_round,
        subject_id=_text(hypothesis.get("subject"), "hypothesis.subject"),
        object_id=_text(hypothesis.get("object"), "hypothesis.object"),
        subject_name=_text(hypothesis.get("subject_name"), "hypothesis.subject_name"),
        object_name=_text(hypothesis.get("object_name"), "hypothesis.object_name"),
        scientific_scores=scientific_scores,
        path=tuple(_mapping(item, "hypothesis.path[]") for item in path),
        evidence=evidence,
        caveats=tuple(_text(item, "hypothesis.caveats[]", allow_empty=True) for item in caveats),
        verification=verification,
        issues=validated_issues,
        provenance=_text(
            hypothesis.get("provenance", ""), "hypothesis.provenance", allow_empty=True
        ),
        raw=hypothesis,
    )


def extract_hypothesis_candidates(payload: Mapping[str, Any]) -> tuple[HypothesisCandidate, ...]:
    """Validate a locked Slate and retain every exact producer candidate ID.

    This helper is intended for orchestrator fan-out.  Highlander ingestion
    should still call :func:`adapt_hypothesis_generator` on a packet bound to
    one exact ``hypothesisId``.
    """

    slate = _mapping(payload, "payload")
    graph_id = _text(slate.get("graph_id"), "payload.graph_id")
    graph_round = _integer(slate.get("round"), "payload.round", minimum=0)
    hypotheses = _sequence(slate.get("hypotheses"), "payload.hypotheses")
    candidates = tuple(
        _candidate_from_slate(_mapping(item, f"payload.hypotheses[{index}]"), graph_id, graph_round)
        for index, item in enumerate(hypotheses)
    )
    ids = [candidate.source_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ContractError("payload.hypotheses contains duplicate source ids")
    return candidates


def adapt_hypothesis_generator(packet: ModulePacket) -> AdaptedModuleResult:
    """Bind one exact Slate candidate and expose only its native scientific scores."""

    early = _preflight(packet, HYPOTHESIS_GENERATOR)
    if early is not None:
        return early
    try:
        payload = _mapping(packet.payload, "payload")
        candidates = extract_hypothesis_candidates(payload)
        graph_id = _text(payload.get("graph_id"), "payload.graph_id")
        graph_round = _integer(payload.get("round"), "payload.round", minimum=0)
        mismatch: list[str] = []
        if packet.subject.graph_id is not None and packet.subject.graph_id != graph_id:
            mismatch.append("graph_id_mismatch")
        if packet.subject.graph_round is not None and packet.subject.graph_round != graph_round:
            mismatch.append("graph_round_mismatch")
        if mismatch:
            return _result(
                packet,
                qualifiers=_qualifiers(packet, "QUARANTINED"),
                quarantined=True,
                quarantine_reasons=tuple(mismatch),
            )

        matches = [candidate for candidate in candidates if candidate.source_id == packet.hypothesis_id]
        if len(matches) != 1:
            raise ContractError(
                "packet hypothesisId must match exactly one locked Slate hypothesis id"
            )
        candidate = matches[0]
        missing_scores = [
            score_name
            for _, score_name, _ in _HYPOTHESIS_OBJECTIVES
            if score_name not in candidate.scientific_scores
        ]
        if missing_scores:
            return _result(
                packet,
                candidate=candidate,
                details={"graphId": graph_id, "graphRound": graph_round},
                qualifiers=_qualifiers(packet, "SCHEMA_ERROR"),
                missing_reasons=(
                    "missing_scientific_scores:" + ",".join(sorted(missing_scores)),
                ),
            )
        for _, score_name, _ in _HYPOTHESIS_OBJECTIVES:
            _number(
                candidate.scientific_scores[score_name],
                f"hypothesis.scores.{score_name}",
                minimum=0.0,
                maximum=1.0,
            )

        coverage = _mapping(payload.get("coverage", {}), "payload.coverage")
        qualifiers = list(_qualifiers(packet))
        if bool(coverage.get("truncated")):
            qualifiers.append("TRUNCATED")
        if candidate.verification is None:
            qualifiers.append("UNVERIFIED")
        else:
            verdict = candidate.verification.get("verdict")
            qualifiers.append(verdict.upper())
        if _candidate_has_blocking_issue(candidate):
            qualifiers.append("BLOCKED")
        objective_qualifiers = tuple(dict.fromkeys(qualifiers))
        uncertainty = {
            "absenceReliability": candidate.scientific_scores.get("absence_reliability"),
            "coverage": coverage,
            "verification": candidate.verification,
            "caveats": candidate.caveats,
        }
        comparison_basis = {
            "nativeSchemaId": packet.native_schema_id,
            "nativeSchemaVersion": packet.native_schema_version,
            "adapterVersion": packet.adapter_version,
            "graphId": graph_id,
            "graphRound": graph_round,
        }
        scientific_context = {
            "path": candidate.path,
            "evidence": candidate.evidence,
            "issues": candidate.issues,
            "provenance": candidate.provenance,
        }
        observations = tuple(
            _observation(
                packet,
                objective_id=objective_id,
                kind="NUMERIC",
                direction=direction,
                raw_value=candidate.scientific_scores[score_name],
                unit="score_0_1",
                uncertainty=uncertainty,
                basis=comparison_basis,
                source_path=f"$.hypotheses[id={candidate.source_id}].scores.{score_name}",
                qualifiers=objective_qualifiers,
            )
            for objective_id, score_name, direction in _HYPOTHESIS_OBJECTIVES
        )
        return _result(
            packet,
            objectives=observations,
            candidate=candidate,
            details={
                "graphId": graph_id,
                "graphRound": graph_round,
                "question": payload.get("question"),
                "coverage": coverage,
                "counts": payload.get("counts", {}),
                "params": payload.get("params", {}),
                "scientificContext": scientific_context,
            },
            qualifiers=objective_qualifiers,
        )
    except ContractError as error:
        return _malformed(packet, error)


def adapt_recruitment(packet: ModulePacket) -> AdaptedModuleResult:
    """Expose the forecaster's required native score with its full simulated basis."""

    early = _preflight(packet, RECRUITMENT)
    if early is not None:
        return early
    try:
        out = _mapping(packet.payload, "payload")
        score = _number(out.get("score"), "payload.score", minimum=0.0, maximum=1.0)
        months = _number(
            out.get("simulatedMonthsToEnroll"),
            "payload.simulatedMonthsToEnroll",
            minimum=0.0,
        )
        months_range = _sequence(out.get("simulatedMonthsRange"), "payload.simulatedMonthsRange")
        if len(months_range) != 2:
            raise ContractError("payload.simulatedMonthsRange must contain exactly two values")
        lower = _number(months_range[0], "payload.simulatedMonthsRange[0]", minimum=0.0)
        upper = _number(months_range[1], "payload.simulatedMonthsRange[1]", minimum=0.0)
        if lower > months or months > upper:
            raise ContractError(
                "payload.simulatedMonthsRange must be ordered and contain simulatedMonthsToEnroll"
            )
        required_n = _integer(out.get("requiredN"), "payload.requiredN", minimum=1)
        sites = _integer(out.get("sites"), "payload.sites", minimum=1)
        sites_basis = _text(out.get("sitesBasis"), "payload.sitesBasis")
        if sites_basis not in {"input", "precedent", "default"}:
            raise ContractError("payload.sitesBasis is outside the locked union")

        counterfactual = None
        if "counterfactual" in out:
            counterfactual = _mapping(
                out["counterfactual"], "payload.counterfactual"
            )
            achieves = _text(
                counterfactual.get("achieves"),
                "payload.counterfactual.achieves",
            )
            if achieves not in {"good", "feasible", "none"}:
                raise ContractError(
                    "payload.counterfactual.achieves is outside the locked union"
                )
            _text(
                counterfactual.get("change"),
                "payload.counterfactual.change",
            )
            _number(
                counterfactual.get("simulatedMonthsAfter"),
                "payload.counterfactual.simulatedMonthsAfter",
                minimum=0.0,
            )

        eligibility = _mapping(out.get("eligibility"), "payload.eligibility")
        _number(
            eligibility.get("multiplier"),
            "payload.eligibility.multiplier",
            minimum=0.0,
            maximum=1.0,
        )
        _text_sequence(
            eligibility.get("citedTrials"), "payload.eligibility.citedTrials"
        )
        _text_sequence(eligibility.get("drivers"), "payload.eligibility.drivers")
        _text(eligibility.get("reasoning"), "payload.eligibility.reasoning")

        evidence = _mapping(out.get("evidence"), "payload.evidence")
        _integer(
            evidence.get("competingTrials"),
            "payload.evidence.competingTrials",
            minimum=0,
        )
        _text_sequence(
            evidence.get("precedentTrials"), "payload.evidence.precedentTrials"
        )

        failed_precedents = _sequence(
            out.get("failedPrecedents"), "payload.failedPrecedents"
        )
        for index, item in enumerate(failed_precedents):
            failed = _mapping(item, f"payload.failedPrecedents[{index}]")
            _text(failed.get("nctId"), f"payload.failedPrecedents[{index}].nctId")
            _text(
                failed.get("whyStopped"),
                f"payload.failedPrecedents[{index}].whyStopped",
            )

        phase3_median_n = None
        if "phase3MedianN" in out:
            phase3_median_n = _number(
                out["phase3MedianN"],
                "payload.phase3MedianN",
                minimum=0.0,
            )
        precedent_median_n = None
        if "precedentMedianN" in out:
            precedent_median_n = _number(
                out["precedentMedianN"],
                "payload.precedentMedianN",
                minimum=0.0,
            )

        _number(
            out.get("screensPerEnrollee"),
            "payload.screensPerEnrollee",
            minimum=1.0,
        )
        _number(out.get("waterfallDelta"), "payload.waterfallDelta")
        _text(out.get("poweringBasis"), "payload.poweringBasis")
        _text(out.get("why"), "payload.why")
        payload_as_of = None
        if "asOf" in out:
            payload_as_of = _text(out["asOf"], "payload.asOf")
        if (
            packet.subject.as_of is not None
            and payload_as_of is not None
            and packet.subject.as_of != payload_as_of
        ):
            return _result(
                packet,
                qualifiers=_qualifiers(packet, "QUARANTINED"),
                quarantined=True,
                quarantine_reasons=("as_of_mismatch",),
            )
        as_of = payload_as_of or packet.subject.as_of

        qualifier_extras = ("SIMULATED",)
        if as_of is None:
            qualifier_extras += ("HORIZON_UNSPECIFIED",)
        qualifiers = _qualifiers(packet, *qualifier_extras)
        uncertainty = {
            "simulatedMonthsToEnroll": months,
            "simulatedMonthsRange": (lower, upper),
            "counterfactual": counterfactual,
        }
        comparison_basis = {
            "nativeSchemaId": packet.native_schema_id,
            "nativeSchemaVersion": packet.native_schema_version,
            "adapterVersion": packet.adapter_version,
            "asOf": as_of or "UNSPECIFIED",
        }
        native_basis = {
            "requiredN": required_n,
            "poweringBasis": out["poweringBasis"],
            "sites": sites,
            "sitesBasis": sites_basis,
            "phase3MedianN": phase3_median_n,
            "precedentMedianN": precedent_median_n,
            "screensPerEnrollee": out["screensPerEnrollee"],
            "eligibility": eligibility,
            "evidence": evidence,
            "failedPrecedents": out["failedPrecedents"],
            "waterfallDelta": out["waterfallDelta"],
            "why": out["why"],
        }
        observation = _observation(
            packet,
            objective_id="recruitability",
            kind="NUMERIC",
            direction="MAX",
            raw_value=score,
            unit="score_0_1",
            uncertainty=uncertainty,
            basis=comparison_basis,
            source_path="$.score",
            qualifiers=qualifiers,
        )
        return _result(
            packet,
            objectives=(observation,),
            details={
                "uncertainty": uncertainty,
                "comparisonBasis": comparison_basis,
                "nativeBasis": native_basis,
            },
            qualifiers=qualifiers,
        )
    except ContractError as error:
        return _malformed(packet, error)


def adapt_economics(packet: ModulePacket) -> AdaptedModuleResult:
    """Read AnalysisResult 1.3.0's nested summary without normalising rNPV."""

    early = _preflight(packet, ECONOMICS)
    if early is not None:
        return early
    try:
        out = _mapping(packet.payload, "payload")
        schema_version = _text(out.get("schema_version"), "payload.schema_version")
        if schema_version != "1.3.0" or packet.native_schema_version != "1.3.0":
            raise ContractError("economics adapter accepts only locked AnalysisResult 1.3.0")
        summary = _mapping(out.get("summary"), "payload.summary")
        p10 = _number(summary.get("p10_rnpv"), "payload.summary.p10_rnpv")
        p50 = _number(summary.get("p50_rnpv"), "payload.summary.p50_rnpv")
        p90 = _number(summary.get("p90_rnpv"), "payload.summary.p90_rnpv")
        if not p10 <= p50 <= p90:
            raise ContractError("payload.summary rNPV percentiles must satisfy p10 <= p50 <= p90")
        probability = _number(
            summary.get("probability_positive_rnpv"),
            "payload.summary.probability_positive_rnpv",
            minimum=0.0,
            maximum=1.0,
        )
        program_id = _text(summary.get("program_id"), "payload.summary.program_id")
        snapshot = _mapping(out.get("input_snapshot"), "payload.input_snapshot")
        has_program = "program" in snapshot
        has_cashflow = "cashflow_inputs" in snapshot
        if has_program == has_cashflow:
            raise ContractError(
                "payload.input_snapshot must contain exactly one of program or "
                "cashflow_inputs"
            )
        if has_program:
            snapshot_mode = "program"
            snapshot_program = _mapping(
                snapshot.get("program"), "payload.input_snapshot.program"
            )
            _mapping(
                snapshot.get("comparables"), "payload.input_snapshot.comparables"
            )
            base_year = _integer(
                snapshot_program.get("base_year"),
                "payload.input_snapshot.program.base_year",
            )
        else:
            snapshot_mode = "cashflow_inputs"
            snapshot_program = _mapping(
                snapshot.get("cashflow_inputs"),
                "payload.input_snapshot.cashflow_inputs",
            )
            base_year_raw = snapshot_program.get("base_year")
            base_year = (
                _integer(
                    base_year_raw,
                    "payload.input_snapshot.cashflow_inputs.base_year",
                )
                if base_year_raw is not None
                else None
            )
        snapshot_program_id = _text(
            snapshot_program.get("program_id"),
            f"payload.input_snapshot.{snapshot_mode}.program_id",
        )
        if snapshot_program_id != program_id:
            raise ContractError("summary.program_id does not match input_snapshot.program_id")
        currency = _text(
            snapshot_program.get("currency"),
            f"payload.input_snapshot.{snapshot_mode}.currency",
        )
        if not re.fullmatch(r"[A-Za-z]{3}", currency):
            raise ContractError(
                f"payload.input_snapshot.{snapshot_mode}.currency must be a "
                "three-letter currency code"
            )
        currency = currency.upper()
        valuation_year = _integer(
            snapshot_program.get("valuation_year"),
            f"payload.input_snapshot.{snapshot_mode}.valuation_year",
        )
        run_id = _text(out.get("run_id"), "payload.run_id")
        engine_version = _text(out.get("engine_version"), "payload.engine_version")
        if engine_version != "0.4.0":
            raise ContractError("economics adapter accepts only locked engine 0.4.0")
        input_digest = _text(out.get("input_digest"), "payload.input_digest")
        if not _SHA256_URI_RE.fullmatch(input_digest):
            raise ContractError("payload.input_digest must be a sha256: digest")
        expected_run_id = f"run_{input_digest.removeprefix('sha256:')[:20]}"
        if run_id != expected_run_id:
            raise ContractError(
                "payload.run_id does not match the locked input_digest-derived run ID"
            )
        seed = _integer(out.get("seed"), "payload.seed")
        simulations = _integer(out.get("simulations"), "payload.simulations", minimum=1)
        simulation_assumptions = _mapping(
            out.get("simulation_assumptions"), "payload.simulation_assumptions"
        )
        for range_name in sorted(_ECONOMICS_SIMULATION_RANGES):
            triangular = _mapping(
                simulation_assumptions.get(range_name),
                f"payload.simulation_assumptions.{range_name}",
            )
            low = _number(
                triangular.get("low"),
                f"payload.simulation_assumptions.{range_name}.low",
            )
            mode = _number(
                triangular.get("mode"),
                f"payload.simulation_assumptions.{range_name}.mode",
            )
            high = _number(
                triangular.get("high"),
                f"payload.simulation_assumptions.{range_name}.high",
            )
            if not low <= mode <= high:
                raise ContractError(
                    f"payload.simulation_assumptions.{range_name} must satisfy "
                    "low <= mode <= high"
                )
        decision_grade = _text(out.get("decision_grade"), "payload.decision_grade")
        if decision_grade not in _ECONOMICS_DECISION_GRADES:
            raise ContractError("payload.decision_grade is outside the locked union")
        recommendation = _text(out.get("recommendation"), "payload.recommendation")
        if recommendation not in _ECONOMICS_RECOMMENDATIONS:
            raise ContractError("payload.recommendation is outside the locked union")
        summary_recommendation = _text(
            summary.get("recommendation"), "payload.summary.recommendation"
        )
        if recommendation != summary_recommendation:
            raise ContractError(
                "payload.recommendation does not match summary.recommendation"
            )
        if (decision_grade == "NOT_DECISION_GRADE") != (
            recommendation == "NOT_DECISION_GRADE"
        ):
            raise ContractError(
                "NOT_DECISION_GRADE status and recommendation must agree"
            )
        warnings = _sequence(out.get("warnings"), "payload.warnings")
        warning_severities: list[str] = []
        for index, item in enumerate(warnings):
            warning = _mapping(item, f"payload.warnings[{index}]")
            _text(warning.get("code"), f"payload.warnings[{index}].code", allow_empty=True)
            _text(
                warning.get("message"),
                f"payload.warnings[{index}].message",
                allow_empty=True,
            )
            severity = warning.get("severity", "WARNING")
            if severity not in _ECONOMICS_WARNING_SEVERITIES:
                raise ContractError(
                    f"payload.warnings[{index}].severity is outside the locked union"
                )
            warning_severities.append(severity)
        uncertainty_raw = _mapping(out.get("uncertainty"), "payload.uncertainty")
        uncertainty_seed = _integer(
            uncertainty_raw.get("seed"), "payload.uncertainty.seed"
        )
        uncertainty_simulations = _integer(
            uncertainty_raw.get("simulations"),
            "payload.uncertainty.simulations",
            minimum=1,
        )
        if uncertainty_seed != seed or uncertainty_simulations != simulations:
            raise ContractError(
                "payload.uncertainty seed/simulations must match the result envelope"
            )
        uncertainty_contract = {
            "rngBitGenerator": _text(
                uncertainty_raw.get("rng_bit_generator"),
                "payload.uncertainty.rng_bit_generator",
            ),
            "numpyVersion": _text(
                uncertainty_raw.get("numpy_version"),
                "payload.uncertainty.numpy_version",
            ),
            "drawOrderContractVersion": _text(
                uncertainty_raw.get("draw_order_contract_version"),
                "payload.uncertainty.draw_order_contract_version",
            ),
            "commercialDriverCorrelation": _text(
                uncertainty_raw.get("commercial_driver_correlation"),
                "payload.uncertainty.commercial_driver_correlation",
            ),
        }
        rnpv_distribution = _mapping(
            uncertainty_raw.get("rnpv"), "payload.uncertainty.rnpv"
        )
        uncertainty_p10 = _number(
            rnpv_distribution.get("p10"), "payload.uncertainty.rnpv.p10"
        )
        uncertainty_p50 = _number(
            rnpv_distribution.get("p50"), "payload.uncertainty.rnpv.p50"
        )
        uncertainty_p90 = _number(
            rnpv_distribution.get("p90"), "payload.uncertainty.rnpv.p90"
        )
        uncertainty_probability = _number(
            rnpv_distribution.get("probability_positive"),
            "payload.uncertainty.rnpv.probability_positive",
            minimum=0.0,
            maximum=1.0,
        )
        if (
            uncertainty_p10 != p10
            or uncertainty_p50 != p50
            or uncertainty_p90 != p90
            or uncertainty_probability != probability
        ):
            raise ContractError(
                "payload.summary rNPV values must match payload.uncertainty.rnpv"
            )
        critical_status = _mapping(
            out.get("critical_evidence_status"), "payload.critical_evidence_status"
        )
        if not critical_status:
            raise ContractError("payload.critical_evidence_status must not be empty")
        for name, supported in critical_status.items():
            _text(name, "payload.critical_evidence_status key")
            _boolean(supported, f"payload.critical_evidence_status.{name}")
        references = _sequence(out.get("evidence_references"), "payload.evidence_references")
        pricing = _sequence(out.get("pricing"), "payload.pricing")
        pricing_grades: list[str] = []
        for index, item in enumerate(pricing):
            pricing_item = _mapping(item, f"payload.pricing[{index}]")
            pricing_grade = _text(
                pricing_item.get("decision_grade"),
                f"payload.pricing[{index}].decision_grade",
            )
            if pricing_grade not in _ECONOMICS_DECISION_GRADES:
                raise ContractError(
                    f"payload.pricing[{index}].decision_grade is outside the locked union"
                )
            pricing_grades.append(pricing_grade)
        if decision_grade == "DECISION_GRADE" and (
            not all(critical_status.values())
            or "ERROR" in warning_severities
            or any(grade != "DECISION_GRADE" for grade in pricing_grades)
        ):
            raise ContractError(
                "payload.decision_grade is inconsistent with critical evidence, "
                "warnings, or pricing grades"
            )

        extra_qualifiers = [decision_grade]
        if warnings:
            extra_qualifiers.append("HAS_WARNINGS")
        qualifiers = _qualifiers(packet, *extra_qualifiers)
        uncertainty = {
            "p10Rnpv": p10,
            "p50Rnpv": p50,
            "p90Rnpv": p90,
            "probabilityPositiveRnpv": probability,
            "native": uncertainty_raw,
        }
        comparison_basis = {
            "nativeSchemaId": packet.native_schema_id,
            "nativeSchemaVersion": packet.native_schema_version,
            "adapterVersion": packet.adapter_version,
            "engineVersion": engine_version,
            "currency": currency,
            "baseYear": base_year,
            "valuationYear": valuation_year,
            "inputSnapshotMode": snapshot_mode,
            "simulations": simulations,
            "simulationAssumptions": simulation_assumptions,
            "uncertaintyContract": uncertainty_contract,
        }
        native_basis = {
            "programId": program_id,
            "currency": currency,
            "baseYear": base_year,
            "valuationYear": valuation_year,
            "runId": run_id,
            "engineVersion": engine_version,
            "inputDigest": input_digest,
            "seed": seed,
            "simulations": simulations,
            "inputSnapshotMode": snapshot_mode,
            "simulationAssumptions": simulation_assumptions,
            "uncertaintyContract": uncertainty_contract,
            "decisionGrade": decision_grade,
            "recommendation": recommendation,
            "warnings": warnings,
            "criticalEvidenceStatus": critical_status,
            "evidenceReferences": references,
        }
        observation = _observation(
            packet,
            objective_id="roi",
            kind="NUMERIC",
            direction="MAX",
            raw_value=p50,
            unit=currency,
            uncertainty=uncertainty,
            basis=comparison_basis,
            source_path="$.summary.p50_rnpv",
            qualifiers=qualifiers,
        )
        return _result(
            packet,
            objectives=(observation,),
            details={
                "summary": summary,
                "uncertainty": uncertainty,
                "comparisonBasis": comparison_basis,
                "nativeBasis": native_basis,
            },
            qualifiers=qualifiers,
        )
    except ContractError as error:
        return _malformed(packet, error)


def _normalised_optional(value: Any, path: str, *, casefold: bool = False) -> str | None:
    if value is None:
        return None
    text = _text(value, path).strip()
    return text.casefold() if casefold else text


def adapt_tractability(packet: ModulePacket) -> AdaptedModuleResult:
    """Preserve the dossier verdict as categorical, subject-bound evidence.

    There is deliberately no derived ``bio_reality`` number.  Target,
    mechanism, and as-of mismatches are quarantined before the verdict is made
    visible to portfolio comparison.
    """

    early = _preflight(packet, TRACTABILITY)
    if early is not None:
        return early
    try:
        native_payload = json.loads(canonical_json_bytes(packet.payload))
        violations = validate_dossier(native_payload)
        if violations:
            return _result(
                packet,
                details={
                    "validatorSha256": TRACTABILITY_VALIDATOR_SHA256,
                    "nativeValidationViolations": [
                        violation.as_dict() for violation in violations
                    ],
                },
                qualifiers=_qualifiers(
                    packet, "QUARANTINED", "NATIVE_VALIDATION_FAILED"
                ),
                quarantined=True,
                quarantine_reasons=("native_tractability_validator_rejected",),
            )
        out = _mapping(native_payload, "payload")
        target = _mapping(out.get("target"), "payload.target")
        dossier_input = _mapping(out.get("input"), "payload.input")
        resolved_accession = _text(
            target.get("uniprot_accession"), "payload.target.uniprot_accession"
        ).upper()
        input_accession = _text(
            dossier_input.get("uniprot_accession"), "payload.input.uniprot_accession"
        ).upper()
        gene_symbol = _text(target.get("gene_symbol"), "payload.target.gene_symbol").upper()
        if resolved_accession != input_accession:
            return _result(
                packet,
                details={"target": target, "input": dossier_input},
                qualifiers=_qualifiers(packet, "QUARANTINED"),
                quarantined=True,
                quarantine_reasons=("dossier_input_target_mismatch",),
            )

        modality = _normalised_optional(packet.subject.modality, "subject.modality", casefold=True)
        if modality is None:
            return _result(
                packet,
                details={"target": target, "input": dossier_input},
                qualifiers=_qualifiers(packet, "NEEDS_INPUT"),
                missing_reasons=("subject.modality:missing",),
            )
        if modality != "small_molecule":
            return _result(
                packet,
                details={"target": target, "input": dossier_input},
                qualifiers=_qualifiers(packet, "NOT_AMENABLE"),
                missing_reasons=(f"modality_not_amenable:{packet.subject.modality}",),
            )

        mismatches: list[str] = []
        subject_accession = _normalised_optional(
            packet.subject.uniprot_accession,
            "subject.uniprot_accession",
            casefold=True,
        )
        if subject_accession is None:
            mismatches.append("subject_missing_uniprot_accession")
        elif subject_accession != resolved_accession.casefold():
            mismatches.append("uniprot_accession_mismatch")
        subject_symbol = _normalised_optional(
            packet.subject.target_symbol, "subject.target_symbol", casefold=True
        )
        if subject_symbol is not None and subject_symbol != gene_symbol.casefold():
            mismatches.append("target_symbol_mismatch")

        dossier_mechanism = _normalised_optional(
            dossier_input.get("mechanism_hypothesis"),
            "payload.input.mechanism_hypothesis",
            casefold=True,
        )
        subject_mechanism = _normalised_optional(
            packet.subject.mechanism_hypothesis,
            "subject.mechanism_hypothesis",
            casefold=True,
        )
        if dossier_mechanism != subject_mechanism:
            mismatches.append("mechanism_hypothesis_mismatch")

        input_as_of = _normalised_optional(
            dossier_input.get("as_of_date"), "payload.input.as_of_date"
        )
        root_as_of = _normalised_optional(out.get("as_of_date"), "payload.as_of_date")
        if input_as_of != root_as_of:
            mismatches.append("dossier_as_of_inconsistency")
        subject_as_of = _normalised_optional(packet.subject.as_of, "subject.as_of")
        if input_as_of != subject_as_of:
            mismatches.append("as_of_mismatch")

        verdict = _text(out.get("verdict"), "payload.verdict")
        if verdict not in _TRACTABILITY_VERDICTS:
            raise ContractError("payload.verdict is outside the locked union")
        verdict_basis = _text(out.get("verdict_basis"), "payload.verdict_basis")
        if verdict_basis not in _TRACTABILITY_BASES:
            raise ContractError("payload.verdict_basis is outside the locked union")
        tractability = _mapping(out.get("tractability"), "payload.tractability")
        falsification = _mapping(out.get("falsification"), "payload.falsification")
        next_experiment = _mapping(out.get("next_experiment"), "payload.next_experiment")
        not_found = _sequence(out.get("not_found"), "payload.not_found")
        axis_conflict = out.get("axis_conflict")
        if axis_conflict is not None:
            _text(axis_conflict, "payload.axis_conflict")
        target_precedent = out.get("target_precedent")
        if target_precedent is not None:
            _mapping(target_precedent, "payload.target_precedent")

        details = {
            "validatorSha256": TRACTABILITY_VALIDATOR_SHA256,
            "input": dossier_input,
            "target": target,
            "asOfDate": root_as_of,
            "verdict": verdict,
            "verdictBasis": verdict_basis,
            "axisConflict": axis_conflict,
            "targetPrecedent": target_precedent,
            "tractability": tractability,
            "falsification": falsification,
            "nextExperiment": next_experiment,
            "notFound": not_found,
        }
        if mismatches:
            return _result(
                packet,
                details=details,
                qualifiers=_qualifiers(packet, "QUARANTINED"),
                quarantined=True,
                quarantine_reasons=tuple(dict.fromkeys(mismatches)),
            )

        extra = [verdict.upper(), verdict_basis.upper()]
        if axis_conflict is not None:
            extra.append("AXIS_CONFLICT")
        if falsification.get("survived") is False:
            extra.append("FALSIFICATION_NOT_SURVIVED")
        qualifiers = _qualifiers(packet, *extra)
        if verdict == "insufficient_evidence":
            return _result(
                packet,
                details=details,
                qualifiers=qualifiers,
                missing_reasons=("tractability:insufficient_evidence",),
            )

        observation = _observation(
            packet,
            objective_id="tractability_posture",
            kind="CATEGORICAL",
            direction="CATEGORICAL",
            raw_value=verdict,
            unit=None,
            uncertainty={
                "axisConflict": axis_conflict,
                "notFound": not_found,
            },
            basis={
                "verdictBasis": verdict_basis,
                "targetPrecedent": target_precedent,
                "computedTractability": tractability,
                "falsification": falsification,
                "nextExperiment": next_experiment,
            },
            source_path="$.verdict",
            qualifiers=qualifiers,
        )
        return _result(
            packet,
            objectives=(observation,),
            details=details,
            qualifiers=qualifiers,
        )
    except ContractError as error:
        return _malformed(packet, error)
