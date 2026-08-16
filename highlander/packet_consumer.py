"""Production request seam for comparing orchestrator-owned module packets.

The output orchestrator owns packet selection, terminal revision hashes, and
timestamps.  Highlander validates that immutable request, adapts the five
locked producer payloads, and performs a pure portfolio comparison.  It never
calls a producer module or fills a missing objective.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import math
import re
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .packet_adapters import (
    ECONOMICS,
    HYPOTHESIS_GENERATOR,
    MAPPER,
    RECRUITMENT,
    TRACTABILITY,
    adapt_economics,
    adapt_hypothesis_generator,
    adapt_mapper,
    adapt_recruitment,
    adapt_tractability,
    extract_hypothesis_candidates,
)
from .packet_contracts import (
    AdaptedModuleResult,
    ContractError,
    ModulePacket,
    canonical_json_sha256,
    packet_from_dict,
)
from .packet_portfolio import (
    PortfolioResult,
    candidate_from_adapted_results,
    compare_packets,
)
from .thesis import validate_indication_thesis_wire


REQUEST_SCHEMA_VERSION = "highlander.packet-comparison-request.v1"
INPUT_BINDING_SCHEMA_VERSION = "labrador.module-input-binding.v1"
MAX_CANDIDATES = 500
MAX_MODULE_PACKETS_PER_CANDIDATE = 10
MAX_OBJECTIVES = 32
MAX_EMBEDDED_ARTIFACTS = 5_000
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_TOTAL_RESOLVED_ARTIFACT_BYTES = 128 * 1024 * 1024
MAX_REQUEST_BYTES = 192 * 1024 * 1024
MAX_JSON_DEPTH = 64
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "COMPLETED",
        "COMPLETE",
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

# Public and immutable so callers can inspect the exact supported module set.
ADAPTER_DISPATCH: Mapping[
    str, Callable[[ModulePacket], AdaptedModuleResult]
] = MappingProxyType(
    {
        MAPPER: adapt_mapper,
        HYPOTHESIS_GENERATOR: adapt_hypothesis_generator,
        RECRUITMENT: adapt_recruitment,
        ECONOMICS: adapt_economics,
        TRACTABILITY: adapt_tractability,
    }
)

ArtifactResolver = Callable[[str], bytes]


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{path} must be an object")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractError(f"{path} must be an array")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{path} must be a non-empty string")
    return value.strip()


def _required(value: Mapping[str, Any], names: Sequence[str], path: str) -> None:
    missing = [name for name in names if name not in value]
    if missing:
        raise ContractError(f"{path} is missing field(s): {', '.join(missing)}")


def _strings(value: Any, path: str) -> tuple[str, ...]:
    items = _sequence(value, path)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(_text(item, f"{path}[{index}]"))
    return tuple(result)


def _normalise_sha256(value: Any, path: str) -> str:
    digest = _text(value, path).lower()
    if digest.startswith("sha256:"):
        digest = digest[7:]
    if not _SHA256_RE.fullmatch(digest):
        raise ContractError(f"{path} must contain exactly one SHA-256 digest")
    return digest


def _embedded_artifact_resolver(value: Any) -> ArtifactResolver:
    payloads = _mapping(value, "request.artifactPayloads")
    if len(payloads) > MAX_EMBEDDED_ARTIFACTS:
        raise ContractError(
            "request.artifactPayloads exceeds the configured artifact-count limit"
        )
    encoded_payloads: dict[str, str] = {}
    for ref, encoded in payloads.items():
        artifact_ref = _text(ref, "request.artifactPayloads key")
        if not isinstance(encoded, str):
            raise ContractError(
                f"request.artifactPayloads[{artifact_ref!r}] must be base64 text"
            )
        encoded_payloads[artifact_ref] = encoded

    decoded: dict[str, bytes] = {}
    total_resolved = 0

    def resolve(ref: str) -> bytes:
        nonlocal total_resolved
        if ref in decoded:
            return decoded[ref]
        try:
            encoded = encoded_payloads[ref]
        except KeyError as error:
            raise ContractError(f"no artifact bytes supplied for {ref!r}") from error
        if len(encoded) > ((MAX_ARTIFACT_BYTES + 2) // 3) * 4:
            raise ContractError(f"artifact {ref!r} exceeds the configured byte limit")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ContractError(
                f"request.artifactPayloads[{ref!r}] is not valid base64"
            ) from error
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ContractError(f"artifact {ref!r} exceeds the configured byte limit")
        if total_resolved + len(raw) > MAX_TOTAL_RESOLVED_ARTIFACT_BYTES:
            raise ContractError("resolved artifacts exceed the configured total byte limit")
        decoded[ref] = raw
        total_resolved += len(raw)
        return raw

    return resolve


def _bounded_artifact_resolver(resolver: ArtifactResolver) -> ArtifactResolver:
    """Apply the same per-request byte budget to embedded and external stores."""

    cache: dict[str, bytes] = {}
    total_resolved = 0

    def resolve(ref: str) -> bytes:
        nonlocal total_resolved
        if ref in cache:
            return cache[ref]
        raw = resolver(ref)
        if not isinstance(raw, bytes):
            raise ContractError("artifact resolver must return bytes")
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ContractError(f"artifact {ref!r} exceeds the configured byte limit")
        if total_resolved + len(raw) > MAX_TOTAL_RESOLVED_ARTIFACT_BYTES:
            raise ContractError("resolved artifacts exceed the configured total byte limit")
        cache[ref] = raw
        total_resolved += len(raw)
        return raw

    return resolve


def strict_json_loads(raw: bytes, label: str) -> Any:
    """Parse one I-JSON document with strict key, Unicode, and numeric invariants."""

    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"{label} contains duplicate object key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str):
        raise ContractError(f"{label} contains non-JSON numeric constant {value!r}")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except ContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ContractError(f"{label} is not valid JSON") from error

    def check_text(text: str) -> None:
        if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
            raise ContractError(f"{label} contains an unpaired UTF-16 surrogate")

    def check_depth(item: Any, depth: int = 0) -> None:
        if depth > MAX_JSON_DEPTH:
            raise ContractError(f"{label} exceeds the configured JSON nesting limit")
        if isinstance(item, Mapping):
            for key, child in item.items():
                check_text(key)
                check_depth(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check_depth(child, depth + 1)
        elif isinstance(item, str):
            check_text(item)
        elif isinstance(item, int) and not isinstance(item, bool):
            if abs(item) > 9_007_199_254_740_991:
                raise ContractError(
                    f"{label} contains an integer outside the I-JSON safe range"
                )
        elif isinstance(item, float) and not math.isfinite(item):
            raise ContractError(f"{label} contains a non-finite number")

    check_depth(value)
    return value


def _validate_input_binding(packet: ModulePacket, input_raw: bytes) -> None:
    binding = _mapping(
        strict_json_loads(input_raw, packet.input_artifact_ref),
        packet.input_artifact_ref,
    )
    _required(
        binding,
        (
            "schemaVersion",
            "runId",
            "hypothesisId",
            "moduleId",
            "attemptId",
            "dependsOn",
            "inputIdentity",
            "nativeInput",
        ),
        packet.input_artifact_ref,
    )
    expected = {
        "schemaVersion": INPUT_BINDING_SCHEMA_VERSION,
        "runId": packet.run_id,
        "hypothesisId": packet.hypothesis_id,
        "moduleId": packet.module_id,
        "attemptId": packet.attempt_id,
        "dependsOn": [dict(dependency) for dependency in packet.dependencies],
    }
    actual = {key: binding[key] for key in expected}
    if canonical_json_sha256(actual) != canonical_json_sha256(expected):
        raise ContractError(
            f"{packet.input_artifact_ref!r} does not bind the selected module attempt"
        )

    input_identity = _mapping(
        binding["inputIdentity"], f"{packet.input_artifact_ref}.inputIdentity"
    )
    if _text(
        input_identity.get("hypothesisId"),
        f"{packet.input_artifact_ref}.inputIdentity.hypothesisId",
    ) != packet.hypothesis_id:
        raise ContractError(
            f"{packet.input_artifact_ref!r} input identity does not bind the "
            "selected hypothesis"
        )
    native_input = _mapping(
        binding["nativeInput"], f"{packet.input_artifact_ref}.nativeInput"
    )
    if packet.module_id == RECRUITMENT:
        thesis = native_input
        thesis_path = f"{packet.input_artifact_ref}.nativeInput"
        if "thesis" in native_input:
            thesis = _mapping(native_input["thesis"], f"{thesis_path}.thesis")
            thesis_path += ".thesis"
        outputless_unsuccessful = (
            packet.payload is None
            and not packet.succeeded
            and packet.execution_status != "PARTIAL"
        )
        if not outputless_unsuccessful:
            try:
                validate_indication_thesis_wire(dict(thesis))
            except ValueError as error:
                raise ContractError(
                    f"{packet.input_artifact_ref!r} recruitment thesis is not canonical: {error}"
                ) from error
        if _text(
            thesis.get("id"), f"{thesis_path}.id"
        ) != packet.hypothesis_id:
            raise ContractError(
                f"{packet.input_artifact_ref!r} recruitment thesis does not bind "
                "the selected hypothesis"
            )
        if _text(
            input_identity.get("thesisId"),
            f"{packet.input_artifact_ref}.inputIdentity.thesisId",
        ) != packet.hypothesis_id:
            raise ContractError(
                f"{packet.input_artifact_ref!r} recruitment input identity does not "
                "bind the selected thesis"
            )
    elif packet.module_id == ECONOMICS:
        recruitment_dependency = next(
            (
                dependency
                for dependency in packet.dependencies
                if dependency["moduleId"] == RECRUITMENT
            ),
            None,
        )
        if recruitment_dependency is not None and input_identity.get(
            "recruitmentOutputCanonicalSha256"
        ) != recruitment_dependency["outputCanonicalSha256"]:
            raise ContractError(
                f"{packet.input_artifact_ref!r} economics input does not bind "
                "the selected recruitment result"
            )
        has_program = "program" in native_input
        has_cashflow = "cashflow_inputs" in native_input
        if has_program == has_cashflow:
            raise ContractError(
                f"{packet.input_artifact_ref!r} economics native input must contain "
                "exactly one of program or cashflow_inputs"
            )
        native_mode = "program" if has_program else "cashflow_inputs"
        program = _mapping(
            native_input.get(native_mode),
            f"{packet.input_artifact_ref}.nativeInput.{native_mode}",
        )
        native_program_id_raw = program.get("program_id")
        if native_program_id_raw is None and packet.payload is None:
            native_program_id = None
        else:
            native_program_id = _text(
                native_program_id_raw,
                f"{packet.input_artifact_ref}.nativeInput.{native_mode}.program_id",
            )
        identity_program_id = input_identity.get("programId")
        if identity_program_id is not None:
            identity_program_id = _text(
                identity_program_id,
                f"{packet.input_artifact_ref}.inputIdentity.programId",
            )
        if identity_program_id != native_program_id:
            raise ContractError(
                f"{packet.input_artifact_ref!r} economics input identity does not "
                "bind the native program"
            )
        # A valid native output must echo this program ID, but malformed output
        # stays candidate-local and is handled by ``adapt_economics`` below.
        snapshot = packet.payload.get("input_snapshot") if packet.payload is not None else None
        snapshot_program = (
            snapshot.get(native_mode) if isinstance(snapshot, Mapping) else None
        )
        if isinstance(snapshot_program, Mapping) and isinstance(
            snapshot_program.get("program_id"), str
        ):
            if snapshot_program["program_id"] != native_program_id:
                raise ContractError(
                    f"{packet.input_artifact_ref!r} economics input program does not "
                    "match the native output snapshot"
                )
    elif packet.module_id == TRACTABILITY:
        if packet.subject.uniprot_accession is not None:
            accession = _text(
                native_input.get("uniprot_accession"),
                f"{packet.input_artifact_ref}.nativeInput.uniprot_accession",
            )
            if accession.casefold() != packet.subject.uniprot_accession.casefold():
                raise ContractError(
                    f"{packet.input_artifact_ref!r} tractability input does not bind "
                    "the selected accession"
                )
            identity_accession = _text(
                input_identity.get("uniprotAccession"),
                f"{packet.input_artifact_ref}.inputIdentity.uniprotAccession",
            )
            if identity_accession.casefold() != packet.subject.uniprot_accession.casefold():
                raise ContractError(
                    f"{packet.input_artifact_ref!r} tractability input identity does "
                    "not bind the selected accession"
                )


def _verify_artifacts(packet: ModulePacket, resolver: ArtifactResolver) -> None:
    input_raw = resolver(packet.input_artifact_ref)
    if not isinstance(input_raw, bytes):
        raise ContractError("artifact resolver must return bytes")
    packet.verify_raw_artifacts(input_raw=input_raw)
    _validate_input_binding(packet, input_raw)

    output_raw = None
    if packet.output_artifact_ref is not None:
        output_raw = resolver(packet.output_artifact_ref)
        if not isinstance(output_raw, bytes):
            raise ContractError("artifact resolver must return bytes")

    execution_raw = None
    if packet.execution_artifact_ref is not None:
        execution_raw = resolver(packet.execution_artifact_ref)
        if not isinstance(execution_raw, bytes):
            raise ContractError("artifact resolver must return bytes")

    packet.verify_raw_artifacts(
        input_raw=input_raw,
        output_raw=output_raw,
        execution_raw=execution_raw,
    )
    if output_raw is not None:
        assert packet.output_artifact_ref is not None
        parsed_output = strict_json_loads(output_raw, packet.output_artifact_ref)
        if canonical_json_sha256(parsed_output) != packet.output_canonical_sha256:
            raise ContractError(
                "resolved output artifact does not match the packet's canonical payload"
            )


def _has_canonical_partial_hypgen_output(packet: ModulePacket) -> bool:
    if (
        packet.module_id != HYPOTHESIS_GENERATOR
        or packet.execution_status != "PARTIAL"
        or packet.payload is None
        or packet.output_canonical_sha256 is None
    ):
        return False
    try:
        candidates = extract_hypothesis_candidates(
            packet.payload,
            allow_partial=True,
        )
    except ContractError:
        return False
    return len(candidates) == 1 and candidates[0].source_id == packet.hypothesis_id


def _validate_dependencies(packets: Sequence[ModulePacket], path: str) -> None:
    by_module = {packet.module_id: packet for packet in packets}
    allowed_parents = {
        MAPPER: frozenset(),
        HYPOTHESIS_GENERATOR: frozenset({MAPPER}),
        RECRUITMENT: frozenset({HYPOTHESIS_GENERATOR}),
        # Tractability can run directly from the branch focus/accession even if
        # HypGen fails.  It may still declare exact mapper/HypGen lineage when
        # those outputs were actually used, but neither parent is mandatory.
        TRACTABILITY: frozenset({MAPPER, HYPOTHESIS_GENERATOR}),
        ECONOMICS: frozenset({HYPOTHESIS_GENERATOR, RECRUITMENT}),
    }
    for packet in packets:
        dependency_modules = {
            dependency["moduleId"] for dependency in packet.dependencies
        }
        disallowed = dependency_modules - allowed_parents[packet.module_id]
        if disallowed:
            raise ContractError(
                f"{path} module {packet.module_id!r} declares a dependency "
                f"outside the locked DAG: {sorted(disallowed)}"
            )
        unknown = dependency_modules - set(by_module)
        if unknown:
            raise ContractError(
                f"{path} module {packet.module_id!r} references an unselected parent module"
            )
        for dependency in packet.dependencies:
            parent = by_module[dependency["moduleId"]]
            if (
                dependency["outputCanonicalSha256"]
                != parent.output_canonical_sha256
            ):
                raise ContractError(
                    f"{path} module {packet.module_id!r} has a mismatched parent hash "
                    f"for {parent.module_id!r}"
                )
            if (
                dependency["envelopeCanonicalSha256"]
                != parent.envelope_canonical_sha256
            ):
                raise ContractError(
                    f"{path} module {packet.module_id!r} has a mismatched parent "
                    f"envelope hash for {parent.module_id!r}"
                )

    def require_parent(
        child: str,
        parent: str,
        *,
        allow_partial_parent_output: bool = False,
    ) -> None:
        child_packet = by_module.get(child)
        if child_packet is None:
            return
        parent_packet = by_module.get(parent)
        if parent_packet is None:
            raise ContractError(
                f"{path} module {child!r} requires selected parent {parent!r}"
            )
        parent_has_partial_output = (
            allow_partial_parent_output
            and _has_canonical_partial_hypgen_output(parent_packet)
        )
        if (
            child_packet.succeeded
            and not parent_packet.succeeded
            and not parent_has_partial_output
        ):
            raise ContractError(
                f"{path} successful module {child!r} depends on non-successful "
                f"parent {parent!r}"
            )
        dependency_hashes = {
            dependency["moduleId"]: (
                dependency["outputCanonicalSha256"],
                dependency["envelopeCanonicalSha256"],
            )
            for dependency in child_packet.dependencies
        }
        if dependency_hashes.get(parent) != (
            parent_packet.output_canonical_sha256,
            parent_packet.envelope_canonical_sha256,
        ):
            raise ContractError(
                f"{path} module {child!r} does not bind parent output {parent!r}"
            )

    if MAPPER in by_module and HYPOTHESIS_GENERATOR in by_module:
        require_parent(HYPOTHESIS_GENERATOR, MAPPER)
    # A PARTIAL HypGen attempt can still carry the complete canonical
    # hypothesis/thesis needed by clinical while lacking its separate ROI
    # request.  Preserve that exact partial artifact and allow clinical to use
    # it; HypGen objectives remain missing, so a full scientific policy still
    # makes the branch incomparable.
    require_parent(
        RECRUITMENT,
        HYPOTHESIS_GENERATOR,
        allow_partial_parent_output=True,
    )
    require_parent(ECONOMICS, HYPOTHESIS_GENERATOR)


def _lineage_exclusion(results: Sequence[AdaptedModuleResult]) -> str | None:
    """Return a stable exclusion when mapper and HypothesisDocument lineage diverge."""

    by_module = {result.module_id: result for result in results}
    mapper = by_module.get(MAPPER)
    hypgen = by_module.get(HYPOTHESIS_GENERATOR)
    if mapper is None or hypgen is None:
        return None

    mapper_graph = mapper.details.get("graphId")
    mapper_round = mapper.details.get("graphRound")
    hypgen_graph = hypgen.details.get("graphId")
    hypgen_round = hypgen.details.get("graphRound")
    if None in (mapper_graph, mapper_round, hypgen_graph, hypgen_round):
        return None
    if (mapper_graph, mapper_round) == (hypgen_graph, hypgen_round):
        return None
    return (
        "CROSS_MODULE_LINEAGE_MISMATCH:"
        f"mapper={mapper_graph}@{mapper_round}:"
        f"hypothesis-generator={hypgen_graph}@{hypgen_round}"
    )


def _dependency_result_exclusions(
    packets: Sequence[ModulePacket],
    results: Sequence[AdaptedModuleResult],
) -> tuple[str, ...]:
    """Propagate unusable parent validation into dependent observations."""

    results_by_module = {result.module_id: result for result in results}
    packets_by_module = {packet.module_id: packet for packet in packets}
    exclusions: list[str] = []
    for child in packets:
        for dependency in child.dependencies:
            parent_module = dependency["moduleId"]
            parent_result = results_by_module[parent_module]
            parent_packet = packets_by_module[parent_module]
            partial_hypgen_thesis_used_by_clinical = (
                child.module_id == RECRUITMENT
                and parent_module == HYPOTHESIS_GENERATOR
                and _has_canonical_partial_hypgen_output(parent_packet)
            )
            if parent_result.quarantined:
                exclusions.append(
                    f"UNUSABLE_DEPENDENCY:{child.module_id}:{parent_module}:QUARANTINED"
                )
            elif parent_result.missing_reasons and not partial_hypgen_thesis_used_by_clinical:
                exclusions.append(
                    f"UNUSABLE_DEPENDENCY:{child.module_id}:{parent_module}:MISSING_RESULT"
                )
    return tuple(exclusions)


def _producer_next_actions(
    packets: Sequence[ModulePacket],
) -> tuple[dict[str, Any], ...]:
    """Extract advisory actions only from exact producer-emitted fields."""

    actions: list[dict[str, Any]] = []
    for packet in packets:
        if not packet.succeeded or not isinstance(packet.payload, Mapping):
            continue
        payload = packet.payload
        if packet.module_id == HYPOTHESIS_GENERATOR:
            document = payload.get("hypothesis")
            asks = document.get("asks", ()) if isinstance(document, Mapping) else ()
            if isinstance(asks, Sequence) and not isinstance(asks, (str, bytes)):
                for index, value in enumerate(asks):
                    if not isinstance(value, Mapping):
                        continue
                    action_type = value.get("ask")
                    target = value.get("target")
                    if action_type not in {
                        "expand_node", "resolve_link", "test_gap", "new_question"
                    } or not isinstance(target, str) or not target.strip():
                        continue
                    reason = value.get("reason")
                    if not isinstance(reason, str) or not reason.strip():
                        continue
                    actions.append(
                        {
                            "priority": 0,
                            "candidateId": packet.hypothesis_id,
                            "actionType": action_type,
                            "target": target,
                            "description": reason,
                            "producerModuleId": packet.module_id,
                            "producerOutputSha256": packet.output_canonical_sha256,
                            "sourceId": target,
                            "sourcePath": f"$.hypothesis.asks[{index}]",
                        }
                    )
            hypothesis = (
                document.get("hypothesis")
                if isinstance(document, Mapping)
                else None
            )
            evidence = hypothesis.get("evidence") if isinstance(hypothesis, Mapping) else None
            gap = evidence.get("gap") if isinstance(evidence, Mapping) else None
            if isinstance(gap, Mapping):
                gap_id = gap.get("id")
                note = gap.get("note")
                if isinstance(gap_id, str) and gap_id.strip() and isinstance(note, str) and note.strip():
                    actions.append(
                        {
                            "priority": 2,
                            "candidateId": packet.hypothesis_id,
                            "actionType": "test_gap",
                            "target": gap_id,
                            "description": note,
                            "producerModuleId": packet.module_id,
                            "producerOutputSha256": packet.output_canonical_sha256,
                            "sourceId": gap_id,
                            "sourcePath": "$.hypothesis.hypothesis.evidence.gap",
                        }
                    )
        elif packet.module_id == MAPPER:
            gaps = payload.get("gaps", ())
            if isinstance(gaps, Sequence) and not isinstance(gaps, (str, bytes)):
                for index, value in enumerate(gaps):
                    if not isinstance(value, Mapping):
                        continue
                    gap_id = value.get("id")
                    description = value.get("note") or value.get("reason")
                    if not isinstance(gap_id, str) or not gap_id.strip():
                        continue
                    if not isinstance(description, str) or not description.strip():
                        continue
                    actions.append(
                        {
                            "priority": 1,
                            "candidateId": packet.hypothesis_id,
                            "actionType": "test_gap",
                            "target": gap_id,
                            "description": description,
                            "producerModuleId": packet.module_id,
                            "producerOutputSha256": packet.output_canonical_sha256,
                            "sourceId": gap_id,
                            "sourcePath": f"$.gaps[{index}]",
                        }
                    )
        elif packet.module_id == TRACTABILITY:
            follow_up = payload.get("next_experiment")
            if isinstance(follow_up, Mapping):
                description = follow_up.get("description")
                if isinstance(description, str) and description.strip():
                    actions.append(
                        {
                            "priority": 3,
                            "candidateId": packet.hypothesis_id,
                            "actionType": "run_follow_up",
                            "target": packet.subject.uniprot_accession or packet.hypothesis_id,
                            "description": description,
                            "producerModuleId": packet.module_id,
                            "producerOutputSha256": packet.output_canonical_sha256,
                            "sourceId": "next_experiment",
                            "sourcePath": "$.next_experiment",
                        }
                    )
    return tuple(actions)


def _select_next_evidence_action(
    actions: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Prefer an action shared by most candidate branches, then stable source order."""

    grouped: dict[str, dict[str, Any]] = {}
    for action in actions:
        identity = {
            key: action[key]
            for key in (
                "actionType", "target", "description", "producerModuleId",
                "sourceId", "sourcePath",
            )
        }
        key = canonical_json_sha256(identity)
        group = grouped.setdefault(
            key,
            {
                **identity,
                "producerOutputSha256": action["producerOutputSha256"],
                "priority": action["priority"],
                "candidateIds": set(),
            },
        )
        group["candidateIds"].add(action["candidateId"])
        # One semantic action can be emitted independently by several branches.
        # Keep a deterministic exact producer artifact as the compact grounding;
        # candidateIds records every branch that emitted the same action.
        group["producerOutputSha256"] = min(
            group["producerOutputSha256"], action["producerOutputSha256"]
        )
    if not grouped:
        return None
    selected_key, selected = min(
        grouped.items(),
        key=lambda item: (
            -len(item[1]["candidateIds"]),
            item[1]["priority"],
            item[1]["producerModuleId"],
            item[1]["actionType"],
            item[1]["target"],
            item[0],
        ),
    )
    candidate_ids = sorted(selected.pop("candidateIds"))
    selected.pop("priority")
    action_id = "action-" + selected_key[:16]
    return {
        "actionId": action_id,
        **selected,
        "candidateIds": candidate_ids,
        "candidateCount": len(candidate_ids),
        "selectionBasis": "PRODUCER_EMITTED_MOST_BRANCHES_STABLE_TIEBREAK",
    }


