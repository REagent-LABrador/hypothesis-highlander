"""End-to-end tests for the orchestrator packet-comparison request seam."""
from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

from highlander.packet_adapters import (
    ECONOMICS,
    HYPOTHESIS_GENERATOR,
    MAPPER,
    RECRUITMENT,
    TRACTABILITY,
)
from highlander.packet_consumer import (
    ADAPTER_DISPATCH,
    REQUEST_SCHEMA_VERSION,
    compare_packet_request,
)
from highlander.packet_contracts import (
    ContractError,
    canonical_json_bytes,
    canonical_json_sha256,
    raw_sha256,
)


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "locked_module_slices.json").read_text()
)
FIXTURES["hypothesis_generator"] = json.loads(
    (Path(__file__).parent / "fixtures" / "hypgen-irak4-ra.slate.json").read_text()
)
FIXTURES["tractability"] = json.loads(
    (Path(__file__).parent / "fixtures" / "jak1_P23458.json").read_text()
)
FIXTURES["economics"] = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "economics-analysis-result-cashflow-inputs.json"
    ).read_text()
)
STAMP = "2026-08-16T12:00:00Z"
HEX = "0" * 64
ARTIFACTS: dict[str, bytes] = {}
SCHEMAS = {
    MAPPER: ("EvidenceGraph", "1.1"),
    HYPOTHESIS_GENERATOR: ("hyp_gen.schema.Slate", "locked-5131cd1"),
    RECRUITMENT: ("RecruitabilityResult", "locked-5131cd1"),
    ECONOMICS: ("labrador_roi.engine.AnalysisResult", "1.3.0"),
    TRACTABILITY: ("small-molecule-tractability-dossier", "locked-5131cd1"),
}


def economics_native_input(payload: dict | None) -> tuple[dict, str | None]:
    """Return the exact locked economics input branch and its program ID."""

    if payload is None:
        return {"program": {"program_id": None}}, None
    snapshot = payload["input_snapshot"]
    if "program" in snapshot:
        return {
            "program": copy.deepcopy(snapshot["program"]),
            "comparables": copy.deepcopy(snapshot["comparables"]),
        }, snapshot["program"]["program_id"]
    return {
        "cashflow_inputs": copy.deepcopy(snapshot["cashflow_inputs"]),
    }, snapshot["cashflow_inputs"]["program_id"]


