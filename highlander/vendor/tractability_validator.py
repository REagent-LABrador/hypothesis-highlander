#!/usr/bin/env python3
"""Structural honesty checks for a filled druggability dossier.

This module does NOT check whether a dossier is *right*. It checks whether it is
*honest about what it knows* — that every number names where it came from, that
the two axes were never averaged, that a biologic never got counted as
small-molecule precedent, that `insufficient_evidence` stayed reachable, and
that a null and a zero were kept apart.

Every rule here exists because the corresponding mistake is easy, plausible and
invisible in the output. A dossier that passes is not necessarily correct; a
dossier that fails is definitely wrong in the way the violation names.

Usage
-----
    from validate_dossier import validate_dossier
    violations = validate_dossier(dossier_dict)   # -> list[Violation]

    $ python3 validate_dossier.py examples/jak1_P23458.json
    OK  0 violations

Exit code is 1 when any violation is found, so this is usable as a gate.

Pure stdlib. No dependencies beyond `json`.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

VERDICTS = frozenset(
    {"small_molecule_tractable", "not_tractable", "insufficient_evidence"}
)

# Which axis carries the verdict. Without it "tractability claimed on precedent
# grounds" is not a checkable statement, and MODALITY_SEPARATION's third clause
# cannot fire. This began as a validator-only extension; it is now a CLAUDE.md
# template key.
VERDICT_BASES = frozenset(
    {"retrieved_precedent", "computed_tractability", "both", "none"}
)

REQUIRED_TOP_LEVEL = (
    # The verbatim echo of the request. Added 2026-08-15, after a downstream
    # team was told to key a cache on (accession, mechanism_hypothesis,
    # as_of_date) and discovered none of the three survived into the output, so
    # the promise was unsupportable. It is an echo and nothing else: never
    # inferred, never back-filled. `input.uniprot_accession` is what the CALLER
    # said and `target.uniprot_accession` is the RESOLVED accession, and the
    # top-level `as_of_date` stays authoritative.
    "input",
    "target",
    "as_of_date",
    "verdict",
    "verdict_basis",
    "axis_conflict",
    "target_precedent",
    "biologic_precedent",
    "family_precedent",
    "structural_neighbour_precedent",
    "pocket_neighbour_precedent",
    "structure",
    "tractability",
    "affinity",
    "falsification",
    "next_experiment",
    "not_found",
)

# The interface-classification vocabulary, and it is the TOOL's vocabulary, not
# a shorter one invented here. `interface_analysis.classify_pocket` and
# `modal_app`'s aggregation step between them emit all seven of these, and a
# validator that accepts fewer than the tool emits forces the agent to launder
# a real value through `not_found` — which is what happened before `mixed` and
# `no_pocket_to_classify` were admitted.
#
# `mixed` is the one that matters. It is a MEASURED DISAGREEMENT, not a hedge:
# two symmetry copies of one ligand in one structure can land either side of the
# 0.25 overlap boundary. Measured on 8DYG, ligand U5Q — copy A
# `allosteric_candidate` at 0.22, copy B `orthosteric_candidate` at 0.36, both
# flagged borderline. `pocket-scan`'s aggregation rule produces `mixed` there
# deliberately, because the alternative is a coin flip between two different
# mechanistic claims. Admitting it is not a loosening; see
# `check_mixed_interface_is_resolvable`, which is what stops it becoming one.
INTERFACE_CLASSES = frozenset(
    {
        "orthosteric_candidate",
        "allosteric_candidate",
        "destabiliser_candidate",
        "no_partner_structure",
        "mixed",
        "no_pocket_to_classify",
        # The seventh, and the aggregation step emits it too: every
        # classification on the structure was excluded because the numbering
        # could not be reconciled. An abstention, so it demands nothing — but
        # rejecting it applies the same laundering pressure `mixed` had, on a
        # rarer path where nobody would notice.
        "numbering_mismatch_not_interpretable",
    }
)

ENUMS: dict[str, frozenset[str]] = {
    "verdict": VERDICTS,
    "verdict_basis": VERDICT_BASES,
    "structure.tier": frozenset(
        {
            "holo_experimental",
            "apo_experimental",
            "cofolded",
            "predicted",
            "sampled_ensemble",
            "none",
        }
    ),
    "tractability.cryptic_pocket_risk": frozenset(
        {"low", "medium", "high", "undetermined"}
    ),
    "tractability.cryptic_mechanism": frozenset(
        {
            "loop_or_backbone_motion",
            "sidechain_occlusion",
            "subunit_occlusion",
            "none",
            "undetermined",
        }
    ),
    "tractability.cryptic_potency_prior.expected_ceiling": frozenset(
        {"nanomolar", "micromolar_at_best", "unknown"}
    ),
    "tractability.pocket_vs_interface.classification": INTERFACE_CLASSES,
    # Rule 4b's off-site guard in `check_site_consistency` reads this with an
    # exact `== "site_from_density"`. It was in no ENUMS entry, so any other
    # string passed and the guard silently stopped running — with the geometry
    # still reported. That guard exists because `site_from_density`'s centroid
    # sat 29.57 A from the TNF-alpha ligand, which is nearly four times the
    # 7.73 A error that forced the original retraction. Same dead-guard shape as
    # the `isinstance(ev, dict)` checks documented in SKILL.md.
    "tractability.mdpocket_site_definition_used": frozenset(
        {"site_from_ligand", "site_from_density", "none"}
    ),
}

# A key whose value is a source. Presence of any one of these, with a non-empty
# value, makes every number in that dict and below it attributable.
PROVENANCE_KEYS = frozenset(
    {
        "source",
        "sources",
        "_provenance",
        "provenance",
        "doi",
        "pubmed_id",
        "pmid",
        "url",
        "query",
        "queries",
        "chembl_target_id",
        "chembl_id",
        "chembl",
        "assay_id",
        "best_potency_assay",
        "pdb_id",
        "source_pdb_id",
        "reference_pdb_id",
        "partner_pdb_id",
        "query_structure",
        "ensemble_pdb_ids",
        "tool",
        "basis",
        "descriptor_basis",
    }
)

# fpocket site-selection bases, from pocket-scan/modal_app.py. SIX, and this
# list was transcribed with five for long enough that three other artifacts
# copied the wrong count — `no_pocket_overlapped_ligand_site` was missing, which
# is the basis emitted when a holo ligand site EXISTS and no pocket touches it
# at that clustering value. That is the exact false negative rule 4 was written
# around: TNF-alpha scores 0.002 at D=1.6 at a co-crystallised 570 Da ligand,
# because the channel fragments and the 12-sphere cluster falls below fpocket's
# `-i 15` floor and is discarded silently. Rejecting the basis forces the case
# to be laundered rather than reported.
SELECTION_BASES = frozenset(
    {
        "ligand_site_jaccard",
        "site_signature_overlap",
        "site_signature_unreliable_homooligomer",
        "max_druggability_no_ligand_site",
        "no_pocket_matched_site_signature",
        "no_pocket_overlapped_ligand_site",
    }
)
# FOUR of the six do not identify a site. This comment said "two" while the set
# below held three, which is where `assemble-dossier/SKILL.md`'s "two of its five
# possible values" came from — a miscount that propagated into three artifacts.
#
# Legal to report is not the same as legal to pool.
# `max_druggability_no_ligand_site` is "the most druggable pocket anywhere in the
# chain"; `site_signature_unreliable_homooligomer` is a residue-number match a
# homo-oligomer's protomers make ambiguous in principle;
# `no_pocket_matched_site_signature` matched nothing; and
# `no_pocket_overlapped_ligand_site` is the strongest case of all — a ligand site
# was known and no pocket reached it, so the measurement is anchored to nothing.
# Pooling any of them across an ensemble compares different pockets in different
# places.
NOT_A_SAME_SITE_BASIS = frozenset(
    {
        "max_druggability_no_ligand_site",
        "site_signature_unreliable_homooligomer",
        "no_pocket_matched_site_signature",
        "no_pocket_overlapped_ligand_site",
    }
)

# Keys that combine, average or rank. None of these may exist at any depth.
BANNED_SCORE_KEYS = frozenset(
    {
        "overall_score",
        "overall",
        "score",
        "total_score",
        "final_score",
        "composite_score",
        "combined_score",
        "aggregate_score",
        "weighted_score",
        "average_score",
        "mean_score",
        "axis_average",
        "axis_mean",
        "merged_score",
        "summary_score",
        "confidence_score",
        "tractability_score",
        "druggability_index",
        "verdict_score",
        "priority_score",
    }
)
BANNED_KEY_SUBSTRINGS = ("overall", "composite", "axis_average", "averaged")
PRECEDENT_TOKENS = ("precedent", "actives", "potency", "approved", "clinical")
TRACTABILITY_TOKENS = ("druggability", "pocket", "tractability", "volume")

# Fields where a null and a zero mean different things. A null must be explained
# in `not_found`; a zero must NOT be, because a measured zero is a result.
MEASURED_FIELDS = (
    "target_precedent.distinct_actives",
    "target_precedent.best_potency_nm",
    "target_precedent.assay_concentration.top_assay_share_pct",
    "structure.total_pdb_structures",
    "structure.holo_count",
    "tractability.pocket_volume_a3.min",
    "tractability.pocket_volume_a3.max",
    "tractability.pocket_druggability.min",
    "tractability.pocket_druggability.max",
    "tractability.pocket_hydrophobic_density",
    "tractability.disorder_fraction",
    "tractability.annotated_binding_site_overlap",
    "tractability.ligand_site_jaccard",
    "tractability.max_backbone_ca_displacement_a",
    "tractability.ensemble_consensus_fraction.fraction_with_strong_pocket",
)

# Under an as_of_date these cannot be filtered at the source, so they must carry
# an explicit leakage flag or be omitted. The clinical-candidate one is
# unconditional: ChEMBL's max_phase is a current value with no phase history, so
# "no clinical candidate existed at that date" is not a retrievable statement.
NOT_DATE_FILTERABLE: dict[str, str] = {
    "target_precedent.clinical_stage_small_molecules": (
        "ChEMBL max_phase is current state with no phase history — neither the "
        "presence nor the absence of a clinical candidate is date-filterable"
    ),
    "target_precedent.distinct_actives": (
        "bioactivities_by_accession carries no date column"
    ),
    "target_precedent.best_potency_nm": (
        "same undated bioactivity table as distinct_actives"
    ),
    "target_precedent.patents": "patent counts are not filtered at the source",
}
ALWAYS_FLAG_UNDER_CUTOFF = frozenset(
    {"target_precedent.clinical_stage_small_molecules"}
)

# Thresholds. Named, not inline, because they are policy and a reader must be
# able to see and argue with them.
INSUFFICIENT_ACTIVES_THRESHOLD = 50
SINGLE_ASSAY_DOMINANCE_PCT = 30.0
CRYPTIC_APO_ABSENCE_FRACTION = 0.8
AXIS_CONFLICT_ACTIVES_THRESHOLD = 500

# Rule 4b, and it says so itself: A PROPOSAL, NOT A CALIBRATED NUMBER. Roughly
# half the one error ever measured (7.73 A on apo TNF-alpha) and well above the
# ~1 A grid spacing, resting on a single case. The 2026-08-15 TNF run put
# `site_from_density` 29.57 A from the ligand, so the hazard is not theoretical.
OFF_SITE_CENTROID_DISTANCE_A = 4.0

# ---- Rule 4.0/4a, the 2026-08-15 re-prioritisation ----------------------
# 15 targets, 67 structures, 134 measurements. fpocket's druggability score does
# not separate druggable from hard: target-level AUC 0.720 at D=1.6 with a
# bootstrap 95% CI of 0.44-0.94 (includes chance) and 0.520 at D=2.4. The
# label-free test is the one that settles it — on holo structures where a
# drug-like ligand is physically bound and the scored pocket is anchored to it,
# the median score is 0.320 and a large fraction fall below 0.1. EGFR 6LUD with
# osimertinib bound scores 0.013; JAK1's median is 0.009 across nine approved
# drugs.
#
# THE DENOMINATOR IS UNDER AUDIT (2026-08-15). This was reported as 37 holo
# structures, 25 of 37 below 0.5 and 15 of 37 (41%) below 0.1. At least one of
# the 37 is not a certain positive: RORgt's 6C1P contains no RORgt (sole entity
# A8EVM5, an ion transport protein) and its 1N7 anchor is CHAPSO, a detergent.
# So the clean denominator is 36, and the other 36 have never been re-audited at
# residue level — the audit that caught this one. The demotion does NOT turn on
# that case and is unaffected; only the rate is uncertain. Quote the direction
# and the named cases, not the percentage, until the 36 are audited.
#
# These two are the false-negative band, NOT a druggability threshold. Nothing
# is classified by them; they are the trip-wire for "this score is in the range
# where it has been measured to be wrong about certain positives".
DRUGGABILITY_FALSE_NEGATIVE_BAND = 0.5
DRUGGABILITY_FALSE_NEGATIVE_FLOOR = 0.1

# The primary number is volume AT D=1.6 specifically. At D=2.4 volumes exceed
# 1000 A^3 and sites merge with neighbouring cavities, which is also why a
# reported site volume above this bound is a merge artifact rather than a site.
PRIMARY_VOLUME_CLUSTERING_D = 1.6
MERGED_VOLUME_A3 = 1000.0

# A DISCLOSURE TRIGGER. NOT A THRESHOLD, NOT A PROPOSAL, NOT A CLASSIFIER.
#
# These two numbers used to be documented here as an uncalibrated proposal
# resting on a measured separation: volume at D=1.6 giving AUC 1.000 stable under
# all 15 leave-one-target-out refits, with >=242 A^3 entirely druggable and
# <=207 A^3 entirely hard. THAT SEPARATION IS RETRACTED (2026-08-15; CLAUDE.md
# rule 4a, rubric.md, OUTPUT_NOTES.md). It is not merely uncalibrated — it is
# withdrawn, and it may not be revived from this file.
#
# What the audit found, in one line each:
#   - four of the five hard anchors do not measure their target. MYC's pocket is
#     100% MAX (P61244), zero MYC lining residues; IL-11's is 100% IL-11 receptor
#     alpha (Q14626); CD20's anchor ligand is cholesterol hemisuccinate, a
#     detergent; TL1A had no site anchor at all. TNF's pocket is on TNF but has
#     zero residue overlap with its only drug-anchored site;
#   - RORgt's 6C1P contains no RORgt (sole entity A8EVM5, an ion transport
#     protein) and was selected by ligand_site_jaccard, the trusted path, on a
#     CHAPSO detergent anchor — so restricting to the target's chains is
#     necessary and not sufficient;
#   - chain_accessions was {} on EVERY entry, so every chain of every assembly
#     was scored as target, and max_druggability_no_ligand_site (which identifies
#     no site) set the headline median for MYC, IL-11, TL1A and TNF;
#   - the bootstrap CI of [1.000, 1.000] was degenerate by construction, since
#     resampling a perfectly separated set cannot create an inversion;
#   - and the confound is fatal: the binary flag "a drug-like ligand was
#     co-crystallised" separates the groups at AUC 0.900 with no structural
#     measurement at all. The label and the measurability are the same variable.
#
# THE CONSTANTS BELOW ARE UNCHANGED AND DELIBERATELY SO. Nothing in this file
# classifies on them and nothing ever did; they are read by exactly one rule, to
# decide when a low druggability sitting beside a large volume disagree loudly
# enough that the disagreement must be written into tractability.caveat. That is
# a disclosure trigger, and it survives the retraction because it asserts nothing
# about which side of it a target falls on. Do not add a rule that gates on them.
VOLUME_GUIDE_DRUGGABLE_A3 = 240.0
VOLUME_GUIDE_HARD_A3 = 210.0

# Verdicts druggability may never carry on its own.
NEGATIVE_VERDICTS = frozenset({"not_tractable", "insufficient_evidence"})

# A classification that says something about WHICH pocket relative to the
# partner. `no_partner_structure` is the abstention and is always legal.
SUBSTANTIVE_INTERFACE_CLASSES = frozenset(
    {"orthosteric_candidate", "allosteric_candidate", "destabiliser_candidate"}
)

# USAN stems. Cheap, and they catch the exact mistake the dossier exists to
# prevent: an antibody sitting in the small-molecule list.
#
# This constant used to carry a third entry, `"nib-cept"`, which is not a USAN
# stem and could never have matched the `endswith` test — and it did not matter,
# because nothing read the constant: the rule hardcoded its own tuple. A named
# constant exists so a reader can see the policy and argue with it, so one that
# nothing reads is worse than no constant at all. It is now the tuple the rule
# actually uses.
BIOLOGIC_NAME_STEMS = ("mab", "cept")


# --------------------------------------------------------------------------
# Violation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One rule, one place, one sentence saying what is wrong."""

    rule: str
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"[{self.rule}] {self.path}: {self.message}"

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "path": self.path, "message": self.message}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _get(obj: Any, path: str, default: Any = None) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _has_reported_value(value: Any) -> bool:
    """Is there an actual claim here, as opposed to an all-null template stub?

    `{"count": null, "source": null}` is the template's shape for "we did not
    look". Treating it as a reported value made the as-of rule demand a leakage
    flag on a field carrying nothing.
    """
    if value is None:
        return False
    if isinstance(value, dict):
        return any(
            _has_reported_value(v)
            for k, v in value.items()
            if not str(k).startswith("_")
        )
    if isinstance(value, (list, tuple, set)):
        return any(_has_reported_value(v) for v in value)
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _dict_has_provenance(node: dict) -> bool:
    return any(_nonempty(node.get(k)) for k in PROVENANCE_KEYS if k in node)


