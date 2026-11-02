# sheltersim_native

Rust kernels for ShelterSim, on the `feature/rust-port` branch. The port moves the
state the tick computes on into Rust in stages; each stage must keep the world,
events and KPI hashes of a seven-day Run (`benchmarks/investigate_performance.py
--expect`). The plan and the measurements are in issue #272.

`sheltersim.native` uses the extension when it can be imported and
`SHELTERSIM_NATIVE` is not `0`; otherwise every caller runs its Python code, which
stays the reference. `tests/integration/test_native_kernels.py` compares each kernel
with that Python code at every tick and is skipped without the extension. CI on this
branch builds and installs the extension on Linux, Windows and macOS, and on Linux
runs the tests once more without it.

## Build and use without installing

```shell
cd native
../.venv/bin/maturin build --release -i ../.venv/bin/python
mkdir -p pylib && ../.venv/bin/python -m zipfile -e target/wheels/sheltersim_native-*.whl pylib
cd .. && PYTHONPATH=src:native/pylib SHELTERSIM_LLM_MODE=offline .venv/bin/python -m pytest -q -n auto
```

`native/tests/parity_check.py` compares the kernels with Python byte for byte
(random and edge-case floats, strings, structures, and optionally a saved
Checkpoint document) and times them.

## Layout for lanes

- `src/core/`: what every lane shares. `value.rs` (`V`, a JSON value that writes canonical
  bytes through `Encoder` and comes back to Python the same), `mathx.rs`, `rng.rs`,
  `interner.rs` (IDs and enum members as small numbers in rows, one Python object for each).
- `src/state/*.rs` and `src/rules/*.rs`: `build.rs` makes each file a module and a Python
  submodule, so a lane adds a file and edits neither `lib.rs` nor `sheltersim/native.py`. A file
  exports `pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()>`, and Python reaches it with
  `native.rules("movement")` or `native.state("movement")`, which are `None` without the
  extension. Catch `native.Unsupported` around a call; the Python code then decides.
  `Unsupported` is a type of its own in `lib.rs`: building one notes its line for
  `benchmarks/rule_shadow/refusal_census.py` (`src/core/census.rs`), off unless that tool turns it
  on. New refusals are counted without doing anything.
  `rules/parity.rs` is the example and exposes `core` to the tests. `rules/social_propagation.rs`
  and `rules/information.rs` hold the contact kernels; what they share is in `contacts.rs`.
- `src/core/world_core.rs`: `WorldCore`, one per world, in `world.__dict__["_core"]`, and a
  `NamespaceMirror` per owned namespace, which holds its `Row` and is the sink of its tracked tree
  (`sheltersim/core/state_mirror.py`). `src/state/sample_namespace.rs` is the small test-only
  namespace; `docs/rust-port/lanes/p4-world-core.md` explains the mechanism. Rows hold no Python handles.
  A `Row`'s `apply` runs no Python code and no `PyRefMut` is held across a callback. Read the
  world's core again after any callback (`core_of`), since a copy-and-swap may have moved it.
  `NamespaceMirror.generation_of(tree)` changes with every write that reaches a tree, from Python
  or from Rust, so a check that read a namespace need not read it again
  (`state_mirror.namespace_stamp`, `field_stamp`; `core/tick_validation.py`).
- `src/state/population.rs`: the `V` row of `population_presence_v1`, `population_lifecycle_v1`,
  `population_dynamics_v1`, `temporary_spaces_v1`, `geometry_overlay_v1` and `model_configs`
  (`docs/rust-port/lanes/m6-population.md`).
- `src/state/facility_queues.rs`: the typed row of `facility_queues_v1`. The rules read it and write
  through the tree (`docs/rust-port/lanes/m6-overlay.md`).
- `src/rules/population.rs`, `src/rules/population_lifecycle.rs`: the rules of `domain/population.py`
  and `domain/population_lifecycle.py`, each behind `kernel_first`. A kernel plans, refuses before
  its first write what the Python rule raises on, then writes with the rule's operations on the
  tracked containers, which forward them to the rows. `rules/population.rs` also reads what admission
  needs of `temporary_spaces.py` and `geometry_overlay.py`
  (`docs/rust-port/lanes/m6-population-rules.md`).