def module_packet(
    module_id: str,
    payload: dict | None,
    *,
    run_id: str,
    hypothesis_id: str,
    attempt_id: str = "attempt-1",
    subject: dict | None = None,
    depends_on: list[dict] | None = None,
    execution_status: str = "SUCCEEDED",
    native_input: dict | None = None,
    input_identity: dict | None = None,
) -> dict:
    schema = SCHEMAS[module_id]
    dependencies = copy.deepcopy(depends_on or [])
    input_ref = f"artifact://{run_id}/{hypothesis_id}/{module_id}/{attempt_id}/input"
    output_ref = f"artifact://{run_id}/{hypothesis_id}/{module_id}/{attempt_id}/output"
    execution_ref = (
        f"artifact://{run_id}/{hypothesis_id}/{module_id}/{attempt_id}/execution"
    )
    if native_input is None:
        if module_id == RECRUITMENT:
            native_input = {"id": hypothesis_id}
        elif module_id == ECONOMICS:
            recruitment_dependency = next(
                (
                    item
                    for item in dependencies
                    if item["moduleId"] == RECRUITMENT
                ),
                None,
            )
            native_input, _ = economics_native_input(payload)
        elif module_id == TRACTABILITY:
            native_input = {
                "uniprot_accession": (subject or {}).get("uniprotAccession"),
            }
        else:
            native_input = {"hypothesisId": hypothesis_id, "moduleId": module_id}
    if input_identity is None:
        input_identity = {"hypothesisId": hypothesis_id}
        if module_id == RECRUITMENT:
            input_identity["thesisId"] = hypothesis_id
        elif module_id == ECONOMICS:
            recruitment_dependency = next(
                (
                    item
                    for item in dependencies
                    if item["moduleId"] == RECRUITMENT
                ),
                None,
            )
            _, program_id = economics_native_input(payload)
            input_identity.update(
                {
                    "programId": program_id,
                    "recruitmentOutputCanonicalSha256": (
                        recruitment_dependency["outputCanonicalSha256"]
                        if recruitment_dependency is not None
                        else None
                    ),
                }
            )
        elif module_id == TRACTABILITY:
            input_identity["uniprotAccession"] = (subject or {}).get(
                "uniprotAccession"
            )
    input_binding = {
        "schemaVersion": "labrador.module-input-binding.v1",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "moduleId": module_id,
        "attemptId": attempt_id,
        "dependsOn": dependencies,
        "inputIdentity": input_identity,
        "nativeInput": native_input,
    }
    input_raw = canonical_json_bytes(input_binding)
    ARTIFACTS[input_ref] = input_raw
    output_raw = canonical_json_bytes(payload) if payload is not None else None
    if output_raw is not None:
        ARTIFACTS[output_ref] = output_raw
    execution_raw = None
    if payload is None:
        execution_raw = f"{execution_status}: test terminal status".encode()
        ARTIFACTS[execution_ref] = execution_raw
    envelope = {
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "moduleId": module_id,
        "attemptId": attempt_id,
        "nativeSchemaId": schema[0],
        "nativeSchemaVersion": schema[1],
        "producerCodeVersion": "5131cd109bef1f9eebe0b109a04a0fcb98908454",
        "adapterVersion": "packet-adapters.v1",
        "executionStatus": execution_status,
        "executionReason": (
            None
            if execution_status in {"SUCCEEDED", "COMPLETED", "COMPLETE"}
            else "test terminal status"
        ),
        "evidenceBasis": "LIVE",
        "inputRawSha256": raw_sha256(input_raw),
        "outputRawSha256": raw_sha256(output_raw) if output_raw is not None else None,
        "outputCanonicalSha256": (
            canonical_json_sha256(payload) if payload is not None else None
        ),
        "inputArtifactRef": input_ref,
        "outputArtifactRef": output_ref if payload is not None else None,
        "executionArtifactRef": execution_ref if execution_raw is not None else None,
        "executionArtifactRawSha256": (
            raw_sha256(execution_raw) if execution_raw is not None else None
        ),
        "dependsOn": dependencies,
        "subject": subject or {},
        "qualifiers": ["LIVE"],
        "payload": payload,
    }
    envelope["envelopeCanonicalSha256"] = canonical_json_sha256(envelope)
    return envelope


def dependency_for(parent: dict) -> dict:
    return {
        "moduleId": parent["moduleId"],
        "outputCanonicalSha256": parent["outputCanonicalSha256"],
        "envelopeCanonicalSha256": parent["envelopeCanonicalSha256"],
    }


def resign_module(module: dict, *, refresh_input_binding: bool = True) -> None:
    """Re-sign a deliberately mutated test envelope and its bound input artifact."""

    if refresh_input_binding:
        input_ref = module["inputArtifactRef"]
        native_input = {"hypothesisId": module["hypothesisId"], "moduleId": module["moduleId"]}
        input_identity = {"hypothesisId": module["hypothesisId"]}
        try:
            old_binding = json.loads(ARTIFACTS[input_ref])
            native_input = old_binding.get("nativeInput", native_input)
            input_identity = old_binding.get("inputIdentity", input_identity)
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        binding = {
            "schemaVersion": "labrador.module-input-binding.v1",
            "runId": module["runId"],
            "hypothesisId": module["hypothesisId"],
            "moduleId": module["moduleId"],
            "attemptId": module["attemptId"],
            "dependsOn": copy.deepcopy(module["dependsOn"]),
            "inputIdentity": input_identity,
            "nativeInput": native_input,
        }
        input_raw = canonical_json_bytes(binding)
        ARTIFACTS[input_ref] = input_raw
        module["inputRawSha256"] = raw_sha256(input_raw)
    body = {key: value for key, value in module.items() if key != "envelopeCanonicalSha256"}
    module["envelopeCanonicalSha256"] = canonical_json_sha256(body)


