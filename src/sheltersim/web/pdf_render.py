"""Searchable A4 report composition with an embedded, redistributable Japanese font."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, PolyLine, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from sheltersim.web.pdf_report import METRICS, number

FONT = "ShelterSimJapanese"
NAVY = colors.HexColor("#102766")
BLUE = colors.HexColor("#0878ff")
TEAL = colors.HexColor("#008f80")
INK = colors.HexColor("#172d5d")
MUTED = colors.HexColor("#526783")
PALE = colors.HexColor("#e9f4ff")
WIDTH = A4[0] - 88
_EVENT_LABELS = {
    "facility_outage": "施設停止",
    "facility_recovery": "施設復旧",
    "water_outage": "断水",
    "water_restored": "給水復旧",
    "resource_delivery": "物資到着",
    "delivery_delayed": "配送遅延",
    "staff_unavailable": "スタッフ稼働停止",
    "staff_shift_change": "スタッフ交代",
    "power_outage": "停電",
    "power_restored": "電力復旧",
    "equipment_failure": "設備故障",
    "equipment_repaired": "設備修理",
    "extreme_heat": "高温",
    "extreme_cold": "低温",
    "branch-add-toilet": "トイレの追加",
    "branch-add-staff": "スタッフの追加",
    "effect_program_apply": "介入プログラムの適用",
}
_OPERATORS = {
    "add_toilet": "トイレの追加",
    "add_staff": "スタッフの追加",
    "add_facility": "施設の追加",
    "add_resource": "物資の追加",
    "set_facility_availability": "施設の稼働条件変更",
    "facility_outage": "施設の停止",
    "set_staff_availability": "スタッフの稼働条件変更",
    "set_ambient_noise": "騒音条件の変更",
    "set_privacy": "プライバシー条件の変更",
    "set_temperature": "温度条件の変更",
    "set_lighting": "照明条件の変更",
    "set_ventilation": "換気条件の変更",
    "modify_demand_rate": "需要発生率の変更",
    "move_facility": "施設の移動",
    "set_route_access": "経路の通行条件変更",
    "transfer_equipment": "設備の移設",
    "add_evacuees": "避難者の追加",
    "depart_evacuees": "避難者の退出",
}
_STATUS = {
    "applied": "適用済み",
    "failed": "失敗",
    "suppressed": "抑制",
    "scheduled": "予定（未実行）",
}
_PARAMETERS = {
    "capacity": "容量",
    "working_units": "稼働数",
    "count": "数",
    "quantity": "数量",
    "water_ml": "水量（ml）",
    "food_meal_units": "食数",
    "available": "稼働可能",
    "multiplier": "倍率",
    "ratio": "割合",
    "facility_id": "施設ID",
    "zone_id": "ゾーンID",
    "target_id": "対象ID",
    "end_minute": "終了予定分",
    "delta": "変化量",
    "resource_id": "物資",
    "household_size": "世帯人数",
    "include_initial": "初期の避難者を含む",
    "to_minute": "変更後の到着予定分",
    "staff_id": "スタッフID",
    "until_minute": "終了予定分",
}


def _parameters(values: dict[str, Any]) -> str:
    parts = []
    for key, value in values.items():
        if key == "parameters" and isinstance(value, dict):
            parts.append(_parameters(value))
        elif key not in {"op", "kind"}:
            parts.append(f"{_PARAMETERS.get(key, key)}: {value}")
    return " / ".join(parts) or "追加の数値条件は未取得"


def _intervention_text(item: dict[str, Any]) -> str:
    label = _OPERATORS.get(item["operator"], _EVENT_LABELS.get(item["type"], item["type"]))
    return (
        f"{label}\n{_parameters(item['details'])}\n"
        f"{_STATUS[item['status']]} / event_id: {item['event_id']}"
    )


def _fact_text(value: dict[str, Any]) -> str:
    if "calibration" in value:
        return (
            value["calibration"]
            + f" 対象範囲 {value['range'][0]}〜{value['range'][1]}分。"
            + ("途中結果。" if value["partial"] else "計算期間の終了時点。")
        )
    if "population" in value:
        return (
            f"登録避難者 {value['population']}人 / スタッフ {value['staff_count']}人 / "
            f"予定期間 {value['duration_minutes']}分"
        )
    if "delta" in value:
        return (
            f"{value['label']}: A {fmt(value['baseline'])} / B {fmt(value['candidate'])} / "
            f"差 {fmt(value['delta'])} {value['unit']}（{value['assessment']}）"
        )
    if "metric" in value:
        return f"{value['label']}: {fmt(value['value'])} {value['unit']} / {clock(value['minute'])}"
    if "criterion" in value:
        label = next((m[1] for m in METRICS if value["criterion"].startswith(m[0])), "指標")
        return (
            f"{label}が正：初回 {clock(value['first_observed_minute'])} / "
            f"観測継続 {value['positive_interval_minutes']}分 / 欠測 {value['missing_minutes']}分"
        )
    if "recorded_commands" in value:
        return (
            f"介入コマンド {value['recorded_commands']}件 / "
            f"対象範囲の介入実行記録 {value['recorded_intervention_events']}件"
        )
    if "operator" in value:
        return (
            f"{_OPERATORS.get(value['operator'], value['operator'])} / "
            f"{_parameters(value['parameters'])} / "
            f"予定 {clock(value['scheduled_minute'])} / {_STATUS[value['status']]}"
        )
    return (
        f"{_EVENT_LABELS.get(value['type'], value['type'])} / "
        f"{clock(value['minute'])} / {_STATUS[value['status']]}"
    )


def fmt(value: Any, factor: float = 1) -> str:
    if not number(value):
        return "未取得"
    scaled = value * factor
    if scaled == 0:
        return "0"
    return f"{scaled:,.4f}".rstrip("0").rstrip(".")


def clock(minute: int | None) -> str:
    if minute is None:
        return "未観測"
    return f"{minute // 1440 + 1}日目 {minute // 60 % 24:02d}:{minute % 60:02d} ({minute}分)"


class ReportCanvas(Canvas):
    """Second pass page numbering, including variable-length overflow pages."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["invariant"] = 1
        super().__init__(*args, **kwargs)
        self.pages: list[dict[str, Any]] = []

    def showPage(self) -> None:  # noqa: N802
        self.pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self.pages)
        for state in self.pages:
            self.__dict__.update(state)
            self.setStrokeColor(colors.HexColor("#dcecfb"))
            self.line(44, 43, A4[0] - 44, 43)
            self.setFillColor(MUTED)
            self.setFont(FONT, 8)
            self.drawString(
                44, 29, "避難所IF！ | シミュレーション分析資料 | 実運用の予測ではありません"
            )
            self.drawRightString(A4[0] - 44, 29, f"{self._pageNumber} / {total}")
            super().showPage()
        super().save()


