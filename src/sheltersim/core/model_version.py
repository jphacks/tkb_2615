"""The whole-model version a Run was computed with, and read-only views of older Runs.

A Checkpoint records ``model_signatures()`` in ``model_versions``. Besides the
signatures of each installed policy and state contract it holds two whole-model items:

- ``engine:model``, the version of the simulation as a whole. It is raised once, in one
  place (ENGINE_VERSION), when results change for every Scenario without a
  per-Scenario signature changing: libm, RNG keys or the order of rules.
- ``engine:rng_algorithm``, the ``algorithm_id`` of the RNG (core/rng.py).

Checkpoints and Run records saved before these items existed hold neither. They were
computed by engine version 1 with the only RNG that existed, so an absent item means
UNRECORDED_ENGINE: an explicit rule, tested on fixtures/saved_runs/engine-v1-small.

A Run whose recorded versions differ from the installed ones is an old-model Run. It is
view-only (docs/rust-port/step4-plan.md, L9): its saved points are shown as saved, a
minute between them is rounded to the saved point at or before it and says so (D2), and
continuing, forking, submitting or applying an EffectProgram fails with
ViewOnlyRunError. Nothing is computed again with the installed model.

Saves of older shapes that the installed code still migrates when it restores them (the
pre-queue FIFO mode and information state schema 1) are continued today. With
LEGACY_SHAPES_TICKABLE false they are view-only too, so no tick reaches them: the
switch for the change that removes their Python migrations (step 4, L10).

A world restored for viewing carries a SavedPointView mark. TickEngine, the controller,
BranchRuntime and Checkpoint.capture refuse a marked world, so a view can't be ticked,
continued or saved as a new Checkpoint.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sheltersim.core.rng import CounterRNG

ENGINE_VERSION_KEY = "engine:model"
RNG_ALGORITHM_KEY = "engine:rng_algorithm"
# Raised by the change that changes results for every Scenario (step 4, L11).
ENGINE_VERSION = "1"
# Saves from before engine:* was recorded: engine 1 and the only RNG there was.
UNRECORDED_ENGINE = {ENGINE_VERSION_KEY: "1", RNG_ALGORITHM_KEY: "sha256-keyed-v1"}
VIEW_ONLY_CODE = "run_model_view_only"
# Whether saves of older shapes may still be continued (see the module docstring).
LEGACY_SHAPES_TICKABLE = True

_MODEL_PREFIXES = ("policy:", "simulator:", "state:", "operator:")
_QUEUE_SIGNATURE = "simulator:facility_queues_v1"


def engine_signatures() -> dict[str, str]:
    """The whole-model items model_signatures() records for the installed code."""
    return {ENGINE_VERSION_KEY: ENGINE_VERSION, RNG_ALGORITHM_KEY: CounterRNG.algorithm_id}


def recorded_engine(model_versions: Mapping[str, str]) -> dict[str, str]:
    """The engine:* items of recorded versions, with the rule for saves without them."""
    return {
        **UNRECORDED_ENGINE,
        **{key: value for key, value in model_versions.items() if key.startswith("engine:")},
    }


def same_models(first: Mapping[str, str], second: Mapping[str, str]) -> bool:
    """Whether two recorded version maps name the same models (absent engine:* is v1)."""
    return {**UNRECORDED_ENGINE, **first} == {**UNRECORDED_ENGINE, **second}


def _model_items(versions: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in versions.items()
        if key.startswith(_MODEL_PREFIXES) or key == "model_suite"
    }


@dataclass(frozen=True)
class ModelVersionStatus:
    """How a saved Run's recorded versions compare with the installed code."""

    recorded_version: str
    installed_version: str
    # Recorded items that differ from the installed ones, sorted.
    differences: tuple[str, ...] = ()
    # "legacy_queue" when the explicit facility-queue migration restores the save.
    migration: str | None = None

    @property
    def current(self) -> bool:
        """Whether the Run may be continued with the installed model."""
        return not self.differences

    def message(self) -> str:
        """The user-facing Japanese text of an old-model Run."""
        tail = "閲覧のみです。保存した時点だけを表示し、続きの計算、分岐、介入の適用はできません。"
        if self.recorded_version != self.installed_version:
            older = (
                self.recorded_version.isdigit()
                and self.installed_version.isdigit()
                and int(self.recorded_version) < int(self.installed_version)
            )
            return (
                f"このRunは{'旧' if older else '別の版の'}モデル（版 {self.recorded_version}）で"
                f"計算されました。今のモデルは版 {self.installed_version} です。{tail}"
            )

        def named(keys: list[str]) -> str:
            return "、".join(keys[:3]) + (" ほか" if len(keys) > 3 else "")

        models = [key for key in self.differences if not key.startswith("legacy:")]
        if models:
            return f"このRunは今と異なるモデルの版（{named(models)}）で計算されました。{tail}"
        shapes = [key.removeprefix("legacy:") for key in self.differences]
        return f"このRunは以前の保存の形（{named(shapes)}）で保存されました。{tail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "view_only": not self.current,
            "recorded_version": self.recorded_version,
            "installed_version": self.installed_version,
            "differences": list(self.differences),
            "message": None if self.current else self.message(),
        }