def candidate_packet(
    hypothesis_id: str,
    modules: list[dict],
    *,
    run_id: str = "run-001",
    revision: str | None = None,
    exclusions: list[str] | None = None,
    prefixed_hash: bool = False,
) -> dict:
    exclusions = exclusions or []
    revision = revision or f"packet-{hypothesis_id}-r1"
    unique_modules: dict[str, dict] = {}
    for module in modules:
        unique_modules.setdefault(module["moduleId"], module)
    body = {
        "packetRevisionId": revision,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "modulePackets": [unique_modules[module_id] for module_id in sorted(unique_modules)],
        "exclusionReasons": sorted(set(exclusions)),
    }
    digest = canonical_json_sha256(body)
    return {
        "packetRevisionId": revision,
        "packetHash": f"sha256:{digest}" if prefixed_hash else digest,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "modulePackets": modules,
        "exclusionReasons": exclusions,
    }


def rehash_candidate(candidate: dict) -> None:
    unique_modules: dict[str, dict] = {}
    for module in candidate["modulePackets"]:
        unique_modules.setdefault(module["moduleId"], module)
    candidate["packetHash"] = canonical_json_sha256(
        {
            "packetRevisionId": candidate["packetRevisionId"],
            "runId": candidate["runId"],
            "hypothesisId": candidate["hypothesisId"],
            "modulePackets": [
                unique_modules[module_id] for module_id in sorted(unique_modules)
            ],
            "exclusionReasons": sorted(set(candidate["exclusionReasons"])),
        }
    )


def request(candidates: list[dict], objectives: list[dict] | None = None) -> dict:
    artifact_refs = {
        module[field]
        for candidate in candidates
        for module in candidate.get("modulePackets", [])
        for field in (
            "inputArtifactRef",
            "outputArtifactRef",
            "executionArtifactRef",
        )
        if module.get(field) is not None
    }
    return {
        "schemaVersion": REQUEST_SCHEMA_VERSION,
        "snapshotId": "snapshot-001",
        "createdAt": STAMP,
        "objectivePolicy": {
            "policyId": "scientific-score-policy.v1",
            "objectives": objectives
            or [
                {"objectiveId": "support", "direction": "MAX"},
                {"objectiveId": "contradiction_risk", "direction": "MIN"},
            ],
        },
        "candidatePackets": candidates,
        "artifactPayloads": {
            ref: base64.b64encode(ARTIFACTS[ref]).decode("ascii")
            for ref in sorted(artifact_refs)
        },
    }


def hypgen_candidate(
    hypothesis_id: str,
    support: float,
    risk: float,
    *,
    run_id: str = "run-001",
    graph_id: str = "g_shared",
    graph_round: int = 2,
) -> dict:
    payload = copy.deepcopy(FIXTURES["hypothesis_generator"])
    payload["graph_id"] = graph_id
    payload["round"] = graph_round
    payload["hypotheses"][0]["id"] = hypothesis_id
    payload["hypotheses"][0]["scores"]["support"] = support
    payload["hypotheses"][0]["scores"]["contradiction_risk"] = risk
    module = module_packet(
        HYPOTHESIS_GENERATOR,
        payload,
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        subject={"graphId": graph_id, "graphRound": graph_round},
    )
    return candidate_packet(hypothesis_id, [module], run_id=run_id)


