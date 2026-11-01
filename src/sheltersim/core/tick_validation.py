"""What the check at the end of a tick may skip, because it is provably unchanged.

WorldState.validate_after_tick keeps one TickValidationMemo per world, and only while
the rust kernels are available; without them every tick runs the plain per-tick
validation. A validation that fails discards the memo, so it only ever records facts
about a state that passed. The memo holds no simulation state: dropping it costs one
slower check and changes no result.

"Full validation" below is world.validate(), which runs at every Checkpoint save. It reads
everything these savings skip; of old history it skips only entries it finds exactly
unchanged since they last passed (core/save_validation.py).

Each saving rests on one of these arguments:

- Config checks read only model_configs, and the zone environment's also the zone IDs. They
  are skipped while no write reached model_configs (its mirror's generation, which needs no
  reading of 124 KB of JSON), or, after a write or for a world whose configs no mirror follows,
  while they equal what they were when the checks last passed. Equality does not tell 1 from True
  or 1.0; no tick writes model_configs, and the next full validation checks the values.
- A settled support request (resolved or unresolved) depends only on its own fields,
  which SupportRequestState reports writing, on the set of Agent IDs, and on the clock
  only through "not after the current minute". It is checked again when written or
  when the Agent IDs change. The scans of unsettled requests during a tick use the same
  reports (WorldState.unsettled_support_requests).
- The population presence, lifecycle and dynamics states and the geometry overlay are skipped
  when everything their rules read is unchanged: their own state by its mirror's generation, or,
  with no mirror, by its canonical JSON. A write of the same value counts as a change. The
  generation does not see dict.__setitem__ on a node of the tree, which the JSON did; the next
  full validation reads it, as for the rows' own checks (docs/rust-port/lanes/t-tick-validation.md).
- The newest shared operations record is skipped while it and the rest of the state
  are unchanged; new records are checked as they arrive.
- used_reservation_ids is append-only, so only its new tail is checked in full; the
  earlier IDs are compared with the ones that passed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sheltersim import native
from sheltersim.core.context import ContextScope
from sheltersim.core.state_mirror import field_stamp, namespace_stamp, same_stamp

if TYPE_CHECKING:
    from sheltersim.core.world import WorldState

_PRESENCE_KEY = "population_presence_v1"
_LIFECYCLE_KEY = "population_lifecycle_v1"
_DYNAMICS_KEY = "population_dynamics_v1"
_OVERLAY_KEY = "geometry_overlay_v1"
_OPERATIONS_KEY = "shared_operations_v1"


class TickValidationMemo:
    """Facts from the last passed end-of-tick check of one world."""

    def __init__(self) -> None:
        self.minute = -1
        self._configs: dict[str, Any] | None = None
        self._configs_stamp: tuple[Any, int] | None = None
        self._agent_ids: frozenset[str] = frozenset()
        self._zone_ids: frozenset[str] = frozenset()
        self._facility_zones: dict[str, str] = {}
        # The state of each namespace at the last check: its stamp, or its JSON without a mirror.
        self._seen: dict[str, Any] = {}
        self._presence_is_self_contained = False
        # Results of the config checks, valid while configs_unchanged.
        self.psychology_model: str | None = None
        self.required_states: set[str] = set()
        self.declared_states: dict[str, Any] = {}
        self.resource_config: Any = None
        self.information_config: Any = None
        self.geometry_scope: dict[tuple[str, int], tuple[Any, Any]] = {}
        self.body_config_json: bytes | None = None
        self.environment_config_passed = False
        # Facts of the check in progress, set by begin().
        self.configs_unchanged = False
        self.agents_unchanged = False
        self.zones_unchanged = False
        self.presence_unchanged = False
        self.lifecycle_unchanged = False
        self.dynamics_unchanged = False
        self.overlay_unchanged = False
        # Support requests: every value of the dict, in order, then which to recheck.
        self._requests = native.values_snapshot()
        self._open_requests: list[Any] = []
        self._written_requests: list[Any] = []
        self._added_requests: Any = []
        self._checked_requests: list[Any] = []
        # used_reservation_ids: the IDs that passed, as a list and as a set.
        self._used_ids: list[Any] = []
        self._used_id_set: set[Any] = set()
        # Shared operations: the newest record checked, and the state's other fields.
        self._last_operation: Any = None
        self._last_operation_json: bytes | None = None
        self._operations_header_json: bytes | None = None
        self._operations_unchanged = False

    def __deepcopy__(self, memo: dict[int, Any]) -> TickValidationMemo:
        # A copied world holds new objects, so it starts with nothing proven.
        return TickValidationMemo()

    def begin(self, world: WorldState) -> None:
        """Compare what the skipped checks read with the state that last passed."""
        configs_stamp = field_stamp(world, "model_configs")
        self.configs_unchanged = (
            same_stamp(configs_stamp, self._configs_stamp) or world.model_configs == self._configs
        )
        self._configs_stamp = configs_stamp
        if not self.configs_unchanged:
            self.geometry_scope.clear()
            self.environment_config_passed = False
        self.agents_unchanged = world.agents.keys() == self._agent_ids
        zones_unchanged = self.zones_unchanged = world.zones.keys() == self._zone_ids
        model_state = world.model_state
        previous = self._seen
        current = {
            key: _state_seen(world, key)
            for key in (_PRESENCE_KEY, _LIFECYCLE_KEY, _DYNAMICS_KEY, _OVERLAY_KEY)
        }
        self._seen = current

        def same(key: str) -> bool:
            now, before = current[key], previous.get(key)
            if isinstance(now, tuple) or isinstance(before, tuple):
                return same_stamp(now, before)
            return now is not None and now == before

        presence = model_state.get(_PRESENCE_KEY)
        if not same(_PRESENCE_KEY):
            self._presence_is_self_contained = _no_archive_paths(presence)
        self.presence_unchanged = (
            self.agents_unchanged
            and zones_unchanged
            and same(_PRESENCE_KEY)
            and self._presence_is_self_contained
        )
        lifecycle = model_state.get(_LIFECYCLE_KEY)
        self.lifecycle_unchanged = (
            self.agents_unchanged
            and zones_unchanged
            and same(_LIFECYCLE_KEY)
            and (presence is None or self.presence_unchanged)
            # A pending relocation is compared with the tasks and the event queue.
            and isinstance(lifecycle, dict)
            and not lifecycle.get("pending_relocations")
        )
        dynamics = model_state.get(_DYNAMICS_KEY)
        self.dynamics_unchanged = (
            self.agents_unchanged
            and zones_unchanged
            and same(_DYNAMICS_KEY)
            # A pending arrival is compared with the event queue, the clock and the presence.
            and isinstance(dynamics, dict)
            and not dynamics.get("pending_arrivals")
        )
        overlay = model_state.get(_OVERLAY_KEY)
        facility_zones = {key: item.zone_id for key, item in world.facilities.items()}
        self.overlay_unchanged = (
            self.configs_unchanged
            and self.agents_unchanged
            and facility_zones == self._facility_zones
            and same(_OVERLAY_KEY)
            # Rooms expire with the clock, and route access is compared with the graph.
            and isinstance(overlay, dict)
            and not overlay.get("temporary_rooms")
            and not overlay.get("route_modifiers")
        )
        self._facility_zones = facility_zones
        self.minute = world.minute_index

    def commit(self, world: WorldState) -> None:
        """Record what the passed check proved."""
        if not self.configs_unchanged:
            self._configs = deepcopy(world.model_configs)
        if not self.agents_unchanged:
            self._agent_ids = frozenset(world.agents)
        if not self.zones_unchanged:
            self._zone_ids = frozenset(world.zones)
        self._settle_requests(world.support_requests)
        state = world.model_state.get(_OPERATIONS_KEY)
        if isinstance(state, dict) and isinstance(state.get("history"), list):
            self._remember_operations(state)

    def config_scope(self) -> ContextScope:
        """Reuse parsed geometry configs between ticks while the configs are unchanged."""
        from sheltersim.domain.geometry_overlay import _CONFIG_SCOPE

        return ContextScope(_CONFIG_SCOPE, self.geometry_scope)

    def requests_to_check(self, requests: dict[str, Any]) -> list[Any]:
        """The support requests that are not settled, written or new since the last check.

        Their order does not matter to a valid world; with several broken requests it
        decides only which error is raised first.
        """
        added = self._requests.added_values(requests) if self.agents_unchanged else None
        if added is None:
            # The dict no longer starts with the requests held, or the Agents changed.
            self._requests.clear()
            self._open_requests = []
            # Requests settled before still write to the old list, which is dropped.
            self._written_requests = []
            added = self._requests.added_values(requests)
        self._added_requests = added
        checked = {id(item): item for item in self._open_requests}
        checked.update((id(item), item) for item in self._written_requests)
        checked.update((id(item), item) for item in added)
        self._checked_requests = list(checked.values())
        return self._checked_requests

    def unsettled_requests(self, requests: dict[str, Any]) -> list[Any] | None:
        """The requests of the dict that may not be settled, in its order, or None if that is
        not known and the caller has to read them all. For scans during a tick.

        What was settled at the last check reports any write, so this needs only the
        requests open then, the ones added since, and that no settled one was reopened.
        The result may hold requests settled since; the caller reads their status.
        """
        added = self._requests.added_values(requests)
        if added is None or any(not _is_settled(item) for item in self._written_requests):
            return None
        return self._open_requests + added

    def _settle_requests(self, requests: dict[str, Any]) -> None:
        reopened = any(not _is_settled(item) for item in self._written_requests)
        still_open = []
        for request in self._checked_requests:
            if _is_settled(request):
                request.report_writes_to(self._written_requests)
            else:
                request.report_writes_to(None)
                still_open.append(request)
        # A reopened request came last in the check; unsettled_requests keeps the dict's order.
        if reopened:
            still_open = [item for item in requests.values() if not _is_settled(item)]
        self._open_requests = still_open
        self._requests.extend(self._added_requests)
        self._added_requests = []
        self._written_requests.clear()

    def reservation_ids(self, used_ids: list[Any]) -> set[Any] | None:
        """The IDs as a set after checking the new tail, or None to check them all."""
        known = self._used_id_set
        count = len(self._used_ids)
        # A ledger operation works on a copy of the list, so the list object changes.
        if used_ids[:count] != self._used_ids:
            return None
        added = used_ids[count:]
        for value in added:
            if not isinstance(value, str) or value in known:
                return None
            known.add(value)
        self._used_ids.extend(added)
        return known

    def remember_reservation_ids(self, used_ids: list[Any], used_id_set: set[Any]) -> None:
        self._used_ids = list(used_ids)
        self._used_id_set = used_id_set

    def no_new_operations(self, state: dict[str, Any], history: list[Any]) -> bool:
        """Whether the shared operations state is the one that last passed."""
        newest = history[-1] if history else None
        self._operations_unchanged = (
            self._operations_header_json is not None
            and newest is self._last_operation
            and saved_json(newest) == self._last_operation_json
            and _operations_header_json(state) == self._operations_header_json
        )
        return self._operations_unchanged

    def operations_passed_in_rust(self) -> None:
        """The kernel accepted the state: what's remembered stays what last passed here."""
        self._operations_unchanged = True

    def _remember_operations(self, state: dict[str, Any]) -> None:
        if self._operations_unchanged:
            return
        history = state["history"]
        self._last_operation = history[-1] if history else None
        self._last_operation_json = saved_json(self._last_operation)
        self._operations_header_json = _operations_header_json(state)


def _is_settled(request: Any) -> bool:
    # The values of SupportRequestStatus.RESOLVED and UNRESOLVED; a StrEnum equals its value.
    return request.status in ("resolved", "unresolved")


def saved_json(value: Any) -> bytes | None:
    """The value's canonical JSON, or None if it has none, which never compares as same."""
    try:
        return native.canonical_json(value)
    except ValueError:
        # For example a str that cannot be encoded; the rules decide about it.
        return None


def _state_seen(world: WorldState, key: str) -> Any:
    stamp = namespace_stamp(world, key)
    return stamp if stamp is not None else saved_json(world.model_state.get(key))


def _operations_header_json(state: dict[str, Any]) -> bytes | None:
    return saved_json({k: v for k, v in state.items() if k != "history"})


def _no_archive_paths(presence: Any) -> bool:
    """Whether no presence record points into other model state, which may change."""
    if not isinstance(presence, dict):
        return False
    people = presence.get("people")
    return isinstance(people, dict) and not any(
        isinstance(person, dict) and person.get("archive_path") for person in people.values()
    )
