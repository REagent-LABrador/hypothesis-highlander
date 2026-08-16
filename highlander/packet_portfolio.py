"""Safe, deterministic portfolio comparison for orchestrator-owned packets.

This module deliberately does not know how any LABrador producer encodes its native
payload.  Adapters own that boundary.  The comparator accepts a small duck-typed
candidate/observation protocol, preserves the raw observations, and performs only
nominal Pareto comparison.

Safety properties:

* every policy objective must be present and finite before a candidate can compare;
* candidates compare only when direction, unit, basis, and evidence basis match;
* MAX and MIN objectives are handled without normalising or inverting raw values;
* tied vectors retain every distinct candidate in an equivalence group; and
* results contain frontiers and relationships, never a winner or ``top`` ranking.

The public ``coerce_*`` helpers accept either mappings or objects with the documented
attributes.  This lets the output orchestrator pass its frozen packet dataclasses in
without coupling Highlander to their implementation module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import re
from numbers import Real
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


RESULT_SCHEMA_VERSION = "highlander.portfolio-result.v1"
_DIRECTIONS = frozenset({"MAX", "MIN"})
_OBSERVATION_DIRECTIONS = frozenset({"MAX", "MIN", "CATEGORICAL"})
_OBSERVATION_KINDS = frozenset({"NUMERIC", "CATEGORICAL"})
_EVIDENCE_BASES = frozenset(
    {
        "LIVE",
        "OBSERVED",
        "MODELED",
        "INFERRED",
        "PROXY",
        "SYNTHETIC",
        "ASSUMED",
        "SIMULATED",
        "MOCK",
        "NOT_RUN",
        "NOT_WIRED",
    }
)
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")
_MISSING = object()
_DEFAULT_CANDIDATE_EXCLUSION_QUALIFIERS = frozenset(
    {"BLOCKED", "REJECTED", "UNVERIFIED"}
)
_DEFAULT_OBJECTIVE_EXCLUSION_QUALIFIERS = frozenset({"NOT_DECISION_GRADE"})


def _json_native(value: Any, path: str = "$") -> Any:
    """Copy generic mapping/tuple contracts into unambiguous JSON-native data."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            result[key] = _json_native(item, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_native(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ValueError(f"{path} contains unsupported JSON value {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    """Return canonical JSON or raise a useful error for non-JSON input."""
    try:
        return json.dumps(
            _json_native(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _deep_freeze_json(value: Any) -> Any:
    native = _json_native(value)
    if isinstance(native, dict):
        return MappingProxyType(
            {key: _deep_freeze_json(item) for key, item in native.items()}
        )
    if isinstance(native, list):
        return tuple(_deep_freeze_json(item) for item in native)
    return native


def _read(value: Any, *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    if default is not _MISSING:
        return default
    raise ValueError(f"missing required field (accepted names: {', '.join(names)})")


def _as_direction(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    direction = str(value).upper()
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction must be MAX or MIN, got {value!r}")
    return direction


def _as_observation_direction(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    direction = str(value).upper()
    if direction not in _OBSERVATION_DIRECTIONS:
        raise ValueError(f"observation direction is invalid: {value!r}")
    return direction


def _strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence of strings")
    result = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
    return result


def _validate_identifier(value: Any, field_name: str) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _validate_timestamp(value: Any) -> str:
    timestamp = _validate_identifier(value, "created_at")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must include a UTC offset")
    return timestamp


@dataclass(frozen=True)
class ObjectiveRule:
    """One required numeric objective in a disclosed comparison policy."""

    objective_id: str
    direction: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "objective_id", _validate_identifier(self.objective_id, "objective_id")
        )
        object.__setattr__(self, "direction", _as_direction(self.direction))

    def to_dict(self) -> dict[str, Any]:
        return {"objectiveId": self.objective_id, "direction": self.direction}


@dataclass(frozen=True)
class ComparisonPolicy:
    """Versioned set of objectives; every listed objective is required."""

    policy_id: str
    objectives: tuple[ObjectiveRule, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", _validate_identifier(self.policy_id, "policy_id"))
        objectives = tuple(coerce_rule(item) for item in self.objectives)
        if not objectives:
            raise ValueError("comparison policy must contain at least one objective")
        ids = [rule.objective_id for rule in objectives]
        if len(ids) != len(set(ids)):
            raise ValueError("comparison policy objective IDs must be unique")
        object.__setattr__(
            self,
            "objectives",
            tuple(sorted(objectives, key=lambda item: item.objective_id)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policyId": self.policy_id,
            "objectives": [rule.to_dict() for rule in self.objectives],
            "missingValuePolicy": "INCOMPARABLE",
            "mixedBasisPolicy": "SEPARATE_COMPARISON_GROUPS",
            "uncertaintyPolicy": "NOMINAL_ONLY",
            "excludedCandidateQualifiers": sorted(
                _DEFAULT_CANDIDATE_EXCLUSION_QUALIFIERS
            ),
            "excludedObjectiveQualifiers": sorted(
                _DEFAULT_OBJECTIVE_EXCLUSION_QUALIFIERS
            ),
        }


@dataclass(frozen=True)
class PortfolioObservation:
    """Raw adapter observation; values are never normalised by this module.

    ``basis`` must contain the interpretation context needed to compare the value,
    such as valuation year/currency basis or producer scoring-policy version.
    ``evidence_basis`` is separately matched so LIVE, MODELED, PROXY, SYNTHETIC,
    and ASSUMED results cannot silently compete.
    """

    objective_id: str
    value: Any
    direction: str
    unit: str
    basis: Mapping[str, Any]
    evidence_basis: str
    kind: str = "NUMERIC"
    evidence_bases: tuple[str, ...] = field(default_factory=tuple)
    uncertainty: Any | None = None
    source_path: str | None = None
    source_schema_id: str | None = None
    source_schema_version: str | None = None
    source_hash: str | None = None
    derived_policy_id: str | None = None
    qualifiers: tuple[str, ...] = field(default_factory=tuple)
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "objective_id", _validate_identifier(self.objective_id, "objective_id")
        )
        object.__setattr__(self, "direction", _as_observation_direction(self.direction))
        kind = str(self.kind).upper()
        if kind not in _OBSERVATION_KINDS:
            raise ValueError(f"observation kind is invalid: {self.kind!r}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "unit", "" if self.unit is None else str(self.unit).strip())
        basis = _json_copy(self.basis or {})
        if not isinstance(basis, dict):
            raise ValueError("basis must be a JSON object")
        object.__setattr__(self, "basis", _deep_freeze_json(basis))
        evidence_basis = _validate_identifier(
            self.evidence_basis, "evidence_basis"
        ).upper()
        if evidence_basis not in {*_EVIDENCE_BASES, "UNSPECIFIED"}:
            raise ValueError(f"evidence_basis is invalid: {self.evidence_basis!r}")
        object.__setattr__(self, "evidence_basis", evidence_basis)
        evidence_bases = {str(value).upper() for value in self.evidence_bases}
        invalid_bases = evidence_bases - _EVIDENCE_BASES
        if invalid_bases:
            raise ValueError(
                f"evidence_bases contains invalid values: {sorted(invalid_bases)}"
            )
        if evidence_basis != "UNSPECIFIED":
            evidence_bases.add(evidence_basis)
        evidence_bases.update(
            str(qualifier).upper()
            for qualifier in self.qualifiers
            if str(qualifier).upper() in _EVIDENCE_BASES
        )
        object.__setattr__(self, "evidence_bases", tuple(sorted(evidence_bases)))
        _canonical_json(self.value)
        if self.uncertainty is not None:
            object.__setattr__(
                self, "uncertainty", _deep_freeze_json(self.uncertainty)
            )
        for field_name in (
            "source_path",
            "source_schema_id",
            "source_schema_version",
            "source_hash",
            "derived_policy_id",
            "missing_reason",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, str(value))
        object.__setattr__(self, "qualifiers", _strings(self.qualifiers, "qualifiers"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "objectiveId": self.objective_id,
            "rawValue": _json_copy(self.value),
            "kind": self.kind,
            "direction": self.direction,
            "unit": self.unit,
            "basis": _json_copy(self.basis),
            "evidenceBasis": self.evidence_basis,
            "evidenceBases": list(self.evidence_bases),
            "qualifiers": list(self.qualifiers),
        }
        if self.uncertainty is not None:
            result["uncertainty"] = _json_copy(self.uncertainty)
        if self.source_path is not None:
            result["sourcePath"] = self.source_path
        if self.source_schema_id is not None:
            result["sourceSchemaId"] = self.source_schema_id
        if self.source_schema_version is not None:
            result["sourceSchemaVersion"] = self.source_schema_version
        if self.source_hash is not None:
            result["sourceHash"] = self.source_hash
        if self.derived_policy_id is not None:
            result["derivedPolicyId"] = self.derived_policy_id
        if self.missing_reason is not None:
            result["missingReason"] = self.missing_reason
        return result


@dataclass(frozen=True)
class PortfolioCandidate:
    """One immutable hypothesis packet revision selected by the orchestrator."""

    candidate_id: str
    packet_revision_id: str
    packet_hash: str
    observations: tuple[PortfolioObservation, ...]
    run_id: str | None = None
    module_attempts: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    qualifiers: tuple[str, ...] = field(default_factory=tuple)
    exclusion_reasons: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "candidate_id", _validate_identifier(self.candidate_id, "candidate_id")
        )
        object.__setattr__(
            self,
            "packet_revision_id",
            _validate_identifier(self.packet_revision_id, "packet_revision_id"),
        )
        packet_hash = _validate_identifier(self.packet_hash, "packet_hash")
        if not _SHA256_RE.fullmatch(packet_hash):
            raise ValueError(
                "packet_hash must be a SHA-256 hex digest, optionally prefixed by sha256:"
            )
        object.__setattr__(self, "packet_hash", packet_hash)
        if self.run_id is not None:
            object.__setattr__(
                self, "run_id", _validate_identifier(self.run_id, "run_id")
            )
        object.__setattr__(
            self,
            "observations",
            tuple(
                sorted(
                    (coerce_observation(item) for item in self.observations),
                    key=lambda item: item.objective_id,
                )
            ),
        )
        attempts = tuple(
            sorted(
                (_deep_freeze_json(item) for item in self.module_attempts),
                key=lambda item: (
                    str(item.get("moduleId", "")),
                    str(item.get("attemptId", "")),
                ),
            )
        )
        object.__setattr__(self, "module_attempts", attempts)
        object.__setattr__(self, "qualifiers", _strings(self.qualifiers, "qualifiers"))
        object.__setattr__(
            self,
            "exclusion_reasons",
            _strings(self.exclusion_reasons, "exclusion_reasons"),
        )

    def to_input_dict(self) -> dict[str, Any]:
        result = {
            "candidateId": self.candidate_id,
            "packetRevisionId": self.packet_revision_id,
            "packetHash": self.packet_hash,
            "observations": [item.to_dict() for item in self.observations],
            "moduleAttempts": [_json_copy(item) for item in self.module_attempts],
            "qualifiers": list(self.qualifiers),
            "exclusionReasons": list(self.exclusion_reasons),
        }
        if self.run_id is not None:
            result["runId"] = self.run_id
        return result


@dataclass(frozen=True)
class PortfolioResult:
    """Serializable comparison snapshot.  Intentionally has no winner/top field."""

    schema_version: str
    objective_policy_id: str
    snapshot_id: str
    created_at: str
    run_id: str | None
    input_packets: tuple[dict[str, Any], ...]
    objective_policy: dict[str, Any]
    candidates: tuple[dict[str, Any], ...]
    comparison_groups: tuple[dict[str, Any], ...]
    frontier: tuple[str, ...]
    dominated: tuple[str, ...]
    incomparable: tuple[dict[str, Any], ...]
    dominance_relationships: tuple[dict[str, Any], ...]
    equivalence_groups: tuple[dict[str, Any], ...]
    qualifiers: tuple[str, ...]
    next_evidence_action: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in (
            "input_packets",
            "candidates",
            "comparison_groups",
            "incomparable",
            "dominance_relationships",
            "equivalence_groups",
        ):
            object.__setattr__(
                self,
                name,
                tuple(_deep_freeze_json(item) for item in getattr(self, name)),
            )
        object.__setattr__(
            self, "objective_policy", _deep_freeze_json(self.objective_policy)
        )
        for name in ("frontier", "dominated", "qualifiers"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.next_evidence_action is not None:
            object.__setattr__(
                self,
                "next_evidence_action",
                _deep_freeze_json(self.next_evidence_action),
            )

    def to_dict(self) -> dict[str, Any]:
        return _json_copy(
            {
                "schemaVersion": self.schema_version,
                "objectivePolicyId": self.objective_policy_id,
                "snapshotId": self.snapshot_id,
                "createdAt": self.created_at,
                "runId": self.run_id,
                "inputPackets": list(self.input_packets),
                "objectivePolicy": self.objective_policy,
                "candidates": list(self.candidates),
                "comparisonGroups": list(self.comparison_groups),
                "frontier": list(self.frontier),
                "dominated": list(self.dominated),
                "incomparable": list(self.incomparable),
                "dominanceRelationships": list(self.dominance_relationships),
                "equivalenceGroups": list(self.equivalence_groups),
                "qualifiers": list(self.qualifiers),
                "nextEvidenceAction": self.next_evidence_action,
            }
        )


def coerce_rule(value: Any) -> ObjectiveRule:
    if isinstance(value, ObjectiveRule):
        return value
    return ObjectiveRule(
        objective_id=_read(value, "objective_id", "objectiveId", "id"),
        direction=_read(value, "direction"),
    )


def coerce_policy(value: Any) -> ComparisonPolicy:
    if isinstance(value, ComparisonPolicy):
        return value
    return ComparisonPolicy(
        policy_id=_read(
            value, "policy_id", "policyId", "objective_policy_id", "objectivePolicyId"
        ),
        objectives=tuple(_read(value, "objectives", "rules")),
    )


def coerce_observation(value: Any) -> PortfolioObservation:
    """Adapt packet-contract observations through a mapping/attribute protocol.

    Required names are ``objective_id``, ``value``/``raw_value``, ``direction``,
    ``unit``, ``basis``/``comparison_basis``, and ``evidence_basis``.  camelCase
    equivalents are accepted.  Remaining fields are optional and preserved.
    """
    if isinstance(value, PortfolioObservation):
        return value
    qualifiers = tuple(_read(value, "qualifiers", default=()))
    evidence_basis = _read(value, "evidence_basis", "evidenceBasis", default=None)
    inferred_evidence_bases: tuple[str, ...] = ()
    if evidence_basis is None:
        matched_bases = sorted(
            {
                str(qualifier).upper()
                for qualifier in qualifiers
                if str(qualifier).upper() in _EVIDENCE_BASES
            }
        )
        evidence_basis = matched_bases[0] if matched_bases else "UNSPECIFIED"
        inferred_evidence_bases = tuple(matched_bases)
    return PortfolioObservation(
        objective_id=_read(value, "objective_id", "objectiveId", "id"),
        value=_read(value, "raw_value", "rawValue", "value"),
        direction=_read(value, "direction"),
        unit=_read(value, "unit", default=None),
        basis=_read(value, "basis", "comparison_basis", "comparisonBasis"),
        evidence_basis=evidence_basis,
        kind=_read(value, "kind", default="NUMERIC"),
        evidence_bases=tuple(
            _read(
                value,
                "evidence_bases",
                "evidenceBases",
                default=inferred_evidence_bases,
            )
        ),
        uncertainty=_read(value, "uncertainty", default=None),
        source_path=_read(value, "source_path", "sourcePath", default=None),
        source_schema_id=_read(
            value,
            "source_schema_id",
            "sourceSchemaId",
            "native_schema_id",
            "nativeSchemaId",
            default=None,
        ),
        source_schema_version=_read(
            value,
            "source_schema_version",
            "sourceSchemaVersion",
            "native_schema_version",
            "nativeSchemaVersion",
            default=None,
        ),
        source_hash=_read(
            value,
            "source_hash",
            "sourceHash",
            "output_canonical_sha256",
            "outputCanonicalSha256",
            "packet_sha256",
            "packetSha256",
            default=None,
        ),
        derived_policy_id=_read(
            value, "derived_policy_id", "derivedPolicyId", default=None
        ),
        qualifiers=qualifiers,
        missing_reason=_read(value, "missing_reason", "missingReason", default=None),
    )


def coerce_candidate(value: Any) -> PortfolioCandidate:
    """Adapt an orchestrator packet aggregate through mappings or attributes.

    Required protocol:

    ``candidate_id`` (or ``hypothesis_id``), ``packet_revision_id``,
    ``packet_hash`` (or ``packet_sha256``), and ``observations``.  Qualifiers and
    exclusion reasons are optional.  A packet adapter may therefore expose the
    protocol directly without importing any Highlander type.
    """
    if isinstance(value, PortfolioCandidate):
        return value
    return PortfolioCandidate(
        candidate_id=_read(
            value,
            "candidate_id",
            "candidateId",
            "hypothesis_id",
            "hypothesisId",
        ),
        packet_revision_id=_read(
            value, "packet_revision_id", "packetRevisionId", "revision_id", "revisionId"
        ),
        packet_hash=_read(
            value,
            "packet_hash",
            "packetHash",
            "packet_sha256",
            "packetSha256",
            "output_canonical_sha256",
            "outputCanonicalSha256",
        ),
        run_id=_read(value, "run_id", "runId", default=None),
        observations=tuple(_read(value, "observations", "objectives")),
        module_attempts=tuple(
            _read(value, "module_attempts", "moduleAttempts", default=())
        ),
        qualifiers=tuple(_read(value, "qualifiers", default=())),
        exclusion_reasons=tuple(
            _read(value, "exclusion_reasons", "exclusionReasons", default=())
        ),
    )


def candidate_from_adapted_results(
    results: Iterable[Any],
    *,
    packet_revision_id: str,
    packet_hash: str,
    candidate_id: str | None = None,
    exclusion_reasons: Sequence[str] = (),
) -> PortfolioCandidate:
    """Aggregate packet-contract ``AdaptedModuleResult`` objects for comparison.

    The output orchestrator remains responsible for creating and hashing the terminal
    packet revision, so both identifiers are required.  All adapted results must bind
    to one hypothesis.  Quarantined results exclude that candidate; ordinary missing
    module results are retained as structured qualifiers and the required-objective
    policy decides whether the resulting vector is incomplete.
    """
    supplied = tuple(results)
    if not supplied:
        raise ValueError("at least one adapted module result is required")

    selected_by_module: dict[str, Any] = {}
    fingerprints_by_module: dict[str, str] = {}
    for item in sorted(
        supplied,
        key=lambda value: (
            str(_read(value, "module_id", "moduleId")),
            str(_read(value, "attempt_id", "attemptId")),
        ),
    ):
        module_id = _validate_identifier(
            _read(item, "module_id", "moduleId"), "result.module_id"
        )
        fingerprint = _canonical_json(
            {
                "runId": _read(item, "run_id", "runId"),
                "hypothesisId": _read(item, "hypothesis_id", "hypothesisId"),
                "moduleId": module_id,
                "attemptId": _read(item, "attempt_id", "attemptId"),
                "packetSha256": _read(item, "packet_sha256", "packetSha256"),
                "attemptRecord": _read(
                    item, "attempt_record", "attemptRecord", default={}
                ),
                "objectives": [
                    coerce_observation(observation).to_dict()
                    for observation in _read(
                        item, "objectives", "observations", default=()
                    )
                ],
                "qualifiers": sorted(_read(item, "qualifiers", default=())),
                "missingReasons": sorted(
                    _read(item, "missing_reasons", "missingReasons", default=())
                ),
                "quarantined": bool(_read(item, "quarantined", default=False)),
                "quarantineReasons": sorted(
                    _read(
                        item,
                        "quarantine_reasons",
                        "quarantineReasons",
                        default=(),
                    )
                ),
            }
        )
        previous = fingerprints_by_module.get(module_id)
        if previous is not None:
            if previous == fingerprint:
                continue
            raise ValueError(
                f"conflicting selected adapted results for module {module_id!r}"
            )
        fingerprints_by_module[module_id] = fingerprint
        selected_by_module[module_id] = item

    materialized = tuple(selected_by_module[module_id] for module_id in sorted(selected_by_module))
    hypothesis_ids = {
        _validate_identifier(
            _read(item, "hypothesis_id", "hypothesisId"), "result.hypothesis_id"
        )
        for item in materialized
    }
    if len(hypothesis_ids) != 1:
        raise ValueError("adapted module results must bind to exactly one hypothesis")
    run_ids = {
        _validate_identifier(_read(item, "run_id", "runId"), "result.run_id")
        for item in materialized
    }
    if len(run_ids) != 1:
        raise ValueError("adapted module results must bind to exactly one run")
    bound_id = next(iter(hypothesis_ids))
    if candidate_id is not None and candidate_id != bound_id:
        raise ValueError("candidate_id does not match adapted result hypothesis_id")

    observations: list[Any] = []
    module_attempts: list[Mapping[str, Any]] = []
    qualifiers: set[str] = set()
    exclusions = set(exclusion_reasons)
    ordered_results = sorted(
        materialized,
        key=lambda item: (
            str(_read(item, "module_id", "moduleId")),
            str(_read(item, "attempt_id", "attemptId")),
        ),
    )
    for item in ordered_results:
        module_id = _validate_identifier(
            _read(item, "module_id", "moduleId"), "result.module_id"
        )
        observations.extend(_read(item, "objectives", "observations", default=()))
        attempt_record = _read(
            item, "attempt_record", "attemptRecord", default={}
        )
        if attempt_record:
            module_attempts.append(attempt_record)
        result_qualifiers = {
            str(qualifier).upper()
            for qualifier in _read(item, "qualifiers", default=())
        }
        qualifiers.update(result_qualifiers)
        exclusions.update(
            f"QUALIFIER:{qualifier}"
            for qualifier in sorted(
                result_qualifiers & _DEFAULT_CANDIDATE_EXCLUSION_QUALIFIERS
            )
        )
        for reason in _read(item, "missing_reasons", "missingReasons", default=()):
            missing_qualifier = f"MISSING_MODULE_RESULT:{module_id}:{reason}"
            qualifiers.add(missing_qualifier)
        if bool(_read(item, "quarantined", default=False)):
            quarantine_reasons = tuple(
                _read(item, "quarantine_reasons", "quarantineReasons", default=())
            )
            if not quarantine_reasons:
                quarantine_reasons = ("reason_not_supplied",)
            exclusions.update(
                f"QUARANTINED:{module_id}:{reason}" for reason in quarantine_reasons
            )

    return PortfolioCandidate(
        candidate_id=bound_id,
        packet_revision_id=packet_revision_id,
        packet_hash=packet_hash,
        run_id=next(iter(run_ids)),
        observations=tuple(observations),
        module_attempts=tuple(module_attempts),
        qualifiers=tuple(qualifiers),
        exclusion_reasons=tuple(exclusions),
    )


def _is_numeric(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _is_finite_numeric(value: Any) -> bool:
    if not _is_numeric(value):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _comparison_signature(
    observations: Mapping[str, PortfolioObservation], rules: Sequence[ObjectiveRule]
) -> list[dict[str, Any]]:
    return [
        {
            "objectiveId": rule.objective_id,
            "direction": rule.direction,
            "unit": observations[rule.objective_id].unit,
            "basis": _json_copy(observations[rule.objective_id].basis),
            "evidenceBases": list(observations[rule.objective_id].evidence_bases),
            "sourceSchemaId": observations[rule.objective_id].source_schema_id,
            "sourceSchemaVersion": observations[
                rule.objective_id
            ].source_schema_version,
            "derivedPolicyId": observations[rule.objective_id].derived_policy_id,
        }
        for rule in rules
    ]


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _dominates(
    left: Mapping[str, PortfolioObservation],
    right: Mapping[str, PortfolioObservation],
    rules: Sequence[ObjectiveRule],
) -> bool:
    no_worse = True
    strictly_better = False
    for rule in rules:
        left_value = left[rule.objective_id].value
        right_value = right[rule.objective_id].value
        if rule.direction == "MAX":
            no_worse = no_worse and left_value >= right_value
            strictly_better = strictly_better or left_value > right_value
        else:
            no_worse = no_worse and left_value <= right_value
            strictly_better = strictly_better or left_value < right_value
    return no_worse and strictly_better


def compare_packets(
    packets: Iterable[Any],
    policy: ComparisonPolicy | Mapping[str, Any] | Any,
    *,
    snapshot_id: str,
    created_at: str,
) -> PortfolioResult:
    """Compare immutable candidate packets under one explicit objective policy.

    The function is pure for a fixed input: it reads no clock, files, network, or
    environment state.  Callers must provide the orchestrator's snapshot identity and
    timestamp.  Exact idempotent duplicate candidates are collapsed; a duplicate ID
    with conflicting content fails closed.
    """
    selected_policy = coerce_policy(policy)
    snapshot_id = _validate_identifier(snapshot_id, "snapshot_id")
    created_at = _validate_timestamp(created_at)

    by_id: dict[str, PortfolioCandidate] = {}
    fingerprints: dict[str, str] = {}
    for raw_packet in packets:
        candidate = coerce_candidate(raw_packet)
        fingerprint = _canonical_json(candidate.to_input_dict())
        previous = fingerprints.get(candidate.candidate_id)
        if previous is not None:
            if previous != fingerprint:
                raise ValueError(
                    f"conflicting packet snapshots for candidate {candidate.candidate_id!r}"
                )
            continue
        by_id[candidate.candidate_id] = candidate
        fingerprints[candidate.candidate_id] = fingerprint

    candidates = [by_id[candidate_id] for candidate_id in sorted(by_id)]
    explicit_run_ids = {
        candidate.run_id for candidate in candidates if candidate.run_id is not None
    }
    if len(explicit_run_ids) > 1:
        raise ValueError("all candidates in one comparison snapshot must share one run_id")
    rules = selected_policy.objectives
    prepared: dict[str, dict[str, PortfolioObservation]] = {}
    reasons: dict[str, list[str]] = {candidate.candidate_id: [] for candidate in candidates}
    signatures: dict[str, list[dict[str, Any]]] = {}

    for candidate in candidates:
        candidate_reasons = reasons[candidate.candidate_id]
        candidate_reasons.extend(
            f"EXCLUDED:{reason}" for reason in candidate.exclusion_reasons
        )
        observations: dict[str, PortfolioObservation] = {}
        duplicate_ids: set[str] = set()
        for observation in candidate.observations:
            if observation.objective_id in observations:
                duplicate_ids.add(observation.objective_id)
            observations[observation.objective_id] = observation
        candidate_reasons.extend(
            f"DUPLICATE_OBJECTIVE:{objective_id}" for objective_id in sorted(duplicate_ids)
        )

        for rule in rules:
            observation = observations.get(rule.objective_id)
            if observation is None:
                candidate_reasons.append(f"MISSING_REQUIRED_OBJECTIVE:{rule.objective_id}")
                continue
            if observation.missing_reason:
                candidate_reasons.append(
                    f"MISSING_VALUE:{rule.objective_id}:{observation.missing_reason}"
                )
            if observation.kind != "NUMERIC":
                candidate_reasons.append(f"NON_NUMERIC_OBJECTIVE_KIND:{rule.objective_id}")
            if observation.direction != rule.direction:
                candidate_reasons.append(
                    f"DIRECTION_MISMATCH:{rule.objective_id}:"
                    f"expected={rule.direction}:observed={observation.direction}"
                )
            if not _is_numeric(observation.value):
                candidate_reasons.append(f"NON_NUMERIC_VALUE:{rule.objective_id}")
            elif not _is_finite_numeric(observation.value):
                candidate_reasons.append(f"NON_FINITE_VALUE:{rule.objective_id}")
            if not observation.unit:
                candidate_reasons.append(f"MISSING_UNIT:{rule.objective_id}")
            if not observation.basis:
                candidate_reasons.append(f"MISSING_BASIS:{rule.objective_id}")
            if observation.evidence_basis == "UNSPECIFIED":
                candidate_reasons.append(f"MISSING_EVIDENCE_BASIS:{rule.objective_id}")
            for qualifier in sorted(
                set(observation.qualifiers)
                & _DEFAULT_OBJECTIVE_EXCLUSION_QUALIFIERS
            ):
                candidate_reasons.append(
                    f"OBJECTIVE_QUALIFIER:{rule.objective_id}:{qualifier}"
                )

        if not candidate_reasons:
            prepared[candidate.candidate_id] = observations
            signatures[candidate.candidate_id] = _comparison_signature(observations, rules)

    grouped: dict[str, dict[str, Any]] = {}
    for candidate_id, signature in signatures.items():
        group_id = _stable_id("comparison", signature)
        grouped.setdefault(group_id, {"signature": signature, "candidate_ids": []})[
            "candidate_ids"
        ].append(candidate_id)

    relations: list[dict[str, Any]] = []
    equivalence_groups: list[dict[str, Any]] = []
    comparison_groups: list[dict[str, Any]] = []
    frontier: set[str] = set()
    dominated: set[str] = set()
    group_frontiers: dict[str, list[str]] = {}

    for group_id in sorted(grouped):
        group = grouped[group_id]
        member_ids = sorted(group["candidate_ids"])
        incoming: dict[str, set[str]] = {candidate_id: set() for candidate_id in member_ids}
        for left_id in member_ids:
            for right_id in member_ids:
                if left_id == right_id:
                    continue
                if _dominates(prepared[left_id], prepared[right_id], rules):
                    relation_qualifiers: list[str] = []
                    if any(
                        prepared[candidate_id][rule.objective_id].uncertainty is not None
                        for candidate_id in (left_id, right_id)
                        for rule in rules
                    ):
                        relation_qualifiers.append("UNCERTAINTY_NOT_USED_IN_NOMINAL_DOMINANCE")
                    relations.append(
                        {
                            "dominatesCandidateId": left_id,
                            "dominatedCandidateId": right_id,
                            "comparisonGroupId": group_id,
                            "kind": "NOMINAL",
                            "objectiveIds": [rule.objective_id for rule in rules],
                            "qualifiers": relation_qualifiers,
                        }
                    )
                    incoming[right_id].add(left_id)

        group_frontier = sorted(
            candidate_id for candidate_id in member_ids if not incoming[candidate_id]
        )
        group_dominated = sorted(
            candidate_id for candidate_id in member_ids if incoming[candidate_id]
        )
        group_frontiers[group_id] = group_frontier
        dominated.update(group_dominated)

        tied_vectors: dict[tuple[Any, ...], list[str]] = {}
        for candidate_id in member_ids:
            vector = tuple(prepared[candidate_id][rule.objective_id].value for rule in rules)
            tied_vectors.setdefault(vector, []).append(candidate_id)
        for vector, tied_ids in sorted(tied_vectors.items(), key=lambda item: sorted(item[1])):
            if len(tied_ids) < 2:
                continue
            tied_ids = sorted(tied_ids)
            group_payload = {
                "comparisonGroupId": group_id,
                "candidateIds": tied_ids,
                "objectiveVector": [
                    {"objectiveId": rule.objective_id, "rawValue": vector[index]}
                    for index, rule in enumerate(rules)
                ],
            }
            equivalence_groups.append(
                {
                    "equivalenceGroupId": _stable_id("equivalence", group_payload),
                    **group_payload,
                }
            )

        comparison_groups.append(
            {
                "comparisonGroupId": group_id,
                "candidateIds": member_ids,
                "objectiveSemantics": group["signature"],
                "frontier": group_frontier,
                "dominated": group_dominated,
            }
        )

    if len(grouped) == 1:
        frontier.update(next(iter(group_frontiers.values())))
    elif len(grouped) > 1:
        for candidate_ids in group_frontiers.values():
            for candidate_id in candidate_ids:
                reasons[candidate_id].append(
                    "MIXED_COMPARISON_BASES_NO_GLOBAL_FRONTIER"
                )

    status_by_id: dict[str, str] = {}
    for candidate in candidates:
        candidate_id = candidate.candidate_id
        if reasons[candidate_id]:
            status_by_id[candidate_id] = "INCOMPARABLE"
        elif candidate_id in dominated:
            status_by_id[candidate_id] = "DOMINATED"
        else:
            status_by_id[candidate_id] = "FRONTIER"

    candidate_records: list[dict[str, Any]] = []
    all_qualifiers: set[str] = {
        "NO_GLOBAL_WINNER",
        "NOMINAL_PARETO",
        "RAW_OBJECTIVES_PRESERVED",
    }
    candidate_lookup = {candidate.candidate_id: candidate for candidate in candidates}
    for candidate_id in sorted(candidate_lookup):
        candidate = candidate_lookup[candidate_id]
        observation_qualifiers = {
            qualifier
            for observation in candidate.observations
            for qualifier in observation.qualifiers
        }
        candidate_qualifiers = sorted(set(candidate.qualifiers) | observation_qualifiers)
        all_qualifiers.update(candidate_qualifiers)
        record = candidate.to_input_dict()
        record["comparisonStatus"] = status_by_id[candidate_id]
        record["qualifiers"] = candidate_qualifiers
        if candidate_id in signatures:
            record["comparisonGroupId"] = _stable_id(
                "comparison", signatures[candidate_id]
            )
        if reasons[candidate_id]:
            record["incomparableReasons"] = sorted(set(reasons[candidate_id]))
        candidate_records.append(record)

    incomparable = tuple(
        {
            "candidateId": candidate_id,
            "reasons": sorted(set(reasons[candidate_id])),
        }
        for candidate_id in sorted(reasons)
        if reasons[candidate_id]
    )
    if incomparable:
        all_qualifiers.add("INCOMPARABLE_CANDIDATES_PRESENT")
    if len(grouped) > 1:
        all_qualifiers.add("MULTIPLE_COMPARISON_BASES")
    if any(
        observation.uncertainty is not None
        for candidate in candidates
        for observation in candidate.observations
        if observation.objective_id in {rule.objective_id for rule in rules}
    ):
        all_qualifiers.add("UNCERTAINTY_NOT_USED_IN_NOMINAL_DOMINANCE")

    relations.sort(
        key=lambda item: (
            item["comparisonGroupId"],
            item["dominatesCandidateId"],
            item["dominatedCandidateId"],
        )
    )
    equivalence_groups.sort(key=lambda item: item["equivalenceGroupId"])

    return PortfolioResult(
        schema_version=RESULT_SCHEMA_VERSION,
        objective_policy_id=selected_policy.policy_id,
        snapshot_id=snapshot_id,
        created_at=created_at,
        run_id=next(iter(explicit_run_ids), None),
        input_packets=tuple(
            {
                "candidateId": candidate.candidate_id,
                **({"runId": candidate.run_id} if candidate.run_id is not None else {}),
                "packetRevisionId": candidate.packet_revision_id,
                "packetHash": candidate.packet_hash,
            }
            for candidate in candidates
        ),
        objective_policy=selected_policy.to_dict(),
        candidates=tuple(candidate_records),
        comparison_groups=tuple(comparison_groups),
        frontier=tuple(sorted(frontier)),
        dominated=tuple(sorted(dominated)),
        incomparable=incomparable,
        dominance_relationships=tuple(relations),
        equivalence_groups=tuple(equivalence_groups),
        qualifiers=tuple(sorted(all_qualifiers)),
    )