def full_pipeline_candidate(
    hypothesis_id: str,
    *,
    support: float,
    contradiction_risk: float,
    recruitability: float,
    run_id: str = "run-001",
) -> dict:
    """Build one coherent five-module packet from the locked native fixtures."""

    slate = copy.deepcopy(FIXTURES["hypothesis_generator"])
    graph_id = slate["graph_id"]
    graph_round = slate["round"]
    hypothesis = next(
        item for item in slate["hypotheses"] if item["id"] == hypothesis_id
    )
    hypothesis["scores"]["support"] = support
    hypothesis["scores"]["contradiction_risk"] = contradiction_risk

    mapper_payload = copy.deepcopy(FIXTURES["mapper"])
    mapper_payload["graph_id"] = graph_id
    mapper_payload["round"] = graph_round
    mapper = module_packet(
        MAPPER,
        mapper_payload,
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        subject={"graphId": graph_id, "graphRound": graph_round},
    )
    hypgen = module_packet(
        HYPOTHESIS_GENERATOR,
        slate,
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        subject={"graphId": graph_id, "graphRound": graph_round},
        depends_on=[dependency_for(mapper)],
    )

    recruitment_payload = copy.deepcopy(FIXTURES["recruitment"])
    recruitment_payload["score"] = recruitability
    recruitment = module_packet(
        RECRUITMENT,
        recruitment_payload,
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        subject={"asOf": recruitment_payload["asOf"]},
        depends_on=[dependency_for(hypgen)],
        native_input={"id": hypothesis_id},
    )

    tractability = module_packet(
        TRACTABILITY,
        copy.deepcopy(FIXTURES["tractability"]),
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        subject={
            "targetSymbol": "JAK1",
            "uniprotAccession": "P23458",
            "modality": "small_molecule",
        },
        depends_on=[dependency_for(hypgen)],
    )

    economics_payload = copy.deepcopy(FIXTURES["economics"])
    economics = module_packet(
        ECONOMICS,
        economics_payload,
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        depends_on=[dependency_for(hypgen), dependency_for(recruitment)],
    )
    return candidate_packet(
        hypothesis_id,
        [mapper, hypgen, recruitment, economics, tractability],
        run_id=run_id,
    )


def test_two_locked_schema_candidates_compare_end_to_end():
    weaker = hypgen_candidate("H-weaker", 0.6, 0.3)
    stronger = hypgen_candidate("H-stronger", 0.8, 0.1)
    stronger["packetHash"] = "sha256:" + stronger["packetHash"]

    payload = compare_packet_request(request([weaker, stronger])).to_dict()

    assert payload["frontier"] == ["H-stronger"]
    assert payload["dominated"] == ["H-weaker"]
    assert payload["incomparable"] == []
    assert payload["dominanceRelationships"][0]["dominatesCandidateId"] == "H-stronger"
    stronger_record = next(
        item for item in payload["candidates"] if item["candidateId"] == "H-stronger"
    )
    assert stronger_record["moduleAttempts"][0]["executionStatus"] == "SUCCEEDED"
    assert stronger_record["moduleAttempts"][0]["inputArtifactRef"].startswith(
        "artifact://"
    )
    assert set(ADAPTER_DISPATCH) == {
        "research-evidence-mapper",
        "hypothesis-generator",
        "trial-recruitment-forecaster",
        "therapeutic-program-economics",
        "small-molecule-tractability-review",
    }


def test_terminal_packet_hash_tamper_is_rejected_before_adaptation():
    candidate = hypgen_candidate("H-tampered", 0.8, 0.1)
    candidate["packetHash"] = "f" * 64

    with pytest.raises(ContractError, match="canonical terminal packet body"):
        compare_packet_request(request([candidate]))


def test_packet_revision_is_bound_by_terminal_hash():
    candidate = hypgen_candidate("H-revision", 0.8, 0.1)
    candidate["packetRevisionId"] = "forged-revision"

    with pytest.raises(ContractError, match="canonical terminal packet body"):
        compare_packet_request(request([candidate]))


