"""Offline PDF export acceptance, grounding, persistence and failure boundaries.

Most tests use report_saved_run, a 12-person fixture Run whose capacity Branch adds a
toilet with a Branch command the Project screen does not use. The tests at the end
report a Run of the shared generated Scenario whose Branch adds toilets with an
approved EffectProgram, as the Project screen does (#137).

The problem window is checked in tests/unit/web/test_pdf_report.py, and the PDF download
over HTTP in tests/e2e/test_pdf_download.py.
"""

from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pypdfium2
import pytest
from pypdf import PdfReader

from sheltersim.experiments.scenario import ScenarioSpec
from sheltersim.llm.accounting import usage_limits
from sheltersim.llm.config import LLMMode, LLMSettings
from sheltersim.llm.pdf_analysis import generate_analysis, validate_analysis
from sheltersim.llm.provider import LLMRouter
from sheltersim.storage.projects import ProjectStore, StorageError, digest
from sheltersim.storage.usage import UsageLedger
from sheltersim.web.pdf_render import render_pdf
from sheltersim.web.pdf_report import analysis_facts, collect_report, problem_window
from sheltersim.web.projects import ProjectAPI


def test_scope_deltas_interventions_and_read_only_checkpoints(report_saved_run):
    api = report_saved_run
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
def test_ungrounded_prose_is_rejected(report_saved_run, kind, prose_document):
    facts = analysis_facts(collect_report(report_saved_run, "p", "r", ["main"], 40))
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


def test_only_aggregate_allowlisted_evidence_goes_to_opus(
    report_saved_run, report_provider, report_router
):
    data = collect_report(report_saved_run, "p", "r", ["main", "capacity"], 40)
    provider = report_provider()
    result = generate_analysis(analysis_facts(data), report_router(provider))
    assert result["model_id"].startswith("claude-opus-")
    assert provider.calls == 1
    prompt = provider.prompts[0]
    assert "A001" not in prompt and "profiles" not in prompt
    assert "extra" not in prompt and "baseline_100_7d" not in prompt
    assert "raw_prompt" not in prompt and "source_text" not in prompt
    assert set(json.loads(prompt)["evidence"]) == set(analysis_facts(data))


def test_cache_duplicate_clicks_restart_and_changed_scope(
    report_saved_run, monkeypatch, prose_document, report_provider, install_report_router
):
    api = report_saved_run
    provider = report_provider()
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
def test_uncomputed_missing_and_invalid_scopes_fail_before_provider(
    report_saved_run, monkeypatch, body
):
    monkeypatch.setattr(
        report_saved_run.workspace,
        "analysis_router",
        lambda *_a, **_k: pytest.fail("invalid scope must not call Opus"),
    )
    with pytest.raises(ValueError):
        report_saved_run.pdf_reports.export("p", "r", body)


@pytest.mark.parametrize("failure", ["offline", "api", "budget", "calls", "sonnet"])
def test_errors_never_export_fallback_json_or_pdf(
    report_saved_run, monkeypatch, failure, report_provider, report_router
):
    api = report_saved_run
    if failure == "offline":
        router = LLMRouter(LLMSettings(mode=LLMMode.OFFLINE))
    elif failure == "sonnet":
        router = report_router(review_model_family="sonnet")
    else:
        options = {"budget_usd": 0} if failure == "budget" else {}
        router = report_router(report_provider(error=failure == "api"), **options)
        if failure == "calls":
            router.api_request_count = router.settings.max_api_calls
    monkeypatch.setattr(api.workspace, "analysis_router", lambda *_a, **_kw: router)
    with pytest.raises(StorageError, match="pdf_"):
        api.pdf_reports.export("p", "r", {"branch_ids": ["main"], "minute": 40})
    assert not list(api.store._path("p", "runs", "r", "pdf-reports").glob("*.json"))


def test_render_retry_reuses_successful_opus_response(
    report_saved_run, monkeypatch, report_provider, install_report_router
):
    import sheltersim.web.pdf_render as renderer

    api = report_saved_run
    provider = report_provider()
    install_report_router(api, monkeypatch, provider)
    original = renderer.render_pdf
    monkeypatch.setattr(renderer, "render_pdf", lambda *_a: (_ for _ in ()).throw(RuntimeError()))
    body = {"branch_ids": ["main"], "minute": 40}
    with pytest.raises(StorageError, match="pdf_render_failed"):
        api.pdf_reports.export("p", "r", body)
    monkeypatch.setattr(renderer, "render_pdf", original)
    assert api.pdf_reports.export("p", "r", body).startswith(b"%PDF-")
    assert provider.calls == 1


def test_durable_accounting_api_retry_and_saved_response_tape(
    report_saved_run, monkeypatch, report_provider
):
    """Use the real shared ledger and tape; replace only the external SDK boundary."""
    import sheltersim.llm.provider as providers

    api = report_saved_run
    provider = report_provider(error=True)
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