- `src/core/row.rs`: typed rows. `state_row!` writes `from_py`, `to_py`, `write_json`, the handling
  of forwarded writes and mirrored setters from a record's list of fields, and any `Field` is a
  `Row`. `src/state/psychological_distress.rs`, `src/state/metric_state.rs` and
  `src/rules/psychology.rs` are the production template: Rust owns those two, and its rule writes
  them value by value. Start a namespace from `docs/rust-port/lanes/p5-template-namespace.md`.
  `row.rs` also has `Maybe<T>` (a key a record may not have), `str_enum!`, `set_all`,
  `namespace_is_absent` and the `exact_*` readers of a kernel's inputs. A rule other modules call as
  a function uses `core/kernels.py`'s `kernel_first`; `src/state/staff_dynamics.rs` and
  `src/rules/staff_dynamics.rs` are the example of a whole module
  (`docs/rust-port/lanes/s-staff-dynamics.md`).
- `src/state/entities.rs`: `EntityStore`, the typed rows of a world's agents, facilities, staff,
  support requests and active tasks, from `WorldCore::entities`. The objects stay the dataclasses
  of `domain/models.py`, each mirroring its row; `IdMap`, `__setattr__` and `TrackedList` forward
  every write. A rule reads with `with_rows` (a tainted store says `Unsupported`) and writes with
  `write_field` (`docs/rust-port/lanes/p6-entity-windows.md`).
- `src/core/tracked.rs`: the only dict and list subclasses a kernel may read through the C API.
  Use `as_readable_dict` and `as_readable_list`, not `downcast` or `downcast_exact`, to accept
  state: a mirrored namespace and a world's `IdMap` are made of those classes, and any other
  subclass may override reads.
- Crates lanes need are already in `Cargo.toml` (`indexmap`, `smallvec`, `rustc-hash`, `itoa`),
  so `Cargo.lock` stays quiet. `HashMap` and `FxHashMap` are for lookups only: anything iterated
  into an output goes through `IndexMap`, or is sorted.

### Float rules

Every float operation of a rule goes through `core::mathx`, and keeps the order of the Python
expression. `tests/integration/test_native_parity.py` compares `mathx` with CPython.
`cargo clippy --manifest-path native/Cargo.toml -- -A clippy::all -D clippy::disallowed_methods`
enforces the bans in `native/clippy.toml`.

| Python | Rust | Note |
|---|---|---|
| `math.exp(x)`, `math.expm1(x)` | `mathx::exp`, `mathx::expm1` | libm, as CPython. `None` where Python raises |
| `x ** y`, `x ** 2`, `x ** 0.5` | `mathx::pow` | libm. Never `x * x` or `sqrt`: see below. `None` where Python raises or returns a complex, and for a subnormal result |
| `math.hypot(*xs)` | `mathx::hypot` | CPython's algorithm |
| `sum(xs)` | `mathx::py_sum`, `sum_nums` | compensated in 3.12. `sum([])` and a sum of ints are ints: `sum_nums` |
| `math.fsum(xs)` | `mathx::py_fsum` | `None` for what is not finite |
| `math.isclose(a, b)` | `mathx::py_is_close` | the default tolerances |
| `max(a, b)`, `min(a, b)` of floats | `mathx::py_max`, `py_min` | first argument on a tie; a NaN compares false |
| `max(0, x)`, any int with a float | `mathx::max_num`, `min_num` | returns the int: JSON writes `0`, not `0.0` |
| `round(x, 8)` | `mathx::py_round` | half to even on the exact binary value |
| `round(x)` | `mathx::py_round_int` | an int, half to even |

`None` means the rule raises `Unsupported`, and Python raises its error or computes the value.

`f64::powf` and an `extern "C"` `pow` with a constant exponent are rewritten by LLVM: `powf(x, 2.0)`
to `x * x`, `powf(x, 0.5)` to `sqrt`, and `pow(x, 2.0)` likewise (checked in the disassembly), and
those give other bits than `x ** 2` (790 in a million values) and `x ** 0.5` (865 in a million).
`mathx` passes its arguments through `black_box`, so libm is really called and constants are not
folded at compile time with the build machine's libm. Until step 4 this keeps Python's result on
each OS, because both call the same libm.

## Kernels