def test_exact_duplicate_selected_attempt_is_idempotent():
    once = hypgen_candidate("H-idempotent", 0.8, 0.1)
    duplicated = copy.deepcopy(once)
    duplicated["modulePackets"].append(copy.deepcopy(duplicated["modulePackets"][0]))

    left = compare_packet_request(request([once])).to_dict()
    right = compare_packet_request(request([duplicated])).to_dict()

    assert left == right


def test_resolved_raw_artifact_hash_is_mandatory_and_verified():
    candidate = hypgen_candidate("H-artifact", 0.8, 0.1)
    payload = request([candidate])
    output_ref = candidate["modulePackets"][0]["outputArtifactRef"]
    payload["artifactPayloads"][output_ref] = base64.b64encode(b"tampered").decode()

    with pytest.raises(ContractError, match="outputRawSha256"):
        compare_packet_request(payload)

    del payload["artifactPayloads"]
    with pytest.raises(ContractError, match="artifactPayloads is required"):
        compare_packet_request(payload)


@pytest.mark.parametrize("identity_field", ["runId", "hypothesisId"])
def test_module_identity_must_match_terminal_candidate(identity_field):
    candidate = hypgen_candidate("H-bound", 0.8, 0.1)
    candidate["modulePackets"][0][identity_field] = "different"
    resign_module(candidate["modulePackets"][0], refresh_input_binding=False)
    rehash_candidate(candidate)

    with pytest.raises(ContractError, match=f"{identity_field} does not match"):
        compare_packet_request(request([candidate]))


def test_duplicate_selected_attempt_and_unknown_module_fail_closed():
    duplicate = hypgen_candidate("H-duplicate", 0.8, 0.1)
    second_attempt = module_packet(
        HYPOTHESIS_GENERATOR,
        copy.deepcopy(duplicate["modulePackets"][0]["payload"]),
        run_id=duplicate["runId"],
        hypothesis_id=duplicate["hypothesisId"],
        attempt_id="attempt-2",
        subject=copy.deepcopy(duplicate["modulePackets"][0]["subject"]),
    )
    duplicate["modulePackets"].append(second_attempt)
    rehash_candidate(duplicate)
    with pytest.raises(ContractError, match="conflicting attempts"):
        compare_packet_request(request([duplicate]))

    unknown = hypgen_candidate("H-unknown", 0.8, 0.1)
    unknown["modulePackets"][0]["moduleId"] = "not-a-module"
    resign_module(unknown["modulePackets"][0], refresh_input_binding=False)
    rehash_candidate(unknown)
    with pytest.raises(ContractError, match="not supported"):
        compare_packet_request(request([unknown]))


def test_nonterminal_attempt_is_rejected():
    candidate = hypgen_candidate("H-running", 0.8, 0.1)
    candidate["modulePackets"][0]["executionStatus"] = "RUNNING"
    resign_module(candidate["modulePackets"][0])
    rehash_candidate(candidate)

    with pytest.raises(ContractError, match="executionStatus is not terminal"):
        compare_packet_request(request([candidate]))


def test_cross_module_graph_lineage_mismatch_is_incomparable():
    candidate = hypgen_candidate("H-lineage", 0.8, 0.1)
    mapper_payload = copy.deepcopy(FIXTURES["mapper"])
    mapper_payload["graph_id"] = "g_other"
    mapper_payload["round"] = 7
    mapper = module_packet(
        MAPPER,
        mapper_payload,
        run_id=candidate["runId"],
        hypothesis_id=candidate["hypothesisId"],
        subject={"graphId": "g_other", "graphRound": 7},
    )
    candidate["modulePackets"][0]["dependsOn"] = [dependency_for(mapper)]
    resign_module(candidate["modulePackets"][0])
    candidate["modulePackets"].append(mapper)
    rehash_candidate(candidate)

    payload = compare_packet_request(request([candidate])).to_dict()

    assert payload["frontier"] == []
    reasons = payload["incomparable"][0]["reasons"]
    assert any("CROSS_MODULE_LINEAGE_MISMATCH" in reason for reason in reasons)