def model_version_status(
    model_versions: Mapping[str, str],
    configs: Mapping[str, Any],
    model_state: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
    installed: Mapping[str, str] | None = None,
) -> ModelVersionStatus:
    """Compare a Checkpoint's recorded versions with the installed model_signatures().

    model_state is read only when the signatures differ, so a caller may pass a
    function that loads it. Without installed, model_signatures(configs) is computed and
    may raise for configs the installed code no longer accepts.
    """
    if installed is None:
        from sheltersim.contracts.policies import model_signatures

        installed = model_signatures(dict(configs))
    engine_recorded = recorded_engine(model_versions)
    engine_installed = {key: value for key, value in installed.items() if key.startswith("engine:")}
    differences = {
        key
        for key in engine_recorded.keys() | engine_installed.keys()
        if engine_recorded.get(key) != engine_installed.get(key)
    }
    recorded = _model_items(model_versions)
    installed_items = _model_items(installed)
    migration = None
    if recorded != installed_items:
        state = model_state() if callable(model_state) else model_state
        # The one explicit migration of an earlier save that the installed code restores:
        # independent facility queues added under an otherwise unchanged signature.
        if (
            _QUEUE_SIGNATURE in installed_items
            and _QUEUE_SIGNATURE not in recorded
            and "facility_queues_v1" not in state
            and "facility_geometry" in configs
            and {key: value for key, value in recorded.items() if key != _QUEUE_SIGNATURE}
            == {key: value for key, value in installed_items.items() if key != _QUEUE_SIGNATURE}
        ):
            migration = "legacy_queue"
        elif (
            recorded
            or "state_contracts" in configs
            or any(key.startswith("operator:") for key in installed_items)
        ):
            differences.update(
                key
                for key in recorded.keys() | installed_items.keys()
                if recorded.get(key) != installed_items.get(key)
            )
        # Otherwise a save from before any signature was recorded, of a Scenario
        # without state contracts, restores as it is.
    if not LEGACY_SHAPES_TICKABLE:
        differences.update(_legacy_shapes(migration, model_state))
    return ModelVersionStatus(
        recorded_version=engine_recorded[ENGINE_VERSION_KEY],
        installed_version=engine_installed.get(ENGINE_VERSION_KEY, ENGINE_VERSION),
        differences=tuple(sorted(differences)),
        migration=migration,
    )


def _legacy_shapes(
    migration: str | None, model_state: Mapping[str, Any] | Callable[[], Mapping[str, Any]]
) -> set[str]:
    """The older shapes a save holds that the installed code migrates on restore."""
    state = model_state() if callable(model_state) else model_state
    shapes = set()
    if migration == "legacy_queue" or "facility_queues_legacy_mode_v1" in state:
        shapes.add("legacy:facility_queues_fifo")
    information = state.get("information_spread_v1")
    if isinstance(information, Mapping) and information.get("schema_version") == 1:
        shapes.add("legacy:information_schema_1")
    return shapes


def unreadable_status(model_versions: Mapping[str, str]) -> ModelVersionStatus:
    """The status of a save whose configs the installed code can't sign at all."""
    return ModelVersionStatus(
        recorded_version=recorded_engine(model_versions)[ENGINE_VERSION_KEY],
        installed_version=ENGINE_VERSION,
        differences=("model_signatures",),
    )


class ViewOnlyRunError(ValueError):
    """Continuing, forking or changing a Run computed with another model version."""

    code = VIEW_ONLY_CODE

    def __init__(self, status: ModelVersionStatus) -> None:
        super().__init__(f"{VIEW_ONLY_CODE}: {status.message()}")
        self.status = status


class ViewOnlyWorldError(TypeError):
    """A world restored for viewing was passed to a tick, a continuation or a save."""


@dataclass
class SavedPointView:
    """The mark of a world restored for viewing; see the module docstring.

    requested_minute is the minute a caller asked for; the world is the saved state at
    saved_minute. They differ only for an old-model Run (D2).
    """

    status: ModelVersionStatus
    saved_minute: int
    requested_minute: int

    @property
    def rounded(self) -> bool:
        return self.requested_minute != self.saved_minute

    def rounding(self) -> dict[str, Any] | None:
        """What a response says about a minute it rounded, or None."""
        if not self.rounded:
            return None
        return {
            "requested_minute": self.requested_minute,
            "saved_minute": self.saved_minute,
            "message": (
                f"旧モデルのRunのため、{self.requested_minute}分は計算し直さず、"
                f"保存した時点（{self.saved_minute}分）の状態を表示しています。"
            ),
        }

    def describe(self) -> dict[str, Any]:
        """Fields a view response adds: the model status and any rounding."""
        described: dict[str, Any] = {}
        if not self.status.current:
            described["model_view"] = self.status.to_dict()
        rounding = self.rounding()
        if rounding is not None:
            described["saved_point_rounding"] = rounding
        return described


_MARK = "_saved_point_view"


def mark_view(world: Any, view: SavedPointView) -> None:
    """Mark a restored world as a view; it can't be ticked, continued or saved."""
    world.__dict__[_MARK] = view


def saved_point_view(world: Any) -> SavedPointView | None:
    """The view mark of a world, or None for a world that may be ticked."""
    return world.__dict__.get(_MARK)


def require_tickable(world: Any, action: str) -> None:
    """Refuse a world restored for viewing."""
    view = world.__dict__.get(_MARK)
    if view is None:
        return
    if not view.status.current:
        raise ViewOnlyRunError(view.status)
    raise ViewOnlyWorldError(f"a world restored for viewing cannot be {action}")
