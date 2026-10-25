"""Pure offline parsing of a small, explicit subset of intervention requests.

The parser creates a proposal only. It does not receive a WorldState, RNG, event queue,
branch registry, or provider, and its operation names are data rather than executable code.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from sheltersim.domain.environment import ZONE_ENVIRONMENT_POLICY, ZONE_ENVIRONMENT_STATE_KEY
from sheltersim.domain.operational import OPERATIONAL_EVENTS_KEY
from sheltersim.domain.resources import RESOURCE_LEDGER_KEY
from sheltersim.events.effects import (
    DEFAULT_PRIMITIVE_REGISTRY,
    PrimitiveRegistry,
    PrimitiveSpec,
    available_primitives,
)
from sheltersim.llm.contracts import LLMStage, LLMTask
from sheltersim.llm.provider import LLMExecutionError, LLMRouter
from sheltersim.llm.schemas import (
    Ambiguity,
    CompilerProvenance,
    CompilerResult,
    CompilerUsage,
    DraftPlan,
    FacilityTarget,
    ParseIssue,
    ParseRequest,
    ParseResult,
    PlanAssumption,
    ScenarioSummary,
    ShipmentTarget,
    SupportMode,
    TargetSelector,
    TimedAction,
    ZoneTarget,
)

if TYPE_CHECKING:
    from sheltersim.core.world import WorldState

PARSER_VERSION = "h06-offline-v1"
PARSER_SOURCE_ID = "offline-parser-h06"
_MAX_STAFF = 100
_MAX_TOILETS = 20

_KANJI_NUMBERS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_FACILITY_FAILURE = re.compile(
    r"故障|壊れ|使えな|使用できな|停止|故障|outage|fail(?:ed|s|ure)?|broken|unavailable",
    re.IGNORECASE,
)
_WATER_FAILURE = re.compile(
    r"断水|水.{0,5}(?:止ま|停止|出な)|給水.{0,5}(?:止ま|停止)|"
    r"water.{0,12}(?:outage|stop|stoppage|cut|fail)|(?:outage|cut).{0,12}water",
    re.IGNORECASE,
)
_ADD = re.compile(
    r"追加|増員|増や|設置|導入|add(?:ed|ing)?|increase|install(?:ed|ing)?|provide",
    re.IGNORECASE,
)
_TOILET = re.compile(r"トイレ|便器|restroom|toilet", re.IGNORECASE)
_STAFF = re.compile(r"スタッフ|職員|職員|staff|worker", re.IGNORECASE)
_WATER = re.compile(r"水|給水|water", re.IGNORECASE)
_EVACUEE_ARRIVAL = re.compile(
    r"避難者.{0,12}(?:到着|来る|受け入れ|追加)|(?:到着|来る|受け入れ).{0,12}避難者|"
    r"evacuees?.{0,24}(?:arriv|admit|add)|(?:arriv|admit|add).{0,24}evacuees?",
    re.IGNORECASE,
)
_EVACUEE_DEPARTURE = re.compile(
    r"避難者.{0,12}(?:帰宅|退所|帰る|退出)|(?:帰宅|退所|退出|帰る).{0,12}避難者|"
    r"evacuees?.{0,24}(?:depart|leave|go home)|(?:depart|remove|leave).{0,24}evacuees?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _TimeWindow:
    start_minute: int
    end_minute: int | None = None


@dataclass(frozen=True)
class _TargetResolution:
    selector: TargetSelector | None
    ambiguity: Ambiguity | None = None
    issue: ParseIssue | None = None


def parse_natural_language(request: ParseRequest | dict[str, object]) -> ParseResult:
    """Parse a supported facility/staff/toilet/water request without side effects.

    `request` may be a validated ParseRequest or its JSON-shaped mapping. Unsupported
    story text is rejected with a stable issue code; uncertain targets become choices.
    """
    if not isinstance(request, ParseRequest):
        request = ParseRequest.model_validate(request)

    text = _normalize(request.text)
    time_result = _parse_time_window(text, request.scenario_summary)
    if isinstance(time_result, ParseIssue):
        return ParseResult(status="rejected", issues=[time_result])
    window = time_result or _TimeWindow(request.selected_minute)

    operations = _detect_operations(text)
    if not operations:
        return ParseResult(
            status="rejected",
            issues=[
                ParseIssue(
                    code="unsupported_event",
                    message="この入力に対応するオフライン作用を特定できません。対象・出来事・時刻を指定してください。",
                    choices=[
                        "facility_outage",
                        "add_staff",
                        "add_toilet",
                        "water_outage",
                        "add_evacuees",
                        "depart_evacuees",
                    ],
                )
            ],
        )

    effects: list[TimedAction] = []
    ambiguities: list[Ambiguity] = []
    issues: list[ParseIssue] = []
    seen_ops: set[str] = set()
    for index, op in enumerate(operations, start=1):
        if (
            op in {"add_evacuees", "depart_evacuees"}
            and "population_dynamics_v1" not in request.scenario_summary.available_policies
        ):
            issues.append(
                ParseIssue(
                    code="unsupported_policy",
                    message="このシナリオでは人口動態イベントを実行できません。",
                )
            )
            continue
        count = None if op == "water_outage" else _extract_count(text, op)
        limit = (
            _MAX_STAFF
            if op == "add_staff"
            else _MAX_TOILETS
            if op == "add_toilet"
            else 1000
        )
        if count is not None and (count <= 0 or count > limit):
            noun = (
                "スタッフ"
                if op == "add_staff"
                else "トイレ"
                if op == "add_toilet"
                else "避難者"
            )
            issues.append(
                ParseIssue(
                    code="invalid_count",
                    message=f"{noun}の数は1以上{limit}以下で指定してください。",
                    choices=[str(limit)],
                )
            )
            continue
        if count is None:
            count = 1

        target = _resolve_target(text, op, request.scenario_summary)
        if target.issue is not None:
            issues.append(target.issue)
            continue
        if target.ambiguity is not None:
            ambiguities.append(target.ambiguity)

        assert target.selector is not None
        if op in {"add_evacuees", "depart_evacuees"} and window.end_minute is not None:
            issues.append(
                ParseIssue(
                    code="unsupported_time_window",
                    message="到着・帰宅は一時点のイベントです。開始時刻だけを指定してください。",
                )
            )
            continue
        if op == "facility_outage":
            facility = _selected_facility(target.selector, request.scenario_summary)
            if facility is not None and count > facility.working_units:
                issues.append(
                    ParseIssue(
                        code="invalid_count",
                        message=(
                            f"{facility.name} の稼働中ユニット数は {facility.working_units} です。"
                        ),
                        choices=[str(facility.working_units)],
                    )
                )
                continue
            units = "facility_unit"
            parameters = {"units": count}
            dependencies = ["facility_capacity"]
        elif op == "add_staff":
            units = "staff_member"
            parameters = {"count": count}
            dependencies = ["staff_operations"]
        elif op == "add_toilet":
            units = "toilet_stall"
            parameters = {"count": count}
            dependencies = ["facility_capacity"]
        elif op in {"add_evacuees", "depart_evacuees"}:
            units = "evacuee"
            parameters = {"count": count}
            dependencies = ["population_dynamics_v1"]
        else:
            units = "water_supply"
            parameters = {"outage": True}
            dependencies = ["operational_events_v1", "resource_service_v1"]

        effect_id = f"effect-{index}-{op}"
        effects.append(
            TimedAction(
                effect_id=effect_id,
                op=op,
                target_selector=target.selector,
                start_minute=window.start_minute,
                end_minute=(
                    None
                    if op in {"add_evacuees", "depart_evacuees"}
                    else window.end_minute
                ),
                parameters=parameters,
                units=units,
                source_id=PARSER_SOURCE_ID,
                assumption_ids=[],
                policy_dependencies=dependencies,
                causation_id=None,
                reversible=True,
            )
        )
        seen_ops.add(op)

    if issues:
        return ParseResult(status="rejected", issues=issues)
    if not effects:
        return ParseResult(
            status="rejected",
            issues=[ParseIssue(code="unknown_target", message="対象を確定できません。")],
        )

    duration = request.scenario_summary.duration_minutes
    if duration is not None and any(
        effect.start_minute >= duration
        or (effect.end_minute is not None and effect.end_minute > duration)
        for effect in effects
    ):
        return ParseResult(
            status="rejected",
            issues=[
                ParseIssue(
                    code="time_out_of_range",
                    message=f"指定時刻はシナリオの終了時刻（{duration}分）を超えています。",
                )
            ],
        )

    fork_at_minute = min(request.selected_minute, min(item.start_minute for item in effects))
    plan_id = hashlib.sha256(
        f"{request.branch_id}\0{request.text}\0{fork_at_minute}".encode("utf-8")
    ).hexdigest()[:16]
    support_mode = (
        SupportMode.CLARIFICATION_REQUIRED
        if ambiguities
        else SupportMode.COMPOSED
        if len(seen_ops) > 1
        else SupportMode.NATIVE
    )
    plan = DraftPlan(
        schema_version=1,
        program_id=f"nl-h06-{plan_id}",
        narrative=(
            "Offline parser proposal; validate the selected mechanism and target before approval."
        ),
        source_text=request.text,
        simulation_start_minute=fork_at_minute,
        effects=effects,
        assumptions=[],
        support_mode=support_mode,
        compiler_provenance=CompilerProvenance(
            compiler_kind="offline_rule",
            compiler_version=PARSER_VERSION,
            model_id=None,
            prompt_hash=None,
        ),
        branch_id=request.branch_id,
        fork_at_minute=fork_at_minute,
        ambiguities=ambiguities,
        requires_confirmation=True,
    )
    return ParseResult(
        status="clarification_required" if ambiguities else "parsed",
        draft_plan=plan,
        issues=[],
    )


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _detect_operations(text: str) -> list[str]:
    ops: list[str] = []
    clauses = re.split(r"[,、。;+]|\b(?:and|then)\b|さらに|そして|および", text)
    for clause in clauses:
        is_add = bool(_ADD.search(clause))
        if _EVACUEE_ARRIVAL.search(clause):
            ops.append("add_evacuees")
        if _EVACUEE_DEPARTURE.search(clause):
            ops.append("depart_evacuees")
        has_water_failure = bool(_WATER.search(clause) and _WATER_FAILURE.search(clause))
        if has_water_failure:
            ops.append("water_outage")
        if _TOILET.search(clause) and is_add:
            ops.append("add_toilet")
        if _STAFF.search(clause) and is_add:
            ops.append("add_staff")
        if _FACILITY_FAILURE.search(clause) and not has_water_failure:
            if _TOILET.search(clause) or re.search(r"施設|facility", clause, re.IGNORECASE):
                ops.append("facility_outage")
    # A request may mention one concept several times, but it creates one effect per op.
    return list(dict.fromkeys(ops))


def _extract_count(text: str, op: str) -> int | None:
    quantity_text = re.sub(r"\bday\s*\d{1,3}\b|\d{1,3}\s*日目", " ", text)
    quantity_text = re.sub(
        r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}/\d{1,2}\b", " ", quantity_text
    )
    quantity_text = re.sub(r"(?<!\d)\d{1,2}:\d{2}\s*(?:am|pm)?(?![a-z0-9])", " ", quantity_text)
    if op == "facility_outage":
        tokens = r"トイレ|便器|restrooms?|toilets?"
        units = r"基|台|個|unit(?:s)?"
    elif op == "add_toilet":
        tokens = r"トイレ|便器|restrooms?|toilets?"
        units = r"基|台|個|unit(?:s)?"
    elif op == "add_staff":
        tokens = r"スタッフ|職員|staff|workers?"
        units = r"人|名|member(?:s)?|people|workers?"
    elif op in {"add_evacuees", "depart_evacuees"}:
        tokens = r"避難者|evacuees?|residents?"
        units = r"人|名|people|persons?|residents?"
    else:
        tokens = r"水|water"
        units = r""
    number = r"(?P<value>[+\-]?\d{1,6}|[零〇一二三四五六七八九十]+)"
    patterns = (
        rf"(?:{tokens})(?:を|が|は)?\s*{number}\s*(?:{units})?",
        rf"{number}\s*(?:{units})?(?:の|を)?\s*(?:{tokens})",
    )
    for pattern in patterns:
        match = re.search(pattern, quantity_text, re.IGNORECASE)
        if match:
            raw = match.group("value")
            if raw.startswith(("+", "-")) and raw[1:].isdigit():
                return int(raw)
            if raw.isdigit():
                return int(raw)
            return _parse_kanji_number(raw)
    return None


def _parse_kanji_number(value: str) -> int | None:
    if value in _KANJI_NUMBERS:
        return _KANJI_NUMBERS[value]
    if value == "十":
        return 10
    if len(value) == 2 and value[0] == "十" and value[1] in _KANJI_NUMBERS:
        return 10 + _KANJI_NUMBERS[value[1]]
    if len(value) == 2 and value[1] == "十" and value[0] in _KANJI_NUMBERS:
        return _KANJI_NUMBERS[value[0]] * 10
    return None


def _resolve_target(text: str, op: str, summary: ScenarioSummary) -> _TargetResolution:
    if op in {"facility_outage", "water_outage"}:
        if op == "water_outage":
            facilities = [
                item for item in summary.facilities if item.kind.casefold() == "water_station"
            ]
            eligible_zone_ids = {zone.id for zone in summary.zones if zone.has_water_station} | {
                item.zone_id for item in facilities if item.zone_id is not None
            }
            zones = [zone for zone in summary.zones if zone.id in eligible_zone_ids]
            candidates: list[tuple[str, str, str]] = [
                (zone.id, zone.name, "zone") for zone in zones
            ]
            # If the known water station cannot be scoped to a zone, retain its real ID.
            candidates.extend(
                (item.id, item.name, "facility")
                for item in facilities
                if item.zone_id is None and not zones
            )
        else:
            facilities = [item for item in summary.facilities if _is_toilet(item.kind)]
            candidates = [(item.id, item.name, "facility") for item in facilities]

        named_water_stations = (
            [
                (item.id, item.name, "facility")
                for item in facilities
                if op == "water_outage"
                and (_contains_label(text, item.id) or _contains_label(text, item.name))
            ]
            if op == "water_outage"
            else []
        )
        named = named_water_stations or [
            candidate
            for candidate in candidates
            if _contains_label(text, candidate[0]) or _contains_label(text, candidate[1])
        ]
        # Generic natural-language target names resolve only when exactly one installed
        # mechanism matches. Multiple matches stay visible as candidate choices.
        matches = named or candidates
        explicit_id = _explicit_facility_id(text)
        if explicit_id is not None and not named:
            return _TargetResolution(
                selector=None,
                issue=ParseIssue(
                    code="unknown_target",
                    message=f"指定した対象ID {explicit_id} はシナリオにありません。",
                    choices=[item[0] for item in candidates],
                ),
            )
        if not matches:
            return _TargetResolution(
                selector=None,
                issue=ParseIssue(
                    code="unknown_target",
                    message="シナリオ内に対応する対象がありません。存在するIDを指定してください。",
                    choices=[],
                ),
            )
        if len(matches) == 1:
            target_id, _, target_kind = matches[0]
            return _TargetResolution(selector=TargetSelector(kind=target_kind, target_id=target_id))
        candidate_ids = [item[0] for item in matches]
        ambiguity = Ambiguity(
            code="ambiguous_target",
            field="target_selector",
            message="対象を一つ選択してください。",
            choices=candidate_ids,
        )
        return _TargetResolution(
            selector=TargetSelector(kind=matches[0][2], candidate_ids=candidate_ids),
            ambiguity=ambiguity,
        )

    candidates = summary.zones
    target_kind = "zone"
    named_zones = [
        zone
        for zone in candidates
        if _contains_label(text, zone.id) or _contains_label(text, zone.name)
    ]
    explicit_zone_id = _explicit_zone_id(text)
    if explicit_zone_id is not None:
        matches = [zone for zone in candidates if zone.id == explicit_zone_id]
        if not matches:
            return _TargetResolution(
                selector=None,
                issue=ParseIssue(
                    code="unknown_target",
                    message=f"指定したZone ID {explicit_zone_id} はシナリオにありません。",
                    choices=[zone.id for zone in candidates],
                ),
            )
    elif named_zones:
        matches = named_zones
    elif summary.default_zone_id is not None:
        matches = [zone for zone in candidates if zone.id == summary.default_zone_id]
    else:
        matches = candidates
    if not matches:
        return _TargetResolution(
            selector=None,
            issue=ParseIssue(
                code="unknown_target",
                message="対象Zoneがシナリオにありません。存在するZone IDを指定してください。",
            ),
        )
    if len(matches) == 1:
        return _TargetResolution(selector=TargetSelector(kind=target_kind, target_id=matches[0].id))
    candidate_ids = [zone.id for zone in matches]
    ambiguity = Ambiguity(
        code="ambiguous_target",
        field="target_selector",
        message="追加先Zoneを選択してください。",
        choices=candidate_ids,
    )
    return _TargetResolution(
        selector=TargetSelector(kind=target_kind, candidate_ids=candidate_ids),
        ambiguity=ambiguity,
    )


def _is_toilet(kind: str) -> bool:
    return kind.casefold() in {"toilet", "restroom", "toilet_facility", "sanitation"}


def _explicit_facility_id(text: str) -> str | None:
    match = re.search(
        r"\b(?:toilet|restroom|facility|water_station)[_:#-]([a-z0-9][a-z0-9._:-]*)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(0)
    match = re.search(
        r"\b(?:toilet|restroom|facility|water station)\s+([a-z][a-z0-9._:-]*)",
        text,
        re.IGNORECASE,
    )
    if match and match.group(1).casefold() not in {
        "fail",
        "fails",
        "failed",
        "failure",
        "broken",
        "outage",
        "is",
        "was",
    }:
        return match.group(0)
    return None


def _explicit_zone_id(text: str) -> str | None:
    match = re.search(
        r"\b(?:zone|zone_id)\s*[:=#]?\s*([a-z][a-z0-9._:-]*)",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _contains_label(text: str, label: str) -> bool:
    # IDs and display names are literal scenario data; no user text becomes a pattern.
    normalized = unicodedata.normalize("NFKC", label).casefold().strip()
    if normalized in {"トイレ", "便器", "toilet", "restroom", "staff", "water", "水"}:
        return False
    if not normalized:
        return False
    if normalized.isascii() and (normalized[0].isalnum() or normalized[0] == "_"):
        return bool(
            re.search(
                rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])",
                text,
                re.IGNORECASE,
            )
        )
    return normalized in text


def _selected_facility(selector: TargetSelector, summary: ScenarioSummary):
    if selector.target_id is None:
        return None
    return next((item for item in summary.facilities if item.id == selector.target_id), None)


def _parse_time_window(text: str, summary: ScenarioSummary) -> _TimeWindow | ParseIssue | None:
    day_match = re.search(r"\bday\s*(\d{1,3})\b|(\d{1,3})\s*日目", text, re.IGNORECASE)
    full_date_match = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    short_date_match = None if full_date_match else re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)

    time_tokens = _find_time_tokens(text)
    if re.search(
        r"翌朝|翌日(?:の)?(?:朝|午前|昼)?|次(?:の|の日の)?朝|\b(?:next|following)\s+(?:morning|day)\b",
        text,
        re.IGNORECASE,
    ):
        return ParseIssue(
            code="ambiguous_time",
            message="『翌朝』『翌日』は対象イベントとの基点が曖昧です。各作用のDay番号と時刻を指定してください。",
            choices=["HH:MM"],
        )
    day_references = re.findall(
        r"\bday\s*\d{1,3}\b|\d{1,3}\s*日目|\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
        text,
        re.IGNORECASE,
    )
    if len(day_references) > 1 and len(time_tokens) > 1:
        return ParseIssue(
            code="ambiguous_time",
            message="複数のイベント時刻を一つの作用時刻にまとめられません。各作用にDay番号と時刻を指定してください。",
            choices=["HH:MM"],
        )
    if (
        re.search(
            r"午後|午前|朝|昼|正午|夕方|夜|\b(?:afternoon|morning|evening|night|noon|midday)\b",
            text,
            re.IGNORECASE,
        )
        and not time_tokens
    ):
        return ParseIssue(
            code="ambiguous_time",
            message="『午後』などの幅がある時間表現だけでは時刻を決められません。具体的な時刻を指定してください。",
            choices=["HH:MM"],
        )
    if len(time_tokens) > 2:
        return ParseIssue(
            code="ambiguous_time",
            message="複数の時刻を読み取れません。開始・終了を一つずつ指定してください。",
        )
    if len(time_tokens) == 2 and not re.search(
        r"から.{0,24}まで|\bto\b|[-–—]", text, re.IGNORECASE
    ):
        return ParseIssue(
            code="ambiguous_time",
            message="複数の時刻があるため、開始と終了の範囲を明示してください。",
        )

    base_day: int | None = None
    if day_match:
        day = int(day_match.group(1) or day_match.group(2))
        if day < 1 or day > 366:
            return ParseIssue(code="invalid_time", message="Dayは1から366で指定してください。")
        base_day = (day - 1) * 1440
    elif full_date_match:
        year, month, day = map(int, full_date_match.groups())
        try:
            requested = date(year, month, day)
        except ValueError:
            return ParseIssue(code="invalid_time", message="日付が正しくありません。")
        if summary.start_date is None:
            return ParseIssue(
                code="missing_date_anchor",
                message="日付をシミュレーション分へ変換する開始日がありません。Day番号を指定してください。",
                choices=["Day N"],
            )
        base_day = (requested - summary.start_date).days * 1440
        if base_day < 0:
            return ParseIssue(code="invalid_time", message="シナリオ開始日より前の日付です。")
    elif short_date_match:
        if summary.start_date is None:
            return ParseIssue(
                code="missing_date_anchor",
                message="月日表記を変換する開始日がありません。Day番号を指定してください。",
                choices=["Day N"],
            )
        month, day = map(int, short_date_match.groups())
        try:
            requested = date(summary.start_date.year, month, day)
        except ValueError:
            return ParseIssue(code="invalid_time", message="日付が正しくありません。")
        base_day = (requested - summary.start_date).days * 1440
        if base_day < 0:
            return ParseIssue(code="invalid_time", message="シナリオ開始日より前の日付です。")

    if not time_tokens and base_day is None:
        return None
    start_time = time_tokens[0] if time_tokens else (0, 0)
    start = (base_day or 0) + start_time[0] * 60 + start_time[1]
    end = None
    if len(time_tokens) == 2:
        end_hour, end_minute = time_tokens[1]
        end = (base_day or 0) + end_hour * 60 + end_minute
        if end <= start:
            return ParseIssue(
                code="invalid_time_range",
                message="終了時刻は開始時刻より後にしてください。日をまたぐ指定はDay番号も付けてください。",
            )
    return _TimeWindow(start_minute=start, end_minute=end)


def _find_time_tokens(text: str) -> list[tuple[int, int]]:
    patterns = (
        re.compile(r"(?<!\d)(\d{1,2}):(\d{2})\s*(am|pm)?(?!\d)", re.IGNORECASE),
        re.compile(r"(?<!\d)(\d{1,2})\s*(am|pm)(?![a-z])", re.IGNORECASE),
        re.compile(r"(午前|午後)\s*(\d{1,2})(?:\s*時)?(?:\s*(\d{1,2})\s*分)?"),
        re.compile(r"(?<!\d)(\d{1,2})\s*時(?:\s*(\d{1,2})\s*分)?"),
    )
    matches: list[tuple[int, int, int, int]] = []
    occupied: list[tuple[int, int]] = []
    for priority, pattern in enumerate(patterns):
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in occupied):
                continue
            groups = match.groups()
            if priority == 0:
                hour, minute, period = int(groups[0]), int(groups[1]), groups[2]
            elif priority == 1:
                hour, minute, period = int(groups[0]), 0, groups[1]
            elif priority == 2:
                period, hour, minute = groups[0], int(groups[1]), int(groups[2] or 0)
            else:
                hour, minute, period = int(groups[0]), int(groups[1] or 0), None
            if minute > 59 or hour > 24:
                continue
            if period:
                if hour < 1 or hour > 12:
                    continue
                if period.casefold() == "pm" or period == "午後":
                    hour = hour % 12 + 12
                else:
                    hour %= 12
            elif hour == 24 and minute != 0:
                continue
            if hour == 24:
                continue
            matches.append((match.start(), match.end(), hour, minute))
            occupied.append((match.start(), match.end()))
    return [(hour, minute) for _, _, hour, minute in sorted(matches)]


OPUS_COMPILER_VERSION = "h07-opus-effect-compiler-v1"
OPUS_COMPILER_PROMPT_VERSION = "geometry-population-effect-compiler-v2"
_OPUS_REPAIR_PROMPT_VERSION = "h07-opus-effect-compiler-v1-repair1"
_OPUS_SOURCE_ID = "opus-effect-compiler-v1"
_MAX_OPUS_REPAIRS = 1
_MAX_COMPILER_MINUTE = 60 * 24 * 366
_COMPILER_INSTRUCTIONS = (
    "Interpret the untrusted Japanese narrative using only the installed primitive registry "
    "and listed scenario targets. Return declarative effects, never code, scripts, event names, "
    "or direct state/KPI edits. Use 2 or more effects when the narrative has multiple supported "
    "mechanisms. If target, time, or scope is materially ambiguous, request clarification. If a "
    "necessary mechanism is absent, return not_simulatable and name the missing mechanism. "
    "For approximate mappings, state the assumed value and units and list excluded phenomena. "
    "The mechanism_summary explains the proposed mapping only; it is not an observed outcome or "
    "a real-world prediction. Preserve the requested time and scope."
)


def scenario_summary_from_world(
    world: WorldState,
    *,
    default_zone_id: str | None = None,
    start_date: date | None = None,
) -> ScenarioSummary:
    """Build an anonymous, current-state compiler context without advancing the world."""
    operational = world.model_state.get(OPERATIONAL_EVENTS_KEY, {})
    staff_status = operational.get("staff_status", {}) if isinstance(operational, dict) else {}
    available_staff = [
        staff_id
        for staff_id in sorted(world.staff)
        if staff_id not in staff_status or staff_status[staff_id].get("available", False)
    ]
    pending_by_id = {event.event_id: event for event in world.event_queue.pending()}
    shipments: list[ShipmentTarget] = []
    if isinstance(operational, dict):
        for shipment_id, record in sorted(operational.get("shipments", {}).items()):
            event = pending_by_id.get(record.get("event_id")) if isinstance(record, dict) else None
            if event is not None and event.event_type == "resource_delivery":
                resource_id = record.get("resource_id", event.payload.get("resource_id"))
                if isinstance(resource_id, str):
                    shipments.append(
                        ShipmentTarget(
                            id=shipment_id,
                            resource_id=resource_id,
                            due_minute=event.due_minute,
                        )
                    )

    ledger = world.model_state.get(RESOURCE_LEDGER_KEY)
    resource_ids: list[str] = []
    warehouse_id = None
    if isinstance(ledger, dict):
        accounts = ledger.get("accounts", {})
        resource_ids = sorted(accounts) if isinstance(accounts, dict) else []
        warehouse_id = ledger.get("warehouse_id")

    # The facility/staff structural handlers are part of the core runtime even when a
    # particular scenario starts with zero units. Other policies are scenario opt-ins.
    policies: set[str] = {"facility_capacity", "staff_operations"}
    if RESOURCE_LEDGER_KEY in world.model_state:
        policies.add("resource_ledger_v1")
    if OPERATIONAL_EVENTS_KEY in world.model_state:
        policies.add("operational_events_v1")
    if "population_dynamics_v1" in world.model_state:
        policies.add("population_dynamics_v1")
    if ("population_lifecycle_v1" in world.model_state
            and "population_presence_v1" in world.model_state):
        policies.add("population_lifecycle_v1")
        policies.add("population_presence_v1")
    if "geometry_overlay_v1" in world.model_state:
        policies.add("geometry_overlay_v1")
    if "resource_service_v1" in world.model_state:
        policies.add("resource_service_v1")
    if ZONE_ENVIRONMENT_STATE_KEY in world.model_state:
        policies.add(ZONE_ENVIRONMENT_POLICY)

    zones = [
        ZoneTarget(
            id=zone.id,
            name=zone.name,
            has_water_station=any(
                facility.kind == "water_station" and facility.zone_id == zone.id
                for facility in world.facilities.values()
            ),
        )
        for zone in sorted(world.zones.values(), key=lambda item: item.id)
    ]
    if default_zone_id is None and len(zones) == 1:
        default_zone_id = zones[0].id

    return ScenarioSummary(
        duration_minutes=world.duration_minutes,
        start_date=start_date,
        default_zone_id=default_zone_id,
        agent_count=len(world.agents),
        facilities=[
            FacilityTarget(
                id=facility.id,
                name=facility.id,
                kind=facility.kind,
                zone_id=facility.zone_id,
                capacity=facility.capacity,
                working_units=facility.working_units,
                active_occupancy=len(facility.active_agent_ids)
                + len(getattr(facility, "resource_active_agent_ids", [])),
            )
            for facility in sorted(world.facilities.values(), key=lambda item: item.id)
        ],
        zones=zones,
        staff_ids=sorted(world.staff),
        available_staff_ids=available_staff,
        resource_ids=resource_ids,
        warehouse_id=warehouse_id,
        pending_shipments=shipments,
        available_policies=sorted(policies),
        person_ids=sorted(set(world.agents) | set(world.model_state.get("population_lifecycle_v1", {}).get("people", {})))[:10000],
        person_targets_truncated=len(set(world.agents) | set(world.model_state.get("population_lifecycle_v1", {}).get("people", {}))) > 10000,
        route_ids=sorted(world.zone_edges)[:2000],
        zone_environment_settings=(
            world.model_configs["zone_environment"]
            if "zone_environment" in world.model_configs
            else None
        ),
    )


def compile_natural_language(
    request: ParseRequest | dict[str, object],
    router: LLMRouter | None = None,
    *,
    registry: PrimitiveRegistry = DEFAULT_PRIMITIVE_REGISTRY,
) -> CompilerResult:
    """Compile free text to a validated, approval-only EffectProgram preview.

    Opus receives a bounded snapshot and only primitives whose required policies and target
    kinds are present. On provider failure, the known offline parser is used only when it can
    produce a context-valid plan; every other narrative returns `not_simulatable`.
    """
    if not isinstance(request, ParseRequest):
        request = ParseRequest.model_validate(request)
    active_router = router or LLMRouter()
    summary = request.scenario_summary
    target_ids = _target_ids_by_kind(summary)
    eligible = _eligible_primitive_specs(summary, target_ids, registry)
    output_schema = _compiler_output_schema(summary, target_ids, eligible)
    sanitized_text = _sanitize_narrative(request.text)
    base_payload = _compiler_payload(
        request, sanitized_text, summary, eligible, target_ids, registry
    )
    state_hash = _sha256(
        _canonical_json(
            {
                "branch_id": request.branch_id,
                "selected_minute": request.selected_minute,
                "scenario_summary": summary.model_dump(mode="json"),
                "primitive_registry": [item.op for item in eligible],
            }
        )
    )
    prompt_hash = _sha256(
        _canonical_json(
            {
                "instructions": _COMPILER_INSTRUCTIONS,
                "prompt_version": OPUS_COMPILER_PROMPT_VERSION,
                "output_schema": output_schema,
            }
        )
    )
    offline_result = parse_natural_language(request)

    if not eligible:
        return _offline_compiler_result(
            request,
            offline_result,
            registry=registry,
            eligible=eligible,
            target_ids=target_ids,
            usage=_empty_compiler_usage(prompt_hash, _sha256(_canonical_json(base_payload))),
            fallback_used=False,
            reason=("この分岐に登録済みのprimitiveと有効policyの組み合わせがありません。"),
        )

    fallback_text = _compiler_fallback_text()
    task = _compiler_task(
        base_payload,
        state_hash=state_hash,
        output_schema=output_schema,
        fallback_text=fallback_text,
        prompt_version=OPUS_COMPILER_PROMPT_VERSION,
    )
    record_start = len(active_router.records)
    accepted: dict[str, Any] | None = None
    semantic_errors: list[str] = []
    try:
        response = active_router.complete(task)
        records = active_router.records[record_start:]
        if not records or not records[-1].fallback_used:
            try:
                accepted = _parse_compiler_output(response, request, registry, eligible, target_ids)
            except (ValueError, ValidationError, TypeError, KeyError) as exc:
                semantic_errors = _error_messages(exc)

        if accepted is None and semantic_errors and records and not records[-1].fallback_used:
            repair_payload = {
                **base_payload,
                "repair_request": {
                    "previous_candidate": response,
                    "validation_errors": semantic_errors[:20],
                    "instruction": (
                        "Return one corrected complete JSON object. "
                        "Do not add unsupported mechanics."
                    ),
                },
            }
            repair_task = _compiler_task(
                repair_payload,
                state_hash=state_hash,
                output_schema=output_schema,
                fallback_text=fallback_text,
                prompt_version=_OPUS_REPAIR_PROMPT_VERSION,
            )
            try:
                repaired = active_router.complete(repair_task)
                records = active_router.records[record_start:]
                if records and not records[-1].fallback_used:
                    accepted = _parse_compiler_output(
                        repaired, request, registry, eligible, target_ids
                    )
            except LLMExecutionError:
                accepted = None
            except (ValueError, ValidationError, TypeError, KeyError) as exc:
                semantic_errors = _error_messages(exc)
                accepted = None
    except LLMExecutionError:
        accepted = None

    records = active_router.records[record_start:]
    usage = _compiler_usage(records, task, prompt_hash)
    if accepted is None:
        failure_reason = (
            "Opusの案をschema/domain検証できませんでした: " + "; ".join(semantic_errors)
            if semantic_errors
            else _provider_failure_reason(records)
        )
        return _offline_compiler_result(
            request,
            offline_result,
            registry=registry,
            eligible=eligible,
            target_ids=target_ids,
            usage=usage,
            fallback_used=True,
            reason=failure_reason,
        )

    return _compiler_result_from_output(
        accepted,
        request,
        usage=usage,
        prompt_hash=prompt_hash,
        fallback_used=False,
        registry=registry,
        eligible=eligible,
        target_ids=target_ids,
    )


def _target_ids_by_kind(summary: ScenarioSummary) -> dict[str, list[str]]:
    return {
        "facility": [item.id for item in summary.facilities],
        "zone": [item.id for item in summary.zones],
        "staff": list(summary.available_staff_ids),
        "resource": list(summary.resource_ids),
        "shipment": [item.id for item in summary.pending_shipments],
        "person": list(summary.person_ids),
        "route": list(summary.route_ids),
    }


def _eligible_primitive_specs(
    summary: ScenarioSummary,
    target_ids: dict[str, list[str]],
    registry: PrimitiveRegistry,
) -> list[PrimitiveSpec]:
    policies = set(summary.available_policies)
    return [
        spec
        for spec in registry.operators
        if spec.required_policies <= policies
        and any(target_ids.get(kind) for kind in spec.target_kinds)
    ]


def _compiler_payload(
    request: ParseRequest,
    sanitized_text: str,
    summary: ScenarioSummary,
    eligible: list[PrimitiveSpec],
    target_ids: dict[str, list[str]],
    registry: PrimitiveRegistry,
) -> dict[str, Any]:
    descriptions = {item["op"]: item for item in available_primitives(registry)}
    return {
        "task": "Compile a free-form shelter intervention into a typed EffectProgram preview.",
        "instructions": _COMPILER_INSTRUCTIONS,
        "population_geometry_rules": [
            "Reunion uses population_presence(action=reunite), candidate_ids with at least two known people and target_id as one anchor; other operations use one target_id.",
            "Geometry operators are bounded: same-zone idle permanent moves; temporary partitions and route closures require an end minute. Never infer missing coordinates or claim an unsupported layout change succeeded.",
            "A capability or compiler result is only a proposal; runtime geometry, capacity, identity, approval and effect validation still apply.",
        ],
        "untrusted_narrative": sanitized_text,
        "branch": {
            "branch_id": request.branch_id,
            "selected_minute": request.selected_minute,
        },
        "scenario": summary.model_dump(mode="json"),
        "target_ids_by_kind": target_ids,
        "installed_primitives": [
            descriptions[spec.op] for spec in eligible if spec.op in descriptions
        ],
    }


def _compiler_output_schema(
    summary: ScenarioSummary,
    target_ids: dict[str, list[str]],
    eligible: list[PrimitiveSpec],
) -> dict[str, Any]:
    operator_names = sorted(item.op for item in eligible)
    target_kinds = sorted(
        {kind for item in eligible for kind in item.target_kinds if target_ids.get(kind)}
    )
    valid_target_ids = sorted({value for kind in target_kinds for value in target_ids[kind]})
    maximum_minute = (
        summary.duration_minutes - 1
        if summary.duration_minutes is not None
        else _MAX_COMPILER_MINUTE - 1
    )
    parameter_properties: dict[str, dict[str, Any]] = {}
    for primitive in eligible:
        model_schema = primitive.parameters_model.model_json_schema()
        for key, property_schema in model_schema.get("properties", {}).items():
            _merge_parameter_schema(parameter_properties, key, _inline_parameter_schema(property_schema, model_schema.get("$defs", {})))

    effect_schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": operator_names},
            "target_kind": {"type": "string", "enum": target_kinds},
            "target_id": {"type": "string", "enum": valid_target_ids},
            "candidate_ids": {"type": "array", "items": {"type": "string", "enum": valid_target_ids}, "minItems": 2, "maxItems": 100},
            "start_minute": {"type": "integer", "minimum": 0, "maximum": maximum_minute},
            "end_minute": {
                "type": "integer",
                "minimum": 0,
                "maximum": summary.duration_minutes or _MAX_COMPILER_MINUTE,
            },
            "parameters": {
                "type": "object",
                "properties": parameter_properties,
                "required": [],
                "additionalProperties": False,
            },
            "assumption_indexes": {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": 99},
                "maxItems": 50,
            },
        },
        "required": ["op", "target_kind", "target_id", "start_minute", "parameters"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["compiled", "clarification_required", "not_simulatable"],
            },
            "support_mode": {
                "type": "string",
                "enum": [item.value for item in SupportMode],
            },
            "mechanism_summary": {"type": "string", "maxLength": 500},
            "effects": {
                "type": "array",
                "minItems": 0,
                "maxItems": 50 if eligible else 0,
                "items": effect_schema,
            },
            "assumptions": {
                "type": "array",
                "minItems": 0,
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string", "maxLength": 500},
                        "units": {"type": "string", "maxLength": 64},
                        "value": {"type": "string", "maxLength": 120},
                    },
                    "required": ["description", "units", "value"],
                    "additionalProperties": False,
                },
            },
            "limitations": {
                "type": "array",
                "minItems": 0,
                "maxItems": 50,
                "items": {"type": "string", "maxLength": 500},
            },
            "questions": {
                "type": "array",
                "minItems": 0,
                "maxItems": 10,
                "items": {"type": "string", "maxLength": 500},
            },
            "choices": {
                "type": "array",
                "minItems": 0,
                "maxItems": 100,
                "items": {"type": "string", "enum": valid_target_ids},
            },
        },
        "required": [
            "status",
            "support_mode",
            "mechanism_summary",
            "effects",
            "assumptions",
            "limitations",
            "questions",
            "choices",
        ],
        "additionalProperties": False,
    }


def _inline_parameter_schema(value: Any, definitions: dict[str, Any], depth: int = 0) -> Any:
    """Resolve code-owned local model references before embedding in compiler schema."""
    if depth > 24:
        raise ValueError("recursive or excessively nested primitive schema")
    if isinstance(value, list):
        return [_inline_parameter_schema(item, definitions, depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/$defs/") or reference[8:] not in definitions:
            raise ValueError("primitive schema has an unsupported external reference")
        return _inline_parameter_schema({**definitions[reference[8:]], **{k:v for k,v in value.items() if k != "$ref"}}, definitions, depth + 1)
    return {key: _inline_parameter_schema(item, definitions, depth + 1) for key, item in value.items()}


def _merge_parameter_schema(
    properties: dict[str, dict[str, Any]], key: str, incoming: dict[str, Any]
) -> None:
    current = properties.get(key)
    if current is None:
        properties[key] = json.loads(json.dumps(incoming))
        return
    if current.get("type") != incoming.get("type"):
        current["type"] = (
            "number"
            if {current.get("type"), incoming.get("type")}
            <= {
                "integer",
                "number",
            }
            else "string"
        )
        current.pop("enum", None)
        return
    for bound in ("minimum", "maximum", "minLength", "maxLength"):
        if bound in incoming:
            operation = min if bound in {"minimum", "minLength"} else max
            current[bound] = operation(current.get(bound, incoming[bound]), incoming[bound])
    if "enum" in incoming:
        current["enum"] = sorted(set(current.get("enum", [])) | set(incoming["enum"]))


def _compiler_task(
    payload: dict[str, Any],
    *,
    state_hash: str,
    output_schema: dict[str, Any],
    fallback_text: str,
    prompt_version: str,
) -> LLMTask:
    return LLMTask(
        stage=LLMStage.INTERVENTION,
        input_text=_canonical_json(payload),
        state_hash=state_hash,
        prompt_version=prompt_version,
        schema_version=1,
        output_schema=output_schema,
        fallback_text=fallback_text,
        max_output_tokens=4096,
    )


def _compiler_fallback_text() -> str:
    return _canonical_json(
        {
            "status": "not_simulatable",
            "support_mode": SupportMode.NOT_SIMULATABLE.value,
            "mechanism_summary": "オフライン時の固定応答です。適用可能な案は確定していません。",
            "effects": [],
            "assumptions": [],
            "limitations": [
                "Opus compiler unavailable; use offline parser only for its recognized forms."
            ],
            "questions": [],
            "choices": [],
        }
    )


def _parse_compiler_output(
    response: str,
    request: ParseRequest,
    registry: PrimitiveRegistry,
    eligible: list[PrimitiveSpec],
    target_ids: dict[str, list[str]],
) -> dict[str, Any]:
    document = json.loads(response, parse_constant=_reject_json_constant)
    if not isinstance(document, dict):
        raise ValueError("compiler output must be one JSON object")
    status = document["status"]
    support_mode = SupportMode(document["support_mode"])
    effects = document["effects"]
    assumptions = document["assumptions"]
    if status == "not_simulatable":
        if support_mode is not SupportMode.NOT_SIMULATABLE or effects:
            raise ValueError("not_simulatable output cannot contain executable effects")
        if not document["limitations"]:
            raise ValueError("not_simulatable output must explain the missing mechanism")
    elif status == "clarification_required":
        if support_mode is not SupportMode.CLARIFICATION_REQUIRED or effects:
            raise ValueError("clarification output cannot contain executable effects")
        if not document["questions"] and not document["choices"]:
            raise ValueError("clarification output must ask a question or offer choices")
        if not set(document["choices"]) <= {
            target_id for values in target_ids.values() for target_id in values
        }:
            raise ValueError("clarification choices must be existing scenario IDs")
    elif status == "compiled":
        if support_mode not in {
            SupportMode.NATIVE,
            SupportMode.COMPOSED,
            SupportMode.APPROXIMATE,
        }:
            raise ValueError("compiled output needs a supported or explicitly approximate mode")
        if not effects:
            raise ValueError("compiled output must contain at least one effect")
        if support_mode is SupportMode.APPROXIMATE and (
            not assumptions or not document["limitations"]
        ):
            raise ValueError("approximate output requires assumptions and limitations")
        if support_mode is SupportMode.COMPOSED and len({item["op"] for item in effects}) < 2:
            raise ValueError("composed output must combine at least two primitive operators")
        _validate_model_effects(document, request.scenario_summary, registry, eligible, target_ids)
    else:
        raise ValueError(f"unknown compiler status {status!r}")
    mechanism_summary = document["mechanism_summary"]
    if (
        not isinstance(mechanism_summary, str)
        or not mechanism_summary.strip()
        or len(mechanism_summary) > 1000
    ):
        raise ValueError("mechanism_summary must describe the proposed mapping")
    for field in ("limitations", "questions"):
        maximum = 500 if field == "limitations" else 10
        if len(document[field]) > maximum or any(
            not isinstance(item, str) or not item.strip() or len(item) > 500
            for item in document[field]
        ):
            raise ValueError(f"{field} must contain bounded, non-empty text")
    if status == "compiled":
        for index, item in enumerate(assumptions, start=1):
            PlanAssumption(
                assumption_id=f"assumption-{index:02d}",
                description=item["description"],
                source_id=_OPUS_SOURCE_ID,
                units=item["units"],
                value=item["value"],
            )
    return document


def _validate_model_effects(
    document: dict[str, Any],
    summary: ScenarioSummary,
    registry: PrimitiveRegistry,
    eligible: list[PrimitiveSpec],
    target_ids: dict[str, list[str]],
) -> None:
    eligible_by_name = {item.op: item for item in eligible}
    facilities = {item.id: item for item in summary.facilities}
    shipments = {item.id: item for item in summary.pending_shipments}
    assumptions = document["assumptions"]
    effects = document["effects"]
    if len(effects) > 50 or sum(2 if "end_minute" in item else 1 for item in effects) > 100:
        raise ValueError("program exceeds the bounded effect/event count")
    staff_additions = 0
    toilet_additions = 0
    for index, effect in enumerate(effects):
        op = effect["op"]
        primitive = registry.get(op)
        if primitive is None or op not in eligible_by_name:
            raise ValueError(f"effect {index} uses an unavailable primitive {op!r}")
        if not primitive.required_policies <= set(summary.available_policies):
            raise ValueError(f"effect {index} requires an unavailable policy")
        target_kind = effect["target_kind"]
        target_id = effect["target_id"]
        if target_kind not in primitive.target_kinds or target_id not in target_ids.get(
            target_kind, []
        ):
            raise ValueError(f"effect {index} references an unknown or incompatible target")
        candidates = effect.get("candidate_ids", [])
        if candidates and (op != "population_presence" or effect["parameters"].get("action") != "reunite"
                or target_kind != "person" or target_id not in candidates or len(set(candidates)) != len(candidates)
                or not set(candidates) <= set(summary.person_ids)):
            raise ValueError("candidate_ids are restricted to known people in a reunion")
        if op == "population_presence" and effect["parameters"].get("action") == "reunite" and len(candidates) < 2:
            raise ValueError("reunion requires candidate_ids including its target_id anchor")
        if effect["start_minute"] >= (summary.duration_minutes or _MAX_COMPILER_MINUTE):
            raise ValueError(f"effect {index} starts outside the scenario")
        end_minute = effect.get("end_minute")
        if end_minute is not None:
            if end_minute <= effect["start_minute"]:
                raise ValueError(f"effect {index} has an invalid time window")
            if summary.duration_minutes is not None and end_minute > summary.duration_minutes:
                raise ValueError(f"effect {index} ends outside the scenario")
        if end_minute is not None and not primitive.timed:
            raise ValueError(f"effect {index} adds end_minute to a one-shot operation")
        parameters = primitive.parameters_model.model_validate(effect["parameters"])
        parameter_values = parameters.model_dump(mode="json")
        _derive_effect_units(primitive, parameter_values)
        if any(item >= len(assumptions) for item in effect.get("assumption_indexes", [])):
            raise ValueError(f"effect {index} references an undeclared assumption")

        if op in {"set_facility_availability", "facility_outage"}:
            facility = facilities.get(target_id)
            if facility is None:
                raise ValueError(f"effect {index} references an unknown facility")
            if op == "facility_outage" and parameters.units > facility.working_units:
                raise ValueError(f"effect {index} outage exceeds working units")
            if op == "set_facility_availability" and (
                parameters.working_units > facility.working_units
                or parameters.working_units < facility.active_occupancy
            ):
                raise ValueError(f"effect {index} availability conflicts with current occupancy")
        elif op == "deliver_resource":
            if (
                parameters.warehouse_id != summary.warehouse_id
                or parameters.resource_id != target_id
                or target_id not in summary.resource_ids
            ):
                raise ValueError(f"effect {index} must match the installed resource ledger")
        elif op == "modify_supply_schedule":
            shipment = shipments.get(target_id)
            if shipment is None or parameters.shipment_id != target_id:
                raise ValueError(f"effect {index} must reference a known pending shipment")
            if parameters.to_minute <= max(shipment.due_minute, effect["start_minute"]):
                raise ValueError(f"effect {index} must delay a shipment to a later minute")
            if (
                summary.duration_minutes is not None
                and parameters.to_minute >= summary.duration_minutes
            ):
                raise ValueError(f"effect {index} delays a shipment beyond the scenario")
        elif op == "set_staff_availability":
            if target_id not in summary.available_staff_ids or end_minute is None:
                raise ValueError(f"effect {index} requires available staff and an expiry minute")
        elif op == "water_outage":
            if end_minute is None:
                raise ValueError(f"effect {index} requires an explicit water-restoration minute")
            if target_kind == "facility":
                facility = facilities.get(target_id)
                if facility is None or facility.kind != "water_station":
                    raise ValueError(f"effect {index} must target a water station")
            elif not next(
                (item for item in summary.zones if item.id == target_id and item.has_water_station),
                None,
            ):
                raise ValueError(f"effect {index} zone has no water station")
        elif op in {"set_ambient_noise", "modify_demand_rate"}:
            settings = summary.zone_environment_settings
            if end_minute is None or settings is None:
                raise ValueError(
                    f"effect {index} requires the enabled zone-environment policy and expiry"
                )
            if summary.duration_minutes is not None and end_minute >= summary.duration_minutes:
                raise ValueError(f"effect {index} expiry must be before the scenario end")
            if op == "modify_demand_rate":
                if not (
                    settings.min_probability_multiplier
                    <= parameters.probability_multiplier
                    <= settings.max_probability_multiplier
                ):
                    raise ValueError(f"effect {index} exceeds configured demand-rate limits")
                if parameters.action in {"fetch_water", "collect_meal"} and (
                    "resource_service_v1" not in summary.available_policies
                ):
                    raise ValueError(f"effect {index} action requires resource-service policy")
        elif op == "add_staff":
            staff_additions += parameters.count
        elif op == "add_toilet":
            toilet_additions += parameters.count

    if len(summary.staff_ids) + staff_additions > 100:
        raise ValueError("program exceeds the 100-staff runtime limit")
    if len(summary.facilities) + toilet_additions > 200:
        raise ValueError("program exceeds the 200-facility runtime limit")


def _derive_effect_units(primitive: PrimitiveSpec, parameters: dict[str, Any]) -> str:
    if len(primitive.units) == 1:
        return next(iter(primitive.units))
    resource_id = parameters.get("resource_id")
    if resource_id in primitive.units:
        return resource_id
    raise ValueError(f"primitive {primitive.op!r} needs an explicit registered unit")


def _compiler_result_from_output(
    document: dict[str, Any],
    request: ParseRequest,
    *,
    usage: CompilerUsage,
    prompt_hash: str,
    fallback_used: bool,
    registry: PrimitiveRegistry,
    eligible: list[PrimitiveSpec],
    target_ids: dict[str, list[str]],
) -> CompilerResult:
    status = document["status"]
    mode = SupportMode(document["support_mode"])
    if status == "not_simulatable":
        return CompilerResult(
            status=status,
            support_mode=mode,
            mechanism_summary=document["mechanism_summary"],
            limitations=document["limitations"],
            questions=document["questions"],
            choices=document["choices"],
            fallback_used=fallback_used,
            usage=usage,
        )
    if status == "clarification_required":
        return CompilerResult(
            status=status,
            support_mode=mode,
            mechanism_summary=document["mechanism_summary"],
            limitations=document["limitations"],
            questions=document["questions"],
            choices=document["choices"],
            fallback_used=fallback_used,
            usage=usage,
        )

    response_hash = _sha256(_canonical_json(document))
    program_id = (
        "opus-"
        + _sha256(
            f"{request.branch_id}\0{request.text}\0{request.selected_minute}\0{usage.input_hash}\0{response_hash}"
        )[:24]
    )
    assumptions: list[PlanAssumption] = []
    assumption_ids: list[str] = []
    for index, item in enumerate(document["assumptions"], start=1):
        assumption_id = f"assumption-{index:02d}-{_sha256(program_id + str(index))[:10]}"
        assumption_ids.append(assumption_id)
        assumptions.append(
            PlanAssumption(
                assumption_id=assumption_id,
                description=item["description"],
                source_id=_OPUS_SOURCE_ID,
                units=item["units"],
                value=item["value"],
            )
        )

    specs = {item.op: item for item in eligible}
    planned_effects: list[TimedAction] = []
    starts: list[int] = []
    for index, item in enumerate(document["effects"], start=1):
        primitive = specs[item["op"]]
        start_minute = item["start_minute"]
        starts.append(start_minute)
        effect_id = f"effect-{index:02d}-{_sha256(program_id + item['op'] + str(index))[:10]}"
        referenced_assumptions = [
            assumption_ids[assumption_index]
            for assumption_index in item.get("assumption_indexes", [])
        ]
        parameter_values = primitive.parameters_model.model_validate(item["parameters"]).model_dump(
            mode="json"
        )
        planned_effects.append(
            TimedAction(
                effect_id=effect_id,
                op=item["op"],
                target_selector=TargetSelector(
                    kind=item["target_kind"], target_id=None if item.get("candidate_ids") else item["target_id"],
                    candidate_ids=item.get("candidate_ids", [])
                ),
                start_minute=start_minute,
                end_minute=item.get("end_minute"),
                parameters=parameter_values,
                units=_derive_effect_units(primitive, parameter_values),
                source_id=_OPUS_SOURCE_ID,
                assumption_ids=referenced_assumptions,
                policy_dependencies=sorted(primitive.required_policies),
                causation_id=None,
                reversible=primitive.timed,
            )
        )

    fork_at_minute = min(request.selected_minute, min(starts))
    plan = DraftPlan(
        schema_version=1,
        program_id=program_id,
        narrative=document["mechanism_summary"],
        source_text=request.text,
        simulation_start_minute=fork_at_minute,
        effects=planned_effects,
        assumptions=assumptions,
        support_mode=mode,
        compiler_provenance=CompilerProvenance(
            compiler_kind="llm",
            compiler_version=OPUS_COMPILER_VERSION,
            model_id=usage.model_id,
            prompt_hash=prompt_hash,
        ),
        branch_id=request.branch_id,
        fork_at_minute=fork_at_minute,
        ambiguities=[],
        requires_confirmation=True,
    )
    # The whole-program preview remains read-only. Runtime apply repeats semantic checks
    # against the actual branch WorldState before scheduling any effect.
    _validate_model_effects(document, request.scenario_summary, registry, eligible, target_ids)
    return CompilerResult(
        status="compiled",
        support_mode=mode,
        draft_plan=plan,
        mechanism_summary=document["mechanism_summary"],
        limitations=document["limitations"],
        fallback_used=fallback_used,
        usage=usage,
    )


def _offline_compiler_result(
    request: ParseRequest,
    offline_result: ParseResult,
    *,
    registry: PrimitiveRegistry,
    eligible: list[PrimitiveSpec],
    target_ids: dict[str, list[str]],
    usage: CompilerUsage,
    fallback_used: bool,
    reason: str,
) -> CompilerResult:
    if offline_result.status == "parsed" and offline_result.draft_plan is not None:
        if not _offline_fallback_covers_all_clauses(request.text):
            reason = (
                f"{reason}; offline fallback recognized only part of the request, "
                "so the partial plan was discarded"
            )
        else:
            return _validated_offline_plan(
                offline_result.draft_plan,
                request,
                registry=registry,
                eligible=eligible,
                target_ids=target_ids,
                usage=usage,
                reason=reason,
            )
    if offline_result.status == "clarification_required" and offline_result.draft_plan is not None:
        ambiguities = offline_result.draft_plan.ambiguities
        return CompilerResult(
            status="clarification_required",
            support_mode=SupportMode.CLARIFICATION_REQUIRED,
            mechanism_summary="オフライン規則では候補まで特定しました。対象を選択してください。",
            questions=[item.message for item in ambiguities],
            choices=list(dict.fromkeys(choice for item in ambiguities for choice in item.choices)),
            limitations=["対象が確定するまで実行可能な案として扱いません。"],
            fallback_used=fallback_used,
            usage=usage,
        )
    parser_reasons = [item.message for item in offline_result.issues]
    time_questions = [
        item.message for item in offline_result.issues if item.code == "ambiguous_time"
    ]
    if time_questions and eligible:
        return CompilerResult(
            status="clarification_required",
            support_mode=SupportMode.CLARIFICATION_REQUIRED,
            mechanism_summary="時刻を一意に決められないため、作用案を作成していません。",
            questions=time_questions[:10],
            choices=["HH:MM"],
            limitations=["各イベントの時刻を明示するまで作用を適用できません。"],
            fallback_used=fallback_used,
            usage=usage,
        )
    limitation = reason or "この入力を現在のシミュレーション機構へ安全に対応付けられません。"
    if parser_reasons:
        limitation += " オフライン判定: " + " / ".join(parser_reasons)
    return CompilerResult(
        status="not_simulatable",
        support_mode=SupportMode.NOT_SIMULATABLE,
        mechanism_summary="利用可能な作用へ変換できる案はありません。",
        limitations=[limitation[:500]],
        fallback_used=fallback_used,
        usage=usage,
    )


def _validated_offline_plan(
    plan: DraftPlan,
    request: ParseRequest,
    *,
    registry: PrimitiveRegistry,
    eligible: list[PrimitiveSpec],
    target_ids: dict[str, list[str]],
    usage: CompilerUsage,
    reason: str,
) -> CompilerResult:
    try:
        _validate_existing_plan(
            plan,
            request.scenario_summary,
            registry,
            eligible,
            target_ids,
        )
    except (ValueError, ValidationError, TypeError) as exc:
        reason = f"{reason}; offline proposal rejected: {exc}"
    else:
        return CompilerResult(
            status="compiled",
            support_mode=plan.support_mode,
            draft_plan=plan,
            mechanism_summary=(
                "Opusが利用できないため、認識済みのオフライン規則で対象と作用を構成しました。"
            ),
            limitations=[
                "これは規則ベースのfallbackで、自由文全体をOpusが解釈した結果ではありません。"
            ],
            fallback_used=True,
            usage=usage,
        )
    return CompilerResult(
        status="not_simulatable",
        support_mode=SupportMode.NOT_SIMULATABLE,
        mechanism_summary="オフライン案は現在のscenario/policy条件を満たしません。",
        limitations=[reason[:500]],
        fallback_used=True,
        usage=usage,
    )


def _offline_fallback_covers_all_clauses(text: str) -> bool:
    """Reject a regex fallback when any non-temporal clause went unrecognized."""
    clauses = re.split(
        r"[,、。;+]|\b(?:and|then|while)\b|さらに|そして|および|また|加えて|同時に|一方で|そのうえ",
        _normalize(text),
        flags=re.IGNORECASE,
    )
    for clause in clauses:
        if not clause.strip() or _detect_operations(clause):
            continue
        if not _is_temporal_clause(clause):
            return False
    return True


def _is_temporal_clause(clause: str) -> bool:
    residual = clause
    for pattern in (
        r"\bday\s*\d{1,3}\b",
        r"\d{1,3}\s*日目",
        r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
        r"\b\d{1,2}/\d{1,2}\b",
        r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b",
        r"\b\d{1,2}\s*(?:am|pm)\b",
        r"(?:午前|午後)?\s*\d{1,2}\s*時(?:\s*\d{1,2}\s*分)?",
        r"\b(?:at|on|in|by|from|until|to|day|night|midnight|noon|morning|afternoon|evening)\b",
        r"深夜|夜間|夜|朝|昼|夕方|午前|午後|ごろ|頃|から|まで|\d+",
    ):
        residual = re.sub(pattern, " ", residual, flags=re.IGNORECASE)
    residual = re.sub(r"[\s:：,、。;；/()（）-]+", "", residual)
    return not residual


def _validate_existing_plan(
    plan: DraftPlan,
    summary: ScenarioSummary,
    registry: PrimitiveRegistry,
    eligible: list[PrimitiveSpec],
    target_ids: dict[str, list[str]],
) -> None:
    specs = {item.op: item for item in eligible}
    document = {
        "effects": [
            {
                "op": effect.op,
                "target_kind": effect.target_selector.kind,
                "target_id": effect.target_selector.target_id,
                "start_minute": effect.start_minute,
                **({"end_minute": effect.end_minute} if effect.end_minute is not None else {}),
                "parameters": effect.parameters,
                "assumption_indexes": [
                    next(
                        index
                        for index, assumption in enumerate(plan.assumptions)
                        if assumption.assumption_id == assumption_id
                    )
                    for assumption_id in effect.assumption_ids
                ],
            }
            for effect in plan.effects
        ],
        "assumptions": [item.model_dump(mode="json") for item in plan.assumptions],
    }
    for effect in plan.effects:
        primitive = specs.get(effect.op)
        if primitive is None:
            raise ValueError(f"offline primitive {effect.op!r} is not installed")
        parameters = primitive.parameters_model.model_validate(effect.parameters)
        if effect.units != _derive_effect_units(primitive, parameters.model_dump(mode="json")):
            raise ValueError(f"offline effect {effect.effect_id!r} has invalid units")
        if effect.policy_dependencies != sorted(primitive.required_policies):
            raise ValueError(f"offline effect {effect.effect_id!r} has invalid policy dependencies")
    _validate_model_effects(document, summary, registry, eligible, target_ids)


def _empty_compiler_usage(prompt_hash: str, input_hash: str) -> CompilerUsage:
    return CompilerUsage(
        model_id=None,
        prompt_version=OPUS_COMPILER_PROMPT_VERSION,
        prompt_hash=prompt_hash,
        input_hash=input_hash,
        api_request_count=0,
    )


def _compiler_usage(records: list[Any], task: LLMTask, prompt_hash: str) -> CompilerUsage:
    last_model = records[-1].model_id if records else None
    model_id = last_model if last_model and not last_model.startswith("offline:") else None
    return CompilerUsage(
        model_id=model_id,
        prompt_version=task.prompt_version,
        prompt_hash=prompt_hash,
        input_hash=task.input_hash,
        api_request_count=sum(item.api_request_count for item in records),
        input_tokens=sum(item.input_tokens for item in records),
        output_tokens=sum(item.output_tokens for item in records),
        estimated_cost_usd=sum(item.estimated_cost_usd for item in records),
        cache_hit=bool(records) and all(item.cache_hit for item in records),
        error_kind=records[-1].error_kind if records else None,
    )


def _provider_failure_reason(records: list[Any]) -> str:
    if not records:
        return "Opus compiler did not return a validated proposal."
    last = records[-1]
    if last.error_kind == "rate_limited":
        return "Opus APIが429を返しました。"
    if last.error_kind:
        return f"Opus compiler unavailable ({last.error_kind})."
    if last.fallback_used:
        return "Opusの出力または予算に問題があり、決定的fallbackを確認しました。"
    return "Opus compiler did not return a validated proposal."


def _sanitize_narrative(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(char if char in "\n\t" or ord(char) >= 32 else " " for char in normalized)
    normalized = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[redacted-email]",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"(?<!\w)(?:\+?81[- ]?)?0\d{1,4}[- ]?\d{1,4}[- ]?\d{3,4}(?!\w)",
        "[redacted-phone]",
        normalized,
    )
    normalized = re.sub(r"(?<!\w)\d{10,13}(?!\w)", "[redacted-phone]", normalized)
    return normalized[:4000]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _error_messages(exc: Exception) -> list[str]:
    if isinstance(exc, ValidationError):
        return [
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in exc.errors()[:20]
        ]
    return [str(exc)[:500]]