- `canonical_json(value)` / `canonical_sha256(value)`: the bytes (and their SHA-256)
  of `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
  allow_nan=False).encode()` for plain JSON values. Other values, NaN and infinity
  raise `Unsupported`, so the caller falls back to `json`.
  `core/checkpoint_cache.py` uses `canonical_json` for what a Checkpoint save encodes;
  elsewhere replacing `json` alone saved about 1% (see below).

- `rules/psychology.rs`: each person's minute (`update_psychology`, all the built-in psychology
  models, for a tick with no trace), the distress monitor and long-term distress
  (`update_long_term`) and writing the new values (`apply_deltas`). They replace steps of the
  update phase (`experiments/phases/segments.py`), read the entity rows, and read the zone's
  conditions, the body record, the group and what a person heard from `rules/world_view.rs`. Every float operation keeps the Python order; `max`/`min`
  return the first argument on a tie and `sum` is CPython 3.12's compensated sum, so the results
  are the same floats. A value `AgentStateDelta`'s checks might refuse raises `Unsupported` before
  anything changes, so the Python step runs and raises its own text.

- `rules/overlay.rs`: `domain/temporary_spaces.py` and the rules of `domain/geometry_overlay.py`:
  capacity, beds and room intake, placing an installed facility, checking and applying a floor-plan
  change, and the checks of both states. `rules/effects.rs` and `rules/proposals.rs` call it directly.
- `rules/facility_queues.rs`: `domain/facility_queues.py`. `rules/queues.rs` and `rules/service.rs`
  serve an independent FIFO through it.
- `rules/effects.rs`: the rules of an EffectProgram's events: the guard, the five handlers of
  `events/effects.py`, the two reads of modifier stacks and `validate_effect_program_state`. A
  handler keeps the Python rule's copy, check and swap. `model_validate` and the rules of other
  modules are Python, called by name. Anything but a success says `Unsupported` with the world as it
  was. `state/effect_program.rs` is the row of `effect_program_v1`.

- `rules/tick.rs`: `run_tick(controller, world, minute, rng, observer)`, one tick's phases in order,
  through `controller.run_tick` (`experiments/phases/driver.py`). Unsupported before anything changes
  for a tick that a hook watches or changes (a trace, a Provider that can be called, a shadow, a patched
  step or rule in the table of `install`). After that a kernel's Unsupported runs that step's Python rule.
  `python_path_only(True)` sends every tick to `phase_handler`; `stats()` says what ran.

- `rules/dispatch.rs`: `run_phase(controller, world, minute)`, the dispatch phase. An event type with
  a Rust function (`social_interaction`, `agent_travel_complete`, `toilet_service_complete`,
  `resource_service_complete`, `support_request_timeout`, `staff_support_complete`) runs it when the registered handler
  is that class with its `apply` as defined. The loop calls the other handlers' `apply`, listed in
  `install_table`, with the Python dispatcher's steps around it: that runs their kernels
  (`rules/handlers.rs`, `rules/information_events.rs`, `rules/staff_operation_events.rs`,
  `docs/rust-port/lanes/m6-handlers-rest.md`), or their Python rule on a refusal. An EffectProgram's
  marked event, a patched handler and any other event a Rust function refused calls
  `controller._dispatch_one_event`. Unsupported only before the first change. `rules/travel_completion.rs`
  is the walk's handler, with `_after_arrival`'s kernel, and `rules/toilet.rs`, `rules/support.rs` and
  `rules/care_receipts.rs` the toilet's service, the support requests and the care receipts
  (`docs/rust-port/lanes/m6-handlers.md`). No borrow is held
  across a Python call. `use_python_entries(True)` makes it call each rule through its Python entry,
  for `benchmarks/rule_shadow`, and `stats()` says what ran and why anything was refused.