def test_downstream_module_must_bind_exact_selected_parent_output():
    candidate = hypgen_candidate("H-parent", 0.8, 0.1)
    recruitment = module_packet(
        RECRUITMENT,
        copy.deepcopy(FIXTURES["recruitment"]),
        run_id=candidate["runId"],
        hypothesis_id=candidate["hypothesisId"],
    )
    candidate["modulePackets"].append(recruitment)
    rehash_candidate(candidate)

    with pytest.raises(ContractError, match="does not bind parent output"):
        compare_packet_request(request([candidate]))

    recruitment["dependsOn"] = [
        {
            "moduleId": HYPOTHESIS_GENERATOR,
            "outputCanonicalSha256": "f" * 64,
            "envelopeCanonicalSha256": candidate["modulePackets"][0][
                "envelopeCanonicalSha256"
            ],
        }
    ]
    resign_module(recruitment)
    rehash_candidate(candidate)
    with pytest.raises(ContractError, match="mismatched parent hash"):
        compare_packet_request(request([candidate]))


@pytest.mark.parametrize("parent_status", ["FAILED", "CANCELLED", "PARTIAL"])
def test_successful_downstream_module_cannot_depend_on_unsuccessful_parent(parent_status):
    candidate = hypgen_candidate("H-failed-parent", 0.8, 0.1)
    candidate["modulePackets"][0]["executionStatus"] = parent_status
    candidate["modulePackets"][0]["executionReason"] = "upstream did not complete"
    resign_module(candidate["modulePackets"][0])
    recruitment = module_packet(
        RECRUITMENT,
        copy.deepcopy(FIXTURES["recruitment"]),
        run_id=candidate["runId"],
        hypothesis_id=candidate["hypothesisId"],
        depends_on=[dependency_for(candidate["modulePackets"][0])],
    )
    candidate["modulePackets"].append(recruitment)
    rehash_candidate(candidate)

    with pytest.raises(ContractError, match="depends on non-successful parent"):
        compare_packet_request(request([candidate]))


def test_missing_policy_objective_is_incomparable_not_imputed():
    candidate = hypgen_candidate("H-partial", 0.8, 0.1)

    payload = compare_packet_request(
        request(
            [candidate],
            objectives=[
                {"objectiveId": "support", "direction": "MAX"},
                {"objectiveId": "roi", "direction": "MAX"},
            ],
        )
    ).to_dict()

    assert payload["frontier"] == []
    assert payload["dominated"] == []
    assert payload["incomparable"] == [
        {"candidateId": "H-partial", "reasons": ["MISSING_REQUIRED_OBJECTIVE:roi"]}
    ]


def test_object_key_order_does_not_change_hash_or_result():
    candidate = hypgen_candidate("H-order", 0.8, 0.1)
    reversed_candidate = {
        key: copy.deepcopy(candidate[key]) for key in reversed(tuple(candidate.keys()))
    }
    reversed_candidate["modulePackets"][0] = {
        key: reversed_candidate["modulePackets"][0][key]
        for key in reversed(tuple(reversed_candidate["modulePackets"][0].keys()))
    }

    left = compare_packet_request(request([candidate])).to_dict()
    right_request = request([reversed_candidate])
    right_request = {key: right_request[key] for key in reversed(tuple(right_request.keys()))}
    right = compare_packet_request(right_request).to_dict()

    assert left == right


def test_outer_request_and_nonempty_module_contract_fail_closed():
    with pytest.raises(ContractError, match="schemaVersion"):
        compare_packet_request({})

    empty = candidate_packet("H-empty", [])
    with pytest.raises(ContractError, match="modulePackets must not be empty"):
        compare_packet_request(request([empty]))


