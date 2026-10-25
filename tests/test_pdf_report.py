"""Offline PDF export acceptance, grounding, persistence and failure boundaries."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pypdfium2
import pytest
from pypdf import PdfReader

from sheltersim.core.world import WorldState
from sheltersim.experiments.branch import AddToiletCommand, BranchRuntime
from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.experiments.timeline import TimelineStore
from sheltersim.llm.accounting import usage_limits
from sheltersim.llm.cache import ResponseTape
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.contracts import LLMProviderError, ProviderResponse
from sheltersim.llm.pdf_analysis import generate_analysis, validate_analysis
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.storage.usage import UsageLedger
from sheltersim.web.inspector import InspectorService
from sheltersim.web.pdf_render import render_pdf
from sheltersim.web.pdf_report import analysis_facts, collect_report, problem_window
from sheltersim.web.projects import ProjectAPI
from sheltersim.web.server import _handler_for


def prose_document():
    return {
        "executive": [
            {
                "text": "待機と支援需要を合わせて確認することで、"
                "運営上の着眼点を整理できる可能性があります。",
                "refs": ["B0.toilet_queue_length"],
            }
        ],
        "interpretation": [
            {
                "text": "待機の継続には処理能力だけでなく、"
                "需要の集中が関係している可能性があります。",
                "refs": ["B0.window.toilet_queue_length"],
            }
        ],
        "hypotheses": [
            {
                "text": "待機と支援需要の重なりが運営上の負担につながる可能性があります。",
                "refs": ["B0.unresolved_support_request_count", "B0.toilet_queue_length"],
            }
        ],
        "proposals": [
            {
                "text": "優先的に、需要の時間帯と設備容量をそろえた比較条件で、"
                "追加検証することを検討してください。",
                "refs": ["B0.toilet_queue_length"],
            },
            {
                "text": "代替案として、担当者の配置条件を変えて、"
                "待機と支援の推移を比較することを検討してください。",
                "refs": ["B0.unresolved_support_request_count"],
            },
        ],
        "limitations": [
            {
                "text": "現地の集計データとの照合と、異なる条件での反復検証が必要です。",
                "refs": ["L"],
            }
        ],
    }


class ReportProvider:
    def __init__(self, *, error=False):
        self.calls = 0
        self.error = error
        self.prompts = []

    def complete(self, *, model_id, task, **_kwargs):
        self.calls += 1
        self.prompts.append(task.input_text)
        if self.error:
            raise LLMProviderError("Anthropic API request failed; HTTP 429", status_code=429)
        return ProviderResponse(
            json.dumps(prose_document(), ensure_ascii=False), model_id, 200, 500, 1
        )


def router_for(provider=None, **kwargs):
    settings = LLMSettings(mode=LLMMode.RECORD, api_key="test-only", **kwargs)
    return LLMRouter(settings, provider=provider or ReportProvider(), tape=ResponseTape())


@pytest.fixture
def saved_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
    source = json.loads(ScenarioSpec.load("fixtures/scenario_100_agents.json").canonical_json())
    source["duration_minutes"] = 40
    source["profiles"] = source["profiles"][:12]
    source["scheduled_events"] = []
    source["model_configs"]["toilet_service"] = {}
    scenario = ScenarioSpec.model_validate(source)
    store = ProjectStore(tmp_path / "projects")
    store.create_project("p")
    store.save_scenario("p", scenario, approved_hash=digest(json.loads(scenario.canonical_json())))
    store.create_run("p", "r", scenario.id, 1)
    timeline = TimelineStore(store, "p", "r")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    timeline.advance("main", 40, checkpoint_interval=10)
    timeline.fork("main", 10, "noop")
    timeline.advance("noop", 40)
    source_runtime = timeline.seek("main", 10)
    child = source_runtime.fork("capacity")
    child.runtime_state["fork_origin"] = {
        "branch_id": "main",
        "minute": 10,
        "state_hash": digest(source_runtime.world.to_dict()),
        "checkpoint_hash": source_runtime.checkpoint().checkpoint_hash,
    }
    child.apply(AddToiletCommand("extra", "toilet", capacity=3))
    timeline.record(child, fork_minute=10)
    timeline.advance("capacity", 40)
    api = ProjectAPI(store)
    try:
        yield api
    finally:
        api.close()


def test_scope_deltas_interventions_and_read_only_checkpoints(saved_run):
    api = saved_run
    before = {
        key: api.store.load_branch("p", "r", key)[1].to_json()
        for key in ("main", "noop", "capacity")
    }
    single = collect_report(api, "p", "r", ["main"], 40)
    noop = collect_report(api, "p", "r", ["main", "noop"], 40)
    capacity = collect_report(api, "p", "r", ["main", "capacity"], 40)
    assert not single["differences"] and not single["partial"]
    assert not single["branches"][0]["interventions"]
    assert all(item["delta"] == 0 for item in noop["differences"][0]["metrics"].values())
    assert all(
        item["assessment"] == "変化なし" for item in noop["differences"][0]["metrics"].values()
    )
    assert capacity["paired"] and capacity["range"] == [10, 40]
    assert capacity["branches"][1]["interventions"][0]["details"]["capacity"] == 3
    for metric, item in capacity["differences"][0]["metrics"].items():
        assert item["delta"] == (
            capacity["branches"][1]["final"][metric] - capacity["branches"][0]["final"][metric]
        )
    partial = collect_report(api, "p", "r", ["main"], 20)
    assert partial["partial"] and partial["range"] == [0, 20]
    for key, checkpoint in before.items():
        assert api.store.load_branch("p", "r", key)[1].to_json() == checkpoint
    # Existing JSON report/evidence/comparison routes stay available and serializable.
    assert api.get(["p", "runs", "r", "branches", "main", "report"], {}) == {
        "status": "not_generated"
    }
    assert api.get(["p", "runs", "r", "evidence"], {})
    assert api.workspace.comparison("p", "r", ["main", "noop"], 40)["minute"] == 40


def test_problem_duration_does_not_invent_missing_intervals():
    rows = [{"minute_index": m, "q": q} for m, q in [(0, 0), (1, 2), (2, 3), (4, 2), (5, 0)]]
    window = problem_window(rows, "q", 0, 5)
    assert window["first_observed_minute"] == 1
    assert window["positive_interval_minutes"] == 2
    assert window["intervals"] == [[1, 2], [4, 5]]
    assert window["missing_minutes"] == 2
    assert problem_window([], "q", 0, 5)["missing_minutes"] == 5


@pytest.mark.parametrize(
    "kind",
    [
        "reference",
        "number",
        "kanji_number",
        "causal",
        "pii",
        "extra",
        "unlabelled",
        "model_scope",
        "operator_absence",
    ],
)
def test_ungrounded_prose_is_rejected(saved_run, kind):
    facts = analysis_facts(collect_report(saved_run, "p", "r", ["main"], 40))
    document = prose_document()
    if kind == "reference":
        document["executive"][0]["refs"] = ["unknown"]
    elif kind == "number":
        document["executive"][0]["text"] = "待機時間は９９分となる可能性があります。"
    elif kind == "kanji_number":
        document["executive"][0]["text"] = "百人の待機が生じる可能性があります。"
    elif kind == "causal":
        document["executive"][0]["text"] = (
            "設備の不足が原因です。需要増も影響する可能性があります。"
        )
    elif kind == "pii":
        document["executive"][0]["text"] = (
            "担当者test@example.comへの連絡が必要となる可能性があります。"
        )
    elif kind == "extra":
        document["made_up"] = []
    elif kind == "model_scope":
        document["limitations"][0]["text"] = "動線や設備配置の影響は反映されていません。"
    elif kind == "operator_absence":
        document["hypotheses"][0]["text"] = "運営側の働きかけがない場合の推移と考えられます。"
    else:
        document["executive"][0]["text"] = "設備の不足が待機時間の増加を引き起こしています。"
    with pytest.raises(ValueError):
        validate_analysis(document, facts)


def test_only_aggregate_allowlisted_evidence_goes_to_opus(saved_run):
    data = collect_report(saved_run, "p", "r", ["main", "capacity"], 40)
    provider = ReportProvider()
    result = generate_analysis(analysis_facts(data), router_for(provider))
    assert result["model_id"].startswith("claude-opus-")
    assert provider.calls == 1
    prompt = provider.prompts[0]
    assert "A001" not in prompt and "profiles" not in prompt
    assert "extra" not in prompt and "baseline_100_7d" not in prompt
    assert "raw_prompt" not in prompt and "source_text" not in prompt
    assert set(json.loads(prompt)["evidence"]) == set(analysis_facts(data))


def install_report_router(api, monkeypatch, provider=None):
    router = router_for(provider)
    monkeypatch.setattr(api.workspace, "analysis_router", lambda *_a, **_kw: router)
    return router


def test_cache_duplicate_clicks_restart_and_changed_scope(saved_run, monkeypatch):
    api = saved_run
    provider = ReportProvider()
    router = install_report_router(api, monkeypatch, provider)
    body = {"branch_ids": ["main", "capacity"], "minute": 40}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: api.pdf_reports.export("p", "r", body), range(2)))
    assert results[0] == results[1] and provider.calls == 1
    saved = list(api.store._path("p", "runs", "r", "pdf-reports").glob("*.json"))
    artifact = api.store._read(saved[0])
    assert artifact["schema_version"] == 1
    assert artifact["analysis"]["sections"] == prose_document()
    assert artifact["data"]["differences"]
    restarted = ProjectAPI(ProjectStore(api.store.root))
    monkeypatch.setattr(
        restarted.workspace,
        "analysis_router",
        lambda *_a, **_k: pytest.fail("cached export must not create a router"),
    )
    try:
        assert restarted.pdf_reports.export("p", "r", body) == results[0]
    finally:
        restarted.close()
    api.pdf_reports.export("p", "r", {"branch_ids": ["main", "noop"], "minute": 40})
    assert provider.calls == 2
    api.pdf_reports.export("p", "r", {"branch_ids": ["main"], "minute": 20})
    assert provider.calls == 3
    assert len(router.records) == 3


@pytest.mark.parametrize(
    "body",
    [
        {"branch_ids": ["main"], "minute": 41},
        {"branch_ids": ["main", "capacity"], "minute": 5},
        {"branch_ids": ["missing"], "minute": 40},
        {"branch_ids": ["main", "main"], "minute": 40},
        {"branch_ids": ["main", "noop", "capacity"], "minute": 40},
        {"branch_ids": ["../outside"], "minute": 40},
        {"branch_ids": ["main"], "minute": True},
        {"branch_ids": [], "minute": 40},
    ],
)
def test_uncomputed_missing_and_invalid_scopes_fail_before_provider(saved_run, monkeypatch, body):
    monkeypatch.setattr(
        saved_run.workspace,
        "analysis_router",
        lambda *_a, **_k: pytest.fail("invalid scope must not call Opus"),
    )
    with pytest.raises(ValueError):
        saved_run.pdf_reports.export("p", "r", body)


@pytest.mark.parametrize("failure", ["offline", "api", "budget", "calls", "sonnet"])
def test_errors_never_export_fallback_json_or_pdf(saved_run, monkeypatch, failure):
    api = saved_run
    if failure == "offline":
        router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    elif failure == "sonnet":
        router = router_for(review_model_family="sonnet")
    else:
        options = {"budget_usd": 0} if failure == "budget" else {}
        router = router_for(ReportProvider(error=failure == "api"), **options)
        if failure == "calls":
            router.api_request_count = router.settings.max_api_calls
    monkeypatch.setattr(api.workspace, "analysis_router", lambda *_a, **_kw: router)
    with pytest.raises(StorageError, match="pdf_"):
        api.pdf_reports.export("p", "r", {"branch_ids": ["main"], "minute": 40})
    assert not list(api.store._path("p", "runs", "r", "pdf-reports").glob("*.json"))


def test_render_retry_reuses_successful_opus_response(saved_run, monkeypatch):
    import sheltersim.web.pdf_render as renderer

    api = saved_run
    provider = ReportProvider()
    install_report_router(api, monkeypatch, provider)
    original = renderer.render_pdf
    monkeypatch.setattr(renderer, "render_pdf", lambda *_a: (_ for _ in ()).throw(RuntimeError()))
    body = {"branch_ids": ["main"], "minute": 40}
    with pytest.raises(StorageError, match="pdf_render_failed"):
        api.pdf_reports.export("p", "r", body)
    monkeypatch.setattr(renderer, "render_pdf", original)
    assert api.pdf_reports.export("p", "r", body).startswith(b"%PDF-")
    assert provider.calls == 1


def test_durable_accounting_api_retry_and_saved_response_tape(saved_run, monkeypatch):
    """Use the real shared ledger and tape; replace only the external SDK boundary."""
    import sheltersim.llm.provider as providers

    api = saved_run
    provider = ReportProvider(error=True)
    monkeypatch.setattr(providers, "AnthropicProvider", lambda _key: provider)
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "record")
    body = {"branch_ids": ["main"], "minute": 40}
    with pytest.raises(StorageError, match="pdf_analysis"):
        api.pdf_reports.export("p", "r", body)
    assert provider.calls == 1
    provider.error = False
    pdf = api.pdf_reports.export("p", "r", body)
    assert pdf.startswith(b"%PDF-") and provider.calls == 2
    ledger = UsageLedger.for_run(api.store, "p", "r", usage_limits(LLMSettings.from_env()))
    summary = ledger.summary()
    assert summary["by_operation"]["pdf_report"]["calls"] == 2
    assert summary["uncertain_calls"] == 1
    assert api.pdf_reports.export("p", "r", body) == pdf and provider.calls == 2
    # Damage only the PDF blob: rebuild with the accepted response, without a third call.
    path = next(api.store._path("p", "runs", "r", "pdf-reports").glob("*.json"))
    document = api.store._read(path)
    document["pdf_base64"] = "broken"
    api.store._write(path, document)
    assert api.pdf_reports.export("p", "r", body) == pdf and provider.calls == 2


def test_durable_shared_budget_exhaustion_blocks_new_pdf_scope(saved_run, monkeypatch):
    import sheltersim.llm.provider as providers

    provider = ReportProvider()
    monkeypatch.setattr(providers, "AnthropicProvider", lambda _key: provider)
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "record")
    monkeypatch.setenv("SHELTERSIM_LLM_MAX_API_CALLS", "1")
    body = {"branch_ids": ["main"], "minute": 40}
    saved_run.pdf_reports.export("p", "r", body)
    with pytest.raises(StorageError, match="pdf_analysis"):
        saved_run.pdf_reports.export("p", "r", {"branch_ids": ["main"], "minute": 20})
    assert provider.calls == 1
    assert saved_run.pdf_reports.export("p", "r", body).startswith(b"%PDF-")


@pytest.mark.parametrize("ids", [["main"], ["main", "noop"], ["main", "capacity"]])
def test_pdf_a4_searchable_japanese_fonts_pages_and_rendering(saved_run, ids, tmp_path):
    data = collect_report(saved_run, "p", "r", ids, 40)
    facts = analysis_facts(data)
    analysis = generate_analysis(facts, router_for())
    pdf = render_pdf(data, analysis, facts)
    reader = PdfReader(BytesIO(pdf))
    assert 5 <= len(reader.pages) <= 7
    texts = [page.extract_text() for page in reader.pages]
    text = "\n".join(texts)
    for heading in (
        "エグゼクティブサマリー",
        "シミュレーション条件",
        "結果分析",
        "介入と比較",
        "AI分析",
        "限界と今後",
    ):
        assert heading in text
    assert "優先案" in text and "代替案" in text
    assert "実測校正状況" in text and data["scenario_hash"] in text.replace("\n", "")
    assert "表示対象外" not in text and "\ufffd" not in text and "■" not in text
    if len(ids) == 1:
        assert "比較と差分は評価対象外" in text
    if ids[-1] == "noop":
        assert "変化なし" in text
    document = pypdfium2.PdfDocument(pdf)
    for index, page in enumerate(reader.pages):
        assert float(page.mediabox.width) == pytest.approx(595.276, abs=0.01)
        assert float(page.mediabox.height) == pytest.approx(841.89, abs=0.01)
        assert f"{index + 1} / {len(reader.pages)}" in texts[index]
        fonts = page["/Resources"]["/Font"]
        assert any(
            "/FontFile2" in font.get_object().get("/FontDescriptor", {}) for font in fonts.values()
        )
        image = document[index].render(scale=0.8).to_pil()
        assert image.width > 400 and image.height > 600
        image.save(tmp_path / f"report-{index + 1}.png")


def test_missing_metrics_and_sparse_history_are_explicit(saved_run):
    data = collect_report(saved_run, "p", "r", ["main"], 20)
    data["branches"][0]["final"]["stress_mean"] = None
    data["branches"][0]["series"] = []
    data["branches"][0]["problems"] = {
        key: problem_window([], key, 0, 20) for key in data["branches"][0]["problems"]
    }
    facts = analysis_facts(data)
    pdf = render_pdf(data, generate_analysis(facts, router_for()), facts)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert "未取得" in text and "途中結果" in text and "20分" in text


def test_pdf_http_attachment_and_existing_origin_guard(saved_run, monkeypatch, tmp_path):
    api = saved_run
    install_report_router(api, monkeypatch)
    handler_type = _handler_for(InspectorService(tmp_path), projects=api)

    def request(origin="http://localhost:8765"):
        handler = handler_type.__new__(handler_type)
        statuses, headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda key, value: headers.__setitem__(key, value)
        handler.end_headers = lambda: None
        handler.wfile = BytesIO()
        body = json.dumps({"branch_ids": ["main"], "minute": 40}).encode()
        handler.rfile = BytesIO(body)
        handler.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": origin,
            "Host": "localhost:8765",
        }
        handler.path = "/api/v1/projects/p/runs/r/pdf-report"
        handler.do_POST()
        return statuses, headers, handler.wfile.getvalue()

    statuses, headers, body = request()
    assert statuses == [200] and headers["Content-Type"] == "application/pdf"
    assert "attachment" in headers["Content-Disposition"] and body.startswith(b"%PDF-")
    assert int(headers["Content-Length"]) == len(body)
    assert request("https://untrusted.example")[0] == [403]
