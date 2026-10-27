"""Every evidence ID the code cites is recorded in fixtures/source-manifest.json.

AGENTS.md asks that a new coefficient is registered in the source manifest. This
test finds the assumption (ASM-) and source (SRC-) IDs written as fixed strings
in src/ and fails when one is neither in the manifest nor a known gap below.
IDs built at runtime (SRC-PLAN-<digest>, ASM-POLICY-OVERRIDE-<hash>) and
prefixes passed to str.startswith are not IDs and are skipped.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from support.paths import SOURCE_MANIFEST

ROOT = Path(__file__).parents[3]
EVIDENCE_ID = re.compile(r"\b(?:ASM|SRC)-[A-Z0-9]+(?:-[A-Z0-9]+)*(?![-A-Z0-9])")

# Gaps found on 2026-10-27 (#61). Remove an ID once the manifest records it.
# What to record for an assumption is the model owner's decision, so never add
# an ASM- ID here: register it in the manifest with the code that cites it.

# Described in another file, but not in the manifest.
RECORDED_ELSEWHERE = {
    "ASM-A03-INFRASTRUCTURE-V1": "docs/infrastructure-events.md",
    "ASM-ACTION-LIFECYCLE-V1": "fixtures/regression-contract.json",
    "ASM-H19-ZONE-ENVIRONMENT-V1": "docs/environmental-demand-effects.md",
}
# Not described in any manifest, fixture or document.
NOT_RECORDED = frozenset(
    {
        "ASM-AFFECT-TRANSFER-V1",
        "ASM-AFFECT-TRUST-WEIGHTING-V1",
        "ASM-BODY-FEEDBACK-COMPLETE-V1",
        "ASM-BODY-FEEDBACK-SLEEP-DEBT-V1",
        "ASM-BOUNDED-RUNTIME-GEOMETRY-V1",
        "ASM-CARE-HYGIENE-FIXED-RELIEF-V1",
        "ASM-CARE-HYGIENE-PROPORTIONAL-RELIEF-V1",
        "ASM-CARE-TRANSITION-PRODUCER-V1",
        "ASM-CARE-TYPED-SUPPORT-RESPONSE-V1",
        "ASM-DISTANCE-DURATION-REPEAT-TRUST-V1",
        "ASM-DURATION-CONTACT-TRUST-V1",
        "ASM-EVACUATION-SELECTION-UNIFORM-V1",
        "ASM-FACILITY-QUEUES-V1",
        "ASM-FACILITY-RECOVERY-PRODUCER-V1",
        "ASM-H19-ZONE-ENVIRONMENT-V2",
        "ASM-HOUSEHOLD-CARE-RELATIONSHIPS-V1",
        "ASM-INITIAL-SCENARIO-DEFAULTS-V1",
        "ASM-LIFE-STATE-FEEDBACK-V1",
        "ASM-NEED-WEIGHTED-PHYSICAL-QUEUE-V1",
        "ASM-NO-SOCIAL-PROXIMITY-V1",
        "ASM-PLAN-CAPACITY-RECONCILIATION-V1",
        "ASM-PLAN-RASTER-HEURISTIC-V1",
        "ASM-POPULATION-DYNAMICS-V1",
        "ASM-POPULATION-LIFECYCLE-V1",
        "ASM-PROXIMITY-CONTACT-V1",
        "ASM-REGION-INFRASTRUCTURE-V1",
        "ASM-REGION-RESOURCE-SERVICE-V1",
        "ASM-SHELTER-AREA-HEURISTIC-V1",
        "ASM-SPATIAL-MOVEMENT-V2",
        "ASM-STAFF-DYNAMICS-V1",
        "ASM-STAFF-DYNAMICS-V2",
        "ASM-STAFF-FATIGUE-LINEAR-V1",
        "ASM-STAFF-FATIGUE-SATURATING-V1",
        "ASM-STAFF-HANDOFF-PRODUCER-V1",
        "ASM-STAFF-OPERATION-RECEIPT-V1",
        "ASM-STAFF-OPERATIONS-V1",
        "ASM-STAFF-RELIEF-V1",
        "ASM-STAFF-TRAVEL-FIRST-V1",
        "ASM-STAFF-URGENCY-FIRST-V1",
        "ASM-STRICT-FIFO-QUEUE-V1",
        "ASM-SYNTHETIC-AGE-BANDS-V1",
        "ASM-SYNTHETIC-HOUSEHOLD-SIZES-V1",
        "ASM-SYNTHETIC-LANGUAGE-V1",
        "ASM-SYNTHETIC-PERSON-TRAITS-V1",
        "ASM-TEMPORARY-CAPACITY-V1",
        "ASM-TEMPORARY-ROOM-BEDS-V1",
        "ASM-TEMPORARY-ROOM-INTAKE-V1",
        "ASM-URGENT-STAFF-SUPPORT-PREEMPTION-V1",
        "ASM-ZONE-HYGIENE-LINEAR-V1",
        "ASM-ZONE-HYGIENE-QUADRATIC-V1",
        "ASM-ZONE-PRIVACY-PROPORTIONAL-V1",
        "ASM-ZONE-PRIVACY-THRESHOLD-V1",
        "ASM-ZONE-REST-LIMITING-V1",
        "ASM-ZONE-REST-MULTIPLICATIVE-V1",
        "ASM-ZONE-TEMP-COMFORT-BAND-V1",
        "ASM-ZONE-TEMP-MIDPOINT-V1",
    }
)
# Public data the region research fetches at runtime; each fetch records its own
# access time and URL. Whether the manifest should list these providers is open.
RUNTIME_DATA_SOURCES = frozenset(
    {
        "SRC-ESTAT-API",
        "SRC-GSI-ADDRESS-SEARCH",
        "SRC-GSI-DESIGNATED-SHELTERS",
        "SRC-GSI-HAZARD-OPEN-DATA",
        "SRC-MLIT-P20-HISTORICAL",
    }
)
# Values an operator entered; the Scenario provenance records them as such.
OPERATOR_INPUT_SOURCES = frozenset(
    {
        "SRC-OPERATOR-AREA",
        "SRC-OPERATOR-FACILITY-CORRECTION",
        "SRC-OPERATOR-FACILITY-ZONE",
        "SRC-OPERATOR-INITIAL-CONDITIONS",
        "SRC-OPERATOR-PLAN-REVISION",
        "SRC-OPERATOR-POPULATION-CONDITIONS",
    }
)
KNOWN_GAPS = set(RECORDED_ELSEWHERE) | NOT_RECORDED | RUNTIME_DATA_SOURCES | OPERATOR_INPUT_SOURCES


def _ids_in_python(text: str) -> set[str]:
    tree = ast.parse(text)
    skipped = {
        id(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for value in node.values
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
        ):
            skipped.update(id(item) for arg in node.args for item in ast.walk(arg))
    return {
        found
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
        for found in EVIDENCE_ID.findall(node.value)
    }


def _cited_ids() -> set[str]:
    cited: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        cited |= _ids_in_python(path.read_text(encoding="utf-8"))
    for path in (ROOT / "src").rglob("*.json"):
        cited |= set(EVIDENCE_ID.findall(path.read_text(encoding="utf-8")))
    return cited


def _manifest_ids() -> set[str]:
    manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    return {row["id"] for row in [*manifest["sources"], *manifest["assumptions"]]}


def test_every_cited_id_is_in_the_manifest_or_a_known_gap() -> None:
    unregistered = _cited_ids() - _manifest_ids() - KNOWN_GAPS

    assert unregistered == set(), "register these IDs in fixtures/source-manifest.json"


def test_known_gaps_are_still_cited_and_still_missing() -> None:
    cited, manifest = _cited_ids(), _manifest_ids()

    assert KNOWN_GAPS & manifest == set(), "the manifest records these now; remove them here"
    assert KNOWN_GAPS - cited == set(), "the code no longer cites these; remove them here"


def test_gaps_recorded_elsewhere_are_still_described_there() -> None:
    for evidence_id, path in RECORDED_ELSEWHERE.items():
        assert evidence_id in (ROOT / path).read_text(encoding="utf-8"), (evidence_id, path)


def test_known_gap_lists_do_not_overlap() -> None:
    groups = [set(RECORDED_ELSEWHERE), NOT_RECORDED, RUNTIME_DATA_SOURCES, OPERATOR_INPUT_SOURCES]

    assert sum(len(group) for group in groups) == len(KNOWN_GAPS)
    assert all(evidence_id.startswith("ASM-") for evidence_id in RECORDED_ELSEWHERE)
    assert all(evidence_id.startswith("ASM-") for evidence_id in NOT_RECORDED)


def test_runtime_ids_and_prefixes_are_not_taken_for_cited_ids() -> None:
    text = (
        'SOURCE = f"SRC-PLAN-{digest}"\n'
        'PREFIX = "SRC-GSI-SHELTERS-"\n'
        'ok = source.startswith(("SRC-GSI", "SRC-ESTAT"))\n'
        'CITED = "ASM-EXAMPLE-V1"\n'
    )

    assert _ids_in_python(text) == {"ASM-EXAMPLE-V1"}