def test_all_five_locked_modules_compare_end_to_end_with_exact_lineage():
    stronger = full_pipeline_candidate(
        "H-g2",
        support=0.8,
        contradiction_risk=0.1,
        recruitability=0.7,
    )
    weaker = full_pipeline_candidate(
        "H-g1",
        support=0.6,
        contradiction_risk=0.3,
        recruitability=0.4,
    )

    payload = compare_packet_request(
        request(
            [weaker, stronger],
            objectives=[
                {"objectiveId": "support", "direction": "MAX"},
                {"objectiveId": "contradiction_risk", "direction": "MIN"},
                {"objectiveId": "recruitability", "direction": "MAX"},
                {"objectiveId": "roi", "direction": "MAX"},
            ],
        )
    ).to_dict()

    assert payload["runId"] == "run-001"
    assert payload["frontier"] == ["H-g2"]
    assert payload["dominated"] == ["H-g1"]
    assert payload["incomparable"] == []
    assert {item["runId"] for item in payload["inputPackets"]} == {"run-001"}
    assert all(len(item["moduleAttempts"]) == 5 for item in payload["candidates"])
    assert all(
        any(
            observation["objectiveId"] == "tractability_posture"
            and observation["kind"] == "CATEGORICAL"
            for observation in item["observations"]
        )
        for item in payload["candidates"]
    )


def test_outputless_failure_chain_is_a_valid_terminal_ledger_not_fake_output():
    hypgen = module_packet(
        HYPOTHESIS_GENERATOR,
        None,
        run_id="run-failure",
        hypothesis_id="H-failure",
        execution_status="FAILED",
    )
    recruitment = module_packet(
        RECRUITMENT,
        None,
        run_id="run-failure",
        hypothesis_id="H-failure",
        execution_status="BLOCKED",
        depends_on=[dependency_for(hypgen)],
    )
    economics = module_packet(
        ECONOMICS,
        None,
        run_id="run-failure",
        hypothesis_id="H-failure",
        execution_status="BLOCKED",
        depends_on=[dependency_for(hypgen), dependency_for(recruitment)],
    )
    candidate = candidate_packet(
        "H-failure",
        [hypgen, recruitment, economics],
        run_id="run-failure",
    )

    payload = compare_packet_request(
        request(
            [candidate],
            objectives=[{"objectiveId": "support", "direction": "MAX"}],
        )
    ).to_dict()

    assert payload["frontier"] == []
    assert payload["incomparable"][0]["candidateId"] == "H-failure"
    attempts = payload["candidates"][0]["moduleAttempts"]
    assert len(attempts) == 3
    assert all(item["outputArtifactRef"] is None for item in attempts)
    assert all(item["outputCanonicalSha256"] is None for item in attempts)
    assert all(item["executionArtifactRef"].startswith("artifact://") for item in attempts)


def test_parent_envelope_identity_and_native_hypothesis_input_are_both_bound():
    candidate = full_pipeline_candidate(
        "H-g2",
        support=0.8,
        contradiction_risk=0.1,
        recruitability=0.7,
    )
    recruitment = next(
        item
        for item in candidate["modulePackets"]
        if item["moduleId"] == RECRUITMENT
    )
    recruitment["dependsOn"][0]["envelopeCanonicalSha256"] = "f" * 64
    resign_module(recruitment)
    rehash_candidate(candidate)
    with pytest.raises(ContractError, match="mismatched parent envelope hash"):
        compare_packet_request(request([candidate]))

    candidate = full_pipeline_candidate(
        "H-g2",
        support=0.8,
        contradiction_risk=0.1,
        recruitability=0.7,
    )
    recruitment = next(
        item
        for item in candidate["modulePackets"]
        if item["moduleId"] == RECRUITMENT
    )
    input_ref = recruitment["inputArtifactRef"]
    binding = json.loads(ARTIFACTS[input_ref])
    binding["nativeInput"]["id"] = "H-g1"
    raw = canonical_json_bytes(binding)
    ARTIFACTS[input_ref] = raw
    recruitment["inputRawSha256"] = raw_sha256(raw)
    resign_module(recruitment, refresh_input_binding=False)
    economics = next(
        item
        for item in candidate["modulePackets"]
        if item["moduleId"] == ECONOMICS
    )
    economics_recruitment_dependency = next(
        item
        for item in economics["dependsOn"]
        if item["moduleId"] == RECRUITMENT
    )
    economics_recruitment_dependency["envelopeCanonicalSha256"] = recruitment[
        "envelopeCanonicalSha256"
    ]
    resign_module(economics)
    rehash_candidate(candidate)
    with pytest.raises(ContractError, match="recruitment thesis does not bind"):
        compare_packet_request(request([candidate]))


