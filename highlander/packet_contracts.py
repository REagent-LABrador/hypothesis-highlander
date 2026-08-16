"""Immutable contracts for consuming orchestrator-owned module packets.

The scientific modules keep their native schemas.  This module defines only the
outer envelope and the lossless observations Highlander is allowed to derive
from it.  Packet hashing uses RFC 8785 JCS so Bun/TypeScript and Python produce
the same digest for the same I-JSON value.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import rfc8785


class ContractError(ValueError):
    """Raised when an orchestrator envelope is invalid or has been tampered with."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "COMPLETED", "COMPLETE"})
_KNOWN_STATUSES = frozenset(
    {
        *_SUCCESS_STATUSES,
        "QUEUED",
        "RUNNING",
        "FAILED",
        "CANCELLED",
        "SKIPPED",
        "PARTIAL",
        "NOT_AMENABLE",
        "NEEDS_INPUT",
        "BLOCKED",
        "UNKNOWN_OUTCOME",
    }
)
_OUTPUT_OPTIONAL_STATUSES = frozenset(
    {
        "FAILED",
        "CANCELLED",
        "SKIPPED",
        "NOT_AMENABLE",
        "NEEDS_INPUT",
        "BLOCKED",
        "UNKNOWN_OUTCOME",
    }
)
_ARTIFACT_REF_RE = re.compile(
    r"^(?:artifact|cas)://[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$"
)
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


def _jsonable(value: Any, path: str = "$") -> Any:
    """Return a JSON-native copy, rejecting ambiguous or non-finite values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{path} contains a non-string object key")
            out[key] = _jsonable(item, f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise ContractError(f"{path} contains unsupported JSON value {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode RFC 8785 JCS bytes for cross-language hashing.

    JCS constrains numbers to the I-JSON/IEEE-754 domain and uses ECMAScript
    number formatting and UTF-16 property ordering.  Invalid input fails closed.
    """
    try:
        return rfc8785.dumps(_jsonable(value))
    except (rfc8785.CanonicalizationError, OverflowError, ValueError) as error:
        raise ContractError(f"value is not valid RFC 8785 JCS input: {error}") from error


def canonical_json_sha256(value: Any) -> str:
    """Return the lowercase hexadecimal SHA-256 of canonical JSON."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def raw_sha256(raw: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 of exact artifact bytes."""

    if not isinstance(raw, bytes):
        raise ContractError("raw artifact must be bytes")
    return hashlib.sha256(raw).hexdigest()


def _normalise_sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field_name} must be a SHA-256 string")
    digest = value.lower()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if not _SHA256_RE.fullmatch(digest):
        raise ContractError(f"{field_name} must contain exactly one SHA-256 digest")
    return digest


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field_name} must be a non-empty string")
    return value.strip()


def _artifact_ref(value: Any, field_name: str) -> str:
    ref = _required_text(value, field_name)
    if not _ARTIFACT_REF_RE.fullmatch(ref):
        raise ContractError(
            f"{field_name} must be an opaque artifact:// or cas:// reference "
            "without credentials, query, or fragment"
        )
    segments = ref.split("://", 1)[1].split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise ContractError(
            f"{field_name} must not contain current-directory or parent-directory segments"
        )
    return ref


def _deep_freeze(value: Any) -> Any:
    """Recursively freeze JSON data while retaining normal mapping access."""

    native = _jsonable(value)
    if isinstance(native, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in native.items()})
    if isinstance(native, list):
        return tuple(_deep_freeze(item) for item in native)
    return native