- `rules/decisions.rs`: `propose_rows(controller, view, rng)`, `observed_row(controller, view,
  agent_id)` and `record_outcome(controller, proposal, *, target_id, next_decision_at, committed,
  observed_state)` replace the steps `_propose_due`, `_observed_state` and `_record_outcome`. A
  `ProposalRow` answers what `_apply_effect`'s Python body reads and makes the `ActionProposal`
  for anything else. `record_outcome` appends the `_IndexedMetricSink` record as JSON bytes
  (`decision_sink` in a sink's own class says what it may do), then calls `commit_action` and
  `remember_decision`. Unsupported comes before anything is appended; after that, a rule that
  refuses runs in Python on its own. A trace or a sink of another class is Unsupported.
- `rules/behavior.rs`: `propose_due(controller, view, rng)` and `observed_state(controller, view,
  agent_id)` compute the proposals and the snapshot `rules/decisions.rs` keeps as rows, here as the
  Python `ActionProposal`s and the snapshot dict. They read the snapshot of `rules/world_view.rs`, so
  a tick makes no Python view of the people for them. They run for `UtilityBehaviorPolicy`
  and `PriorityRuleBehaviorPolicy` (the exact classes) with a `CounterRNG`. They write nothing, so
  `Unsupported` at any point runs the Python step: a view Python made (or a partial one), another
  class, and anything `ActionCandidate` and `ActionProposal` would turn down.

- `rules/proximity.rs`: `sample_contacts(controller, minute)` replaces the step
  `_sample_proximity_contacts`, with `DistanceSampledProximityPolicy.sample` on the positions of the
  `movement_v1` row, or of its tree when the row is tainted. `sample_of` gives the pairs alone.
- `rules/world_view.rs`: `view_after_movement(controller, world)` and
  `view_for_proposals(controller, world, minute)` replace the two steps of the phases that make a
  `WorldView`. A snapshot of plain Rust values (`ViewData`) is built from the entity rows, the rows of
  `information_spread_v1` and `psychological_distress_v1`, and a read of the namespaces Python still
  owns. It is what `WorldView.from_world` and `after_state_update` provide, with the same audit hash
  (`state_hash`, which writes the canonical document from the snapshot). The returned `WorldView`
  makes `agents`, `facilities`, `zone_members` and `group_members` when a Python reader first asks
  (`RustView.python_fields`, through `WorldView.__getattr__`). A person's context comes from
  `rules/person_context.rs` (`PersonContext` and `_fields_of_person`) and a zone's conditions from
  `rules/zone_environment.rs` (`zone_environment_view` and its 8 formulas), each kept while its
  document stays equal. `selected_models` stays the Python contract. A value a typed field can't hold, or anything Python would raise on, says
  `Unsupported`, and `from_world` runs. `view_of(view)` gives a kernel the snapshot; for a view Python
  made it reads the fields the update kernels read and sets `partial`. Nothing refuses a partial
  view for a rule: a rule that reads other fields must check `partial` and say `Unsupported`.

- `rules.social_propagation.update_social_propagation(controller, world, view, deltas, minute, rng)`:
  the update phase's step `_social_propagation` for the three social policies (contact sampling,
  durations, cooldowns, each stress operator). Rust owns `social_propagation_v1`
  (`src/state/social_propagation.rs`) and reads agents from the entity rows. Each tick refills the
  contacts in the order of Python's new dict, reusing the nodes of the records
  (`NamespaceMirror::refill_record_map`). `CounterRNG` draws, `math.fsum`, `sum`, `max`/`min` ties
  and float order are those of Python. A trace, another RNG, a policy's subclass, and what the
  Python rule turns down give `Unsupported`, and the Python step runs. `core::rng` can keep each
  draw of a thread for the comparison of `benchmarks/rule_shadow` (`start_draw_log`, `take_draw_log`).

- `state.social_network.ROW`, `validate`: Rust owns `social_network_v1`
  (`src/state/social_network.rs`). The row lists the records written since the last check, whichever
  way they were written, so the check of a tick reads those and not all of them.

- `rules.social_network.observe_event(world, event, minute)`: what an applied event records, with the
  body's trust copied (`domain/social_network.py`). `rules/social_propagation.rs` records its observed
  contacts through the same code, with no Python call. `rules.social_dialogue.capture_context`,
  `validate_context`: the state context of a social event, read from the rows. Each is
  `kernel_first`: what the Python rule would refuse, or a value of another type, gives `Unsupported`
  before any change.

- `rules.social_interaction.apply(handler, world, event)`: `SocialInteractionHandler.apply` for a
  social action, which raises the rule's `EventSuppressed` text for one that no longer applies. A
  failed event and the other types of interaction give `Unsupported`, and the Python rule runs
  (`docs/rust-port/lanes/m3-social-network.md`, `m3-dispatch.md`).