def test_durable_shared_budget_exhaustion_blocks_new_pdf_scope(
    report_saved_run, monkeypatch, report_provider
):
    import sheltersim.llm.provider as providers

    provider = report_provider()
    monkeypatch.setattr(providers, "AnthropicProvider", lambda _key: provider)
    monkeypatch.setenv("SHELTERSIM_LLM_MODE", "record")
    monkeypatch.setenv("SHELTERSIM_LLM_MAX_API_CALLS", "1")
    body = {"branch_ids": ["main"], "minute": 40}
    report_saved_run.pdf_reports.export("p", "r", body)
    with pytest.raises(StorageError, match="pdf_analysis"):
        report_saved_run.pdf_reports.export("p", "r", {"branch_ids": ["main"], "minute": 20})
    assert provider.calls == 1
    assert report_saved_run.pdf_reports.export("p", "r", body).startswith(b"%PDF-")


@pytest.mark.parametrize("ids", [["main"], ["main", "noop"], ["main", "capacity"]])
def test_pdf_a4_searchable_japanese_fonts_pages_and_rendering(
    report_saved_run, ids, tmp_path, report_router
):
    data = collect_report(report_saved_run, "p", "r", ids, 40)
    facts = analysis_facts(data)
    analysis = generate_analysis(facts, report_router())
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


def test_missing_metrics_and_sparse_history_are_explicit(report_saved_run, report_router):
    data = collect_report(report_saved_run, "p", "r", ["main"], 20)
    data["branches"][0]["final"]["stress_mean"] = None
    data["branches"][0]["series"] = []
    data["branches"][0]["problems"] = {
        key: problem_window([], key, 0, 20) for key in data["branches"][0]["problems"]
    }
    facts = analysis_facts(data)
    pdf = render_pdf(data, generate_analysis(facts, report_router()), facts)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert "未取得" in text and "途中結果" in text and "20分" in text


# One hour, checkpointed every 10 minutes; the Branch adds toilets at minute 10.
GENERATED_DURATION_DAYS = 1 / 24
GENERATED_END = 60
FORK_MINUTE = 10
ADDED_TOILETS = 2


@pytest.fixture(scope="module")
def generated_saved_run(tmp_path_factory, generated_previews, add_toilets_program, saved_timeline):
    """A ProjectAPI over Run r of the generated Scenario with a Branch that adds toilets.

    Shared by the module: reports only read the Run, which the first test checks.
    """
    preview = generated_previews(duration_days=GENERATED_DURATION_DAYS)
    scenario = ScenarioSpec.model_validate(copy.deepcopy(preview["scenario"]))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SHELTERSIM_LLM_MODE", "offline")
        store, timeline = saved_timeline(
            tmp_path_factory.mktemp("generated-report") / "projects", scenario
        )
        timeline.advance("main", GENERATED_END, checkpoint_interval=10)
        program = add_toilets_program(ADDED_TOILETS, start_minute=FORK_MINUTE)
        timeline.fork(
            "main",
            FORK_MINUTE,
            "capacity",
            program=program,
            approved_program_hash=digest(program.model_dump(mode="json")),
        )
        timeline.advance("capacity", GENERATED_END)
        api = ProjectAPI(store)
        try:
            yield api
        finally:
            api.close()


def test_effect_program_intervention_is_reported_with_deltas_and_read_only_checkpoints(
    generated_saved_run,
):
    api = generated_saved_run
    before = {
        key: api.store.load_branch("p", "r", key)[1].to_json() for key in ("main", "capacity")
    }

    report = collect_report(api, "p", "r", ["main", "capacity"], GENERATED_END)

    assert report["paired"] and report["range"] == [FORK_MINUTE, GENERATED_END]
    (intervention,) = report["branches"][1]["interventions"]
    assert intervention["operator"] == "add_toilet"
    assert intervention["status"] == "applied"
    assert intervention["realized_minute"] == FORK_MINUTE
    assert intervention["details"]["zone_id"] == "toilet"
    assert analysis_facts(report)["B1.I0"]["operator"] == "add_toilet"
    for metric, item in report["differences"][0]["metrics"].items():
        assert item["delta"] == (
            report["branches"][1]["final"][metric] - report["branches"][0]["final"][metric]
        )
    for key, checkpoint in before.items():
        assert api.store.load_branch("p", "r", key)[1].to_json() == checkpoint


def test_effect_program_intervention_reports_how_many_toilets_were_added(generated_saved_run):
    report = collect_report(generated_saved_run, "p", "r", ["main", "capacity"], GENERATED_END)

    (intervention,) = report["branches"][1]["interventions"]
    assert intervention["details"]["count"] == ADDED_TOILETS
    assert analysis_facts(report)["B1.I0"]["parameters"]["count"] == ADDED_TOILETS


def test_pdf_of_a_generated_run_with_an_effect_program_renders_every_section(
    generated_saved_run, report_router
):
    data = collect_report(generated_saved_run, "p", "r", ["main", "capacity"], GENERATED_END)
    facts = analysis_facts(data)
    pdf = render_pdf(data, generate_analysis(facts, report_router()), facts)
    reader = PdfReader(BytesIO(pdf))
    assert 5 <= len(reader.pages) <= 7
    text = "\n".join(page.extract_text() for page in reader.pages)
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
    assert "表示対象外" not in text and "\ufffd" not in text and "■" not in text
    # The EffectProgram's count reaches the body, not only the analysis facts (#160).
    assert f"数: {ADDED_TOILETS}" in text
