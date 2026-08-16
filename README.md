# Hypothesis Highlander

Hypothesis Highlander is the read-only portfolio layer above the LABrador
modules. Its production path consumes immutable, terminal output packets from
the orchestrator, validates their integrity and lineage, and returns a
versioned Pareto snapshot. It does not call producer modules, change their
schemas, impute missing scores, or choose a global winner.

## Production flow

```text
current pinned module outputs
        |
        v
hash + artifact + identity + dependency validation
        |
        v
lossless module adapters
        |
        v
explicit objective policy + comparability cohorts
        |
        v
frontier / dominated / incomparable + attempt ledger
```

Run the production consumer with a self-contained request:

```bash
python -m highlander compare --request packet-request.json --out result.json
```

The caller owns the wall-clock limit; the demo orchestrator uses four minutes.

Or call it in-process from the output orchestrator:

```python
from highlander import compare_packet_request

result = compare_packet_request(request).to_dict()
```

`compare_packet_request` accepts an optional artifact resolver for a
content-addressed store. Without one, the request must include base64-encoded
`artifactPayloads` keyed by the exact artifact references in each module
envelope. Exact input, output, and (when present) execution-artifact bytes are
verified before adaptation. Input artifacts must use the orchestrator-owned
`labrador.module-input-binding.v1` wrapper so candidate, attempt, and selected
parent lineage cannot be relabeled without changing the input digest.

## Locked producer adapters

| Producer | Highlander use | Important behavior |
|---|---|---|
| Evidence Mapper | Evidence, coverage, and graph provenance | Preserves `yes`, `no`, and neutral `no_effect`; emits no hypothesis fitness value |
| Hypothesis Generator | Candidate identity plus support, novelty, testability, and contradiction risk | Consumes the current headless response and its nested `HypothesisDocument` (`schema_version: 2.0`); a successful response must also carry cards and the complete ROI request; `contradiction_risk` is minimized; rejected or unverified candidates are excluded by default |
| Trial Recruitment Forecaster | Recruitability objective and enrollment uncertainty | Consumes the current snake_case result (`simulated_months_to_enroll`, `simulated_months_range`, and related fields) and preserves the native record |
| Therapeutic Program Economics | ROI objective and economic uncertainty | Reads `summary.p50_rnpv`; preserves P10/P50/P90, currency, valuation year, engine, warnings, and decision grade |
| Small-Molecule Tractability Review | Categorical tractability posture and scientific context | Never fabricates a numeric biology score; validates target, mechanism, as-of date, and modality |

Each adapter has its own schema ID/version and producer-commit lock in
`highlander/producer_locks.json`; the obsolete monorepo-wide lock is not used.
The canonical public snake_case thesis is vendored from `platform-contracts`
with its source commit and SHA-256 in `highlander/contracts/contract.lock.json`.
A mismatch is quarantined rather than interpreted.

## Request and integrity contract

The top-level schema is
`highlander.packet-comparison-request.v1` and contains:

- `snapshotId` and timezone-aware `createdAt`;
- an explicit `objectivePolicy` with objective IDs and `MAX`/`MIN` direction;
- one or more `candidatePackets`;
- `artifactPayloads` when no external artifact resolver is supplied.

All candidates in one v1 request must belong to the same `runId`; cross-run
frontiers require a future, explicit policy. The result repeats that run ID at
the top level and in every input-packet receipt.

Each candidate packet pins `packetRevisionId`, `runId`, `hypothesisId`, selected
module attempts, and attributed exclusion reasons. `packetHash` is the RFC 8785
JCS SHA-256 of that normalized terminal body, including the revision ID.

Each module envelope carries:

- run, hypothesis, module, and attempt identity;
- locked native schema, producer, and adapter versions;
- terminal execution status plus a reason for unsuccessful states;
- a structured evidence basis and qualifiers;
- exact input/output raw hashes and a canonical output hash when output exists;
- a raw execution-artifact hash for an output-less terminal attempt;
- an RFC 8785 JCS hash of the identity-bearing module envelope;
- safe opaque `artifact://` or `cas://` references (never presigned URLs or
  credentials);
- typed `dependsOn` references containing both the selected parent's output
  hash and its candidate-specific envelope hash;
- the scientific subject and untouched native payload.

The consumer checks that downstream packets bind every parent they actually
used. Recruitment depends on the selected hypothesis. Economics always depends
on HypGen's complete ROI request and binds recruitment only when that output was
actually incorporated into its native input. Tractability may run independently
from the branch accession when HypGen fails; if it declares mapper or HypGen
lineage, those exact selected outputs are still verified. A successful child
cannot depend on a failed, cancelled, partial, malformed, or quarantined parent.
Clinical has one narrow exception: it may bind a `PARTIAL` HypGen attempt with
a complete canonical hypothesis output when only the separate ROI request is
missing. The exact partial status/reason remains visible, and HypGen objectives
are not emitted from that attempt.