- `state.movement.ROW`: Rust owns `movement_v1` (`src/state/movement.rs`).
- `rules.movement`: the movement rules of a tick (`src/rules/movement.rs`, with the floor plan and
  its geometry in `src/rules/movement_geometry.rs`): `advance_movement(world, minute)`,
  `start_movement`, `finish_movement`, `route_with_movement_time`, `route_is_physically_clear`,
  `plan_in_room_route`, `queue_slot_position`, `facility_approach_route`, and `validate(world)` for
  `register_rust_validator`. `domain/movement.py` calls them through `kernel_first`.
  `advance_movement` says Unsupported for a tick in which a queue trip must replan or a person is
  placed in a queue slot, which depend on the order of the walk. `zone_center` is for the tests.
  See `docs/rust-port/lanes/m4-movement.md`.
- `rules.queues`: a facility's physical queue (`src/rules/queues.rs`):
  `prepare_facility_queue(world, facility_id, *, resource, minute)`,
  `progress_physical_queue_positions` and `progress_physical_queue_head_to_service` (same
  arguments, optional `queue_id`). `domain/queueing.py` and `domain/movement.py` call them through
  `kernel_first`. Each plans and then writes, in one call.
  A person who gives up on the queue gives `Unsupported` before any write.
  See `docs/rust-port/lanes/m4-queues.md`.

- `state.staff_operations.ROW`: Rust owns `staff_operations_v1` (`src/state/staff_operations.rs`).
  `validate(world)` accepts a world where no staff member has a task; an active record reads the
  pending events, which is the Python rule's.
- `rules.staff`: a tick of `domain/staff.py` and `domain/staff_operations.py` where no staff member
  starts anything (`src/rules/staff.rs`): `assign_pending(service, minute, validate_state=)`,
  `update_operations(world, minute)`, `assign_operations(world, minute, router)`,
  `relief(world, minute)`, and `wait_snapshot(world, active_support, minute)` for
  `experiments/metrics.py`. They say Unsupported as soon as a worker would take something, and the
  Python rule runs the step. See `docs/rust-port/lanes/m4-staff.md`.
- `rules.metrics`: the KPI columns of a minute (`src/rules/metrics.rs`):
  `snapshot_fields(world, unsettled, minute)` for `MetricSnapshot.from_world` and
  `kpi_values(world, snapshot)` for `operational_kpi_values`. They read the rows and change nothing.
  `capture(world, recorder, minute)` is `MetricsRecorder.capture`, which `rules/tick.rs` calls for a
  `_MetricSink`; it says Unsupported with nothing changed for what the Python rule raises on.
- `rules.tick_validation`: the per-tick check (`src/rules/tick_validation.rs`):
  `entity_rules(world, requests, resource_config, initial_needs_type, needs_type)` answers for the
  entity and event rules of `WorldState.validate` at the end of a tick, reading the objects and not
  the entity rows. It has no text: it returns the zones whose capacity is still the Python rule's,
  or says Unsupported and the Python rules run. `population_dynamics(world)` is the validator of
  that namespace with no one pending. See `docs/rust-port/lanes/m5-metrics.md`.
- `rules.after_tick`: all of `WorldState.validate_after_tick` on a tick where nothing
  `TickValidationMemo.begin` compares has moved (`src/rules/after_tick.rs`):
  `after_tick(world, memo, shadow=False)` returns the zones whose capacity is still the Python
  rule's, with the memo moved on, or says Unsupported with the memo as it was and the Python check
  runs. `Plan` is what it reads of `model_configs`, made by `core/tick_validation.py` after a check
  passed. Validators come from `_RUST_VALIDATORS`; a rule with none is called in Python from there.
  `install` lists what a patch of sends the tick to the Python check. `stats()` says what ran. See
  `docs/rust-port/lanes/m6-validate.md`.
- `state.shared_operations.ROW`: Rust owns `shared_operations_v1`
  (`src/state/shared_operations.rs`): the heads typed, each receipt its canonical bytes.
  `validate_tick(world)` accepts a tick's state (no replay of the history) when the kernel, or the
  Python rules through `recent_passed(world)`, checked the recent receipts.
  `rules.common_operations.record_shared_operation(world, record)` appends a `CareTransitionReceipt`.
- `state.resource_ledger.ROW`, `validate`: Rust owns `resource_ledger_v1`
  (`src/state/resource_ledger.rs`): the accounts typed, the audit trail as the canonical bytes of each
  record, with the newest of each resource kept typed for the check. A record the rules would turn
  down, or a write into a record already there, taints the namespace. `validate` answers for the
  check of a tick; the replay of a save stays in Python. `rules.resources.apply_operation(world,
  event, operation)`, `metric_values(world, *, tick_memo=None)` and `reservation_id_was_used(world,
  id)` are `kernel_first` (`docs/rust-port/lanes/m4-resources.md`).

