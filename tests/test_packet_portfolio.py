"""Regression and metamorphic tests for the packet portfolio comparator."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from highlander.packet_contracts import (
    AdaptedModuleResult,
    ObjectiveObservation as ContractObservation,
)
from highlander.packet_portfolio import (
    ComparisonPolicy,
    ObjectiveRule,
    PortfolioCandidate,
    PortfolioObservation,
    candidate_from_adapted_results,
    compare_packets,
)


STAMP = "2026-08-16T12:00:00Z"
HASHES = {
    "a": "sha256:" + "a" * 64,
    "b": "sha256:" + "b" * 64,
    "c": "sha256:" + "c" * 64,
    "d": "sha256:" + "d" * 64,
    "e": "sha256:" + "e" * 64,
}
POLICY = ComparisonPolicy(
    policy_id="locked-schema-v1",
    objectives=(
        ObjectiveRule("contradiction_risk", "MIN"),
        ObjectiveRule("support", "MAX"),
    ),
)


def obs(
    objective_id,
    value,
    direction,
    *,
    unit="score_0_1",
    basis=None,
    evidence_basis="LIVE",
    uncertainty=None,
    qualifiers=(),
):
    return PortfolioObservation(
        objective_id=objective_id,
        value=value,
        direction=direction,
        unit=unit,
        basis=basis or {"producerPolicy": "hyp-gen.locked-v1"},
        evidence_basis=evidence_basis,
        uncertainty=uncertainty,
        source_path=f"scores.{objective_id}",
        source_schema_id="hyp_gen.schema.Slate",
        source_schema_version="locked",
        source_hash="sha256:" + "e" * 64,
        qualifiers=qualifiers,
    )


def candidate(candidate_id, support, risk, **kwargs):
    observations = kwargs.pop(
        "observations",
        (
            obs("support", support, "MAX"),
            obs("contradiction_risk", risk, "MIN"),
        ),
    )
    return PortfolioCandidate(
        candidate_id=candidate_id,
        packet_revision_id=f"packet-{candidate_id}-r1",
        packet_hash=HASHES[candidate_id],
        observations=observations,
        **kwargs,
    )


def adapted_result(
    candidate_id="a",
    *,
    run_id="run-001",
    module_id="hypothesis-generator",
    attempt_id="attempt-1",
    qualifiers=(),
):
    return AdaptedModuleResult(
        run_id=run_id,
        module_id=module_id,
        hypothesis_id=candidate_id,
        attempt_id=attempt_id,
        packet_sha256="e" * 64,
        attempt_record={
            "runId": run_id,
            "hypothesisId": candidate_id,
            "moduleId": module_id,
            "attemptId": attempt_id,
            "executionStatus": "SUCCEEDED",
        },
        objectives=(
            obs("support", 0.8, "MAX", qualifiers=qualifiers),
            obs("contradiction_risk", 0.2, "MIN", qualifiers=qualifiers),
        ),
        qualifiers=qualifiers,
    )


def compare(items, policy=POLICY):
    return compare_packets(
        items,
        policy,
        snapshot_id="snapshot-locked-001",
        created_at=STAMP,
    )


def relations(result):
    return {
        (item["dominatesCandidateId"], item["dominatedCandidateId"])
        for item in result.to_dict()["dominanceRelationships"]
    }


def test_direction_aware_pareto_preserves_raw_snapshot_and_has_no_winner():
    result = compare([candidate("b", 0.7, 0.4), candidate("a", 0.8, 0.2)])
    payload = result.to_dict()

    assert payload["schemaVersion"] == "highlander.portfolio-result.v1"
    assert payload["objectivePolicyId"] == "locked-schema-v1"
    assert payload["snapshotId"] == "snapshot-locked-001"
    assert payload["frontier"] == ["a"]
    assert payload["dominated"] == ["b"]
    assert payload["incomparable"] == []
    assert relations(result) == {("a", "b")}
    assert [item["candidateId"] for item in payload["inputPackets"]] == ["a", "b"]
    assert payload["inputPackets"][0]["packetHash"] == HASHES["a"]
    support = next(
        item
        for item in payload["candidates"][0]["observations"]
        if item["objectiveId"] == "support"
    )
    assert support["rawValue"] == 0.8
    assert support["direction"] == "MAX"
    assert support["sourcePath"] == "scores.support"
    assert "winner" not in payload
    assert "top" not in payload
    assert "NO_GLOBAL_WINNER" in payload["qualifiers"]


def test_complete_singleton_is_nondominated_and_its_group_is_emitted():
    payload = compare([candidate("a", 0.8, 0.2)]).to_dict()

    assert payload["frontier"] == ["a"]
    assert payload["dominated"] == []
    assert payload["incomparable"] == []
    assert len(payload["comparisonGroups"]) == 1
    assert payload["comparisonGroups"][0]["candidateIds"] == ["a"]
    assert payload["comparisonGroups"][0]["frontier"] == ["a"]
    assert payload["candidates"][0]["comparisonGroupId"] == payload[
        "comparisonGroups"
    ][0]["comparisonGroupId"]


def test_partial_vector_cannot_dominate_complete_vectors():
    partial = candidate(
        "a",
        1.0,
        0.0,
        observations=(obs("support", 1.0, "MAX"),),
    )
    strongest_complete = candidate("b", 0.8, 0.2)
    weaker_complete = candidate("c", 0.6, 0.4)

    payload = compare([partial, weaker_complete, strongest_complete]).to_dict()

    assert payload["frontier"] == ["b"]
    assert payload["dominated"] == ["c"]
    assert payload["incomparable"] == [
        {"candidateId": "a", "reasons": ["MISSING_REQUIRED_OBJECTIVE:contradiction_risk"]}
    ]
    assert all(
        relation["dominatesCandidateId"] != "a"
        and relation["dominatedCandidateId"] != "a"
        for relation in payload["dominanceRelationships"]
    )


def test_mixed_unit_basis_and_evidence_basis_never_compare():
    same_basis_a = candidate("a", 0.8, 0.2)
    same_basis_b = candidate("b", 0.7, 0.3)
    different_unit = candidate(
        "c",
        80,
        0.1,
        observations=(
            obs("support", 80, "MAX", unit="percent"),
            obs("contradiction_risk", 0.1, "MIN"),
        ),
    )
    different_basis = candidate(
        "d",
        0.99,
        0.01,
        observations=(
            obs(
                "support",
                0.99,
                "MAX",
                basis={"producerPolicy": "hyp-gen.different-locked-policy"},
            ),
            obs(
                "contradiction_risk",
                0.01,
                "MIN",
                basis={"producerPolicy": "hyp-gen.different-locked-policy"},
            ),
        ),
    )
    different_evidence_basis = candidate(
        "e",
        0.99,
        0.01,
        observations=(
            obs("support", 0.99, "MAX", evidence_basis="PROXY"),
            obs("contradiction_risk", 0.01, "MIN", evidence_basis="PROXY"),
        ),
    )

    payload = compare(
        [
            different_unit,
            same_basis_b,
            different_basis,
            different_evidence_basis,
            same_basis_a,
        ]
    ).to_dict()

    assert payload["frontier"] == []
    assert payload["dominated"] == ["b"]
    assert [item["candidateId"] for item in payload["incomparable"]] == [
        "a",
        "c",
        "d",
        "e",
    ]
    assert relations(compare([same_basis_a, same_basis_b])) == {("a", "b")}
    assert all(
        "c" not in (item["dominatesCandidateId"], item["dominatedCandidateId"])
        and "d" not in (item["dominatesCandidateId"], item["dominatedCandidateId"])
        and "e" not in (item["dominatesCandidateId"], item["dominatedCandidateId"])
        for item in payload["dominanceRelationships"]
    )
    assert "MULTIPLE_COMPARISON_BASES" in payload["qualifiers"]


def test_distinct_tied_candidates_are_preserved_in_equivalence_group():
    tied_a = candidate("a", 0.8, 0.2)
    tied_b = candidate("b", 0.8, 0.2)
    weaker = candidate("c", 0.7, 0.3)

    payload = compare([tied_b, weaker, tied_a]).to_dict()

    assert payload["frontier"] == ["a", "b"]
    assert payload["dominated"] == ["c"]
    assert relations(compare([tied_b, weaker, tied_a])) == {("a", "c"), ("b", "c")}
    assert len(payload["candidates"]) == 3
    assert len(payload["equivalenceGroups"]) == 1
    assert payload["equivalenceGroups"][0]["candidateIds"] == ["a", "b"]


def test_output_is_stable_under_input_and_policy_permutation():
    items = [candidate("a", 0.8, 0.2), candidate("b", 0.7, 0.3), candidate("c", 0.6, 0.4)]
    reversed_policy = ComparisonPolicy(
        policy_id=POLICY.policy_id,
        objectives=tuple(reversed(POLICY.objectives)),
    )

    forward = compare(items, POLICY).to_dict()
    permuted = compare(list(reversed(items)), reversed_policy).to_dict()

    assert forward == permuted


def test_metamorphic_max_improvement_cannot_worsen_dominance():
    baseline_a = candidate("a", 0.4, 0.2)
    improved_a = candidate("a", 0.6, 0.2)
    reference = candidate("b", 0.5, 0.2)

    before = compare([baseline_a, reference])
    after = compare([improved_a, reference])

    assert ("b", "a") in relations(before)
    assert ("b", "a") not in relations(after)
    assert ("a", "b") in relations(after)


def test_metamorphic_min_improvement_cannot_worsen_dominance():
    baseline_a = candidate("a", 0.5, 0.4)
    improved_a = candidate("a", 0.5, 0.2)
    reference = candidate("b", 0.5, 0.3)

    before = compare([baseline_a, reference])
    after = compare([improved_a, reference])

    assert ("b", "a") in relations(before)
    assert ("b", "a") not in relations(after)
    assert ("a", "b") in relations(after)


def test_mapping_and_object_protocol_accepts_packet_contract_shapes():
    object_observation = SimpleNamespace(
        objective_id="support",
        raw_value=0.8,
        direction="max",
        unit="score_0_1",
        comparison_basis={"producerPolicy": "hyp-gen.locked-v1"},
        evidence_basis="live",
        qualifiers=("VERIFIED",),
    )
    object_packet = SimpleNamespace(
        hypothesis_id="a",
        packet_revision_id="packet-a-r1",
        packet_sha256=HASHES["a"],
        observations=(
            object_observation,
            {
                "objectiveId": "contradiction_risk",
                "rawValue": 0.2,
                "direction": "MIN",
                "unit": "score_0_1",
                "comparisonBasis": {"producerPolicy": "hyp-gen.locked-v1"},
                "evidenceBasis": "LIVE",
            },
        ),
    )
    mapping_packet = {
        "hypothesisId": "b",
        "packetRevisionId": "packet-b-r1",
        "packetHash": HASHES["b"],
        "observations": [
            {
                "objectiveId": "support",
                "rawValue": 0.7,
                "direction": "MAX",
                "unit": "score_0_1",
                "basis": {"producerPolicy": "hyp-gen.locked-v1"},
                "evidenceBasis": "LIVE",
            },
            {
                "objectiveId": "contradiction_risk",
                "rawValue": 0.3,
                "direction": "MIN",
                "unit": "score_0_1",
                "basis": {"producerPolicy": "hyp-gen.locked-v1"},
                "evidenceBasis": "LIVE",
            },
        ],
    }

    payload = compare([mapping_packet, object_packet]).to_dict()

    assert payload["frontier"] == ["a"]
    assert payload["dominated"] == ["b"]
    assert "VERIFIED" in payload["qualifiers"]


def test_adapted_result_helper_matches_packet_contract_fields_and_frozen_json():
    contract_observation = ContractObservation(
        objective_id="support",
        kind="NUMERIC",
        direction="MAX",
        raw_value=0.8,
        unit="score_0_1",
        uncertainty={"coverage": {"truncated": False}},
        basis={"producerPolicy": "hyp-gen.locked-v1"},
        source_path="hypotheses[0].scores.support",
        native_schema_id="hyp_gen.schema.Slate",
        native_schema_version="locked",
        packet_sha256="e" * 64,
        evidence_basis="LIVE",
        qualifiers=("LIVE", "VERIFIED"),
        derived_policy_id=None,
    )
    risk_observation = ContractObservation(
        objective_id="contradiction_risk",
        kind="NUMERIC",
        direction="MIN",
        raw_value=0.2,
        unit="score_0_1",
        uncertainty={},
        basis={"producerPolicy": "hyp-gen.locked-v1"},
        source_path="hypotheses[0].scores.contradiction_risk",
        native_schema_id="hyp_gen.schema.Slate",
        native_schema_version="locked",
        packet_sha256="e" * 64,
        evidence_basis="LIVE",
        qualifiers=("LIVE",),
        derived_policy_id=None,
    )
    adapted = AdaptedModuleResult(
        run_id="run-001",
        module_id="hypothesis-generator",
        hypothesis_id="a",
        attempt_id="attempt-1",
        packet_sha256="e" * 64,
        attempt_record={
            "moduleId": "hypothesis-generator",
            "attemptId": "attempt-1",
            "executionStatus": "SUCCEEDED",
        },
        objectives=(contract_observation, risk_observation),
        qualifiers=("LIVE",),
        missing_reasons=(),
        quarantined=False,
        quarantine_reasons=(),
    )
    aggregate = candidate_from_adapted_results(
        [adapted], packet_revision_id="packet-a-r1", packet_hash=HASHES["a"]
    )
    reference = candidate("b", 0.7, 0.3)

    payload = compare([reference, aggregate]).to_dict()

    assert payload["frontier"] == ["a"]
    assert payload["dominated"] == ["b"]
    support = next(
        item
        for item in payload["candidates"][0]["observations"]
        if item["objectiveId"] == "support"
    )
    assert support["kind"] == "NUMERIC"
    assert support["sourceSchemaVersion"] == "locked"
    assert support["sourceHash"] == "e" * 64


def test_categorical_observation_is_preserved_but_cannot_be_numeric_policy_axis():
    categorical = PortfolioObservation(
        objective_id="tractability_posture",
        value="insufficient_evidence",
        direction="CATEGORICAL",
        unit="category",
        basis={"dossierContract": "locked"},
        evidence_basis="LIVE",
        kind="CATEGORICAL",
        qualifiers=("INSUFFICIENT_EVIDENCE",),
    )
    numeric_policy = ComparisonPolicy(
        policy_id="invalid-categorical-policy",
        objectives=(ObjectiveRule("tractability_posture", "MAX"),),
    )
    a = candidate("a", 0, 0, observations=(categorical,))
    b = candidate("b", 0, 0, observations=(categorical,))

    payload = compare([a, b], numeric_policy).to_dict()

    assert payload["frontier"] == []
    assert payload["dominated"] == []
    assert [item["candidateId"] for item in payload["incomparable"]] == ["a", "b"]
    assert all(
        "NON_NUMERIC_OBJECTIVE_KIND:tractability_posture" in item["reasons"]
        for item in payload["incomparable"]
    )
    assert payload["candidates"][0]["observations"][0]["rawValue"] == "insufficient_evidence"


def test_uncertainty_is_preserved_but_dominance_is_explicitly_nominal():
    uncertain = candidate(
        "a",
        0.8,
        0.2,
        observations=(
            obs("support", 0.8, "MAX", uncertainty={"low": 0.5, "high": 0.9}),
            obs("contradiction_risk", 0.2, "MIN"),
        ),
    )
    reference = candidate("b", 0.7, 0.3)

    payload = compare([uncertain, reference]).to_dict()

    assert payload["dominanceRelationships"][0]["kind"] == "NOMINAL"
    assert payload["dominanceRelationships"][0]["qualifiers"] == [
        "UNCERTAINTY_NOT_USED_IN_NOMINAL_DOMINANCE"
    ]
    assert "UNCERTAINTY_NOT_USED_IN_NOMINAL_DOMINANCE" in payload["qualifiers"]
    support = next(
        item
        for item in payload["candidates"][0]["observations"]
        if item["objectiveId"] == "support"
    )
    assert support["uncertainty"] == {"high": 0.9, "low": 0.5}


def test_exact_duplicate_is_idempotent_but_conflicting_duplicate_fails_closed():
    original = candidate("a", 0.8, 0.2)
    reference = candidate("b", 0.7, 0.3)

    once = compare([original, reference]).to_dict()
    repeated = compare([original, reference, original]).to_dict()
    assert once == repeated

    conflict = replace(
        original,
        observations=(obs("support", 0.1, "MAX"), obs("contradiction_risk", 0.2, "MIN")),
    )
    with pytest.raises(ValueError, match="conflicting packet snapshots"):
        compare([original, reference, conflict])


def test_exclusions_and_qualifiers_remain_structured():
    excluded = candidate(
        "a",
        0.99,
        0.01,
        qualifiers=("NOT_DECISION_GRADE",),
        exclusion_reasons=("ECONOMICS_NOT_DECISION_GRADE",),
    )
    reference = candidate("b", 0.7, 0.3)
    third = candidate("c", 0.6, 0.4)

    payload = compare([excluded, reference, third]).to_dict()

    assert payload["frontier"] == ["b"]
    assert payload["dominated"] == ["c"]
    assert payload["incomparable"] == [
        {"candidateId": "a", "reasons": ["EXCLUDED:ECONOMICS_NOT_DECISION_GRADE"]}
    ]
    assert "NOT_DECISION_GRADE" in payload["qualifiers"]


def test_recognized_additional_evidence_bases_form_separate_comparison_groups():
    assumed_a = candidate(
        "a",
        0.8,
        0.2,
        observations=(
            obs("support", 0.8, "MAX", qualifiers=("ASSUMED",)),
            obs("contradiction_risk", 0.2, "MIN", qualifiers=("ASSUMED",)),
        ),
    )
    plain = candidate("b", 0.9, 0.1)
    assumed_c = candidate(
        "c",
        0.7,
        0.3,
        observations=(
            obs("support", 0.7, "MAX", qualifiers=("ASSUMED",)),
            obs("contradiction_risk", 0.3, "MIN", qualifiers=("ASSUMED",)),
        ),
    )

    payload = compare([plain, assumed_c, assumed_a]).to_dict()

    assert payload["frontier"] == []
    assert payload["dominated"] == ["c"]
    assert [item["candidateId"] for item in payload["incomparable"]] == ["a", "b"]
    assumed_support = next(
        item
        for item in payload["candidates"][0]["observations"]
        if item["objectiveId"] == "support"
    )
    assert assumed_support["evidenceBases"] == ["ASSUMED", "LIVE"]


def test_default_eligibility_policy_excludes_unverified_and_not_decision_grade():
    for qualifier in ("UNVERIFIED", "REJECTED", "NOT_DECISION_GRADE", "BLOCKED"):
        aggregate = candidate_from_adapted_results(
            [adapted_result(qualifiers=(qualifier,))],
            packet_revision_id=f"packet-{qualifier}",
            packet_hash="a" * 64,
        )
        payload = compare([aggregate, candidate("b", 0.7, 0.3)]).to_dict()
        reasons = next(
            item["reasons"]
            for item in payload["incomparable"]
            if item["candidateId"] == "a"
        )
        if qualifier == "NOT_DECISION_GRADE":
            assert f"OBJECTIVE_QUALIFIER:support:{qualifier}" in reasons
            assert qualifier in payload["objectivePolicy"][
                "excludedObjectiveQualifiers"
            ]
        else:
            assert f"EXCLUDED:QUALIFIER:{qualifier}" in reasons
            assert qualifier in payload["objectivePolicy"][
                "excludedCandidateQualifiers"
            ]


def test_adapted_attempt_duplicates_are_idempotent_and_conflicts_fail_closed():
    adapted = adapted_result()
    once = candidate_from_adapted_results(
        [adapted], packet_revision_id="packet-a", packet_hash="a" * 64
    )
    repeated = candidate_from_adapted_results(
        [adapted, adapted], packet_revision_id="packet-a", packet_hash="a" * 64
    )
    assert once == repeated
    assert repeated.module_attempts[0]["executionStatus"] == "SUCCEEDED"

    with pytest.raises(ValueError, match="conflicting selected adapted results"):
        candidate_from_adapted_results(
            [adapted, replace(adapted, attempt_id="attempt-2")],
            packet_revision_id="packet-a",
            packet_hash="a" * 64,
        )

    other_run = adapted_result(
        run_id="run-002", module_id="therapeutic-program-economics"
    )
    with pytest.raises(ValueError, match="exactly one run"):
        candidate_from_adapted_results(
            [adapted, other_run],
            packet_revision_id="packet-a",
            packet_hash="a" * 64,
        )


def test_huge_numeric_value_is_incomparable_not_an_overflow_crash():
    huge = candidate(
        "a",
        0,
        0.2,
        observations=(
            obs("support", 10**400, "MAX"),
            obs("contradiction_risk", 0.2, "MIN"),
        ),
    )

    payload = compare([huge, candidate("b", 0.7, 0.3)]).to_dict()

    reasons = next(
        item["reasons"]
        for item in payload["incomparable"]
        if item["candidateId"] == "a"
    )
    assert "NON_FINITE_VALUE:support" in reasons


def test_portfolio_result_nested_state_is_immutable_but_to_dict_is_detached():
    result = compare([candidate("a", 0.8, 0.2), candidate("b", 0.7, 0.3)])

    with pytest.raises(TypeError):
        result.candidates[0]["comparisonStatus"] = "FORGED"
    with pytest.raises(TypeError):
        result.objective_policy["policyId"] = "FORGED"

    detached = result.to_dict()
    detached["candidates"][0]["comparisonStatus"] = "FORGED"
    assert result.to_dict()["candidates"][0]["comparisonStatus"] == "FRONTIER"
