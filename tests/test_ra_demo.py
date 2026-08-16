"""Functional coverage for the scoped 3-biomarker/9-hypothesis RA demo path."""
from __future__ import annotations

from copy import deepcopy
import io
import json

import pytest

from highlander.__main__ import main
from highlander.packet_contracts import ContractError
from highlander.ra_demo import compare_ra_demo_snapshot


BIOMARKERS = (
    ("t2", "myeloid inflammatory signalling"),
    ("t3", "synovial fibroblast driven inflammation"),
    ("t5", "TLR/MyD88/NF-kB signalling axis"),
)
HYPOTHESES = (
    ("H-g2", 51.2, 47.0),
    ("H-g1", 42.3, 36.7),
    ("H-g4", 42.3, 38.9),
)


def ra_snapshot() -> dict:
    cards = [
        {
            "id": source_id,
            "headline": f"RA hypothesis {source_id}",
            "metrics": {
                "support": evidence / 100,
                "novelty": {"H-g2": 0.245, "H-g1": 0.228, "H-g4": 0.245}[
                    source_id
                ],
                "testability": 0.7,
                "rank": plausibility / 100,
            },
            "status": {"verification": "qualified", "flags": []},
        }
        for source_id, evidence, plausibility in HYPOTHESES
    ]
    programs = []
    lane = 0
    for biomarker_slot, (biomarker_id, biomarker_label) in enumerate(BIOMARKERS):
        for hypothesis_slot, (source_id, evidence, plausibility) in enumerate(
            HYPOTHESES
        ):
            programs.append(
                {
                    "id": f"ctx-{biomarker_id}--{source_id}",
                    "source_hypothesis_id": source_id,
                    "biomarker_graph_thing_id": biomarker_id,
                    "association_basis": (
                        "SOURCE_PATH" if (biomarker_slot + hypothesis_slot) % 2 == 0
                        else "CONTEXT_ONLY"
                    ),
                    "lane": lane,
                    "biomarker_slot": biomarker_slot,
                    "hypothesis_slot": hypothesis_slot,
                    "label": f"{source_id} · {biomarker_label}",
                    "metrics": {
                        "boldness": 5.5,
                        "evidence": evidence,
                        "plausibility": plausibility,
                        "rnpv": -24.0,
                        "positive": 0.0,
                        "impact": None,
                        "recruit": 0.0,
                        "duration": 398,
                        "screens": 4,
                        "risk": 100.0,
                        "support": None,
                        "occupancy": None,
                        "convergence": None,
                    },
                    "revision": "packet-r13",
                    # The current projection repeats one shared hash on all nine lanes.
                    "hash": "sha256:" + "a" * 64,
                    "station_payloads": {
                        "hypothesis": {
                            "schema_version": "1.0",
                            "graph_id": "g_1a4f",
                            "hypotheses": deepcopy(cards),
                        },
                        "roi": {"shared": True},
                        "recruitability": {"shared": True},
                        "simulation": {"shared": True},
                    },
                }
            )
            lane += 1
    return {
        "run_id": "LR-20260816-101620-44C9",
        "updated_at": "2026-08-16T10:16:21.320Z",
        "last_event_id": 13,
        "stages": [],
        "biomarkers": [
            {
                "slot": slot,
                "graph_thing_id": biomarker_id,
                "label": label,
                "summary": "Evidence-graph candidate; not a validated biomarker.",
                "station_payload": {"ignored_by_demo_adapter": True},
            }
            for slot, (biomarker_id, label) in enumerate(BIOMARKERS)
        ],
        "programs": programs,
        "highlander_ready": True,
        "highlander_blocked_reason": None,
    }


def test_ra_demo_compares_all_nine_hypotheses_and_retains_ties():
    payload = compare_ra_demo_snapshot(ra_snapshot())

    assert payload["schemaVersion"] == "highlander.ra-demo-result.v1"
    assert payload["mode"] == "RA_DEMO_MINIMAL"
    assert payload["scope"]["biomarkerCount"] == 3
    assert payload["scope"]["hypothesisCount"] == 9
    assert len(payload["biomarkers"]) == 3
    assert len(payload["hypotheses"]) == 9
    assert {
        (item["biomarkerGraphThingId"], item["sourceHypothesisId"])
        for item in payload["hypotheses"]
    } == {
        (biomarker_id, source_id)
        for biomarker_id, _ in BIOMARKERS
        for source_id, _, _ in HYPOTHESES
    }

    portfolio = payload["portfolio"]
    assert portfolio["frontier"] == [
        "ctx-t2--H-g2",
        "ctx-t3--H-g2",
        "ctx-t5--H-g2",
    ]
    assert len(portfolio["dominated"]) == 6
    assert portfolio["incomparable"] == []
    assert len(portfolio["equivalenceGroups"]) == 3
    assert all(len(group["candidateIds"]) == 3 for group in portfolio["equivalenceGroups"])
    assert len({item["packetHash"] for item in portfolio["inputPackets"]}) == 9
    assert {
        observation["sourceSchemaId"]
        for candidate in portfolio["candidates"]
        for observation in candidate["observations"]
    } == {"https://labrador.dev/schemas/cards.schema.json"}
    assert "winner" not in portfolio
    assert "top" not in portfolio