- `state.resource_service.ROW`, `validate`: Rust owns `resource_service_v1`
  (`src/state/resource_service.rs`): typed, with the two histories as lists of records. `validate`
  answers for the check of a tick; a save still reads the histories in Python.
  `rules.service` (`src/rules/service.rs`): `facilities_to_serve(manager)`, `serve_head(manager,
  facility_id, resource_id, minute)`, `complete(manager, event)`, `request(manager, agent_id,
  facility_id, *, minute=None, validate_state=None, queue_id=None)` and
  `record_request_outcome(manager, *, ...)`, each `kernel_first` on `ResourceServiceManager`.
  `serve_head` is one pass of `arbitrate`'s loop after the positions; it returns the started event's
  ID, or None to stop. `serve_independent_head` is the same pass of one independent queue.
- `rules.proposals.apply_effect(controller, proposal)`: `_apply_effect` (`src/rules/proposals.rs`),
  which returns `(target_id, next_decision_at, committed)`. `seek_help`, `seek_information`, a bed in
  a temporary room and a trip of no minutes give `Unsupported` before any write.
- `rules.travel`: `route(controller, origin_zone_id, destination_zone_id, agent_id)` and
  `schedule_travel(controller, *, ...)`: `_route` and `_schedule_travel` (`src/rules/travel.rs`).
  Edge capacities and another route policy give `Unsupported`. The graph as rows stays on the
  controller as `_route_rows` while `space.graph` is the same object. See
  `docs/rust-port/lanes/m4-service.md`.

- `state.body_state.ROW`: Rust owns `body_state_v1` (`src/state/body_state.rs`): the records and
  relationships typed, the history as the canonical bytes of each item. `rules.body_state.
  record_social_event(world, event, minute)` is `domain/body_state.py`'s rule with the operators of
  `policies/affect.py` and `policies/relationships.py`; `rules.psychology.apply_deltas` writes the
  row. `state.body_state.rows_valid(world)` accepts the header, records and relationships, reading
  only the relationships written since it last said yes. The config and history rules read what
  the row doesn't hold and stay in Python (`docs/rust-port/lanes/m3-body-state.md`).

- `rules.information.sample_contacts(controller, minute, rng)`: the sample phase's step
  `_sample_information_contacts` for the three information policies (contact sampling, durations,
  forgetting, transfers and their probabilities). Rust owns `information_spread_v1`
  (`src/state/information.rs`) and reads agents from the entity rows; the household of the view is
  read from `population_presence_v1`, which Python holds. The tree gets single values and the
  refilled contacts, never the whole namespace, and the events are scheduled as rows
  (`EventWriter` of `src/state/events.rs`), with the errors of `ScheduledEvent` for what a row
  refuses. A trace, another RNG, a policy's subclass and whatever the Python
  rule might raise on give `Unsupported`, and the Python step runs. `planned_contacts` returns the
  same tick's draws and contacts without changing anything, for `benchmarks/rule_shadow`.

  Seven days, run beside the previous commit: `update_agent_state` 76.4 s -> 26.1 s,
  `sample_endogenous_events` 38.6 s -> 1.8 s, with the other changes of this step
  (docs/rust-port.md).

- `agent_dynamics_valid`, `movement_positions_valid`, `world_agents_valid`: fast
  acceptance of the large loops of validation, read where Python keeps the state. The
  namespaces Rust owns have their `validate` in `state/` instead, which reads the row
  (`docs/rust-port/lanes/t-tick-validation.md`). Each
  returns true only if every entry passes the Python rules; on false the caller runs
  those rules, which accept the value or raise their own error, so results and error
  messages come from one place.

- `ValuesSnapshot`: the values of a dict at the last check, held so that the next
  check can tell by identity that the dict still starts with them. It lets the
  end-of-tick check (`WorldState.validate_after_tick`) skip settled support requests, and
  the scans of unsettled requests during a tick (`WorldState.unsettled_support_requests`)
  read only the rest.

