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

## Kernels

- `canonical_json(value)` / `canonical_sha256(value)`: the bytes (and their SHA-256)
  of `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
  allow_nan=False).encode()` for plain JSON values. Other values, NaN and infinity
  raise `Unsupported`, so the caller falls back to `json`.
  `core/checkpoint_cache.py` uses `canonical_json` for what a Checkpoint save encodes;
  elsewhere replacing `json` alone saved about 1% (see below).

- `psychology_integrated(globals, agents)`: one minute of
  `IntegratedDistressLifeStatePolicy` with `BodyDynamicsFormula` (simple needs and
  stress, life-state feedback, exact mean reversion) for every agent. Every float
  operation keeps the Python order; `max`/`min` return the first argument on a tie
  and `sum` is CPython 3.12's compensated sum, so the results are the same floats.
  The runner uses it through `IntegratedDistressLifeStatePolicy.native_deltas` when
  no trace needs the per-agent parts, instead of `deltas_without_traces`.
  A new value outside `AgentStateDelta`'s bounds raises `Unsupported`, so its Python
  checks raise the same error; otherwise those checks are skipped.

- `social_channel_update(...)`: one tick of `ExpressivenessChannelOpennessPolicy.update`
  (contact sampling, durations, cooldowns, the bounded stress synchrony and its traces),
  through `native_update`. `CounterRNG` draws, `math.fsum` (`fsum`, exposed for tests),
  `max`/`min` ties and float order are those of Python. Contacts whose weights sum to
  zero make the Python code raise, so they are left to it.

- `information_contacts_without_messages(...)`: one tick of
  `InformationSpreadPolicy.update` while no message, belief or support-request record
  exists: decayed query pressure, sampled contacts and their records. It needs only
  people's IDs and zones, so the runner builds no `WorldView` for it. Any message
  sends the tick to Python.

  Both raise `Unsupported` for any value of another type or any state Python would
  reject, and neither runs when a trace needs the Python contact records.
  Seven days, run beside the previous commit: `update_agent_state` 76.4 s -> 26.1 s,
  `sample_endogenous_events` 38.6 s -> 1.8 s, with the other changes of this step
  (docs/rust-port.md).

- `social_relationships_valid`, `body_relationships_valid`, `body_agents_valid`,
  `psychological_agents_valid`, `agent_dynamics_valid`, `movement_positions_valid`,
  `information_contacts_valid`, `unit_values_valid`, `world_agents_valid`: fast
  acceptance of the large loops of validation, read where Python keeps the state. Each
  returns true only if every entry passes the Python rules; on false the caller runs
  those rules, which accept the value or raise their own error, so results and error
  messages come from one place. Key checks stay Python functions, remembered per key
  (`core/tick_validation.py`, `PairKeys`).

- `RecordWatch` (`native/src/watch.rs`): which records of a dict of dicts were written
  since the last check, from CPython's dict watchers, which report every write
  whichever code makes it. The functions are outside the stable ABI, so
  `sheltersim.native` looks them up with ctypes and passes their addresses; if that
  fails, the checks read every record. A list a rule reads is read at every check.

- `ValuesSnapshot`: the values of a dict at the last check, held so that the next
  check can tell by identity that the dict still starts with them. It lets the
  end-of-tick check (`WorldState.validate_after_tick`) skip settled support requests.

- `EventStore` (stage B1): the realized EventLog behind `sheltersim.events.queue.EventLog`.
  Each record is its Python object, its canonical JSON bytes, or both; the indexes
  (ID, sequence, minute, type, provenance keys, care requests, support kinds, facility
  events) live in Rust. `extend_saved` builds the log from saved bytes, parsing and
  checking them in parallel without the GIL: each must be the canonical JSON of a record
  that passes the `EventLogRecord`/`ScheduledEvent` checks. A record object is made
  only when Python reads it. `split_json_array` cuts a saved chunk into item bytes.
  Day 7: Checkpoint load 17.4 s → 4.2 s, a fork near day 7 24.0 s → 6.2 s
  (docs/rust-port.md).

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