@dataclass(frozen=True, slots=True)
class Subject:
    """The exact scientific subject to which a native result is bound."""

    graph_id: str | None = None
    graph_round: int | None = None
    thing_id: str | None = None
    target_symbol: str | None = None
    uniprot_accession: str | None = None
    mechanism_hypothesis: str | None = None
    as_of: str | None = None
    modality: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "graph_id",
            "thing_id",
            "target_symbol",
            "uniprot_accession",
            "mechanism_hypothesis",
            "as_of",
            "modality",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, f"subject.{name}"))
        if self.graph_round is not None and (
            isinstance(self.graph_round, bool)
            or not isinstance(self.graph_round, int)
            or self.graph_round < 0
        ):
            raise ContractError("subject.graph_round must be a non-negative integer")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Subject":
        if not isinstance(value, Mapping):
            raise ContractError("subject must be an object")
        return cls(
            graph_id=value.get("graphId"),
            graph_round=value.get("graphRound"),
            thing_id=value.get("thingId"),
            target_symbol=value.get("targetSymbol"),
            uniprot_accession=value.get("uniprotAccession"),
            mechanism_hypothesis=value.get("mechanismHypothesis"),
            as_of=value.get("asOf"),
            modality=value.get("modality"),
        )

    def to_dict(self) -> dict[str, Any]:
        values = {
            "graphId": self.graph_id,
            "graphRound": self.graph_round,
            "thingId": self.thing_id,
            "targetSymbol": self.target_symbol,
            "uniprotAccession": self.uniprot_accession,
            "mechanismHypothesis": self.mechanism_hypothesis,
            "asOf": self.as_of,
            "modality": self.modality,
        }
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class ModulePacket:
    """Hash-checked, immutable wrapper around one locked native module output."""

    run_id: str
    hypothesis_id: str
    module_id: str
    attempt_id: str
    native_schema_id: str
    native_schema_version: str
    producer_code_version: str
    adapter_version: str
    execution_status: str
    execution_reason: str | None
    evidence_basis: str
    input_raw_sha256: str
    output_raw_sha256: str | None
    output_canonical_sha256: str | None
    envelope_canonical_sha256: str | None
    input_artifact_ref: str
    output_artifact_ref: str | None
    execution_artifact_ref: str | None
    execution_artifact_raw_sha256: str | None
    dependencies: tuple[Mapping[str, str | None], ...]
    subject: Subject
    qualifiers: tuple[str, ...]
    payload: Mapping[str, Any] | None

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "hypothesis_id",
            "module_id",
            "attempt_id",
            "native_schema_id",
            "native_schema_version",
            "producer_code_version",
            "adapter_version",
            "evidence_basis",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))

        status = _required_text(self.execution_status, "execution_status").upper()
        if status not in _KNOWN_STATUSES:
            raise ContractError(f"unknown execution_status {status!r}")
        object.__setattr__(self, "execution_status", status)
        if self.execution_reason is not None:
            object.__setattr__(
                self,
                "execution_reason",
                _required_text(self.execution_reason, "execution_reason"),
            )
        elif status in {
            "FAILED",
            "CANCELLED",
            "SKIPPED",
            "PARTIAL",
            "NOT_AMENABLE",
            "NEEDS_INPUT",
            "BLOCKED",
            "UNKNOWN_OUTCOME",
        }:
            raise ContractError(
                f"execution_reason is required when execution_status is {status}"
            )

        evidence_basis = _required_text(self.evidence_basis, "evidence_basis").upper()
        if evidence_basis not in _EVIDENCE_BASES:
            raise ContractError(f"unknown evidence_basis {evidence_basis!r}")
        object.__setattr__(self, "evidence_basis", evidence_basis)

        object.__setattr__(
            self,
            "input_raw_sha256",
            _normalise_sha256(self.input_raw_sha256, "input_raw_sha256"),
        )
        object.__setattr__(
            self,
            "input_artifact_ref",
            _artifact_ref(self.input_artifact_ref, "input_artifact_ref"),
        )

        output_fields = (
            self.output_raw_sha256,
            self.output_canonical_sha256,
            self.output_artifact_ref,
            self.payload,
        )
        output_present = all(value is not None for value in output_fields)
        if any(value is not None for value in output_fields) and not output_present:
            raise ContractError(
                "output hashes, artifact reference, and payload must be all present or all null"
            )
        if not output_present and status not in _OUTPUT_OPTIONAL_STATUSES:
            raise ContractError(f"execution_status {status} requires a native output")
        if output_present:
            object.__setattr__(
                self,
                "output_raw_sha256",
                _normalise_sha256(self.output_raw_sha256, "output_raw_sha256"),
            )
            object.__setattr__(
                self,
                "output_canonical_sha256",
                _normalise_sha256(
                    self.output_canonical_sha256, "output_canonical_sha256"
                ),
            )
            object.__setattr__(
                self,
                "output_artifact_ref",
                _artifact_ref(self.output_artifact_ref, "output_artifact_ref"),
            )

        execution_artifact_present = (
            self.execution_artifact_ref is not None
            and self.execution_artifact_raw_sha256 is not None
        )
        if (self.execution_artifact_ref is None) != (
            self.execution_artifact_raw_sha256 is None
        ):
            raise ContractError(
                "execution artifact reference and hash must be both present or both null"
            )
        if not output_present and not execution_artifact_present:
            raise ContractError(
                "an output-less terminal attempt requires an execution artifact"
            )
        if execution_artifact_present:
            object.__setattr__(
                self,
                "execution_artifact_ref",
                _artifact_ref(self.execution_artifact_ref, "execution_artifact_ref"),
            )
            object.__setattr__(
                self,
                "execution_artifact_raw_sha256",
                _normalise_sha256(
                    self.execution_artifact_raw_sha256,
                    "execution_artifact_raw_sha256",
                ),
            )
        if not isinstance(self.dependencies, Sequence) or isinstance(
            self.dependencies, (str, bytes)
        ):
            raise ContractError("dependencies must be a sequence")
        dependencies: list[Mapping[str, str | None]] = []
        seen_dependency_modules: set[str] = set()
        for index, dependency in enumerate(self.dependencies):
            if not isinstance(dependency, Mapping):
                raise ContractError(f"dependencies[{index}] must be an object")
            module_id = _required_text(
                dependency.get("moduleId"), f"dependencies[{index}].moduleId"
            )
            if module_id in seen_dependency_modules:
                raise ContractError(f"dependencies repeats module {module_id!r}")
            seen_dependency_modules.add(module_id)
            dependencies.append(
                MappingProxyType(
                    {
                        "moduleId": module_id,
                        "outputCanonicalSha256": (
                            _normalise_sha256(
                                dependency.get("outputCanonicalSha256"),
                                f"dependencies[{index}].outputCanonicalSha256",
                            )
                            if dependency.get("outputCanonicalSha256") is not None
                            else None
                        ),
                        "envelopeCanonicalSha256": _normalise_sha256(
                            dependency.get("envelopeCanonicalSha256"),
                            f"dependencies[{index}].envelopeCanonicalSha256",
                        ),
                    }
                )
            )
        object.__setattr__(self, "dependencies", tuple(dependencies))

        if not isinstance(self.subject, Subject):
            raise ContractError("subject must be a Subject")
        qualifiers = tuple(
            dict.fromkeys(_required_text(value, "qualifier").upper() for value in self.qualifiers)
        )
        object.__setattr__(self, "qualifiers", qualifiers)
        if output_present:
            frozen_payload = _deep_freeze(self.payload)
            if not isinstance(frozen_payload, Mapping):
                raise ContractError("payload must be a JSON object")
            object.__setattr__(self, "payload", frozen_payload)
            actual = canonical_json_sha256(frozen_payload)
            if actual != self.output_canonical_sha256:
                raise ContractError(
                    "outputCanonicalSha256 does not match the canonical native payload"
                )

        actual_envelope = canonical_json_sha256(self.envelope_body())
        if self.envelope_canonical_sha256 is None:
            object.__setattr__(self, "envelope_canonical_sha256", actual_envelope)
        else:
            supplied_envelope = _normalise_sha256(
                self.envelope_canonical_sha256, "envelope_canonical_sha256"
            )
            if supplied_envelope != actual_envelope:
                raise ContractError(
                    "envelopeCanonicalSha256 does not match the canonical envelope body"
                )
            object.__setattr__(
                self, "envelope_canonical_sha256", supplied_envelope
            )

    def envelope_body(self) -> dict[str, Any]:
        """Return the identity-bearing envelope body, excluding its own digest."""

        return {
            "runId": self.run_id,
            "hypothesisId": self.hypothesis_id,
            "moduleId": self.module_id,
            "attemptId": self.attempt_id,
            "nativeSchemaId": self.native_schema_id,
            "nativeSchemaVersion": self.native_schema_version,
            "producerCodeVersion": self.producer_code_version,
            "adapterVersion": self.adapter_version,
            "executionStatus": self.execution_status,
            "executionReason": self.execution_reason,
            "evidenceBasis": self.evidence_basis,
            "inputRawSha256": self.input_raw_sha256,
            "outputRawSha256": self.output_raw_sha256,
            "outputCanonicalSha256": self.output_canonical_sha256,
            "inputArtifactRef": self.input_artifact_ref,
            "outputArtifactRef": self.output_artifact_ref,
            "executionArtifactRef": self.execution_artifact_ref,
            "executionArtifactRawSha256": self.execution_artifact_raw_sha256,
            "dependsOn": [dict(dependency) for dependency in self.dependencies],
            "subject": self.subject.to_dict(),
            "qualifiers": list(self.qualifiers),
            "payload": self.payload,
        }

    def to_envelope_dict(self) -> dict[str, Any]:
        return {
            **self.envelope_body(),
            "envelopeCanonicalSha256": self.envelope_canonical_sha256,
        }

    @property
    def succeeded(self) -> bool:
        return self.execution_status in _SUCCESS_STATUSES

    @property
    def result_sha256(self) -> str:
        """Stable attempt result identity for success or output-less failure."""

        if self.output_canonical_sha256 is not None:
            return self.output_canonical_sha256
        assert self.execution_artifact_raw_sha256 is not None
        return self.execution_artifact_raw_sha256

    def verify_raw_artifacts(
        self,
        *,
        input_raw: bytes | None = None,
        output_raw: bytes | None = None,
        execution_raw: bytes | None = None,
    ) -> None:
        """Verify exact artifact bytes when the caller has resolved their references."""

        if input_raw is not None and raw_sha256(input_raw) != self.input_raw_sha256:
            raise ContractError("inputRawSha256 does not match the resolved input artifact")
        if output_raw is not None and raw_sha256(output_raw) != self.output_raw_sha256:
            raise ContractError("outputRawSha256 does not match the resolved output artifact")
        if (
            execution_raw is not None
            and raw_sha256(execution_raw) != self.execution_artifact_raw_sha256
        ):
            raise ContractError(
                "executionArtifactRawSha256 does not match the resolved execution artifact"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModulePacket":
        if not isinstance(value, Mapping):
            raise ContractError("packet must be an object")
        required = (
            "runId",
            "hypothesisId",
            "moduleId",
            "attemptId",
            "nativeSchemaId",
            "nativeSchemaVersion",
            "producerCodeVersion",
            "adapterVersion",
            "executionStatus",
            "executionReason",
            "evidenceBasis",
            "inputRawSha256",
            "outputRawSha256",
            "outputCanonicalSha256",
            "envelopeCanonicalSha256",
            "inputArtifactRef",
            "outputArtifactRef",
            "executionArtifactRef",
            "executionArtifactRawSha256",
            "dependsOn",
            "subject",
            "qualifiers",
            "payload",
        )
        missing = [name for name in required if name not in value]
        if missing:
            raise ContractError(f"packet is missing envelope field(s): {', '.join(missing)}")
        if not isinstance(value["envelopeCanonicalSha256"], str):
            raise ContractError("envelopeCanonicalSha256 must be supplied by the orchestrator")
        qualifiers = value["qualifiers"]
        if not isinstance(qualifiers, Sequence) or isinstance(qualifiers, (str, bytes)):
            raise ContractError("qualifiers must be an array of strings")
        dependencies = value["dependsOn"]
        if not isinstance(dependencies, Sequence) or isinstance(
            dependencies, (str, bytes)
        ):
            raise ContractError("dependsOn must be an array")
        return cls(
            run_id=value["runId"],
            hypothesis_id=value["hypothesisId"],
            module_id=value["moduleId"],
            attempt_id=value["attemptId"],
            native_schema_id=value["nativeSchemaId"],
            native_schema_version=value["nativeSchemaVersion"],
            producer_code_version=value["producerCodeVersion"],
            adapter_version=value["adapterVersion"],
            execution_status=value["executionStatus"],
            execution_reason=value["executionReason"],
            evidence_basis=value["evidenceBasis"],
            input_raw_sha256=value["inputRawSha256"],
            output_raw_sha256=value["outputRawSha256"],
            output_canonical_sha256=value["outputCanonicalSha256"],
            envelope_canonical_sha256=value["envelopeCanonicalSha256"],
            input_artifact_ref=value["inputArtifactRef"],
            output_artifact_ref=value["outputArtifactRef"],
            execution_artifact_ref=value["executionArtifactRef"],
            execution_artifact_raw_sha256=value["executionArtifactRawSha256"],
            dependencies=tuple(dependencies),
            subject=Subject.from_mapping(value["subject"]),
            qualifiers=tuple(qualifiers),
            payload=value["payload"],
        )


def packet_from_dict(value: Mapping[str, Any]) -> ModulePacket:
    """Public runtime parser for the orchestrator's camelCase envelope."""

    return ModulePacket.from_mapping(value)


@dataclass(frozen=True, slots=True)
class ObjectiveObservation:
    """One raw, typed objective observation; never an imputed fitness value."""

    objective_id: str
    kind: str
    direction: str
    raw_value: float | str
    unit: str | None
    uncertainty: Mapping[str, Any]
    basis: Mapping[str, Any]
    source_path: str
    native_schema_id: str
    native_schema_version: str
    packet_sha256: str
    evidence_basis: str
    qualifiers: tuple[str, ...] = ()
    derived_policy_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "objective_id",
            "kind",
            "direction",
            "source_path",
            "native_schema_id",
            "native_schema_version",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        kind = self.kind.upper()
        direction = self.direction.upper()
        if kind not in {"NUMERIC", "CATEGORICAL"}:
            raise ContractError("objective kind must be NUMERIC or CATEGORICAL")
        if direction not in {"MAX", "MIN", "CATEGORICAL"}:
            raise ContractError("objective direction must be MAX, MIN, or CATEGORICAL")
        if kind == "NUMERIC":
            if isinstance(self.raw_value, bool) or not isinstance(self.raw_value, (int, float)):
                raise ContractError("numeric objective raw_value must be a number")
            try:
                finite = math.isfinite(float(self.raw_value))
            except OverflowError:
                finite = False
            if not finite:
                raise ContractError("numeric objective raw_value must be finite")
        elif not isinstance(self.raw_value, str) or not self.raw_value:
            raise ContractError("categorical objective raw_value must be a non-empty string")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(
            self,
            "packet_sha256",
            _normalise_sha256(self.packet_sha256, "packet_sha256"),
        )
        evidence_basis = _required_text(self.evidence_basis, "evidence_basis").upper()
        if evidence_basis not in _EVIDENCE_BASES:
            raise ContractError(f"unknown evidence_basis {evidence_basis!r}")
        object.__setattr__(self, "evidence_basis", evidence_basis)
        object.__setattr__(self, "uncertainty", _deep_freeze(self.uncertainty))
        object.__setattr__(self, "basis", _deep_freeze(self.basis))
        object.__setattr__(
            self,
            "qualifiers",
            tuple(dict.fromkeys(_required_text(q, "qualifier").upper() for q in self.qualifiers)),
        )


@dataclass(frozen=True, slots=True)
class HypothesisCandidate:
    """Lossless Highlander candidate extracted from one locked Slate hypothesis."""

    source_id: str
    graph_id: str
    graph_round: int
    subject_id: str
    object_id: str
    subject_name: str
    object_name: str
    scientific_scores: Mapping[str, float]
    path: tuple[Mapping[str, Any], ...]
    evidence: Mapping[str, Any]
    caveats: tuple[str, ...]
    verification: Mapping[str, Any] | None
    issues: tuple[Mapping[str, Any], ...]
    provenance: str
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "source_id",
            "graph_id",
            "subject_id",
            "object_id",
            "subject_name",
            "object_name",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if isinstance(self.graph_round, bool) or not isinstance(self.graph_round, int) or self.graph_round < 0:
            raise ContractError("graph_round must be a non-negative integer")
        object.__setattr__(self, "scientific_scores", _deep_freeze(self.scientific_scores))
        object.__setattr__(self, "path", tuple(_deep_freeze(item) for item in self.path))
        object.__setattr__(self, "evidence", _deep_freeze(self.evidence))
        object.__setattr__(self, "caveats", tuple(self.caveats))
        if self.verification is not None:
            object.__setattr__(self, "verification", _deep_freeze(self.verification))
        object.__setattr__(self, "issues", tuple(_deep_freeze(item) for item in self.issues))
        object.__setattr__(self, "raw", _deep_freeze(self.raw))


@dataclass(frozen=True, slots=True)
class AdaptedModuleResult:
    """Fail-closed result of validating and slicing one module packet."""

    run_id: str
    module_id: str
    hypothesis_id: str
    attempt_id: str
    packet_sha256: str
    attempt_record: Mapping[str, Any]
    objectives: tuple[ObjectiveObservation, ...] = ()
    candidate: HypothesisCandidate | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    raw_payload: Mapping[str, Any] | None = None
    qualifiers: tuple[str, ...] = ()
    missing_reasons: tuple[str, ...] = ()
    quarantined: bool = False
    quarantine_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("run_id", "module_id", "hypothesis_id", "attempt_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "packet_sha256",
            _normalise_sha256(self.packet_sha256, "packet_sha256"),
        )
        object.__setattr__(self, "attempt_record", _deep_freeze(self.attempt_record))
        object.__setattr__(self, "objectives", tuple(self.objectives))
        object.__setattr__(self, "details", _deep_freeze(self.details))
        if self.raw_payload is not None:
            frozen_payload = _deep_freeze(self.raw_payload)
            if not isinstance(frozen_payload, Mapping):
                raise ContractError("raw_payload must be an object or null")
            object.__setattr__(self, "raw_payload", frozen_payload)
        object.__setattr__(
            self,
            "qualifiers",
            tuple(dict.fromkeys(_required_text(q, "qualifier").upper() for q in self.qualifiers)),
        )
        object.__setattr__(self, "missing_reasons", tuple(self.missing_reasons))
        object.__setattr__(self, "quarantine_reasons", tuple(self.quarantine_reasons))
        if self.quarantined and not self.quarantine_reasons:
            raise ContractError("a quarantined result must state at least one reason")