The input-binding wrapper also checks the hypothesis carried by recruitment,
the hypothesis/program/recruitment binding used by economics, and the target
accession used by tractability. This catches branch-swapped native inputs even
when two branches legitimately have byte-identical producer outputs.

The envelope and terminal packet hashes are integrity and lineage checks, not
proof of who produced the request. Any caller can construct a packet and
recompute its hashes. At an untrusted boundary, accept only a signed packet or
look up an orchestrator/DB-attested packet hash using authenticated context;
keep artifact-store credentials out of packet JSON. Inside that boundary,
Highlander assumes the orchestrator is trusted to select the terminal attempts
and assign run/hypothesis identity.

See [COMPOSE.md](COMPOSE.md) for the complete request shape and orchestrator
integration rules.

## Comparison rules

The production comparator is deliberately conservative:

- every objective in the selected policy is required and finite;
- raw values, direction, unit, uncertainty, schema, source path, and source
  hash remain visible;
- candidates compare only under matching units, schema versions, comparison
  bases, and complete structured evidence-basis sets;
- partial vectors never dominate complete vectors;
- missing, failed, cancelled, skipped, not-wired, not-amenable, quarantined,
  rejected, unverified, blocked, and not-decision-grade records do not receive
  plausible fallback values;
- ties retain every hypothesis in an explicit equivalence group;
- uncertainty is preserved and nominal dominance is labeled as such;
- output order is deterministic and contains no `winner` or composite `top`.

The result includes the exact input packet revision/hash, every raw objective,
a structured module-attempt ledger, comparison groups, frontier, dominated and
incomparable IDs, dominance relationships, equivalence groups, exclusions,
and qualifiers. It also includes `nextEvidenceAction`, either `null` or one
deterministically selected action grounded in current producer asks, gaps, or
follow-up fields. This advisory field never changes Pareto membership and never
creates a winner.

The seam is bounded to 500 candidates, 10 selected envelope entries per
candidate (including exact idempotent repeats), 32 objectives, 5,000 embedded
artifacts, 16 MiB per artifact, and 128 MiB of resolved artifact bytes.
Embedded artifacts are decoded lazily. The CLI additionally rejects request
JSON above 192 MiB, duplicate keys, and nesting deeper than 64 levels.

Output-less terminal attempts remain visible in the module-attempt ledger with
their status, reason, envelope hash, and verified execution-artifact identity.
They never fabricate a native payload or numeric objective. A failed axis only
makes a candidate incomparable when that axis is required by the selected
policy or when an unusable parent invalidates a dependent result.

## Minimal RA demo compatibility

The current RA demo can be exercised without changing the orchestrator or any
scientific module. Pipe its existing snake-case `/snapshot` projection into the
scoped Highlander command:

```bash
curl -fsS http://127.0.0.1:8787/api/runs/<run-id>/snapshot \
  | python -m highlander ra-demo --snapshot - --out ra-demo-result.json
```

This mode requires exactly three biomarkers and nine unique hypothesis branches
(the complete 3x3 cross-product). It preserves each composite branch ID, including
ties, and compares the embedded native HypGen Card metrics `support`, `novelty`,
and `testability`. Biomarker association is retained as a qualifier but is not
converted into a score.

The projected card rank and the shared RA recruitment, ROI, and tractability
records do not enter dominance. The output is explicitly
`highlander.ra-demo-result.v1`, carries `DEMO_ONLY`, and contains no winner. This
is a narrow compatibility path; it does not weaken or replace the production
packet consumer.

## Legacy demo is separate

The original quality-diversity search remains available for demonstrations:

```bash
python -m highlander run --generations 4 --seed 42 --out demo-result.json
python -m highlander.demo
streamlit run highlander/app.py
```

That path evaluates explicit mock/proxy tier bodies and may emit legacy
normalized scores. It demonstrates search, archive, visualization, and failure
ledger mechanics only. It is not the production orchestrator seam and its
output is not decision-grade. Production integrations must use `compare` or
`compare_packet_request`.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

The repository is standalone and imports no sibling LABrador runtime. Producer
fixtures used by contract tests live under `tests/fixtures/`; historical
fixture provenance is separate from the current per-module runtime locks.
Highlander adds a consumer-owned envelope and input-binding seam; it does not
change any locked upstream producer input or output schema.