def test_nested_economics_output_program_must_match_bound_native_input():
    candidate = full_pipeline_candidate(
        "H-g2",
        support=0.8,
        contradiction_risk=0.1,
        recruitability=0.7,
    )
    economics = next(
        item for item in candidate["modulePackets"] if item["moduleId"] == ECONOMICS
    )
    input_ref = economics["inputArtifactRef"]
    binding = json.loads(ARTIFACTS[input_ref])
    binding["nativeInput"]["cashflow_inputs"]["program_id"] = "different-program"
    binding["inputIdentity"]["programId"] = "different-program"
    input_raw = canonical_json_bytes(binding)
    ARTIFACTS[input_ref] = input_raw
    economics["inputRawSha256"] = raw_sha256(input_raw)
    resign_module(economics, refresh_input_binding=False)
    rehash_candidate(candidate)

    with pytest.raises(ContractError, match="match the native output snapshot"):
        compare_packet_request(request([candidate]))


def test_cross_run_candidates_require_an_explicit_future_policy_not_v1():
    left = hypgen_candidate("H-left", 0.6, 0.3, run_id="run-left")
    right = hypgen_candidate("H-right", 0.8, 0.1, run_id="run-right")

    with pytest.raises(ContractError, match="must share one runId"):
        compare_packet_request(request([left, right]))


def test_request_limits_and_lazy_artifact_decode_fail_closed(monkeypatch):
    candidate = hypgen_candidate("H-limits", 0.8, 0.1)
    payload = request([candidate])
    payload["artifactPayloads"]["artifact://unused"] = "not-base64"
    assert compare_packet_request(payload).to_dict()["frontier"] == ["H-limits"]

    monkeypatch.setattr("highlander.packet_consumer.MAX_ARTIFACT_BYTES", 8)
    with pytest.raises(ContractError, match="configured byte limit"):
        compare_packet_request(request([candidate]))


def test_malformed_native_output_is_candidate_local_not_request_wide():
    good = full_pipeline_candidate(
        "H-g2",
        support=0.8,
        contradiction_risk=0.1,
        recruitability=0.7,
    )
    bad = full_pipeline_candidate(
        "H-g1",
        support=0.6,
        contradiction_risk=0.3,
        recruitability=0.4,
    )
    economics = next(
        item for item in bad["modulePackets"] if item["moduleId"] == ECONOMICS
    )
    del economics["payload"]["input_snapshot"]
    output_raw = canonical_json_bytes(economics["payload"])
    ARTIFACTS[economics["outputArtifactRef"]] = output_raw
    economics["outputRawSha256"] = raw_sha256(output_raw)
    economics["outputCanonicalSha256"] = canonical_json_sha256(economics["payload"])
    resign_module(economics, refresh_input_binding=False)
    rehash_candidate(bad)

    payload = compare_packet_request(
        request(
            [bad, good],
            objectives=[
                {"objectiveId": "support", "direction": "MAX"},
                {"objectiveId": "roi", "direction": "MAX"},
            ],
        )
    ).to_dict()

    assert payload["frontier"] == ["H-g2"]
    assert payload["dominated"] == []
    assert payload["incomparable"][0]["candidateId"] == "H-g1"
    assert any(
        reason.startswith("MISSING_REQUIRED_OBJECTIVE:roi")
        or "MISSING_MODULE_RESULT" in reason
        for reason in payload["incomparable"][0]["reasons"]
    )
