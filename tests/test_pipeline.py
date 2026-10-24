from sheltersim_foundation.core import Checkpoint, SimulationPipeline, WorldState


def test_pipeline_connects_validation_execution_and_periodic_checkpoints() -> None:
    seen_checkpoints: list[Checkpoint] = []
    pipeline = SimulationPipeline(
        validate_scenario=lambda raw: {"id": str(raw), "duration": 3},
        create_world=lambda scenario: WorldState(
            scenario_id=scenario["id"],
            seed=11,
            duration_minutes=scenario["duration"],
        ),
        checkpoint_sink=seen_checkpoints.append,
        checkpoint_interval=2,
    )

    result = pipeline.run("demo")

    assert result.world.minute_index == 3
    assert result.checkpoint_count == 3
    assert [item.state["minute_index"] for item in seen_checkpoints] == [0, 2, 3]