def test_shared_downstream_values_do_not_change_demo_comparison():
    baseline = ra_snapshot()
    changed = deepcopy(baseline)
    changed["programs"][0]["metrics"]["rnpv"] = 999_999.0
    changed["programs"][0]["metrics"]["recruit"] = 100.0

    before = compare_ra_demo_snapshot(baseline)
    after = compare_ra_demo_snapshot(changed)

    assert before["sourceSnapshot"] == after["sourceSnapshot"]
    assert before["portfolio"] == after["portfolio"]
    assert "programs[].metrics.rnpv" in before["scope"][
        "sharedDownstreamMetricsNotRanked"
    ]
    assert "programs[].metrics.recruit" in before["scope"][
        "sharedDownstreamMetricsNotRanked"
    ]


def test_display_rank_does_not_change_scientific_comparison():
    baseline = ra_snapshot()
    changed = deepcopy(baseline)
    for program in changed["programs"]:
        for card in program["station_payloads"]["hypothesis"]["hypotheses"]:
            card["metrics"]["rank"] = 0.999
        program["metrics"]["plausibility"] = 99.9

    before = compare_ra_demo_snapshot(baseline)
    after = compare_ra_demo_snapshot(changed)

    assert before["portfolio"]["frontier"] == after["portfolio"]["frontier"]
    assert before["portfolio"]["dominated"] == after["portfolio"]["dominated"]
    assert before["scope"]["displayRankFieldsNotUsed"] == [
        "programs[].metrics.plausibility",
        "station_payloads.hypothesis.hypotheses[].metrics.rank",
    ]


@pytest.mark.parametrize(
    "mutate, message",
    (
        (
            lambda value: value["programs"].pop(),
            "exactly 9 hypotheses",
        ),
        (
            lambda value: value["programs"][0]["metrics"].pop("evidence"),
            "metrics.evidence must be a number",
        ),
        (
            lambda value: value.update(
                highlander_ready=False,
                highlander_blocked_reason="run is still active",
            ),
            "snapshot is not ready for Highlander: run is still active",
        ),
    ),
)
def test_ra_demo_fails_closed_for_incomplete_snapshot(mutate, message):
    snapshot = ra_snapshot()
    mutate(snapshot)

    with pytest.raises(ContractError, match=message):
        compare_ra_demo_snapshot(snapshot)


def test_ra_demo_rejects_projected_evidence_that_disagrees_with_native_support():
    snapshot = ra_snapshot()
    snapshot["programs"][0]["metrics"]["evidence"] = 12.3

    with pytest.raises(ContractError, match="does not match native support"):
        compare_ra_demo_snapshot(snapshot)


@pytest.mark.parametrize("verification", ("rejected", None))
def test_rejected_or_unverified_native_card_cannot_enter_frontier(verification):
    snapshot = ra_snapshot()
    for program in snapshot["programs"]:
        for card in program["station_payloads"]["hypothesis"]["hypotheses"]:
            if card["id"] == "H-g2":
                card["status"]["verification"] = verification

    payload = compare_ra_demo_snapshot(snapshot)["portfolio"]

    rejected_ids = {f"ctx-{biomarker_id}--H-g2" for biomarker_id, _ in BIOMARKERS}
    assert rejected_ids.isdisjoint(payload["frontier"])
    assert {item["candidateId"] for item in payload["incomparable"]} == rejected_ids
    assert all(
        item["reasons"]
        == [
            f"EXCLUDED:HYPOTHESIS_VERIFICATION:"
            f"{'UNVERIFIED' if verification is None else 'REJECTED'}"
        ]
        for item in payload["incomparable"]
    )


def test_native_must_not_miss_flag_cannot_enter_frontier_and_is_preserved():
    snapshot = ra_snapshot()
    for program in snapshot["programs"]:
        for card in program["station_payloads"]["hypothesis"]["hypotheses"]:
            if card["id"] == "H-g2":
                card["status"]["flags"] = ["BLOCKED"]

    result = compare_ra_demo_snapshot(snapshot)
    portfolio = result["portfolio"]

    blocked_ids = {f"ctx-{biomarker_id}--H-g2" for biomarker_id, _ in BIOMARKERS}
    assert blocked_ids.isdisjoint(portfolio["frontier"])
    assert {item["candidateId"] for item in portfolio["incomparable"]} == blocked_ids
    assert all(
        item["reasons"] == ["EXCLUDED:HYPOTHESIS_FLAG:BLOCKED"]
        for item in portfolio["incomparable"]
    )
    assert all(
        item["flags"] == ["BLOCKED"]
        for item in result["hypotheses"]
        if item["sourceHypothesisId"] == "H-g2"
    )


def test_ra_demo_cli_reads_current_snapshot_shape_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ra_snapshot())))

    assert main(["ra-demo", "--snapshot", "-", "--out", "-"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert len(payload["hypotheses"]) == 9
    assert len(payload["portfolio"]["frontier"]) == 3
    assert captured.err == ""


def test_ra_demo_cli_reports_contract_error_without_traceback(monkeypatch, capsys):
    snapshot = ra_snapshot()
    snapshot["programs"][0]["hypothesis_slot"] = 99
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot)))

    assert main(["ra-demo"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hypothesis_slot values must be within 0..2" in captured.err
    assert "Traceback" not in captured.err