def _chart(data: dict[str, Any], metric: tuple) -> Drawing:
    key, label, unit, factor, _ = metric
    width, height = WIDTH / 2 - 8, 154
    drawing = Drawing(width, height)
    drawing.add(
        String(3, height - 14, f"{label} ({unit})", fontName=FONT, fontSize=9, fillColor=NAVY)
    )
    left, bottom, plotw, ploth = 38, 29, width - 48, height - 60
    start, end = data["range"]
    series = [branch["series"] for branch in data["branches"]]
    values = [row[key] * factor for rows in series for row in rows if number(row.get(key))]
    if not values:
        drawing.add(String(40, 70, "時系列は未取得", fontName=FONT, fontSize=10, fillColor=MUTED))
        return drawing
    low, high = min(0, min(values)), max(values)
    if high <= low:
        high = low + 1
    for index in range(3):
        y = bottom + index * ploth / 2
        drawing.add(Line(left, y, left + plotw, y, strokeColor=colors.HexColor("#dcecfb")))
        drawing.add(
            String(
                left - 4,
                y - 3,
                fmt(low + index * (high - low) / 2),
                textAnchor="end",
                fontName=FONT,
                fontSize=7,
                fillColor=MUTED,
            )
        )
    for minute in sorted({start, (start + end) // 2, end}):
        x = left + (minute - start) / max(1, end - start) * plotw
        drawing.add(
            String(
                x, 15, str(minute), textAnchor="middle", fontName=FONT, fontSize=7, fillColor=MUTED
            )
        )
    drawing.add(
        String(width - 4, 2, "経過分", textAnchor="end", fontName=FONT, fontSize=7, fillColor=MUTED)
    )
    for index, rows in enumerate(series):
        color = (BLUE, TEAL)[index]
        points: list[float] = []
        previous = None

        def flush() -> None:
            if len(points) >= 4:
                drawing.add(PolyLine(points[:], strokeColor=color, strokeWidth=1.1))
            elif len(points) == 2:
                drawing.add(
                    Line(
                        points[0] - 1,
                        points[1],
                        points[0] + 1,
                        points[1],
                        strokeColor=color,
                        strokeWidth=2,
                    )
                )
            points.clear()

        for row in rows:
            minute, value = row["minute_index"], row.get(key)
            if not number(value):
                flush()
                previous = None
                continue
            if previous is not None and minute != previous + 1:
                flush()
            x = left + (minute - start) / max(1, end - start) * plotw
            y = bottom + (value * factor - low) / (high - low) * ploth
            points.extend([x, y])
            previous = minute
        flush()
    return drawing


def render_pdf(data: dict[str, Any], analysis: dict[str, Any], facts: dict[str, Any]) -> bytes:
    """Compose facts independently of prose; escape every untrusted string before markup."""
    if FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(
            TTFont(FONT, str(Path(__file__).with_name("fonts") / "BIZUDGothic-Regular.ttf"))
        )
    glyphs = pdfmetrics.getFont(FONT).face.charToGlyph
    styles = {
        "body": ParagraphStyle(
            "body",
            fontName=FONT,
            fontSize=9,
            leading=15,
            textColor=INK,
            wordWrap="CJK",
            spaceAfter=7,
        ),
        "small": ParagraphStyle(
            "small",
            fontName=FONT,
            fontSize=7.5,
            leading=12,
            textColor=MUTED,
            wordWrap="CJK",
            spaceAfter=5,
        ),
        "evidence": ParagraphStyle(
            "evidence",
            fontName=FONT,
            fontSize=7.5,
            leading=11,
            textColor=MUTED,
            wordWrap="CJK",
            spaceAfter=0,
        ),
        "title": ParagraphStyle(
            "title",
            fontName=FONT,
            fontSize=24,
            leading=34,
            textColor=NAVY,
            spaceAfter=16,
            wordWrap="CJK",
        ),
        "heading": ParagraphStyle(
            "heading",
            fontName=FONT,
            fontSize=17,
            leading=25,
            textColor=NAVY,
            spaceAfter=14,
            wordWrap="CJK",
        ),
        "sub": ParagraphStyle(
            "sub",
            fontName=FONT,
            fontSize=11,
            leading=18,
            textColor=TEAL,
            spaceBefore=10,
            spaceAfter=7,
            wordWrap="CJK",
        ),
    }

    def p(value: Any, style: str = "body") -> Paragraph:
        text = str(value)
        text = "".join(c if c in "\n\t" or ord(c) in glyphs else "（表示対象外）" for c in text)
        return Paragraph(escape(text).replace("\n", "<br/>"), styles[style])

    def table(
        rows: list[list[Any]], widths: list[float] | None = None, *, compact: bool = False
    ) -> LongTable:
        cells = [[p(cell, "evidence" if compact else "small") for cell in row] for row in rows]
        result = LongTable(
            cells,
            colWidths=widths or [WIDTH / len(rows[0])] * len(rows[0]),
            repeatRows=1,
            splitInRow=1,
            hAlign="LEFT",
        )
        result.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), PALE),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#f7fbfe")],
                    ),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 3 if compact else 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2 if compact else 4),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.6, BLUE),
                    ("LINEBELOW", (0, -1), (-1, -1), 0.4, colors.HexColor("#dcecfb")),
                ]
            )
        )
        return result

    story = []
    branches = data["branches"]
    aliases = {branch["branch_id"]: chr(65 + index) for index, branch in enumerate(branches)}
    c = data["conditions"]

    def heading(text: str, first: bool = False) -> None:
        if not first:
            story.append(PageBreak())
        story.append(p(text, "heading"))

    def prose(key: str, label: str) -> None:
        story.append(p(label, "sub"))
        for index, paragraph in enumerate(analysis["sections"][key]):
            refs = paragraph["refs"]
            prefix = (
                ("優先案：" if index == 0 else "代替案：" if index == 1 else "追加案：")
                if key == "proposals"
                else ""
            )
            story.append(
                KeepTogether(
                    [p(prefix + paragraph["text"]), p("根拠: " + ", ".join(refs), "small")]
                )
            )

    def events_table() -> None:
        events = [(aliases[b["branch_id"]], event) for b in branches for event in b["events"]]
        if not events:
            story.append(p("対象範囲の重要イベント記録はありません。"))
            return
        rows = [["分岐・時刻", "記録イベント", "結果・参照ID"]]
        for alias, event in events[:10]:
            rows.append(
                [
                    f"{alias} | {clock(event['minute'])}",
                    _EVENT_LABELS.get(event["type"], event["type"]),
                    f"{_STATUS[event['status']]} | {event['event_id']}",
                ]
            )
        story.append(table(rows, [140, 160, WIDTH - 300]))
        if len(events) > 10:
            story.append(
                p(
                    f"重要イベントは全{len(events)}件中、記録順の先頭10件を掲載。全件は内部JSONに保存。",
                    "small",
                )
            )

    story.append(p("避難所IF！", "title"))
    heading("01  エグゼクティブサマリー", first=True)
    story.append(p("避難所運営シミュレーション 分析レポート", "sub"))
    story.append(
        p(
            "目的：シミュレーションの条件・観測結果と検討課題を共有し、設備や人員配置、運営方針の比較検討に活用するための資料です。"
        )
    )
    story.append(
        p(
            f"対象：{'A/B比較' if len(branches) > 1 else '単一シナリオ'} | "
            f"評価時刻：{clock(data['range'][1])} | "
            f"{'途中結果（以降は未計算・評価対象外）' if data['partial'] else '計算期間の終了時点'}"
        )
    )
    rows = [["観測KPI", *[aliases[b["branch_id"]] for b in branches], "単位"]]
    for key, label, unit, factor, _ in METRICS:
        rows.append([label, *[fmt(b["final"].get(key), factor) for b in branches], unit])
    story.append(table(rows))
    story.append(Spacer(1, 10))
    for b in branches:
        story.append(
            p(
                f"{aliases[b['branch_id']]} = {b['branch_id']} / "
                f"評価時点の避難者数 {b['population']}人",
                "small",
            )
        )
    prose("executive", "AIの要旨・着眼点（仮説）")
    story.append(p("重要な読み方", "sub"))
    story.append(
        p(
            "表とグラフは記録に基づく観測値です。AIの解釈・原因候補は仮説、施策は今後の検討提案として掲載します。指標の低下だけで実運用の有効性を判断することはできません。"
        )
    )

    heading("02  シミュレーション条件")
    stock = c["initial_stock"]
    stock_text = (
        f"水 {stock['water_ml']} ml / 食料 {stock['food_meal_units']} 食"
        if stock
        else "初期物資量は未取得"
    )
    story.append(
        table(
            [
                ["設定項目", "記録値"],
                ["登録避難者数 / 初期スタッフ数", f"{c['population']}人 / {c['staff_count']}人"],
                [
                    "予定期間 / 評価対象範囲",
                    f"{c['duration_minutes']}分 / {data['range'][0]}〜{data['range'][1]}分",
                ],
                ["スタッフ技能", ", ".join(c["staff_skills"]) or "未取得"],
                ["初期物資", stock_text],
                [
                    "採用モデル・版",
                    " / ".join(f"{key}: {value}" for key, value in data["model_versions"].items()),
                ],
                [
                    "Scenario / seed",
                    f"{data['scenario_id']} v{data['scenario_version']} / {data['seed']}",
                ],
            ],
            [140, WIDTH - 140],
        )
    )
    story.append(p("施設構成・設備（開始時の設定）", "sub"))
    facility_rows = [["施設ID・種別", "所属ゾーン", "容量 / 稼働数"]]
    for facility in c["facilities"][:12]:
        working = facility["working_units"]
        if working is None:
            working = facility["capacity"]
        facility_rows.append(
            [
                f"{facility['id']} / {facility['kind']}",
                facility["zone_id"],
                f"{facility['capacity']} / {working}",
            ]
        )
    story.append(table(facility_rows) if len(facility_rows) > 1 else p("施設設定は未取得。"))
    story.append(
        p(
            f"ゾーン数 {len(c['zones'])} / 施設数 {len(c['facilities'])}。"
            + ("施設表は先頭12件を掲載。" if len(c["facilities"]) > 12 else ""),
            "small",
        )
    )
    story.append(
        p(
            "ゾーン："
            + " / ".join(
                f"{z['name']} ({z['id']}, "
                f"容量 {z['capacity'] if z['capacity'] is not None else '未取得'})"
                for z in c["zones"][:12]
            ),
            "small",
        )
    )
    story.append(p("主な仮定・出典", "sub"))
    story.append(
        p(
            "施設数、初期状態、行動・心理の係数などにはScenario固有の仮定を含みます。実建物の図面・形状と現地の医療結果は評価対象外です。"
        )
    )
    story.append(p("仮定ID：" + ", ".join(c["assumption_ids"]), "small"))
    story.append(p("出典ID：" + (", ".join(c["source_ids"]) or "未取得"), "small"))

    heading("03  結果分析：時系列と問題の継続")
    story.append(
        p(
            "凡例：青=A / 緑=B（比較時）。ストレスは0〜100に換算。稼働率は割合を百分率に換算。"
            "線は連続した分記録だけを結び、欠測区間は補間しません。",
            "small",
        )
    )
    charts = [_chart(data, metric) for metric in METRICS[:4]]
    graph_table = LongTable([charts[:2], charts[2:]], colWidths=[WIDTH / 2] * 2)
    graph_table.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])
    )
    story.append(graph_table)
    story.append(p("問題の観測時刻・継続時間", "sub"))
    rows = [["分岐 / 判定条件", "初回観測", "観測継続時間 / 欠測"]]
    for b in branches:
        for key, label in (
            ("toilet_queue_length", "トイレ待ち人数 > 0"),
            ("unresolved_support_request_count", "未解決支援要請 > 0"),
        ):
            window = b["problems"][key]
            rows.append(
                [
                    f"{aliases[b['branch_id']]} / {label}",
                    clock(window["first_observed_minute"]),
                    f"{window['positive_interval_minutes']}分 / {window['missing_minutes']}分",
                ]
            )
    story.append(table(rows))
    story.append(
        p(
            "継続時間は対象範囲の各1分区間で開始値が正かつ両端が記録済みの区間を合算。閾値は問題の診断基準ではなく表示用です。初回観測は範囲内の初回であり、真の発生時刻とは限りません。",
            "small",
        )
    )

    heading("04  介入と比較・重要イベント")
    if data["differences"]:
        story.append(
            p(
                "Aを基準にB−Aを計算。同じRun・seed・モデル・評価時刻を使用。"
                f"共通の起点：{'確認済み' if data['paired'] else '未確認'}。"
                "系譜の一致だけで現実の因果や効果は確定しません。"
            )
        )
        rows = [["KPI", "A", "B", "差分 B−A", "方向の整理"]]
        for key, label, unit, factor, _ in METRICS:
            item = data["differences"][0]["metrics"][key]
            rows.append(
                [
                    f"{label} ({'pt' if unit in ('%', '/100') else unit})",
                    fmt(item["baseline"], factor),
                    fmt(item["candidate"], factor),
                    fmt(item["delta"], factor),
                    item["assessment"],
                ]
            )
        story.append(table(rows, [155, 70, 70, 85, WIDTH - 380]))
        story.append(
            p(
                "改善・悪化は当該指標の低下・上昇の方向のみ。スタッフ稼働率の差には良否を付けません。",
                "small",
            )
        )
    else:
        story.append(p("比較対象が選択されていないため、比較と差分は評価対象外です。"))
    story.append(p("記録済み介入", "sub"))
    commands = [
        (aliases[b["branch_id"]], command) for b in branches for command in b["interventions"]
    ]
    if commands:
        story.append(
            table(
                [
                    ["分岐 / 実行・予定時刻", "介入内容・結果（型付き作用・参照ID）"],
                    *[
                        [
                            f"{alias} / {clock(cmd['realized_minute'])}\n"
                            f"予定 {clock(cmd['scheduled_minute'])}",
                            _intervention_text(cmd),
                        ]
                        for alias, cmd in commands[:6]
                    ],
                ],
                [135, WIDTH - 135],
            )
        )
        if len(commands) > 6:
            story.append(p(f"全{len(commands)}件中先頭6件を掲載。詳細は内部JSONに保存。", "small"))
    else:
        story.append(p("記録済み介入はありません。"))
    story.append(p("重要イベント（予定ではなく実行結果）", "sub"))
    events_table()
    prose("interpretation", "AIによる結果の解釈（仮説）")

    heading("05  AI分析と優先的に検討すべき施策")
    story.append(
        p(f"執筆モデル：{analysis['model_id']} / 根拠付き構造化出力を検証して掲載。", "small")
    )
    prose("hypotheses", "原因候補・運営上の課題（AIの仮説）")
    prose("proposals", "優先案・代替案（今後の検討提案）")
    story.append(p("参照した観測根拠", "sub"))
    used = list(
        dict.fromkeys(
            ref
            for section in analysis["sections"].values()
            for paragraph in section
            for ref in paragraph["refs"]
        )
    )
    rows = [["根拠ID", "検証済みの記録値・条件"]]
    for ref in used:
        local_ref = ""
        if ".E" in ref or ".I" in ref:
            branch_number, record_number = ref.split(".")
            branch = branches[int(branch_number[1:])]
            records = branch["events"] if record_number.startswith("E") else branch["interventions"]
            local_ref = "\nevent_id: " + records[int(record_number[1:])]["event_id"]
        rows.append([ref, _fact_text(facts[ref]) + local_ref])
    story.append(table(rows, [155, WIDTH - 155], compact=True))

    heading("06  限界と今後の検討・再現情報")
    story.append(
        p(
            "根拠IDの読み方：B0=A、B1=B、D1=B−A。KPIは評価時点、peakは記録内の最大値と時刻、"
            "windowは観測継続時間です。EとIは各Branchのイベント・介入の記録順の先頭12件に対応します。",
            "small",
        )
    )
    prose("limitations", "追加検証が必要な事項（AIの整理）")
    story.append(p("解釈上の限界", "sub"))
    for text in [
        "本資料は避難所運営条件の検討用です。実際の避難者の健康・心理や運営成果を予測・診断するものではありません。",
        "数値係数の実測校正状況はこのレポートでは未取得です。背景資料の存在は係数の校正根拠を意味しません。仮定IDと出典台帳を照合してください。",
        "単一seedの観測と比較です。複数seed、異なる需要・設備故障・スタッフ条件で追加検証し、適用可能な集計実測データで校正を検討してください。",
        "未計算期間、未取得指標、欠測区間、未選択の比較は評価対象外です。実建物の形状を推測しないため施設マップは掲載していません。",
        "AI文の根拠参照・数値混入・断定表現を機械検証していますが、自由記述の意味の正確性を完全に証明するものではありません。共有前に担当者が記録と照合してください。",
    ]:
        story.append(p(text))
    story.append(p("再現用ID・版", "sub"))
    rows = [
        ["項目", "記録値"],
        ["Project / Run", f"{data['project_id']} / {data['run_id']}"],
        ["Scenario hash", data["scenario_hash"]],
        ["seed / 対象分", f"{data['seed']} / {data['range']}"],
        ["レポート / プロンプト版", f"{data['report_version']} / {analysis['prompt_version']}"],
        ["AI入力hash", analysis["input_hash"]],
    ]
    for b, head in zip(branches, data["heads"], strict=True):
        rows.extend(
            [
                [
                    f"{aliases[b['branch_id']]} Branch / state",
                    f"{b['branch_id']} / {b['state_hash']}",
                ],
                ["Checkpoint hash", head["checkpoint_hash"]],
            ]
        )
    story.append(table(rows, [140, WIDTH - 140]))
    story.append(
        p(
            "再現には保存済みScenario、Checkpoint、乱数状態、モデル版、記録済みProvider応答を保持してください。元データ・比較・AI分析のJSONは内部保存を継続します。",
            "small",
        )
    )

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=44,
        leftMargin=44,
        topMargin=48,
        bottomMargin=60,
        title="避難所IF！ シミュレーション分析レポート",
        author="避難所IF！",
        pageCompression=1,
    )

    def header(canvas: Canvas, _doc: Any) -> None:
        canvas.setFillColor(BLUE)
        canvas.rect(44, A4[1] - 27, WIDTH, 3, fill=1, stroke=0)

    doc.build(story, onFirstPage=header, onLaterPages=header, canvasmaker=ReportCanvas)
    return output.getvalue()