def _consume_candidate(
    value: Any,
    index: int,
    artifact_resolver: ArtifactResolver,
):
    path = f"request.candidatePackets[{index}]"
    candidate = _mapping(value, path)
    _required(
        candidate,
        (
            "packetRevisionId",
            "packetHash",
            "runId",
            "hypothesisId",
            "modulePackets",
        ),
        path,
    )
    packet_revision_id = _text(candidate["packetRevisionId"], f"{path}.packetRevisionId")
    run_id = _text(candidate["runId"], f"{path}.runId")
    hypothesis_id = _text(candidate["hypothesisId"], f"{path}.hypothesisId")
    module_values = _sequence(candidate["modulePackets"], f"{path}.modulePackets")
    if not module_values:
        raise ContractError(f"{path}.modulePackets must not be empty")
    if len(module_values) > MAX_MODULE_PACKETS_PER_CANDIDATE:
        raise ContractError(f"{path}.modulePackets exceeds the configured limit")
    exclusion_reasons = tuple(
        sorted(
            set(
                _strings(
                    candidate.get("exclusionReasons", ()),
                    f"{path}.exclusionReasons",
                )
            )
        )
    )

    selected_packets: dict[str, ModulePacket] = {}
    selected_raw_modules: dict[str, Mapping[str, Any]] = {}
    selected_fingerprints: dict[str, str] = {}
    for module_index, raw_module in enumerate(module_values):
        module_path = f"{path}.modulePackets[{module_index}]"
        raw_module_mapping = _mapping(raw_module, module_path)
        packet = packet_from_dict(raw_module_mapping)
        if packet.run_id != run_id:
            raise ContractError(f"{module_path}.runId does not match candidate runId")
        if packet.hypothesis_id != hypothesis_id:
            raise ContractError(
                f"{module_path}.hypothesisId does not match candidate hypothesisId"
            )
        if packet.module_id not in ADAPTER_DISPATCH:
            raise ContractError(f"{module_path}.moduleId is not supported by Highlander")
        if packet.execution_status not in _TERMINAL_EXECUTION_STATUSES:
            raise ContractError(
                f"{module_path}.executionStatus is not terminal"
            )
        fingerprint = canonical_json_sha256(raw_module_mapping)
        previous = selected_fingerprints.get(packet.module_id)
        if previous is not None:
            if hmac.compare_digest(previous, fingerprint):
                continue
            raise ContractError(
                f"{path}.modulePackets selects conflicting attempts for "
                f"module {packet.module_id!r}"
            )
        selected_fingerprints[packet.module_id] = fingerprint
        selected_packets[packet.module_id] = packet
        selected_raw_modules[packet.module_id] = raw_module_mapping

    module_ids = sorted(selected_packets)
    packets = [selected_packets[module_id] for module_id in module_ids]
    terminal_body = {
        "packetRevisionId": packet_revision_id,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "modulePackets": [selected_raw_modules[module_id] for module_id in module_ids],
        "exclusionReasons": list(exclusion_reasons),
    }
    supplied_digest = _normalise_sha256(candidate["packetHash"], f"{path}.packetHash")
    actual_digest = canonical_json_sha256(terminal_body)
    if not hmac.compare_digest(supplied_digest, actual_digest):
        raise ContractError(
            f"{path}.packetHash does not match the canonical terminal packet body"
        )

    _validate_dependencies(packets, path)
    for packet in packets:
        _verify_artifacts(packet, artifact_resolver)

    adapted = tuple(ADAPTER_DISPATCH[packet.module_id](packet) for packet in packets)
    lineage_exclusion = _lineage_exclusion(adapted)
    exclusions = list(exclusion_reasons)
    exclusions.extend(_dependency_result_exclusions(packets, adapted))
    if lineage_exclusion is not None:
        exclusions.append(lineage_exclusion)

    return (
        candidate_from_adapted_results(
            adapted,
            packet_revision_id=packet_revision_id,
            packet_hash=supplied_digest,
            candidate_id=hypothesis_id,
            exclusion_reasons=exclusions,
        ),
        _producer_next_actions(packets),
    )


