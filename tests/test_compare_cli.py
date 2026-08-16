"""Functional CLI coverage for the production packet consumer."""
from __future__ import annotations

import io
import json

import pytest

from highlander.__main__ import main
from highlander.packet_consumer import strict_json_loads
from tests.test_packet_consumer import hypgen_candidate, request


def test_compare_cli_reads_stdin_and_emits_versioned_result(monkeypatch, capsys):
    stronger = hypgen_candidate("H-cli-stronger", 0.8, 0.1)
    weaker = hypgen_candidate("H-cli-weaker", 0.6, 0.3)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps(request([stronger, weaker])))
    )

    assert main(["compare", "--request", "-", "--out", "-"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schemaVersion"] == "highlander.portfolio-result.v1"
    assert payload["frontier"] == ["H-cli-stronger"]
    assert payload["dominated"] == ["H-cli-weaker"]
    assert "winner" not in payload
    assert captured.err == ""


def test_compare_cli_fails_closed_without_traceback(monkeypatch, capsys):
    candidate = hypgen_candidate("H-cli-tampered", 0.8, 0.1)
    candidate["packetHash"] = "f" * 64
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request([candidate]))))

    assert main(["compare"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "canonical terminal packet body" in captured.err
    assert "Traceback" not in captured.err


def test_compare_cli_rejects_duplicate_root_keys(monkeypatch, capsys):
    candidate = hypgen_candidate("H-cli-duplicate-key", 0.8, 0.1)
    valid = json.dumps(request([candidate]))
    duplicate = '{"schemaVersion":"attacker-v0",' + valid[1:]
    monkeypatch.setattr("sys.stdin", io.StringIO(duplicate))

    assert main(["compare"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "duplicate object key 'schemaVersion'" in captured.err
    assert "Traceback" not in captured.err


def test_compare_cli_rejects_non_json_numeric_constants(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"schemaVersion":NaN}'))

    assert main(["compare"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "non-JSON numeric constant 'NaN'" in captured.err


@pytest.mark.parametrize(
    "document",
    (
        '{"schemaVersion":"\\ud800"}',
        '{"schemaVersion":"\\udc00"}',
        '{"\\ud800":"value"}',
    ),
)
def test_compare_cli_rejects_escaped_lone_utf16_surrogates(
    document, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", io.StringIO(document))

    assert main(["compare"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unpaired UTF-16 surrogate" in captured.err
    assert "Traceback" not in captured.err


def test_strict_json_accepts_escaped_utf16_surrogate_pair():
    assert strict_json_loads(b'"\\ud83d\\ude00"', "test document") == chr(0x1F600)