- `state.events.QueueStore` and `EventWriter` (`src/state/events.rs`,
  `docs/rust-port/lanes/e-events.md`): the pending events behind
  `sheltersim.events.queue.EventQueue`, in the order of `pending()`, and what a Rust rule uses to
  allocate a sequence, schedule an `EventRow` and record an outcome with no Python object. Python
  gets a `ScheduledEvent` when it first reads the event. An order outside i64 moves that queue to
  Python (`_LargeOrderEventQueue`), and `EventWriter::of` then says `Unsupported`.

- `state.agent_dynamics.ROW` and `rules.agent_dynamics`: Rust owns `agent_dynamics_v1` and every
  writer but `remove_agent` and `population_lifecycle.py`, which forward one record each
  (`docs/rust-port/lanes/f-action-lifecycle.md`). `rules.action_lifecycle`: the lifecycle receipts.
  `ActiveActions` holds a controller's open actions as rows, gives a Python rule a plain dict
  (`as_plain`) and reads it back when it fits; it is not saved. A receipt is an `EventRow` appended
  through `EventWriter`, with no Python object until Python reads the record.

- `EventStore` (stage B1): the realized EventLog behind `sheltersim.events.queue.EventLog`.
  Each record is its canonical JSON bytes, its Python object, or both; the indexes
  (ID, sequence, minute, type, provenance keys, care requests, support kinds, facility
  events) live in Rust. Appending from Python writes the bytes from the frozen fields in the same
  step, and `all_encoded` says that every record has bytes written or checked here: a save's
  restore check then reads the live log instead of parsing the bytes and comparing them
  (`CheckpointHistoryCache.restored_log`). A value the writer refuses, such as a str subclass in a
  payload, leaves that record's bytes to the Python encoder, and the old check runs. `extend_saved` builds the log from saved bytes, parsing and
  checking them in parallel without the GIL: each must be the canonical JSON of a record
  that passes the `EventLogRecord`/`ScheduledEvent` checks. A record object is made
  only when Python reads it. `split_json_array` cuts a saved chunk into item bytes.
  Day 7: Checkpoint load 17.4 s → 4.2 s, a fork near day 7 24.0 s → 6.2 s
  (docs/rust-port.md). Appending reads a record's index fields through interned names, and
  skips the `Mapping` check of a provenance value that is a str.

- `sensitive_candidates(raws)` (stage B3): the distinct object keys of the JSON texts
  `raws`, and their distinct string values the privacy patterns could match, read in
  parallel without the GIL. The patterns stay in Python (`storage/projects.py`); the
  kernel leaves out only strings that can not match them, as explained at the top of
  `src/checkpoint.rs`. A Checkpoint save passes all its new chunks at once, and each
  history index row is checked through it.

- `chunk_hashes(items, chunks)` (stage B3): the SHA-256 of `[item,item,...]` for each
  `(start, count)` of a list of item bytes, in parallel and without joining them. These
  are the leaves of the Checkpoint v2 hash tree (`core/checkpoint_cache.py`) and the
  archive's chunk keys.

- `same_saved_records(records, raws, record_type, event_type)` (`src/checkpoint.rs`): whether
  each live EventLog record is exactly what restoring its saved bytes gives, field by field
  with the types, without building the restored record. A save uses it only for a log with a
  record whose bytes the Python encoder made. It reads every field of the bytes,
  and a false says nothing about `==`: the restore check of a save then compares restored
  records as before (`WorldState.same_state`).

- `record_json(record, record_type, event_type)` (`src/checkpoint.rs`): the canonical JSON of
  `record.to_dict()`, read from the frozen record without thawing it, or None for any value
  that is not frozen plain JSON. `same_saved_records` reads the result back on its own path.

- `index_row(row)` (`src/checkpoint.rs`): one pass over a history index row of plain values,
  giving its canonical JSON, the hash, its identities and event references
  (`_identities`, `_event_references` in `storage/history_index.py`), the sorted keys joined by NUL
  and the strings for the privacy check, and the text its tokens are read from. None for any other
  row, which Python reads as before.
- `dumps_length(value)` (`src/checkpoint.rs`): `len(json.dumps(value, ensure_ascii=False).encode())`
  of a plain JSON value, for the size limit of a decision's observed state.
