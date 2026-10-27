"""A Project results chat over a saved Run accepts evidence quoted verbatim as a string.

The exact-field fallback and proof matching are checked in
tests/unit/web/test_results_chat.py.
"""

from __future__ import annotations

from support.paths import BASELINE_100_7D


def test_project_chat_accepts_verbatim_string_evidence(tmp_path) -> None:
    import json

    from sheltersim.core.world import WorldState
    from sheltersim.experiments.benchmark import build_benchmark_scenario
    from sheltersim.experiments.branch import BranchRuntime
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.experiments.timeline import TimelineStore
    from sheltersim.llm.cache import ResponseTape
    from sheltersim.llm.config import LLMMode, LLMSettings
    from sheltersim.llm.contracts import ProviderResponse
    from sheltersim.llm.provider import LLMRouter
    from sheltersim.storage.projects import ProjectStore, digest
    from sheltersim.web.projects import ProjectAPI
    from sheltersim.web.results_chat import ResultsChatService

    baseline = BASELINE_100_7D
    scenario = build_benchmark_scenario(
        ScenarioSpec.load(baseline), agent_count=10, seed=7, duration_minutes=30
    )
    store = ProjectStore(tmp_path)
    store.create_project("project")
    saved = store.save_scenario(
        "project", scenario, approved_hash=digest(json.loads(scenario.canonical_json()))
    )
    store.create_run("project", "run", scenario.id, saved["scenario_version"])
    timeline = TimelineStore(store, "project", "run")
    timeline.record(BranchRuntime.from_world(WorldState.from_scenario(scenario)))
    timeline.advance("main", 30)

    def scalars(data, prefix=""):
        for key, value in data.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict):
                yield from scalars(value, path + ".")
            elif isinstance(value, (str, int, float, bool)):
                yield path, value

    class VerbatimProvider:
        """Cite one recorded string verbatim and one number, as live Claude replies did."""

        def complete(self, *, model_id, task, max_output_tokens, timeout_seconds):
            del max_output_tokens, timeout_seconds
            evidence = json.loads(task.input_text)["evidence"]
            found = [
                (item["alias"], path, value)
                for item in evidence
                for path, value in scalars(item["data"])
            ]
            text_proof = next((a, p, v) for a, p, v in found if isinstance(v, str))
            number = next(
                (a, p, v)
                for a, p, v in found
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            )
            reply = {
                "claims": [
                    {
                        "evidence": [
                            {
                                "alias": text_proof[0],
                                "field": text_proof[1],
                                "value": text_proof[2],
                            },
                        ]
                    },
                    {
                        "evidence": [
                            {
                                "alias": number[0],
                                "field": number[1],
                                "value": json.dumps(number[2]),
                            },
                        ]
                    },
                ],
                "explanations": [],
            }
            return ProviderResponse(
                text=json.dumps(reply, ensure_ascii=False),
                model_id=model_id,
                input_tokens=10,
                output_tokens=5,
                elapsed_ms=0.0,
            )

    api = ProjectAPI(store, recover=False)
    settings = LLMSettings(mode=LLMMode.RECORD, api_key="mock-key", budget_usd=1.0)
    api.workspace.analysis_router = lambda *_args, **_kwargs: LLMRouter(
        settings, provider=VerbatimProvider(), tape=ResponseTape()
    )
    try:
        answer = ResultsChatService(None, None, api).ask(
            {
                "question": "What was recorded?",
                "project_id": "project",
                "run_id": "run",
                "branch_id": "main",
                "minute": 30,
                "comparison_branch_ids": [],
            }
        )
    finally:
        api.close()

    assert answer["usage"]["rejected_fact_batch"] is False
    assert answer["usage"]["accepted_fact_count"] == 2