def _walk(node: Any, path: str = "") -> Iterable[tuple[str, str, Any]]:
    """Yield (path, key, value) for every entry, depth first."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, str(k), v
            yield from _walk(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            p = f"{path}[{i}]"
            yield p, "", v
            yield from _walk(v, p)


def _not_found_texts(dossier: Any) -> list[str]:
    out: list[str] = []
    for entry in _get(dossier, "not_found") or []:
        if isinstance(entry, str):
            out.append(entry.lower())
        elif isinstance(entry, dict):
            out.append(" ".join(str(v) for v in entry.values()).lower())
    return out


GENERIC_LEAVES = frozenset({"min", "max", "count", "value", "fraction"})


def _mentions_field(texts: list[str], path: str) -> bool:
    """Does `not_found` name this field?

    Matching is deliberately tight. Naming the enclosing *block* is not enough —
    "tractability" as a candidate would let one not_found line excuse every null
    in the block, which is exactly the laundering this rule exists to stop. The
    parent path is only accepted when the leaf is generic (`min`, `max`, ...)
    and therefore carries no name of its own.
    """
    leaf = path.rsplit(".", 1)[-1]
    candidates = [path]
    if leaf in GENERIC_LEAVES:
        if "." in path:
            candidates.append(path.rsplit(".", 1)[0])
    else:
        candidates.append(leaf)
    return any(c.lower() in t for c in candidates for t in texts)


def _names(entries: Any) -> list[str]:
    out: list[str] = []
    for e in entries or []:
        if isinstance(e, dict):
            n = e.get("name") or e.get("drug_name") or e.get("program")
            if isinstance(n, str) and n.strip():
                out.append(n.strip())
        elif isinstance(e, str) and e.strip():
            out.append(e.strip())
    return out


def _norm_name(name: str) -> str:
    """Lowercase alphanumerics only, salt/hydrate words dropped."""
    n = name.lower()
    for salt in (
        " phosphate",
        " citrate",
        " maleate",
        " hemihydrate",
        " hydrate",
        " hydrochloride",
        " sulfate",
        " succinate",
        " fumarate",
        " tosylate",
        " mesylate",
        " pegol",
    ):
        n = n.replace(salt, "")
    return "".join(c for c in n if c.isalnum())


def _year(date_str: str) -> int | None:
    try:
        return int(str(date_str)[:4])
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


# --------------------------------------------------------------------------
# Rule registry
# --------------------------------------------------------------------------

RuleFn = Callable[[dict], list[Violation]]
RULES: list[RuleFn] = []


def rule(fn: RuleFn) -> RuleFn:
    RULES.append(fn)
    return fn


# --------------------------------------------------------------------------
# R0 — the template is the contract
# --------------------------------------------------------------------------


@rule
def check_well_formed(d: dict) -> list[Violation]:
    """Keys present, enums filled, no template placeholders left standing."""
    v: list[Violation] = []
    if not isinstance(d, dict):
        return [Violation("WELL_FORMED", "", "dossier is not a JSON object")]

    for key in REQUIRED_TOP_LEVEL:
        if key not in d:
            v.append(
                Violation(
                    "WELL_FORMED",
                    key,
                    "required key missing — the template says never omit a key, "
                    "use null",
                )
            )

    for path, allowed in ENUMS.items():
        if _get(d, path, "__absent__") == "__absent__":
            continue
        value = _get(d, path)
        if value is None:
            v.append(
                Violation(
                    "WELL_FORMED", path, "enum field is null; it has an explicit "
                    "unknown value — use it"
                )
            )
        elif value not in allowed:
            v.append(
                Violation(
                    "WELL_FORMED",
                    path,
                    f"{value!r} is not one of {sorted(allowed)}",
                )
            )

    # An unfilled enum placeholder is the literal pipe-separated string from the
    # template. It reads as a filled field to everything downstream.
    for path, key, value in _walk(d):
        if key.startswith("_"):
            continue
        if isinstance(value, str) and " | " in value:
            v.append(
                Violation(
                    "WELL_FORMED",
                    path,
                    "template placeholder left unfilled (pipe-separated enum)",
                )
            )

    # The template illustrates list shape with one all-empty object. Shipping
    # that object is shipping a drug with no name.
    for block in (
        "target_precedent.approved_small_molecules",
        "target_precedent.clinical_stage_small_molecules",
        "target_precedent.terminated_programs",
        "biologic_precedent.approved_biologics",
        "structural_neighbour_precedent.neighbours",
        "pocket_neighbour_precedent.candidates",
    ):
        for i, entry in enumerate(_get(d, block) or []):
            if not isinstance(entry, dict):
                continue
            identifying = [
                entry.get(k)
                for k in ("name", "program", "pdb_id", "source_target", "drug_name")
                if k in entry
            ]
            if identifying and not any(_nonempty(x) for x in identifying):
                v.append(
                    Violation(
                        "WELL_FORMED",
                        f"{block}[{i}]",
                        "empty template stub — use [] for 'none', never a "
                        "nameless entry",
                    )
                )

    if _is_num(_get(d, "verdict")):
        v.append(
            Violation("WELL_FORMED", "verdict", "verdict is a number; it is a label")
        )

    if not _nonempty(_get(d, "next_experiment.description")):
        v.append(
            Violation(
                "WELL_FORMED",
                "next_experiment.description",
                "empty — every dossier names what would move the answer",
            )
        )

    if not _nonempty(_get(d, "biologic_precedent.note")):
        v.append(
            Violation(
                "WELL_FORMED",
                "biologic_precedent.note",
                "the modality disclaimer is part of the template, not optional",
            )
        )

    survived = _get(d, "falsification.survived")
    if not isinstance(survived, bool):
        v.append(
            Violation(
                "WELL_FORMED",
                "falsification.survived",
                "must be true or false after a run — never null",
            )
        )
    if not _nonempty(_get(d, "falsification.checks_run")):
        v.append(
            Violation(
                "WELL_FORMED",
                "falsification.checks_run",
                "empty — checks that found nothing are still evidence",
            )
        )

    for path, _key, value in _walk(d):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            v.append(
                Violation("WELL_FORMED", path, "NaN/Infinity is not a measurement")
            )
    return v


# --------------------------------------------------------------------------
# R1 — no number without provenance
# --------------------------------------------------------------------------


@rule
def check_number_provenance(d: dict) -> list[Violation]:
    """Every numeric leaf sits inside a block that names a source.

    Provenance is inherited downward: a `sources` list on `target_precedent`
    covers the numbers inside it. It is not inherited sideways or upward — a
    source on one drug entry does not cover a count in a sibling block.
    """
    v: list[Violation] = []

    def walk(node: Any, path: str, has_prov: bool) -> None:
        if isinstance(node, dict):
            prov = has_prov or _dict_has_provenance(node)
            for k, val in node.items():
                if str(k).startswith("_"):
                    continue  # notes and warnings carry no claims
                walk(val, f"{path}.{k}" if path else str(k), prov)
        elif isinstance(node, list):
            for i, val in enumerate(node):
                walk(val, f"{path}[{i}]", has_prov)
        elif _is_num(node) and not has_prov:
            v.append(
                Violation(
                    "NUMBER_WITHOUT_PROVENANCE",
                    path,
                    f"{node!r} has no source in scope — add a ChEMBL ID, PDB ID, "
                    "DOI or the query to this block",
                )
            )

    walk(d, "", False)
    return v


# --------------------------------------------------------------------------
# R2 — the two axes are never averaged
# --------------------------------------------------------------------------


@rule
def check_axes_never_averaged(d: dict) -> list[Violation]:
    v: list[Violation] = []
    for path, key, value in _walk(d):
        if not key or key.startswith("_"):
            continue
        low = key.lower()
        if low in BANNED_SCORE_KEYS:
            v.append(
                Violation(
                    "AXES_AVERAGED",
                    path,
                    f"'{key}' is a combined score field; there is no overall number",
                )
            )
            continue
        if any(s in low for s in BANNED_KEY_SUBSTRINGS):
            v.append(
                Violation(
                    "AXES_AVERAGED",
                    path,
                    f"'{key}' names an aggregate; the axes are reported separately",
                )
            )
            continue
        if _is_num(value):
            has_p = any(t in low for t in PRECEDENT_TOKENS)
            has_t = any(t in low for t in TRACTABILITY_TOKENS)
            if has_p and has_t:
                v.append(
                    Violation(
                        "AXES_AVERAGED",
                        path,
                        f"'{key}' is a number combining a precedent term with a "
                        "tractability term",
                    )
                )
    return v


# --------------------------------------------------------------------------
# R3 — modality separation
# --------------------------------------------------------------------------


@rule
def check_modality_separation(d: dict) -> list[Violation]:
    v: list[Violation] = []
    sm_entries = _get(d, "target_precedent.approved_small_molecules") or []
    clin_entries = _get(d, "target_precedent.clinical_stage_small_molecules") or []
    bio_names = {_norm_name(n) for n in _names(_get(d, "biologic_precedent.approved_biologics"))}

    for label, entries in (
        ("approved_small_molecules", sm_entries),
        ("clinical_stage_small_molecules", clin_entries),
    ):
        for i, entry in enumerate(entries):
            path = f"target_precedent.{label}[{i}]"
            name = (_names([entry]) or [""])[0]
            norm = _norm_name(name)
            if norm and norm in bio_names:
                v.append(
                    Violation(
                        "MODALITY_LEAK",
                        path,
                        f"{name!r} also appears in biologic_precedent — nothing "
                        "may be in both blocks",
                    )
                )
            if isinstance(entry, dict):
                mod = entry.get("modality")
                if mod is None:
                    # A missing modality used to pass. It is the one field where
                    # silence is the failure: rule 1's whole content is that
                    # `molecule_type` was READ per drug, and an absent value is
                    # indistinguishable from never having looked.
                    v.append(
                        Violation(
                            "MODALITY_LEAK",
                            f"{path}.modality",
                            "no modality on a small-molecule entry — rule 1 says "
                            "it is read per drug from "
                            "chembl.molecule_dictionary.molecule_type, and an "
                            "absent value says only that it was never read",
                        )
                    )
                elif mod != "small_molecule":
                    v.append(
                        Violation(
                            "MODALITY_LEAK",
                            f"{path}.modality",
                            f"{mod!r} in a small-molecule block",
                        )
                    )
            if norm.endswith(BIOLOGIC_NAME_STEMS):
                v.append(
                    Violation(
                        "MODALITY_LEAK",
                        path,
                        f"{name!r} carries a biologic USAN stem (-mab/-cept); "
                        "check chembl.molecule_dictionary.molecule_type",
                    )
                )

    # Tractability claimed on precedent grounds when the precedent is biologic.
    verdict = _get(d, "verdict")
    basis = _get(d, "verdict_basis")
    characterised = _get(d, "target_precedent.best_potency_characterised")
    if (
        verdict == "small_molecule_tractable"
        and basis in {"retrieved_precedent", "both"}
        and not sm_entries
        and not clin_entries
        and characterised is not True
    ):
        v.append(
            Violation(
                "MODALITY_LEAK",
                "verdict_basis",
                "tractability claimed on retrieved precedent with zero approved "
                "and zero clinical small molecules and no characterised potency "
                "— the only precedent here is biologic",
            )
        )

    # Biologics present, no small molecules, tractable, and nothing said about it.
    if (
        verdict == "small_molecule_tractable"
        and not sm_entries
        and _get(d, "biologic_precedent.approved_biologics")
        and not _nonempty(_get(d, "axis_conflict"))
    ):
        v.append(
            Violation(
                "MODALITY_LEAK",
                "axis_conflict",
                "approved biologics with zero approved small molecules and a "
                "tractable verdict, with no axis_conflict stated",
            )
        )
    return v


# --------------------------------------------------------------------------
# R4 — insufficient_evidence is reachable and used
# --------------------------------------------------------------------------


@rule
def check_insufficient_evidence_reachable(d: dict) -> list[Violation]:
    v: list[Violation] = []
    actives = _get(d, "target_precedent.distinct_actives")
    holo = _get(d, "structure.holo_count")
    approved = _get(d, "target_precedent.approved_small_molecules") or []
    verdict = _get(d, "verdict")

    thin_chem = _is_num(actives) and actives < INSUFFICIENT_ACTIVES_THRESHOLD
    no_holo = holo == 0  # None is a failed measurement, not a zero — see R8
    if thin_chem and no_holo and not approved and verdict != "insufficient_evidence":
        v.append(
            Violation(
                "INSUFFICIENT_EVIDENCE_AVOIDED",
                "verdict",
                f"{actives} distinct actives (< {INSUFFICIENT_ACTIVES_THRESHOLD}), "
                f"0 holo structures and no approved small molecules — the answer "
                f"is 'insufficient_evidence', not {verdict!r}",
            )
        )

    if verdict == "insufficient_evidence" and not _nonempty(
        _get(d, "next_experiment.resolves")
    ):
        v.append(
            Violation(
                "INSUFFICIENT_EVIDENCE_AVOIDED",
                "next_experiment.resolves",
                "declining requires naming what would resolve it",
            )
        )
    return v


# --------------------------------------------------------------------------
# R5 — druggability is a range, never a point
# --------------------------------------------------------------------------


@rule
def check_druggability_is_a_range(d: dict) -> list[Violation]:
    v: list[Violation] = []
    drug = _get(d, "tractability.pocket_druggability")
    if drug is not None and not isinstance(drug, dict):
        v.append(
            Violation(
                "DRUGGABILITY_POINT_ESTIMATE",
                "tractability.pocket_druggability",
                f"scalar {drug!r} — druggability is reported as min/max across "
                "clustering and across structures",
            )
        )
        drug = None

    vol = _get(d, "tractability.pocket_volume_a3")
    if vol is not None and not isinstance(vol, dict):
        v.append(
            Violation(
                "DRUGGABILITY_POINT_ESTIMATE",
                "tractability.pocket_volume_a3",
                f"scalar {vol!r} — volume is reported with its ensemble spread",
            )
        )

    if isinstance(drug, dict):
        lo, hi = drug.get("min"), drug.get("max")
        if (lo is None) != (hi is None):
            v.append(
                Violation(
                    "DRUGGABILITY_POINT_ESTIMATE",
                    "tractability.pocket_druggability",
                    "one-sided range (min=%r, max=%r) — a maximum without its "
                    "minimum is a point estimate wearing a range's clothes"
                    % (lo, hi),
                )
            )
        if _is_num(lo) and _is_num(hi):
            swept = _as_list(_get(d, "tractability.method.clustering_d_swept"))
            if len({str(x) for x in swept}) < 2:
                v.append(
                    Violation(
                        "DRUGGABILITY_POINT_ESTIMATE",
                        "tractability.method.clustering_d_swept",
                        "a druggability range was reported without sweeping at "
                        "least two clustering values — a single D is a coin flip",
                    )
                )
            if not _nonempty(_get(d, "tractability.method.ensemble_pdb_ids")):
                v.append(
                    Violation(
                        "DRUGGABILITY_POINT_ESTIMATE",
                        "tractability.method.ensemble_pdb_ids",
                        "a druggability range was reported with no ensemble named",
                    )
                )
            volmin, volmax = None, None
            if isinstance(vol, dict):
                volmin, volmax = vol.get("min"), vol.get("max")
            if volmin is None and volmax is None:
                if not _mentions_field(
                    _not_found_texts(d), "tractability.pocket_volume_a3"
                ):
                    v.append(
                        Violation(
                            "DRUGGABILITY_POINT_ESTIMATE",
                            "tractability.pocket_volume_a3",
                            "druggability reported without volume, and volume is "
                            "the reproducible number — measure it or record the "
                            "gap in not_found",
                        )
                    )

    # A druggability figure loose anywhere else in the dossier is a point estimate
    # by another name.
    allowed = {
        "tractability.pocket_druggability.min",
        "tractability.pocket_druggability.max",
        "tractability.pocket_druggability.fold_range",
    }
    for path, key, value in _walk(d):
        if not key or key.startswith("_"):
            continue
        if "druggab" in key.lower() and _is_num(value) and path not in allowed:
            v.append(
                Violation(
                    "DRUGGABILITY_POINT_ESTIMATE",
                    path,
                    f"loose druggability figure {value!r} outside the min/max block",
                )
            )
    return v


@rule
def check_druggability_not_load_bearing(d: dict) -> list[Violation]:
    """Druggability is reported. It carries nothing.

    Rule 4.0. Added 2026-08-15 after an evaluation over 15 targets, 67
    structures and 134 measurements demoted the score. This rule does NOT
    replace `check_druggability_is_a_range` — that one still enforces that the
    number is a range at all. This one enforces what the range is allowed to do
    once it exists, which nothing checked before: a dossier could reach
    `not_tractable` on a druggability of 0.013 measured at a pocket with
    osimertinib physically bound in it, and pass clean.

    Four clauses:

    1. `load_bearing` is a declaration and only one value is legal. It exists so
       the demotion is visible in every output rather than living only in prose.
    2. The measured false-negative rate travels with the number, so a reader who
       meets a 0.02 in a dossier knows the rate at which that value is wrong.
    3. A negative verdict may not rest on it. With no volume beside it there is
       nothing else the computed axis could be resting on, and 41% of pockets
       with a drug bound score below 0.1 — so the verdict is unsupported.
    4. A low druggability sitting beside a large volume must be written down in
       `tractability.caveat`. That is the EGFR shape — 290 A^3 at D=1.6 against
       6LUD at 0.013 — and it is reported, not resolved. Nothing here
       classifies on the volume guide; it only decides when the two numbers are
       far enough apart to be worth saying.

    This docstring said "Three clauses" over four for as long as the fourth has
    existed, which is the same species of rot as the rubric's test count. It is
    listed because a reader who counts three and finds four has to work out
    which one is the accident.
    """
    v: list[Violation] = []
    drug = _get(d, "tractability.pocket_druggability")
    if not isinstance(drug, dict):
        return v

    lo, hi = drug.get("min"), drug.get("max")
    reported = _is_num(lo) or _is_num(hi)

    if reported:
        lb = drug.get("load_bearing")
        if lb is not False:
            v.append(
                Violation(
                    "DRUGGABILITY_LOAD_BEARING",
                    "tractability.pocket_druggability.load_bearing",
                    f"{lb!r} — the only legal value is false. fpocket "
                    f"druggability is a reported, non-load-bearing range: "
                    f"target-level AUC 0.720 with a 95% CI of 0.44-0.94 at "
                    f"D=1.6 and 0.520 at D=2.4",
                )
            )
        if not _nonempty(drug.get("_false_negative_rate")):
            v.append(
                Violation(
                    "DRUGGABILITY_LOAD_BEARING",
                    "tractability.pocket_druggability._false_negative_rate",
                    "a druggability range reported without its measured "
                    "false-negative rate beside it — a large fraction of "
                    "pockets with a drug-like ligand physically bound score "
                    f"below {DRUGGABILITY_FALSE_NEGATIVE_FLOOR} (EGFR 6LUD with "
                    "osimertinib bound scores 0.013), and a reader who meets "
                    "the number later cannot discount it without that. The rate "
                    "was reported as 41% (15 of 37); that DENOMINATOR IS UNDER "
                    "AUDIT — one of the 37 was not a certain positive and the "
                    "other 36 are unaudited at residue level, so state the "
                    "direction and the named cases, not the percentage",
                )
            )

    verdict = _get(d, "verdict")
    basis = _get(d, "verdict_basis")
    vol = _get(d, "tractability.pocket_volume_a3")
    vol = vol if isinstance(vol, dict) else {}
    volumes = [
        x
        for x in (vol.get("primary_d1_6_a3"), vol.get("min"), vol.get("max"))
        if _is_num(x)
    ]
    low = _is_num(hi) and hi < DRUGGABILITY_FALSE_NEGATIVE_BAND

    if (
        low
        and verdict in NEGATIVE_VERDICTS
        and basis in {"computed_tractability", "both"}
        and not volumes
    ):
        v.append(
            Violation(
                "DRUGGABILITY_LOAD_BEARING",
                "verdict",
                f"{verdict!r} on computed grounds with druggability max {hi!r} "
                "and no pocket volume anywhere — druggability may not carry a "
                "negative verdict on its own. Measure the D=1.6 volume, or "
                "decline with the unmeasured volume named as the reason",
            )
        )

    # A large volume beside a low druggability is the disagreement the
    # evaluation was full of — EGFR 290 A^3 with 6LUD at 0.013. It is reported,
    # not resolved. Nothing here classifies on the volume guide; it only decides
    # when the two numbers are far enough apart to be worth writing down.
    if low and volumes and max(volumes) >= VOLUME_GUIDE_DRUGGABLE_A3:
        if not _nonempty(_get(d, "tractability.caveat")):
            v.append(
                Violation(
                    "DRUGGABILITY_LOAD_BEARING",
                    "tractability.caveat",
                    f"druggability max {hi!r} against a pocket volume of "
                    f"{max(volumes)!r} A^3 — the demoted number and the primary "
                    "number disagree. Report the disagreement in "
                    "tractability.caveat; do not resolve it in either direction "
                    "(the volume guide is RETRACTED as of rule 4a, and is no "
                    "longer even the uncalibrated proposal it was previously "
                    "described as. It triggers this disclosure and classifies "
                    "nothing)",
                )
            )
    return v


@rule
def check_volume_is_primary(d: dict) -> list[Violation]:
    """Volume at D=1.6 is the computed axis's primary number.

    Rule 4a. The pooled min/max across a D sweep is NOT the primary number: at
    D=2.4 volumes exceed 1000 A^3 and sites merge with neighbouring cavities, so
    a spread that pools both is a spread over two different things. The D=1.6
    figure is carried separately and this rule is what makes it get carried.
    """
    v: list[Violation] = []
    vol = _get(d, "tractability.pocket_volume_a3")
    if not isinstance(vol, dict):
        return v

    primary = vol.get("primary_d1_6_a3")
    reported_vol = _is_num(vol.get("min")) or _is_num(vol.get("max"))
    drug = _get(d, "tractability.pocket_druggability")
    reported_drug = isinstance(drug, dict) and (
        _is_num(drug.get("min")) or _is_num(drug.get("max"))
    )

    if (reported_vol or reported_drug) and not _is_num(primary):
        if not _mentions_field(
            _not_found_texts(d), "tractability.pocket_volume_a3.primary_d1_6_a3"
        ):
            v.append(
                Violation(
                    "VOLUME_NOT_PRIMARY",
                    "tractability.pocket_volume_a3.primary_d1_6_a3",
                    "computed-axis geometry reported without the D=1.6 site "
                    "volume, which is the primary number reported on the "
                    "computed axis — it is a cavity measurement and carries no "
                    "verdict: its AUC 1.000 separation over 15 targets is "
                    "RETRACTED (rule 4a, 2026-08-15 — the calibration anchors "
                    "did not measure the proteins they were attributed to), so "
                    "do not compare it to 210 or 240 A^3 — measure it or record "
                    "the gap in not_found",
                )
            )

    if _is_num(primary) and primary > MERGED_VOLUME_A3:
        v.append(
            Violation(
                "VOLUME_NOT_PRIMARY",
                "tractability.pocket_volume_a3.primary_d1_6_a3",
                f"{primary!r} A^3 is above {MERGED_VOLUME_A3} — that is the "
                "signature of sites merged with neighbouring cavities, not a "
                "D=1.6 site volume",
            )
        )

    if reported_vol and not _nonempty(vol.get("clustering_d")):
        v.append(
            Violation(
                "VOLUME_NOT_PRIMARY",
                "tractability.pocket_volume_a3.clustering_d",
                "a volume spread with no record of which clustering values it "
                "pools — D=1.6 and D=2.4 do not measure the same cavity, so a "
                "spread over both is not a spread over one site",
            )
        )
    return v


@rule
def check_fraction_carries_n(d: dict) -> list[Violation]:
    """A fraction without its N is not a measurement."""
    v: list[Violation] = []
    cons = _get(d, "tractability.ensemble_consensus_fraction")
    if not isinstance(cons, dict):
        return v
    # This one does NOT depend on a fraction having been reported. n_structures
    # disagreeing with the named ensemble is a self-contradiction either way,
    # and gating it behind the fraction killed it on every dossier that names
    # an ensemble but declines the fraction — which is the normal case, since
    # pocket_scan emits the denominators and no consensus fraction at all.
    n_struct = cons.get("n_structures")
    ids = _get(d, "tractability.method.ensemble_pdb_ids")
    if _is_num(n_struct) and _nonempty(ids) and len(ids) != n_struct:
        v.append(
            Violation(
                "FRACTION_WITHOUT_N",
                "tractability.ensemble_consensus_fraction.n_structures",
                f"n_structures={n_struct} but {len(ids)} entries are named in "
                "method.ensemble_pdb_ids",
            )
        )

    frac = cons.get("fraction_with_strong_pocket")
    if frac is None:
        return v
    n_keys = ("n_structures", "n_measurements", "n")
    if not any(_is_num(cons.get(k)) for k in n_keys):
        v.append(
            Violation(
                "FRACTION_WITHOUT_N",
                "tractability.ensemble_consensus_fraction",
                f"fraction {frac!r} reported with no N — 2 of 4 and 200 of 400 "
                "are not the same claim",
            )
        )
    return v


# --------------------------------------------------------------------------
# R6 — a pooled spread must record how the site was chosen
# --------------------------------------------------------------------------


@rule
def check_same_site_basis(d: dict) -> list[Violation]:
    v: list[Violation] = []
    ids = _as_list(_get(d, "tractability.method.ensemble_pdb_ids"))
    swept = _as_list(_get(d, "tractability.method.clustering_d_swept"))
    n_pooled = max(len(ids), 1) * max(len({str(x) for x in swept}), 1)

    for block in ("pocket_volume_a3", "pocket_druggability"):
        path = f"tractability.{block}"
        node = _get(d, path)
        if not isinstance(node, dict):
            continue
        if node.get("min") is None and node.get("max") is None:
            continue  # nothing pooled, nothing to justify
        bases = [
            b
            for b in _as_list(node.get("site_pocket_selected_by"))
            if isinstance(b, str)
        ]
        if not bases:
            v.append(
                Violation(
                    "SAME_SITE_BASIS_MISSING",
                    f"{path}.site_pocket_selected_by",
                    "a spread is only a measurement if every value describes the "
                    "same site — record the basis from pocket_scan",
                )
            )
            continue
        for b in bases:
            if b not in SELECTION_BASES:
                v.append(
                    Violation(
                        "SAME_SITE_BASIS_MISSING",
                        f"{path}.site_pocket_selected_by",
                        f"{b!r} is not a pocket_scan selection basis "
                        f"({sorted(SELECTION_BASES)})",
                    )
                )
            elif b in NOT_A_SAME_SITE_BASIS and n_pooled > 1:
                v.append(
                    Violation(
                        "SAME_SITE_BASIS_INVALID",
                        f"{path}.site_pocket_selected_by",
                        f"{b!r} does not identify a site, so these {n_pooled} "
                        "values must not be pooled as one — report them per "
                        "structure or not at all",
                    )
                )
    return v


# --------------------------------------------------------------------------
# R6b — the numbers must all be about the SAME pocket
# --------------------------------------------------------------------------


@rule
def check_site_consistency(d: dict) -> list[Violation]:
    """Geometry, the cryptic call and the interface class must share a site.

    Until this rule existed, nothing in the file read `pocket_vs_interface` at
    all beyond checking the string was a legal enum, and neither `centroid` nor
    `distance` appeared anywhere. A dossier could report volume from pocket X,
    a cryptic call about pocket Y and an interface classification of pocket Z
    and pass clean — which is not hypothetical: on an apo homo-oligomer that is
    precisely the shape of the retracted claim, where druggability was anchored
    to a residue-number match measured 7.7 A off-site while the cryptic call
    was anchored to the ligand.
    """
    v: list[Violation] = []
    tract = _get(d, "tractability")
    if not isinstance(tract, dict):
        return v

    # -- geometry quoted off a site definition known to be the wrong pocket --
    dist = _get(d, "tractability.site_centroid_to_ligand_distance_a")
    used = _get(d, "tractability.mdpocket_site_definition_used")
    reported = [
        block
        for block in ("pocket_volume_a3", "pocket_druggability")
        if isinstance(_get(d, f"tractability.{block}"), dict)
        and (
            _get(d, f"tractability.{block}.min") is not None
            or _get(d, f"tractability.{block}.max") is not None
        )
    ]
    if (
        used == "site_from_density"
        and _is_num(dist)
        and dist > OFF_SITE_CENTROID_DISTANCE_A
    ):
        for block in reported:
            v.append(
                Violation(
                    "SITE_INCONSISTENT",
                    f"tractability.{block}",
                    f"geometry reported off `site_from_density`, whose centroid "
                    f"is {dist} A from the donor ligand — past the "
                    f"{OFF_SITE_CENTROID_DISTANCE_A} A proposed (NOT calibrated) "
                    "threshold, so it is a different pocket. Rule 4b: report it "
                    "as a distinct cavity, name the distance, and set "
                    "site_hypothesis_basis to not_established",
                )
            )

    # -- an interface class is a measurement, not a reading of the literature --
    classification = _get(d, "tractability.pocket_vs_interface.classification")
    if classification in SUBSTANTIVE_INTERFACE_CLASSES:
        overlap = _get(d, "tractability.pocket_vs_interface.pocket_interface_overlap")
        partner = _get(d, "tractability.pocket_vs_interface.partner_pdb_id")
        if not _nonempty(partner):
            v.append(
                Violation(
                    "SITE_INCONSISTENT",
                    "tractability.pocket_vs_interface.partner_pdb_id",
                    f"{classification!r} asserted with no partner structure named "
                    "— rule 2b requires the classification be measured against a "
                    "complex containing the partner, not read off the literature",
                )
            )
        if overlap is None:
            v.append(
                Violation(
                    "SITE_INCONSISTENT",
                    "tractability.pocket_vs_interface.pocket_interface_overlap",
                    f"{classification!r} asserted with no measured overlap — that "
                    "is the number the classification is made from",
                )
            )
    return v


@rule
def check_mixed_interface_is_resolvable(d: dict) -> list[Violation]:
    """`mixed` is admitted as a value; this is the price of admitting it.

    Rule 6c. `mixed` is the honest output when symmetry copies of one ligand
    classify differently — 8DYG U5Q gave `allosteric_candidate` at overlap 0.22
    and `orthosteric_candidate` at 0.36 across the 0.25 boundary — and
    `pocket-scan`'s aggregation rule mandates it rather than letting a caller
    take whichever copy came first. The validator used to reject it, so a run
    that produced it had to record the conflict in `not_found` and hide the true
    value in a `_consensus_note`. That is laundering, and it is exactly what the
    dossier exists to stop.

    But a bare `mixed` is a worse output than either label it replaces: it names
    no mechanism, so a reader cannot act on it at all. So the value is legal only
    when it carries the two things that make it actionable:

    1. **Mixed between what.** `classifications_seen` — the tool's own key —
       must name at least two distinct classes. One class repeated is a
       consensus, not a disagreement.
    2. **How far apart.** `pocket_interface_overlap` must carry the individual
       overlaps, not one scalar. 0.22 and 0.36 straddling 0.25 is a pocket
       sitting on the boundary; a single 0.22 is a claim that it is not.

    Plus two clauses that stop the disagreement being quietly resolved:

    3. A classification is measured against a complex, so `partner_pdb_id` is
       required for `mixed` exactly as it is for a substantive class.
    4. `matches_mechanism_hypothesis` may not be `true`. A disagreement cannot
       confirm a hypothesis, and the copy that agrees with the caller's prior is
       precisely the one that gets quoted.

    The last clause runs in the other direction and is the first-wins bug
    itself: `classifications_seen` naming two labels while `classification`
    reports one of them is a disagreement collapsed to a coin flip.
    """
    v: list[Violation] = []
    pvi = _get(d, "tractability.pocket_vs_interface")
    if not isinstance(pvi, dict):
        return v

    base = "tractability.pocket_vs_interface"
    classification = pvi.get("classification")
    seen_raw = _as_list(pvi.get("classifications_seen"))
    seen = {s for s in seen_raw if isinstance(s, str) and s.strip()}

    if classification == "mixed":
        if len(seen) < 2:
            v.append(
                Violation(
                    "INTERFACE_MIXED_UNRESOLVED",
                    f"{base}.classifications_seen",
                    f"{sorted(seen)!r} — 'mixed' does not say mixed between what. "
                    "Name every class the run measured (the tool returns them in "
                    "`classifications_seen`); at least two distinct ones, or the "
                    "value is not a disagreement",
                )
            )
        for s in sorted(seen - INTERFACE_CLASSES):
            v.append(
                Violation(
                    "INTERFACE_MIXED_UNRESOLVED",
                    f"{base}.classifications_seen",
                    f"{s!r} is not an interface class ({sorted(INTERFACE_CLASSES)})",
                )
            )

        overlaps = [x for x in _as_list(pvi.get("pocket_interface_overlap")) if _is_num(x)]
        if len(overlaps) < 2:
            v.append(
                Violation(
                    "INTERFACE_MIXED_UNRESOLVED",
                    f"{base}.pocket_interface_overlap",
                    "'mixed' with fewer than two measured overlaps — one scalar "
                    "cannot show a pocket sitting on the boundary. 8DYG U5Q was "
                    "0.22 and 0.36 against a 0.25 boundary; report both, so a "
                    "reader can see how far apart the copies were",
                )
            )

        if not _nonempty(pvi.get("partner_pdb_id")):
            v.append(
                Violation(
                    "INTERFACE_MIXED_UNRESOLVED",
                    f"{base}.partner_pdb_id",
                    "'mixed' asserted with no partner structure named — rule 2b "
                    "requires the classification be measured against a complex "
                    "containing the partner, and a disagreement is still a "
                    "measurement",
                )
            )

        if pvi.get("matches_mechanism_hypothesis") is True:
            v.append(
                Violation(
                    "INTERFACE_MIXED_UNRESOLVED",
                    f"{base}.matches_mechanism_hypothesis",
                    "a disagreement cannot confirm a mechanism hypothesis. Do not "
                    "pick the copy that matches the prior — that is the coin flip "
                    "the aggregation rule exists to prevent",
                )
            )

    elif len(seen) >= 2 and classification in INTERFACE_CLASSES:
        v.append(
            Violation(
                "INTERFACE_MIXED_UNRESOLVED",
                f"{base}.classification",
                f"{classification!r} reported while {sorted(seen)!r} were "
                "measured — a disagreement collapsed to one label. The consensus "
                "over classifications that disagree is 'mixed'; never reach into "
                "per_structure and take the first entry",
            )
        )
    return v


# --------------------------------------------------------------------------
# R7 — cryptic means what the field says it means
# --------------------------------------------------------------------------


@rule
def check_cryptic_definition(d: dict) -> list[Violation]:
    v: list[Violation] = []
    ev = _get(d, "tractability.cryptic_evidence")
    mech = _get(d, "tractability.cryptic_mechanism")
    risk = _get(d, "tractability.cryptic_pocket_risk")
    tier = _get(d, "structure.tier")

    # "Was a census even run?" must key on the VALUE being null, not on the key
    # being absent. The output template always ships a `cryptic_evidence` block,
    # so `not isinstance(ev, dict)` — the old test — is unreachable against any
    # dossier built from the template, and the rule could no longer fire on the
    # case it exists for: a mechanism asserted over an all-null census. The call
    # itself is `is_cryptic`, so that is the field whose nullness means "not
    # run". `isinstance` is kept as a disjunct because a hand-written dossier can
    # still omit the block or set it to null, and `None.get` would raise.
    census_call_made = isinstance(ev, dict) and ev.get("is_cryptic") is not None

    if census_call_made:
        is_cryptic = ev["is_cryptic"]
        if not _nonempty(ev.get("basis")):
            v.append(
                Violation(
                    "CRYPTIC_MISCLAIM",
                    "tractability.cryptic_evidence.basis",
                    "a cryptic call with no stated basis",
                )
            )
        if is_cryptic is True:
            n_exam = ev.get("n_apo_examined")
            n_absent = ev.get("n_apo_site_absent")
            if not _is_num(n_exam) or n_exam < 1:
                v.append(
                    Violation(
                        "CRYPTIC_MISCLAIM",
                        "tractability.cryptic_evidence.n_apo_examined",
                        "is_cryptic=true requires an apo ensemble — one apo "
                        "structure cannot distinguish 'absent' from "
                        "'low-scoring in this crystal form'",
                    )
                )
            elif not _is_num(n_absent):
                v.append(
                    Violation(
                        "CRYPTIC_MISCLAIM",
                        "tractability.cryptic_evidence.n_apo_site_absent",
                        "is_cryptic=true requires counting the apo structures "
                        "the site is absent from",
                    )
                )
            elif n_absent / n_exam < CRYPTIC_APO_ABSENCE_FRACTION:
                v.append(
                    Violation(
                        "CRYPTIC_MISCLAIM",
                        "tractability.cryptic_evidence",
                        f"site absent in {n_absent} of {n_exam} apo structures — "
                        "the field's definition (Vajda 2018) requires all or "
                        "nearly all; this is low-scoring, not cryptic",
                    )
                )
            if ev.get("site_present_in_apo_ensemble") is True:
                v.append(
                    Violation(
                        "CRYPTIC_MISCLAIM",
                        "tractability.cryptic_evidence.is_cryptic",
                        "the site is present in the apo ensemble — that is "
                        "OCCLUDED, not cryptic",
                    )
                )
            if risk == "low":
                v.append(
                    Violation(
                        "CRYPTIC_MISCLAIM",
                        "tractability.cryptic_pocket_risk",
                        "is_cryptic=true with cryptic_pocket_risk 'low'",
                    )
                )
    elif mech not in (None, "none", "undetermined"):
        v.append(
            Violation(
                "CRYPTIC_MISCLAIM",
                "tractability.cryptic_evidence",
                f"cryptic_mechanism {mech!r} asserted with no cryptic census "
                "behind it — cryptic_evidence.is_cryptic is null, so the "
                "mechanism is an assertion, not a finding",
            )
        )

    # Cryptic risk must be measured, not read off the structure tier.
    # Same defect as above, second instance: `not isinstance(ev, dict)` asked
    # whether the block was absent, which the template makes impossible. The
    # question is whether the block carries anything, so test the values.
    # `_has_reported_value` skips `_`-prefixed keys, so the template's `_note`
    # does not by itself count as a measurement.
    if (
        risk == "high"
        and tier in {"apo_experimental", "predicted", "sampled_ensemble"}
        and _get(d, "tractability.max_backbone_ca_displacement_a") is None
        and not _has_reported_value(ev)
    ):
        v.append(
            Violation(
                "CRYPTIC_MISCLAIM",
                "tractability.cryptic_pocket_risk",
                "'high' on an apo/predicted tier with no displacement "
                "measurement and no cryptic_evidence — that fires on every apo "
                "target equally and carries no information",
            )
        )

    # Mechanism is a prior on achievable potency; the two must agree.
    ceiling = _get(d, "tractability.cryptic_potency_prior.expected_ceiling")
    if mech in {"sidechain_occlusion", "subunit_occlusion"} and ceiling == "nanomolar":
        v.append(
            Violation(
                "CRYPTIC_MISCLAIM",
                "tractability.cryptic_potency_prior.expected_ceiling",
                f"{mech} with a nanomolar ceiling — every measured side-chain "
                "site in the CryptoSite set bound low-micromolar at best",
            )
        )
    if mech == "loop_or_backbone_motion" and ceiling == "micromolar_at_best":
        v.append(
            Violation(
                "CRYPTIC_MISCLAIM",
                "tractability.cryptic_potency_prior.expected_ceiling",
                "loop/backbone motion with a micromolar ceiling — 25 of 27 such "
                "sites reached nanomolar",
            )
        )
    return v


# --------------------------------------------------------------------------
# R8 — null is not zero
# --------------------------------------------------------------------------


@rule
def check_null_is_not_zero(d: dict) -> list[Violation]:
    """A failed measurement is null with a reason; a measured zero is a result.

    The general principle, which this project keeps rediscovering under new
    names: **a thing that failed to run is not a thing that scored badly.**

    It has now bitten four ways. A decoy that failed to run was read as a decoy
    that scored badly, and because `analyze.py` filters on `"error" not in r`
    while a repair pass flushed the artifact after each recovered ligand, five
    different separation figures were published off one file at five different
    moments — every one internally consistent, every one a smaller decoy set
    rather than a worse one. A Paperclip statement timeout was read as zero
    rows. An unclassifiable ligand was read as apo. A credential failure was
    read as no data. Each time the absence wore the costume of a measurement,
    and each time it flattered the instrument.

    That is what this rule is: the structural form of that principle, enforced
    on the one axis a validator can see. `null` must say why it is null, or it
    is indistinguishable from a zero somebody forgot to write — and a zero that
    is *also* listed in `not_found` is claiming to be both.
    """
    v: list[Violation] = []
    texts = _not_found_texts(d)
    sentinels = {"n/a", "na", "none", "null", "unknown", "not_found", "-", "?", ""}

    for path in MEASURED_FIELDS:
        present = _get(d, path, "__absent__")
        if present == "__absent__":
            continue
        value = _get(d, path)
        if isinstance(value, str):
            v.append(
                Violation(
                    "NULL_IS_NOT_ZERO",
                    path,
                    f"string {value!r} in a numeric field — a failed measurement "
                    "is null, not a placeholder string",
                )
            )
            continue
        if value is None:
            if not _mentions_field(texts, path):
                v.append(
                    Violation(
                        "NULL_IS_NOT_ZERO",
                        path,
                        "null with no entry in not_found — a null must say why "
                        "it is null, or it is indistinguishable from a zero "
                        "someone forgot to write",
                    )
                )
        elif _is_num(value) and value == 0:
            if _mentions_field(texts, path):
                v.append(
                    Violation(
                        "NULL_IS_NOT_ZERO",
                        path,
                        "reported as 0 and also listed in not_found — a measured "
                        "zero is a result; pick one",
                    )
                )

    for path, key, value in _walk(d):
        if not key or key.startswith("_"):
            continue
        if isinstance(value, str) and value.strip().lower() in sentinels - {""}:
            low = key.lower()
            if low.endswith(("_count", "_nm", "_pct", "_a3", "_fraction", "_a")) or low in {
                "distinct_actives",
                "sequence_length",
            }:
                v.append(
                    Violation(
                        "NULL_IS_NOT_ZERO",
                        path,
                        f"sentinel string {value!r} in a numeric field — use null",
                    )
                )
    return v


# --------------------------------------------------------------------------
# R9 — as-of integrity
# --------------------------------------------------------------------------


def _has_leakage_flag(d: dict, path: str) -> bool:
    value = _get(d, path)
    if isinstance(value, dict) and value.get("leakage_risk") is True:
        return True
    if isinstance(value, list) and value and all(
        isinstance(e, dict) and e.get("leakage_risk") is True for e in value
    ):
        return True
    leaf = path.rsplit(".", 1)[-1]
    block = path.split(".", 1)[0]
    registries = _as_list(_get(d, f"{block}.as_of_leakage")) + _as_list(
        _get(d, "as_of_leakage")
    )
    for entry in registries:
        if not isinstance(entry, dict):
            continue
        if entry.get("leakage_risk") is not True:
            continue
        named = str(entry.get("field", ""))
        if named in (path, leaf) and _nonempty(entry.get("note")):
            return True
    return False


@rule
def check_as_of_integrity(d: dict) -> list[Violation]:
    v: list[Violation] = []
    as_of = _get(d, "as_of_date")
    if not _nonempty(as_of):
        return v

    cutoff_year = _year(as_of)
    if cutoff_year is None or len(str(as_of)) != 10 or str(as_of)[4] != "-":
        v.append(
            Violation(
                "AS_OF_LEAKAGE",
                "as_of_date",
                f"{as_of!r} is not an ISO date (YYYY-MM-DD)",
            )
        )
        return v

    for path, reason in NOT_DATE_FILTERABLE.items():
        value = _get(d, path)
        must_flag = path in ALWAYS_FLAG_UNDER_CUTOFF or _has_reported_value(value)
        if must_flag and not _has_leakage_flag(d, path):
            v.append(
                Violation(
                    "AS_OF_LEAKAGE",
                    path,
                    f"as_of_date is set and this field cannot be date-filtered "
                    f"({reason}) — it must carry leakage_risk: true with a note "
                    f"naming the source, or be omitted",
                )
            )

    for block in (
        "target_precedent.approved_small_molecules",
        "target_precedent.clinical_stage_small_molecules",
        "target_precedent.terminated_programs",
        "biologic_precedent.approved_biologics",
    ):
        for i, entry in enumerate(_get(d, block) or []):
            if not isinstance(entry, dict):
                continue
            yr = entry.get("year")
            if _is_num(yr) and yr > cutoff_year:
                v.append(
                    Violation(
                        "AS_OF_LEAKAGE",
                        f"{block}[{i}].year",
                        f"{yr} is after the as_of_date {as_of} — filter at the "
                        "source, do not retrieve and trim",
                    )
                )

    for path, key, value in _walk(d):
        if key == "release_date" and isinstance(value, str):
            yr = _year(value)
            if yr is not None and value > str(as_of):
                v.append(
                    Violation(
                        "AS_OF_LEAKAGE",
                        path,
                        f"release date {value} is after the as_of_date {as_of}",
                    )
                )
    return v


# --------------------------------------------------------------------------
# R10 — disagreement between the axes must be declared
# --------------------------------------------------------------------------


@rule
def check_axis_conflict_declared(d: dict) -> list[Violation]:
    v: list[Violation] = []
    reasons: list[str] = []

    sm = _get(d, "target_precedent.approved_small_molecules") or []
    bio = _get(d, "biologic_precedent.approved_biologics") or []
    actives = _get(d, "target_precedent.distinct_actives")
    holo = _get(d, "structure.holo_count")
    share = _get(d, "target_precedent.assay_concentration.top_assay_share_pct")
    different = _get(
        d, "target_precedent.assay_concentration.measures_a_different_target"
    )
    characterised = _get(d, "target_precedent.best_potency_characterised")
    best = _get(d, "target_precedent.best_potency_nm")

    # Biologics-with-no-small-molecules is only a CONFLICT if something else in
    # the dossier still points at tractability. On CD20 — three approved
    # antibodies, a four-pass transmembrane protein, nothing for a small
    # molecule to bind — the two axes agree, and demanding an axis_conflict
    # there would be a false positive that trains the agent to write filler.
    if (
        not sm
        and bio
        and (
            _get(d, "verdict") == "small_molecule_tractable"
            or (_is_num(actives) and actives >= AXIS_CONFLICT_ACTIVES_THRESHOLD)
        )
    ):
        reasons.append("approved biologics exist and no approved small molecule does")
    if _is_num(actives) and actives >= AXIS_CONFLICT_ACTIVES_THRESHOLD and holo == 0:
        reasons.append(f"{actives} reported actives against 0 holo structures")
    if _is_num(share) and share >= SINGLE_ASSAY_DOMINANCE_PCT and different is True:
        reasons.append(
            f"{share}% of bioactivity comes from an assay measuring a different "
            "protein"
        )
    if characterised is False and best is not None:
        reasons.append("the headline potency comes from an uncharacterised assay")

    if reasons and not _nonempty(_get(d, "axis_conflict")):
        v.append(
            Violation(
                "AXIS_CONFLICT_UNDECLARED",
                "axis_conflict",
                "the axes disagree and it is not stated: " + "; ".join(reasons),
            )
        )
    return v


# --------------------------------------------------------------------------
# R11 — an actives count is a claim about assays until proven otherwise
# --------------------------------------------------------------------------


@rule
def check_assay_provenance(d: dict) -> list[Violation]:
    v: list[Violation] = []
    actives = _get(d, "target_precedent.distinct_actives")
    if not _is_num(actives) or actives == 0:
        return v
    ac = _get(d, "target_precedent.assay_concentration") or {}
    if not _nonempty(ac.get("top_assay_description")):
        v.append(
            Violation(
                "ASSAY_PROVENANCE_MISSING",
                "target_precedent.assay_concentration.top_assay_description",
                f"{actives} actives reported without naming the top contributing "
                "assay — the count may be about one assay, not the target",
            )
        )
    share = ac.get("top_assay_share_pct")
    if share is None:
        v.append(
            Violation(
                "ASSAY_PROVENANCE_MISSING",
                "target_precedent.assay_concentration.top_assay_share_pct",
                "actives reported without the top assay's share",
            )
        )
    elif _is_num(share) and share >= SINGLE_ASSAY_DOMINANCE_PCT:
        if ac.get("measures_a_different_target") is None:
            v.append(
                Violation(
                    "ASSAY_PROVENANCE_MISSING",
                    "target_precedent.assay_concentration."
                    "measures_a_different_target",
                    f"one assay is {share}% of all bioactivity and it was not "
                    "checked whether it even measures this protein",
                )
            )
    if _get(d, "target_precedent.best_potency_nm") is not None and _get(
        d, "target_precedent.best_potency_characterised"
    ) is None:
        v.append(
            Violation(
                "ASSAY_PROVENANCE_MISSING",
                "target_precedent.best_potency_characterised",
                "a potency was reported without saying whether the assay behind "
                "it is characterised",
            )
        )
    return v


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def validate_dossier(dossier: dict) -> list[Violation]:
    """Run every rule. Returns [] for a dossier that is honest about itself."""
    out: list[Violation] = []
    for fn in RULES:
        out.extend(fn(dossier))
    return sorted(out, key=lambda x: (x.rule, x.path))


def format_violations(violations: list[Violation]) -> str:
    if not violations:
        return "OK  0 violations"
    lines = [f"{len(violations)} violation(s)"]
    lines.extend(f"  {v}" for v in violations)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    status = 0
    for path in argv:
        with open(path, encoding="utf-8") as fh:
            dossier = json.load(fh)
        violations = validate_dossier(dossier)
        print(f"== {path}")
        print(format_violations(violations))
        if violations:
            status = 1
    return status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