- `EntityStore.rows_json(version)` (`src/state/entities.rs`): each entity row's canonical JSON, by
  field, in the order of `WorldState._canonical_data`. A row nothing reached for writing keeps its
  bytes object, so the same object means the same row (`Table::changed`, set by every way to a row
  for writing). `NamespaceMirror.row_json()` is the same for an owned namespace. A capture takes both
  (`state_mirror.owned_row_bytes`), so a new row needs only `state_row!` and its registry line.
- `HistoryPins` (`src/history.rs`): references to everything inside a history's entries, from when
  they were shown equal to their documents. `unchanged` reads only the containers, and false says
  nothing, so the caller compares with `same_json_items`.

- `same_json_items(current, documents, start, count)` (stage B3, `src/history.rs`): whether
  each of `current[:count]` is exactly `documents[start + i]`, with the types: 1, 1.0 and
  True differ, a tuple and a list differ, and floats compare by their bits. A save compares
  each append-only history with the documents of its last save, and its full validation
  does not read the entries found unchanged (`core/save_validation.py`). Anything that is
  not a plain JSON value compares as different, and the rules read the entry again.

- `EventStore.active_lifecycle_records()`: the `agent_action_lifecycle` records that
  decide each agent's active action, in the order a full pass inserts the agents, from
  an index kept as records are added. The runner calls it at each fork and resume
  instead of making every lifecycle record's object. `EventStore.of_types_from(types,
  start)` reads a type's records from a position, for a reader that keeps its result as
  the log grows.

- `scheduled_event_checked(...)` and `view_state_hash(...)` (`native/src/records.rs`, stage
  B4): `ScheduledEvent._check` with its frozen payload and provenance, and the audit hash
  of a `WorldView` without building its document. Each accepts only what the Python code
  accepts (exact types, stable IDs, finite floats, keys it can write without escaping);
  anything else raises `Unsupported`, and the Python code decides or raises. No model
  formula moved in this stage. `float_json` is the float form of the canonical encoder, which
  writes ryu's digits directly in the range where they equal `float.__repr__`.
  Seven days: docs/rust-port.md.

## Results so far (WSL2, 16 cores, generated Scenario, 100 people)

| Check | Python | Rust |
|---|---:|---:|
| Floats compared with `repr` (random bits, decimals, edges) | — | 799,870, 0 mismatches |
| Day-7 world_state document (352 MB), JSON | 3,257 ms | 1,209 ms (byte-identical) |
| Same, JSON + SHA-256 | 3,864 ms | 1,256 ms |
| One day, project route (develop 4272b40) | 71.7 s | 70.8 s (same world/events/KPI hashes) |
| Full offline tests | — | 1372 passed, 4 skipped |

### Checkpoint save (stage B3)

| Check | Before | After |
|---:|---:|---:|
| One late save, `TimelineStore.record` (near day 7) | 1,076 ms | 420 ms |
| Both hashes | 376 ms | 20 ms |
| Privacy check of the new chunks | 119 ms | 10 ms |
| Seven days, `TimelineStore.record` (336 saves) | 288.8 s | 125.2 s (same world/events/KPI hashes) |

### Psychology update (tick prototype)

| Check | Python | Rust |
|---|---:|---:|
| Deltas compared each tick for one day (100 people, 13 fields) | — | 0 mismatches (now `tests/integration/test_native_kernels.py`, 360 minutes) |
| One day, library route: total | 42.6 s | 35.9 s (same world/events/KPI hashes) |
| Same, `update_agent_state` phase | 15.5 s | 9.2 s |
| Psychology policy alone, one day | 6.66 s | 0.61 s |
| └ of which the Rust kernel | — | 0.07 s (about 95x the Python computation) |
| Full offline tests with both kernels | — | 1372 passed, 4 skipped |

Python without the per-agent traces (`deltas_without_traces`, PR #286 on develop) takes the same policy from 6.54 s to
1.25 s a day and one day from 42.0 s to 36.7 s, close to the Rust kernel's 35.9 s.
Against that Python, this kernel alone saves under a second a day.

Most of the native path's time is building the inputs from `WorldView` and the
`AgentStateDelta` objects in Python. The computation itself is about 95x faster;
keeping the per-agent state in Rust would remove most of the boundary cost.

### Canonical JSON

Python's json encoder is already C, and saves reuse most history bytes, so
replacing JSON alone saves about 1%. The time is in Python-level loops (row
building, validation, model updates); those loops, not leaf functions, have to
move for Rust to pay off.