def compare_packet_request(
    value: Mapping[str, Any],
    *,
    artifact_resolver: ArtifactResolver | None = None,
) -> PortfolioResult:
    """Validate and compare one packet-comparison request.

    Structural or integrity failures raise :class:`ContractError` (or the
    comparator's ``ValueError`` for an invalid objective policy).  Valid but
    scientifically incomplete, quarantined, or lineage-inconsistent candidates
    are retained in the result as incomparable; they are never silently scored.
    """

    request = _mapping(value, "request")
    _required(
        request,
        ("schemaVersion", "snapshotId", "createdAt", "objectivePolicy", "candidatePackets"),
        "request",
    )
    schema_version = _text(request["schemaVersion"], "request.schemaVersion")
    if schema_version != REQUEST_SCHEMA_VERSION:
        raise ContractError(
            f"request.schemaVersion must be {REQUEST_SCHEMA_VERSION!r}"
        )
    snapshot_id = _text(request["snapshotId"], "request.snapshotId")
    created_at = _text(request["createdAt"], "request.createdAt")
    policy = _mapping(request["objectivePolicy"], "request.objectivePolicy")
    policy_objectives = _sequence(
        policy.get("objectives"), "request.objectivePolicy.objectives"
    )
    if not policy_objectives:
        raise ContractError("request.objectivePolicy.objectives must not be empty")
    if len(policy_objectives) > MAX_OBJECTIVES:
        raise ContractError(
            "request.objectivePolicy.objectives exceeds the configured limit"
        )
    candidate_values = _sequence(request["candidatePackets"], "request.candidatePackets")
    if not candidate_values:
        raise ContractError("request.candidatePackets must not be empty")
    if len(candidate_values) > MAX_CANDIDATES:
        raise ContractError("request.candidatePackets exceeds the configured limit")
    run_ids = {
        _text(
            _mapping(candidate, f"request.candidatePackets[{index}]").get("runId"),
            f"request.candidatePackets[{index}].runId",
        )
        for index, candidate in enumerate(candidate_values)
    }
    if len(run_ids) != 1:
        raise ContractError("all candidatePackets in one snapshot must share one runId")
    if artifact_resolver is None:
        if "artifactPayloads" not in request:
            raise ContractError(
                "request.artifactPayloads is required when no artifact resolver is supplied"
            )
        artifact_resolver = _embedded_artifact_resolver(request["artifactPayloads"])
    artifact_resolver = _bounded_artifact_resolver(artifact_resolver)
    consumed = tuple(
        _consume_candidate(candidate, index, artifact_resolver)
        for index, candidate in enumerate(candidate_values)
    )
    candidates = tuple(item[0] for item in consumed)
    actions = tuple(action for item in consumed for action in item[1])
    result = compare_packets(
        candidates,
        policy,
        snapshot_id=snapshot_id,
        created_at=created_at,
    )
    return replace(
        result,
        next_evidence_action=_select_next_evidence_action(actions),
    )
