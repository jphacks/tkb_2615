"""Authored acceptance regressions for final #122. Deliberately NOT RUN in round 3."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sheltersim.contracts.workspace import HistoryQuery
from sheltersim.storage.projects import ProjectStore,StorageError,digest


def test_checkpoint_section_reads_only_selected_chunks(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    from sheltersim.storage.checkpoints import CheckpointArchive
    rows=[{"minute_index":minute,"value":"x"*128} for minute in range(5000)]
    archive=CheckpointArchive(tmp_path,chunk_records=128)
    record=archive.encode(SimpleNamespace(checkpoint_hash="a"*64,to_dict=lambda:{"metrics_state":{"snapshots":rows}}))
    reads=[];original=archive._get
    def read(key: str) -> object:
        reads.append(key)
        return original(key)
    monkeypatch.setattr(archive,"_get",read)
    result=archive.list_page(record,("metrics_state","snapshots"),offset=4097,limit=3)
    assert result["items"]==rows[4097:4100]
    assert result["next_offset"]==4100
    assert result["total_items"]==5000
    assert len(reads)<=2
    assert result["full_checkpoint_verified_by_this_read"] is False


def test_indexed_search_does_not_load_a_world_and_rejects_changed_head(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    from sheltersim.storage.history_index import HistoryIndex
    from sheltersim.web.project_queries import ProjectQueries
    store=ProjectStore(tmp_path);store.create_project("p")
    store._path("p","runs","r").mkdir(parents=True)
    head="a"*64
    reference=SimpleNamespace(fork_minute=0,computed_until=5000,checkpoint_hash=head)
    monkeypatch.setattr(store,"branch_reference",lambda *_args:reference)
    def no_world(*_args: object) -> None:
        raise AssertionError("indexed evidence must not decode a whole WorldState")
    monkeypatch.setattr(store,"load_branch",no_world)
    checkpoint=SimpleNamespace(checkpoint_hash=head,simulation_minute=5000,state_hash="b"*64,
        world_state={"event_log":[],"model_state":{}},metrics_state={"snapshots":[
            {"minute_index":minute,"stress_mean":minute/5000} for minute in range(5000)]})
    HistoryIndex(store,"p","r").ingest("main",checkpoint)
    query=HistoryQuery(kind="kpis",start=4097,end=4999,limit=3)
    result=ProjectQueries(store).history("p","r","main",query)
    assert [row["minute_index"] for row in result["items"]]==[4097,4098,4099]
    assert result["total_items"] is None
    assert result["query_strategy"]=="indexed_keyset"
    following=ProjectQueries(store).history("p","r","main",query.model_copy(update={"cursor":result["next_cursor"]}))
    assert [row["minute_index"] for row in following["items"]]==[4100,4101,4102]
    reference.checkpoint_hash="c"*64
    with pytest.raises(StorageError,match="stale_or_foreign_cursor|history_index_stale"):
        ProjectQueries(store).history("p","r","main",query.model_copy(update={"cursor":result["next_cursor"]}))


def test_scan_progress_cannot_be_forged_to_claim_an_earlier_prefix_was_checked(tmp_path: Path) -> None:
    from sheltersim.storage.query_cursors import seal,unseal
    store=ProjectStore(tmp_path)
    token=seal(store,"original signed progress with a gap")
    assert unseal(store,token)=="original signed progress with a gap"
    body=json.loads(base64.urlsafe_b64decode(token));body["payload"]="forged progress without a gap"
    forged=base64.urlsafe_b64encode(json.dumps(body).encode()).decode()
    with pytest.raises(StorageError,match="invalid_scan_progress_cursor"):
        unseal(store,forged)


def test_experiment_plan_and_cell_preparation_never_launch_or_construct_live_provider(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    from sheltersim.experiments.scenario import ScenarioSpec
    from sheltersim.storage.experiments import ExperimentStore
    from sheltersim.web.projects import ProjectAPI
    from sheltersim.llm.provider import AnthropicProvider
    store=ProjectStore(tmp_path);store.create_project("p")
    scenario=ScenarioSpec.load(Path(__file__).resolve().parents[1]/"fixtures/scenarios/h00_small_replay.json")
    saved=store.save_scenario("p",scenario,approved_hash=digest(json.loads(scenario.canonical_json())))
    experiments=ExperimentStore(store)
    plan=experiments.create("p","matrix",{"scenario_id":scenario.id,"scenario_version":saved["scenario_version"],
        "plan":{"seeds":[1,2],"variants":[{"id":"base"},{"id":"other","parameters":{"psychology.stress_pressure":0.008}}]}})
    assert len(plan["cells"])==4
    assert store.runs("p")==[]
    assert plan["external_validation"] is False
    def no_live(*_args: object,**_kwargs: object) -> None:
        raise AssertionError("scientific cell preparation must remain offline and unexecuted")
    monkeypatch.setenv("ANTHROPIC_API_KEY","unit-placeholder-not-a-real-key")
    monkeypatch.setattr(AnthropicProvider,"__init__",no_live)
    api=ProjectAPI(store,recover=False)
    monkeypatch.setattr(api._pool,"submit",no_live)
    try:
        result=experiments.prepare(api,"p","matrix","cell-0000",{"approved":True,"approved_plan_hash":plan["plan_hash"]})
        _,checkpoint=store.load_branch("p",result["run_id"],"main")
        assert checkpoint.simulation_minute==0
        assert checkpoint.runtime_state["execution_purpose"]=="offline_experiment"
        assert checkpoint.runtime_state["llm"]["mode"]=="offline"
        retry=experiments.prepare(api,"p","matrix","cell-0000",{"approved":True,"approved_plan_hash":plan["plan_hash"]})
        assert retry["scenario_version"]==result["scenario_version"]
        assert len(store.runs("p"))==1
    finally:
        api.close()


def test_temporary_absence_cannot_drop_the_return_state_reference() -> None:
    from sheltersim.contracts.presence import PresenceRecord
    from pydantic import ValidationError
    with pytest.raises(ValidationError,match="retained state reference"):
        PresenceRecord(agent_id="person",status="temporarily_absent",since_minute=1)
