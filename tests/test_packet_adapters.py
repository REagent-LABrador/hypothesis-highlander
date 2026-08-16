"""RED/GREEN coverage for locked-schema orchestrator packet consumption."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from highlander.packet_adapters import (
    ECONOMICS,
    HYPOTHESIS_GENERATOR,
    MAPPER,
    RECRUITMENT,
    TRACTABILITY,
    PRODUCER_LOCKS,
    adapt_economics,
    adapt_hypothesis_generator,
    adapt_mapper,
    adapt_recruitment,
    adapt_tractability,
    extract_hypothesis_candidates,
)
from highlander.packet_contracts import (
    ContractError,
    ModulePacket,
    Subject,
    canonical_json_bytes,
    canonical_json_sha256,
    packet_from_dict,
    raw_sha256,
)
from highlander.packet_portfolio import (
    ComparisonPolicy,
    ObjectiveRule,
    candidate_from_adapted_results,
    compare_packets,
)


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "locked_module_slices.json").read_text()
)
HYPGEN_SLATE = json.loads(
    (Path(__file__).parent / "fixtures" / "hypgen-irak4-ra.slate.json").read_text()
)


def hypothesis_document(hypothesis: dict) -> dict:
    return {
        "schema_version": "2.0",
        "provenance": {
            key: copy.deepcopy(HYPGEN_SLATE[key])
            for key in (
                "graph_id", "round", "question", "generated_at", "params", "coverage", "counts"
            )
        },
        "hypothesis": copy.deepcopy(hypothesis),
        "asks": copy.deepcopy(hypothesis.get("asks", HYPGEN_SLATE.get("asks", []))),
    }


def headless_response(document: dict) -> dict:
    return {
        "status": "COMPLETE",
        "execution_mode": "LIVE",
        "output_origin": "LIVE_PROVIDER",
        "hypothesis": copy.deepcopy(document),
        "cards": {},
        "roi_request": {
            "contract_version": "1.0.0",
            "module": "rnpv_roi_calculator",
            "request_id": "roi-test",
            "program": {},
            "comparables": [],
            "execution": {"simulations": 2, "seed": 42},
        },
        "error": None,
    }


FIXTURES["hypothesis_generator"] = headless_response(
    hypothesis_document(HYPGEN_SLATE["hypotheses"][0])
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
FIXTURES["economics_standard"] = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "economics-analysis-result-standard.json"
    ).read_text()
)

SCHEMAS = {
    module_id: (lock["nativeSchemaId"], lock["nativeSchemaVersion"])
    for module_id, lock in PRODUCER_LOCKS.items()
}


def make_packet(
    module_id: str,
    payload: dict,
    *,
    hypothesis_id: str = "H-g2",
    subject: Subject | None = None,
    status: str = "SUCCEEDED",
    basis: str = "LIVE",
    qualifiers: tuple[str, ...] = (),
) -> ModulePacket:
    schema_id, schema_version = SCHEMAS[module_id]
    output_raw = canonical_json_bytes(payload)
    return ModulePacket(
        run_id="run-001",
        hypothesis_id=hypothesis_id,
        module_id=module_id,
        attempt_id=f"attempt-{module_id}",
        native_schema_id=schema_id,
        native_schema_version=schema_version,
        producer_code_version=PRODUCER_LOCKS[module_id]["producerCodeVersion"],
        adapter_version="packet-adapters.v1",
        execution_status=status,
        execution_reason=None if status in {"SUCCEEDED", "COMPLETED", "COMPLETE"} else "test status",
        evidence_basis=basis,
        input_raw_sha256=raw_sha256(b"locked input"),
        output_raw_sha256=raw_sha256(output_raw),
        output_canonical_sha256=canonical_json_sha256(payload),
        envelope_canonical_sha256=None,
        input_artifact_ref="artifact://run-001/input.json",
        output_artifact_ref="artifact://run-001/output.json",
        execution_artifact_ref=None,
        execution_artifact_raw_sha256=None,
        dependencies=(),
        subject=subject or Subject(),
        qualifiers=qualifiers,
        payload=payload,
    )


def test_canonical_hash_ignores_key_order_and_packet_is_deeply_immutable():
    left = {"b": [{"z": 2, "a": 1}], "a": "é"}
    right = {"a": "é", "b": [{"a": 1, "z": 2}]}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_sha256(left) == canonical_json_sha256(right)

    packet = make_packet(MAPPER, left)
    with pytest.raises(TypeError):
        packet.payload["new"] = True
    with pytest.raises(TypeError):
        packet.payload["b"][0]["a"] = 9


def test_canonical_hash_uses_rfc8785_jcs_number_rules_and_ijson_domain():
    assert canonical_json_bytes({"positive": 1.0, "negativeZero": -0.0}) == (
        b'{"negativeZero":0,"positive":1}'
    )
    with pytest.raises(ContractError, match="RFC 8785"):
        canonical_json_bytes({"outsideIJsonIntegerDomain": 10**400})


def test_packet_rejects_canonical_tampering_and_can_verify_raw_artifacts():
    payload = copy.deepcopy(FIXTURES["mapper"])
    packet = make_packet(MAPPER, payload)
    packet.verify_raw_artifacts(
        input_raw=b"locked input", output_raw=canonical_json_bytes(payload)
    )
    with pytest.raises(ContractError, match="outputCanonicalSha256"):
        ModulePacket(
            run_id=packet.run_id,
            hypothesis_id=packet.hypothesis_id,
            module_id=packet.module_id,
            attempt_id=packet.attempt_id,
            native_schema_id=packet.native_schema_id,
            native_schema_version=packet.native_schema_version,
            producer_code_version=packet.producer_code_version,
            adapter_version=packet.adapter_version,
            execution_status=packet.execution_status,
            execution_reason=packet.execution_reason,
            evidence_basis=packet.evidence_basis,
            input_raw_sha256=packet.input_raw_sha256,
            output_raw_sha256=packet.output_raw_sha256,
            output_canonical_sha256="0" * 64,
            envelope_canonical_sha256=None,
            input_artifact_ref=packet.input_artifact_ref,
            output_artifact_ref=packet.output_artifact_ref,
            execution_artifact_ref=None,
            execution_artifact_raw_sha256=None,
            dependencies=(),
            subject=packet.subject,
            qualifiers=packet.qualifiers,
            payload=payload,
        )
    with pytest.raises(ContractError, match="outputRawSha256"):
        packet.verify_raw_artifacts(output_raw=b"tampered")


@pytest.mark.parametrize(
    "unsafe_ref",
    (
        "https://user:secret@example.test/output?token=secret",
        "artifact://../../etc/passwd",
        "artifact:///etc/passwd",
        "cas://sha256/value?credential=secret",
    ),
)
def test_artifact_references_cannot_expose_credentials_or_traverse(unsafe_ref):
    packet = make_packet(MAPPER, copy.deepcopy(FIXTURES["mapper"]))
    with pytest.raises(ContractError, match="artifact:// or cas://|directory"):
        replace(
            packet,
            input_artifact_ref=unsafe_ref,
            envelope_canonical_sha256=None,
        )


def test_not_wired_is_evidence_basis_not_an_execution_status():
    packet = make_packet(MAPPER, copy.deepcopy(FIXTURES["mapper"]))
    with pytest.raises(ContractError, match="unknown execution_status"):
        replace(
            packet,
            execution_status="NOT_WIRED",
            execution_reason="MODULE_NOT_WIRED",
            envelope_canonical_sha256=None,
        )


def test_camel_case_runtime_envelope_is_parsed_and_hash_checked():
    payload = copy.deepcopy(FIXTURES["mapper"])
    envelope = make_packet(
        MAPPER,
        payload,
        subject=Subject(graph_id="g_e087", graph_round=1),
        qualifiers=("LIVE",),
    ).to_envelope_dict()
    packet = packet_from_dict(envelope)
    assert packet.subject.graph_id == "g_e087"
    assert packet.qualifiers == ("LIVE",)

    envelope["runId"] = "relabeled-run"
    with pytest.raises(ContractError, match="envelopeCanonicalSha256"):
        packet_from_dict(envelope)


def test_mapper_preserves_no_effect_and_coverage_without_fabricating_objective():
    packet = make_packet(
        MAPPER,
        copy.deepcopy(FIXTURES["mapper"]),
        subject=Subject(graph_id="g_e087", graph_round=1),
    )
    result = adapt_mapper(packet)
    assert not result.quarantined
    assert result.objectives == ()
    assert result.missing_reasons == ()
    assert result.details["directionCounts"] == {"yes": 1, "no": 0, "no_effect": 1}
    assert result.details["findings"][1]["says"] == "no_effect"
    assert result.details["coverage"]["stop_reason"] == "queries_exhausted"
    assert "TRUNCATED" in result.qualifiers


def test_mapper_lineage_mismatch_is_quarantined():
    packet = make_packet(
        MAPPER,
        copy.deepcopy(FIXTURES["mapper"]),
        subject=Subject(graph_id="some-other-graph", graph_round=1),
    )
    result = adapt_mapper(packet)
    assert result.quarantined
    assert result.objectives == ()
    assert "graph_id_mismatch" in result.quarantine_reasons


def test_hypgen_retains_stable_source_id_and_all_scientific_scores():
    payload = copy.deepcopy(FIXTURES["hypothesis_generator"])
    candidates = extract_hypothesis_candidates(payload)
    assert [candidate.source_id for candidate in candidates] == ["H-g2"]
    assert candidates[0].scientific_scores["structure"] == 0.436
    assert candidates[0].scientific_scores["absence_reliability"] == 0.48

    packet = make_packet(
        HYPOTHESIS_GENERATOR,
        payload,
        subject=Subject(graph_id="g_1a4f", graph_round=2),
    )
    result = adapt_hypothesis_generator(packet)
    assert result.candidate is not None
    assert result.candidate.source_id == "H-g2"
    objectives = {item.objective_id: item for item in result.objectives}
    assert set(objectives) == {
        "support",
        "novelty",
        "testability",
        "contradiction_risk",
    }
    assert objectives["support"].raw_value == 0.512
    assert objectives["contradiction_risk"].direction == "MIN"
    assert all(item.raw_value != 0.4704 for item in result.objectives)
    assert result.candidate.raw["rank_score"] == 0.4704
    assert result.candidate.verification["verdict"] == "qualified"
    assert "TRUNCATED" in result.qualifiers


def test_hypgen_missing_required_score_yields_no_objective_not_a_neutral_value():
    payload = copy.deepcopy(FIXTURES["hypothesis_generator"])
    del payload["hypothesis"]["hypothesis"]["scores"]["support"]
    result = adapt_hypothesis_generator(
        make_packet(
            HYPOTHESIS_GENERATOR,
            payload,
            subject=Subject(graph_id="g_1a4f", graph_round=2),
        )
    )
    assert result.objectives == ()
    assert result.candidate is not None
    assert result.missing_reasons == ("missing_scientific_scores:support",)


@pytest.mark.parametrize(
    "probe",
    (
        "gates_not_array",
        "invalid_gate_status",
        "invalid_gate_issues",
        "invalid_nested_issue_severity",
        "invalid_top_level_issue_severity",
        "invalid_top_level_issue_shape",
        "invalid_gate_halting_type",
        "forbidden_verification_field",
        "verdict_disagrees_with_gates",
        "failed_halting_gate_without_halted_at",
        "non_skipped_gate_after_halt",
    ),
)
def test_hypgen_malformed_verification_or_issues_emit_no_objectives(probe):
    payload = copy.deepcopy(FIXTURES["hypothesis_generator"])
    hypothesis = payload["hypothesis"]["hypothesis"]
    verification = hypothesis["verification"]

    if probe == "gates_not_array":
        verification["gates"] = {"not": "an array"}
    elif probe == "invalid_gate_status":
        verification["gates"][0]["status"] = "green"
    elif probe == "invalid_gate_issues":
        verification["gates"][0]["issues"] = "none"
    elif probe == "invalid_nested_issue_severity":
        verification["gates"][0]["issues"] = [
            {"code": "bad", "detail": "bad", "severity": "fatal"}
        ]
    elif probe == "invalid_top_level_issue_severity":
        hypothesis["issues"][0]["severity"] = "fatal"
    elif probe == "invalid_top_level_issue_shape":
        hypothesis["issues"] = [{"severity": "error"}]
    elif probe == "invalid_gate_halting_type":
        verification["gates"][0]["halting"] = "true"
    elif probe == "forbidden_verification_field":
        verification["trust_me"] = True
    elif probe == "verdict_disagrees_with_gates":
        verification["verdict"] = "verified"
    elif probe == "failed_halting_gate_without_halted_at":
        verification["gates"][0]["status"] = "fail"
        verification["verdict"] = "rejected"
    elif probe == "non_skipped_gate_after_halt":
        verification["gates"][0]["status"] = "fail"
        verification["verdict"] = "rejected"
        verification["halted_at"] = verification["gates"][0]["name"]

    result = adapt_hypothesis_generator(
        make_packet(
            HYPOTHESIS_GENERATOR,
            payload,
            subject=Subject(graph_id="g_1a4f", graph_round=2),
        )
    )

    assert result.objectives == ()
    assert result.candidate is None
    assert "SCHEMA_ERROR" in result.qualifiers
    assert result.missing_reasons[0].startswith("malformed_native_output:")


@pytest.mark.parametrize("issue_location", ("hypothesis", "gate"))
def test_hypgen_error_severity_is_blocking_even_if_gate_issue_is_not_mirrored(
    issue_location,
):
    payload = copy.deepcopy(FIXTURES["hypothesis_generator"])
    hypothesis = payload["hypothesis"]["hypothesis"]
    issue = {"code": "blocking_probe", "detail": "cannot proceed", "severity": "error"}
    if issue_location == "hypothesis":
        hypothesis["issues"].append(issue)
    else:
        hypothesis["verification"]["gates"][0]["issues"].append(issue)

    result = adapt_hypothesis_generator(
        make_packet(
            HYPOTHESIS_GENERATOR,
            payload,
            subject=Subject(graph_id="g_1a4f", graph_round=2),
        )
    )

    assert result.objectives
    assert "BLOCKED" in result.qualifiers
    assert all("BLOCKED" in objective.qualifiers for objective in result.objectives)


def test_hypgen_issue_omitting_defaulted_severity_remains_a_warning():
    payload = copy.deepcopy(FIXTURES["hypothesis_generator"])
    payload["hypothesis"]["hypothesis"]["issues"] = [
        {"code": "warning_by_default", "detail": "locked Pydantic default"}
    ]

    result = adapt_hypothesis_generator(
        make_packet(
            HYPOTHESIS_GENERATOR,
            payload,
            subject=Subject(graph_id="g_1a4f", graph_round=2),
        )
    )

    assert result.objectives
    assert "BLOCKED" not in result.qualifiers


def test_recruitment_uses_required_native_score_and_preserves_uncertainty():
    payload = copy.deepcopy(FIXTURES["recruitment"])
    result = adapt_recruitment(make_packet(RECRUITMENT, payload, basis="MODELED"))
    assert len(result.objectives) == 1
    objective = result.objectives[0]
    assert objective.objective_id == "recruitability"
    assert objective.raw_value == 0.0
    assert objective.direction == "MAX"
    assert objective.uncertainty["simulated_months_range"] == (96.0, 1211.0)
    assert objective.uncertainty["counterfactual"]["achieves"] == "none"
    assert objective.evidence_basis == "MODELED"
    assert objective.basis["nativeSchemaId"] == (
        "https://github.com/REagent-LABrador/clinical_simulation/schemas/output.schema.json"
    )
    assert result.details["nativeBasis"]["evidence"]["competing_trials"] == 189
    assert "SIMULATED" in objective.qualifiers
    assert result.raw_payload["failed_precedents"][0]["nct_id"] == "NCT02390700"


def test_recruitment_missing_score_or_invalid_range_yields_no_objective():
    missing = copy.deepcopy(FIXTURES["recruitment"])
    del missing["score"]
    missing_result = adapt_recruitment(make_packet(RECRUITMENT, missing))
    assert missing_result.objectives == ()
    assert "payload.score" in missing_result.missing_reasons[0]

    invalid = copy.deepcopy(FIXTURES["recruitment"])
    invalid["simulated_months_range"] = [500, 100]
    invalid_result = adapt_recruitment(make_packet(RECRUITMENT, invalid))
    assert invalid_result.objectives == ()
    assert "simulated_months_range" in invalid_result.missing_reasons[0]


def test_current_recruitment_lock_rejects_legacy_camel_case_output_names():
    legacy = copy.deepcopy(FIXTURES["recruitment"])
    legacy["simulatedMonthsToEnroll"] = legacy.pop(
        "simulated_months_to_enroll"
    )
    legacy["simulatedMonthsRange"] = legacy.pop("simulated_months_range")

    result = adapt_recruitment(make_packet(RECRUITMENT, legacy))

    assert result.objectives == ()
    assert "payload.simulated_months_to_enroll" in result.missing_reasons[0]


@pytest.mark.parametrize(
    ("path", "value", "expected_error"),
    (
        (
            ("counterfactual", "achieves"),
            "excellent",
            "payload.counterfactual.achieves is outside the locked union",
        ),
        (
            ("counterfactual", "change"),
            "",
            "payload.counterfactual.change must be a non-empty string",
        ),
        (
            ("counterfactual", "simulated_months_after"),
            -1,
            "payload.counterfactual.simulated_months_after must be at least 0.0",
        ),
        (
            ("eligibility", "multiplier"),
            1.01,
            "payload.eligibility.multiplier must be at most 1.0",
        ),
        (
            ("eligibility", "cited_trials"),
            ["NCT01061736", 123],
            "payload.eligibility.cited_trials[1] must be a string",
        ),
        (
            ("eligibility", "drivers"),
            [""],
            "payload.eligibility.drivers[0] must be a non-empty string",
        ),
        (
            ("eligibility", "reasoning"),
            None,
            "payload.eligibility.reasoning must be a string",
        ),
        (
            ("evidence", "competing_trials"),
            1.5,
            "payload.evidence.competing_trials must be an integer",
        ),
        (
            ("evidence", "precedent_trials"),
            ["NCT01061736", False],
            "payload.evidence.precedent_trials[1] must be a string",
        ),
        (
            ("phase3_median_n",),
            -1,
            "payload.phase3_median_n must be at least 0.0",
        ),
        (
            ("precedent_median_n",),
            "98.5",
            "payload.precedent_median_n must be a number",
        ),
        (
            ("screens_per_enrollee",),
            0,
            "payload.screens_per_enrollee must be at least 1.0",
        ),
    ),
)
def test_recruitment_rejects_malformed_locked_nested_fields(
    path: tuple[str, ...], value: object, expected_error: str
):
    payload = copy.deepcopy(FIXTURES["recruitment"])
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    result = adapt_recruitment(make_packet(RECRUITMENT, payload))

    assert result.objectives == ()
    assert result.missing_reasons == (f"malformed_native_output:{expected_error}",)


@pytest.mark.parametrize(
    ("failed_precedents", "expected_error"),
    (
        (
            ["NCT02390700"],
            "payload.failed_precedents[0] must be an object",
        ),
        (
            [{"nct_id": "", "why_stopped": "Enrollment"}],
            "payload.failed_precedents[0].nct_id must be a non-empty string",
        ),
        (
            [{"nct_id": "NCT02390700"}],
            "payload.failed_precedents[0].why_stopped must be a string",
        ),
    ),
)
def test_recruitment_rejects_malformed_failed_precedents(
    failed_precedents: list[object], expected_error: str
):
    payload = copy.deepcopy(FIXTURES["recruitment"])
    payload["failed_precedents"] = failed_precedents

    result = adapt_recruitment(make_packet(RECRUITMENT, payload))

    assert result.objectives == ()
    assert result.missing_reasons == (f"malformed_native_output:{expected_error}",)


def test_recruitment_accepts_absent_optional_counterfactual_and_medians_losslessly():
    payload = copy.deepcopy(FIXTURES["recruitment"])
    del payload["counterfactual"]
    del payload["phase3_median_n"]
    del payload["precedent_median_n"]

    result = adapt_recruitment(make_packet(RECRUITMENT, payload))

    assert len(result.objectives) == 1
    assert result.objectives[0].uncertainty["counterfactual"] is None
    assert result.details["nativeBasis"]["phase3_median_n"] is None
    assert result.details["nativeBasis"]["precedent_median_n"] is None
    assert canonical_json_bytes(result.raw_payload) == canonical_json_bytes(payload)


def test_recruitment_uses_subject_horizon_when_optional_output_as_of_is_absent():
    payload = copy.deepcopy(FIXTURES["recruitment"])
    del payload["as_of_date"]

    result = adapt_recruitment(
        make_packet(
            RECRUITMENT,
            payload,
            subject=Subject(as_of="2026-08-15"),
        )
    )

    assert len(result.objectives) == 1
    assert result.objectives[0].basis["as_of_date"] == "2026-08-15"
    assert "HORIZON_UNSPECIFIED" not in result.objectives[0].qualifiers
    assert "as_of_date" not in result.raw_payload


def test_recruitment_marks_comparison_horizon_unspecified_when_no_as_of_exists():
    payload = copy.deepcopy(FIXTURES["recruitment"])
    del payload["as_of_date"]

    result = adapt_recruitment(make_packet(RECRUITMENT, payload))

    assert len(result.objectives) == 1
    assert result.objectives[0].basis["as_of_date"] == "UNSPECIFIED"
    assert "HORIZON_UNSPECIFIED" in result.objectives[0].qualifiers
    assert "HORIZON_UNSPECIFIED" in result.qualifiers


def test_recruitment_quarantines_conflicting_payload_and_subject_horizons():
    payload = copy.deepcopy(FIXTURES["recruitment"])

    result = adapt_recruitment(
        make_packet(
            RECRUITMENT,
            payload,
            subject=Subject(as_of="2025-08-15"),
        )
    )

    assert result.quarantined
    assert result.objectives == ()
    assert result.quarantine_reasons == ("as_of_mismatch",)


def test_economics_reads_exact_cashflow_analysis_result_130_and_preserves_basis():
    payload = copy.deepcopy(FIXTURES["economics"])
    result = adapt_economics(
        make_packet(ECONOMICS, payload, basis="MODELED", qualifiers=("ASSUMED",))
    )
    assert len(result.objectives) == 1
    objective = result.objectives[0]
    assert objective.raw_value == -15253691.618176274
    assert objective.source_path == "$.summary.p50_rnpv"
    assert objective.unit == "USD"
    assert objective.uncertainty["p10Rnpv"] == -23236281.846467756
    assert objective.uncertainty["p90Rnpv"] == -7271101.3898847895
    assert objective.uncertainty["probabilityPositiveRnpv"] == 0.0
    assert objective.basis["baseYear"] is None
    assert objective.basis["valuationYear"] == 2026
    assert objective.basis["engineVersion"] == "0.4.0"
    assert objective.basis["inputSnapshotMode"] == "cashflow_inputs"
    assert objective.basis["uncertaintyContract"]["drawOrderContractVersion"] == "1.0.0"
    assert objective.basis["simulationAssumptions"]["price_multiplier"] == {
        "low": 0.75,
        "mode": 1.0,
        "high": 1.25,
    }
    assert objective.evidence_basis == "MODELED"
    assert result.details["nativeBasis"]["seed"] == 42
    assert result.details["nativeBasis"]["simulations"] == 2
    assert "DECISION_GRADE" in objective.qualifiers
    assert "HAS_WARNINGS" not in objective.qualifiers


def test_economics_reads_exact_standard_result_without_upgrading_decision_grade():
    payload = copy.deepcopy(FIXTURES["economics_standard"])

    result = adapt_economics(make_packet(ECONOMICS, payload, basis="SYNTHETIC"))

    assert len(result.objectives) == 1
    objective = result.objectives[0]
    assert objective.raw_value == -69133586.52361934
    assert objective.basis["inputSnapshotMode"] == "program"
    assert objective.basis["baseYear"] == 2026
    assert "NOT_DECISION_GRADE" in objective.qualifiers
    assert "HAS_WARNINGS" in objective.qualifiers
    assert result.details["nativeBasis"]["programId"] == "SYNTHETIC-LAB-001"
    assert len(result.raw_payload["warnings"]) == 63


def test_economics_top_level_or_malformed_rnpv_never_becomes_neutral():
    missing_nested = copy.deepcopy(FIXTURES["economics"])
    del missing_nested["summary"]["p50_rnpv"]
    missing_nested["p50_rnpv"] = 999.0
    result = adapt_economics(make_packet(ECONOMICS, missing_nested))
    assert result.objectives == ()
    assert "payload.summary.p50_rnpv" in result.missing_reasons[0]

    inverted = copy.deepcopy(FIXTURES["economics"])
    inverted["summary"]["p10_rnpv"] = 1.0
    inverted_result = adapt_economics(make_packet(ECONOMICS, inverted))
    assert inverted_result.objectives == ()
    assert "p10 <= p50 <= p90" in inverted_result.missing_reasons[0]


def test_economics_run_id_and_decision_grade_must_match_native_result_state():
    wrong_run = copy.deepcopy(FIXTURES["economics"])
    wrong_run["run_id"] = "run_forged"
    result = adapt_economics(make_packet(ECONOMICS, wrong_run))
    assert result.objectives == ()
    assert "input_digest-derived run ID" in result.missing_reasons[0]

    impossible_grade = copy.deepcopy(FIXTURES["economics_standard"])
    impossible_grade["decision_grade"] = "DECISION_GRADE"
    impossible_grade["recommendation"] = "HOLD"
    impossible_grade["summary"]["recommendation"] = "HOLD"
    result = adapt_economics(make_packet(ECONOMICS, impossible_grade))
    assert result.objectives == ()
    assert "inconsistent with critical evidence" in result.missing_reasons[0]


def tractability_subject(**changes) -> Subject:
    values = {
        "target_symbol": "JAK1",
        "uniprot_accession": "P23458",
        "mechanism_hypothesis": None,
        "as_of": None,
        "modality": "small_molecule",
    }
    values.update(changes)
    return Subject(**values)


def test_tractability_stays_categorical_and_preserves_both_axes():
    payload = copy.deepcopy(FIXTURES["tractability"])
    result = adapt_tractability(
        make_packet(TRACTABILITY, payload, subject=tractability_subject())
    )
    assert not result.quarantined
    assert len(result.objectives) == 1
    objective = result.objectives[0]
    assert objective.objective_id == "tractability_posture"
    assert objective.kind == "CATEGORICAL"
    assert objective.direction == "CATEGORICAL"
    assert objective.raw_value == "small_molecule_tractable"
    assert objective.derived_policy_id is None
    assert objective.basis["verdictBasis"] == "retrieved_precedent"
    assert objective.basis["targetPrecedent"]["approved_small_molecules_count"] == 9
    assert objective.basis["computedTractability"]["pocket_druggability"]["load_bearing"] is False
    assert result.details["nextExperiment"]["description"].startswith(
        "Add at least one apo"
    )
    assert any(
        item["field"] == "tractability.disorder_fraction"
        for item in result.details["notFound"]
    )


def test_tractability_uses_exact_pinned_native_validator_and_quarantines_violation():
    validator_path = (
        Path(__file__).parents[1]
        / "highlander"
        / "vendor"
        / "tractability_validator.py"
    )
    assert hashlib.sha256(validator_path.read_bytes()).hexdigest() == (
        "fc84771646932cc9d570ea82fda912686d05579da5d576c288b007e5430d7490"
    )

    invalid = copy.deepcopy(FIXTURES["tractability"])
    del invalid["biologic_precedent"]
    result = adapt_tractability(
        make_packet(TRACTABILITY, invalid, subject=tractability_subject())
    )

    assert result.quarantined
    assert result.objectives == ()
    assert "NATIVE_VALIDATION_FAILED" in result.qualifiers
    assert any(
        violation["rule"] == "WELL_FORMED"
        and violation["path"] == "biologic_precedent"
        for violation in result.details["nativeValidationViolations"]
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"uniprot_accession": "Q9NWZ3"}, "uniprot_accession_mismatch"),
        ({"target_symbol": "IRAK4"}, "target_symbol_mismatch"),
        ({"mechanism_hypothesis": "orthosteric"}, "mechanism_hypothesis_mismatch"),
        ({"as_of": "2026-08-15"}, "as_of_mismatch"),
    ],
)
def test_tractability_subject_mismatches_are_quarantined(changes, reason):
    result = adapt_tractability(
        make_packet(
            TRACTABILITY,
            copy.deepcopy(FIXTURES["tractability"]),
            subject=tractability_subject(**changes),
        )
    )
    assert result.quarantined
    assert result.objectives == ()
    assert reason in result.quarantine_reasons


def test_tractability_insufficient_evidence_and_other_modalities_have_no_substitute():
    insufficient = copy.deepcopy(FIXTURES["tractability"])
    insufficient["verdict"] = "insufficient_evidence"
    insufficient["verdict_basis"] = "none"
    insufficient_result = adapt_tractability(
        make_packet(TRACTABILITY, insufficient, subject=tractability_subject())
    )
    assert insufficient_result.objectives == ()
    assert insufficient_result.missing_reasons == ("tractability:insufficient_evidence",)

    peptide_result = adapt_tractability(
        make_packet(
            TRACTABILITY,
            copy.deepcopy(FIXTURES["tractability"]),
            subject=tractability_subject(modality="peptide"),
        )
    )
    assert peptide_result.objectives == ()
    assert not peptide_result.quarantined
    assert "NOT_AMENABLE" in peptide_result.qualifiers


@pytest.mark.parametrize(
    ("module_id", "fixture_name", "adapter"),
    [
        (MAPPER, "mapper", adapt_mapper),
        (HYPOTHESIS_GENERATOR, "hypothesis_generator", adapt_hypothesis_generator),
        (RECRUITMENT, "recruitment", adapt_recruitment),
        (ECONOMICS, "economics", adapt_economics),
        (TRACTABILITY, "tractability", adapt_tractability),
    ],
)
def test_failed_module_packets_never_emit_objectives(module_id, fixture_name, adapter):
    subject = tractability_subject() if module_id == TRACTABILITY else Subject()
    packet = make_packet(
        module_id,
        copy.deepcopy(FIXTURES[fixture_name]),
        subject=subject,
        status="FAILED",
    )
    result = adapter(packet)
    assert result.objectives == ()
    assert result.missing_reasons == ("execution_status:FAILED",)
    assert canonical_json_sha256(result.raw_payload) == canonical_json_sha256(
        FIXTURES[fixture_name]
    )


@pytest.mark.parametrize(
    ("module_id", "fixture_name", "adapter"),
    [
        (MAPPER, "mapper", adapt_mapper),
        (HYPOTHESIS_GENERATOR, "hypothesis_generator", adapt_hypothesis_generator),
        (RECRUITMENT, "recruitment", adapt_recruitment),
        (ECONOMICS, "economics", adapt_economics),
        (TRACTABILITY, "tractability", adapt_tractability),
    ],
)
def test_wrong_native_schema_is_quarantined_before_payload_adaptation(
    module_id, fixture_name, adapter
):
    packet = make_packet(
        module_id,
        copy.deepcopy(FIXTURES[fixture_name]),
        subject=tractability_subject() if module_id == TRACTABILITY else Subject(),
    )
    packet = replace(
        packet,
        native_schema_version="unlocked-or-unknown",
        envelope_canonical_sha256=None,
    )

    result = adapter(packet)

    assert result.quarantined
    assert result.objectives == ()
    assert "SCHEMA_MISMATCH" in result.qualifiers
    assert any("native_schema_version_mismatch" in reason for reason in result.quarantine_reasons)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("producer_code_version", "stale-commit", "producer_code_version_mismatch"),
        ("adapter_version", "packet-adapters.unreviewed", "adapter_version_mismatch"),
    ],
)
def test_unpinned_producer_or_adapter_version_is_quarantined(field, value, reason):
    packet = make_packet(
        HYPOTHESIS_GENERATOR,
        copy.deepcopy(FIXTURES["hypothesis_generator"]),
        subject=Subject(graph_id="g_1a4f", graph_round=2),
    )

    result = adapt_hypothesis_generator(
        replace(packet, **{field: value, "envelope_canonical_sha256": None})
    )

    assert result.quarantined
    assert result.objectives == ()
    assert "VERSION_MISMATCH" in result.qualifiers
    assert any(reason in item for item in result.quarantine_reasons)


def test_economics_rejects_invalid_decision_grade_or_recommendation_union():
    invalid_grade = copy.deepcopy(FIXTURES["economics"])
    invalid_grade["decision_grade"] = "BANANA"
    result = adapt_economics(make_packet(ECONOMICS, invalid_grade))
    assert result.objectives == ()
    assert "decision_grade" in result.missing_reasons[0]

    inconsistent = copy.deepcopy(FIXTURES["economics"])
    inconsistent["recommendation"] = "HOLD"
    result = adapt_economics(make_packet(ECONOMICS, inconsistent))
    assert result.objectives == ()
    assert "does not match" in result.missing_reasons[0]


def test_real_adapter_observations_compare_without_candidate_specific_provenance_splitting():
    stronger = copy.deepcopy(
        FIXTURES["hypothesis_generator"]["hypothesis"]["hypothesis"]
    )
    stronger["scores"]["support"] = 0.8
    stronger["scores"]["contradiction_risk"] = 0.1
    weaker = copy.deepcopy(stronger)
    weaker["id"] = "H-g3"
    weaker["scores"]["support"] = 0.6
    weaker["scores"]["contradiction_risk"] = 0.3
    documents = {
        "H-g2": headless_response(hypothesis_document(stronger)),
        "H-g3": headless_response(hypothesis_document(weaker)),
    }
    subject = Subject(graph_id="g_1a4f", graph_round=2)

    adapted_by_id = {}
    for hypothesis_id in ("H-g2", "H-g3"):
        hypgen = adapt_hypothesis_generator(
            make_packet(
                HYPOTHESIS_GENERATOR,
                documents[hypothesis_id],
                hypothesis_id=hypothesis_id,
                subject=subject,
            )
        )
        economics = copy.deepcopy(FIXTURES["economics"])
        roi = adapt_economics(
            make_packet(ECONOMICS, economics, hypothesis_id=hypothesis_id)
        )
        adapted_by_id[hypothesis_id] = candidate_from_adapted_results(
            (hypgen, roi),
            packet_revision_id=f"packet-{hypothesis_id}-r1",
            packet_hash=("a" if hypothesis_id == "H-g2" else "b") * 64,
        )

    result = compare_packets(
        adapted_by_id.values(),
        ComparisonPolicy(
            "locked-native-comparison.v1",
            (
                ObjectiveRule("support", "MAX"),
                ObjectiveRule("contradiction_risk", "MIN"),
                ObjectiveRule("roi", "MAX"),
            ),
        ),
        snapshot_id="snapshot-native-comparison",
        created_at="2026-08-16T12:00:00Z",
    ).to_dict()

    assert result["frontier"] == ["H-g2"]
    assert result["dominated"] == ["H-g3"]
    assert result["incomparable"] == []
