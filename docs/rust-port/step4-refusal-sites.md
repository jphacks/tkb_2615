# 手順4：一度も届かなかった断りの場所

`docs/rust-port/step4-plan.md` の2節の別紙です。`benchmarks/rule_shadow/refusal_census.py report` の `never` を、`handlers-rest` を試しにマージした木（`rust/l0-refusals` + `df84748`）で出しました。行番号はその木のものです。全テスト、ゲート、7日分のどこでも作られなかった `Unsupported` のか所で、**有効な状態から届かないという意味ではありません。** 記号は文を読んで付けた棚卸しの分類（文が合うものだけ。合わないものは —）です。

## `contacts.rs`（4）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 32 | `exact_int` | not an int | C |
| 34 | `exact_int` | int out of range | C |
| 156 | `pair_key` | not UTF-8 | C |
| 158 | `pair_key` | not UTF-8 | C |

## `core/row.rs`（2）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 464 | `unsupported` | (a value) | — |
| 503 | `exact_str` | a str is not UTF-8 | C |

## `core/value.rs`（1）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 44 | `read` | value nests too deeply | C |

## `core/world_core.rs`（1）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 796 | `list_at` | the tree has no such path | C |

## `lib.rs`（2）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 46 | `new_err` | (a value) | — |
| 70 | `value` | value nests too deeply | C |

## `records.rs`（2）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 53 | `frozen` | value nests too deeply | C |
| 150 | `item` | a view field is missing | C |

## `rules/action_lifecycle.rs`（3）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 137 | `into_rows` | an active action is under another agent's key | C |
| 402 | `receipt` | an event the Python class refuses | C |
| 602 | `is_applied` | the record holds another object of the event | C |

## `rules/after_tick.rs`（24）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 197 | `taken_by_kernel` | {name}: its kernel did not take it | — |
| 224 | `seen` | a namespace the memo compares has no mirror | — |
| 236 | `seen` | a namespace the memo compares holds another tree | — |
| 251 | `body_history` | the body state is another kind of dict | — |
| 254 | `body_history` | the body state has no history | — |
| 257 | `body_history` | the body history is another kind of list | — |
| 275 | `body_history` | a body history item is the Python rule's | — |
| 316 | `environment_schema` | no schema version | — |
| 329 | `operational_schema` | the operational events are the Python rule's | — |
| 337 | `operational_schema` | no schema version | — |
| 410 | `check` | the configs have no plan | — |
| 422 | `check` | the world has no model state | — |
| 509 | `check` | a declared namespace is missing | — |
| 512 | `check` | a declared namespace is the Python rule's | — |
| 515 | `check` | no schema version | — |
| 516 | `check` | a declared namespace has another version | — |
| 534 | `check` | temporary spaces without their contract | — |
| 547 | `check` | the body config has no kept JSON | — |
| 550 | `check` | the body state is another kind of dict | — |
| 553 | `check` | the body state has no config | — |
| 593 | `check` | information state without its model | — |
| 598 | `check` | psychological state without its model | — |
| 617 | `check` | a zone environment without its config | — |
| 709 | `after_tick` | domain/simulator_state.py has not installed the table | — |

## `rules/agent_dynamics.rs`（5）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 154 | `awareness_of` | too many initial facilities | C |
| 223 | `create_state` | an agent has no row | C |
| 259 | `add_agent` | an agent has no row | C |
| 292 | `write_known` | the row changed under the plan | C |
| 347 | `learn_facility_of` | the row changed under the plan | C |

## `rules/behavior.rs`（13）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 36 | `unsupported` | (a value) | — |
| 639 | `exact_i64` | an int is too large | C |
| 640 | `exact_i64` | a policy value is not an int | T |
| 652 | `number` | a policy value is too large | C |
| 667 | `snapshot_of` | the view is partial | H |
| 725 | `of` | an action has no trigger | C |
| 866 | `due_outcomes` | the seed is negative | C |
| 887 | `due_outcomes` | the policy version is not a str | C |
| 889 | `due_outcomes` | the policy version is not UTF-8 | C |
| 893 | `due_outcomes` | the policy version is empty or not a str | C |
| 917 | `due_outcomes` | the per-person coefficients are not a plain dict | C |
| 999 | `person_of` | the ID is not a str | C |
| 1001 | `person_of` | the ID is not UTF-8 | C |

## `rules/body_state.rs`（19）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 224 | `payload_of` | the payload is not frozen JSON | C |
| 325 | `config_float` | a body config value is not a float | C |
| 367 | `relationship_trust` | an ID is not plain | C |
| 387 | `record_relationship` | an ID is not plain | C |
| 409 | `record_relationship` | a minute does not fit | C |
| 420 | `record_relationship` | the interaction minutes are not an int | C |
| 422 | `record_relationship` | the body config is not a map | C |
| 429 | `record_relationship` | a duration is too long | C |
| 442 | `record_relationship` | a count does not fit | C |
| 445 | `record_relationship` | a count does not fit | C |
| 654 | `selected_models` | the model selection raises | M |
| 666 | `affect_of_the_config` | the body config raises | C |
| 701 | `models_of` | a relationship parameter is not a number | C |
| 708 | `models_of` | the distance scale is not positive | C |
| 715 | `models_of` | a relationship model this kernel doesn't know | M |
| 745 | `correction_actors` | no information model is selected | M |
| 764 | `correction_actors` | a message is not a str | C |
| 793 | `correction_actors` | a correction flag is not a bool | C |
| 885 | `action_of` | a person interacts with themselves | E |

## `rules/care_receipts.rs`（13）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 85 | `status_of` | an unknown care status | C |
| 125 | `plan_transition` | a source the receipt's contract is for the Python rule to judge | C |
| 133 | `plan_transition` | the event's identity is taken | E |
| 160 | `plan_transition` | a receipt is a document | C |
| 178 | `plan_transition` | the event's values are not what an event takes | C |
| 184 | `plan_transition` | the world holds another document than the row's | C |
| 261 | `append_transition` | the namespace is None | C |
| 267 | `append_transition` | the sources are not a tuple | C |
| 295 | `dict_of` | another kind of dict | C |
| 330 | `person_context` | the configs are not a dict | C |
| 352 | `source_ids_of` | a subclass of str | T |
| 365 | `source_ids_of` | the person's sources are for the Python rule to read | V |
| 764 | `ingest` | the namespace is another value | C |

## `rules/common_operations.rs`（19）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 82 | `observed_value` | a state change's path is too short | C |
| 85 | `observed_value` | a state change's path is too short | C |
| 101 | `observed_value` | a resource's change is not of its value | C |
| 109 | `observed_value` | an unknown kind of entity | C |
| 119 | `observed_value` | a state change reads another dict | C |
| 130 | `state_changes` | the changes are not a tuple | C |
| 142 | `state_changes` | a state change reads nothing | C |
| 150 | `state_changes` | a state change reads a large int | C |
| 183 | `record_shared_operation` | the world holds another document than the row's | C |
| 198 | `record_shared_operation` | an unknown care status | C |
| 206 | `record_shared_operation` | the sources are not a tuple | C |
| 212 | `record_shared_operation` | an ID is not an identifier | C |
| 245 | `record_shared_operation` | the receipt has no event | C |
| 251 | `record_shared_operation` | the receipt's event is not applied at this minute | E |
| 255 | `record_shared_operation` | the receipt's event has no row | C |
| 262 | `record_shared_operation` | the receipt does not match its event | E |
| 334 | `plan_record` | the state is not one the contract takes | C |
| 358 | `plan_record` | too many requests | E |
| 363 | `plan_record` | the update precedes the creation | E |

## `rules/decisions.rs`（2）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 444 | `decision_bytes` | the person is not in the world | E |
| 559 | `record_outcome` | the sink reads another world | H |

## `rules/dispatch.rs`（2）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 1068 | `run_inner` | events/handlers.py has not installed the table | H |
| 1072 | `run_inner` | a negative minute | C |

## `rules/effects.rs`（89）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 87 | `map_of` | not an object | C |
| 115 | `str_list` | a list of the marker holds another value | C |
| 118 | `str_list` | a list of the marker is another value | C |
| 141 | `held_program_mirror` | model_state is another kind of mapping | C |
| 145 | `held_program_mirror` | the world holds another effect_program_v1 | C |
| 161 | `event_row` | not a plain ScheduledEvent | C |
| 363 | `numeric_order` | a guard's value is not finite | C |
| 365 | `numeric_order` | a guard's value is not a number | C |
| 433 | `guard_target` | the guard's marker is another value | C |
| 442 | `guard_target` | the guard's marker has another program ID | C |
| 450 | `guard_target` | a shipment ID is another value | C |
| 479 | `guard_field` | a zone ID is another value | C |
| 512 | `guard_field` | a quantity past 64 bits | C |
| 522 | `guard_field` | a staff ID is another value | C |
| 556 | `guard_passes` | the guard is false, so the effect is suppressed | E |
| 696 | `facility_rule` | an active infrastructure outage | E |
| 705 | `facility_rule` | no effect_program_v1 | C |
| 734 | `facility_rule` | the modifier has expired or never started | E |
| 743 | `facility_rule` | the expiry would reduce capacity below occupancy | E |
| 814 | `environment_rule` | the payload has other fields | C |
| 822 | `environment_rule` | no marker | C |
| 825 | `environment_rule` | no installed policy | C |
| 831 | `environment_rule` | the event's effect is not scheduled | C |
| 836 | `environment_rule` | no zone_environment_v1 | C |
| 839 | `environment_rule` | zone_environment_v1 is another value | C |
| 842 | `environment_rule` | the modifier ID does not match its effect | E |
| 862 | `environment_rule` | the expiry does not match its start | E |
| 877 | `environment_rule` | an unsupported operator | C |
| 880 | `environment_rule` | the operator differs from its program | C |
| 883 | `environment_rule` | the zone does not exist | E |
| 893 | `environment_rule` | the action requires the resource-service policy | E |
| 898 | `environment_rule` | the end minute is not an int | C |
| 902 | `environment_rule` | the end minute is outside the Run | E |
| 917 | `environment_rule` | no unique expiry event | E |
| 920 | `environment_rule` | the expiry event is at another minute | E |
| 932 | `environment_rule` | the operator requires zone-environment-v2 | E |
| 935 | `environment_rule` | the assumption provenance is invalid | C |
| 985 | `temporary_space_rule` | no temporary_spaces_v1 | C |
| 988 | `temporary_space_rule` | temporary_spaces_v1 is another value | C |
| 996 | `temporary_space_rule` | no marker | C |
| 1009 | `temporary_space_rule` | the event does not reference an approved installed effect | E |
| 1013 | `temporary_space_rule` | the payload has other fields | C |
| 1020 | `temporary_space_rule` | the space is not active | E |
| 1033 | `temporary_space_rule` | the space does not match its expiry | E |
| 1037 | `temporary_space_rule` | the space is already expired | E |
| 1042 | `temporary_space_rule` | the payload has other fields | C |
| 1048 | `temporary_space_rule` | the end minute is not an int | C |
| 1052 | `temporary_space_rule` | the end minute is outside its interval | E |
| 1056 | `temporary_space_rule` | no source | C |
| 1097 | `geometry_rule` | the payload has other fields | C |
| 1101 | `geometry_rule` | not an operator of the floor plan | C |
| 1113 | `geometry_rule` | the effect does not reference its approved program | E |
| 1119 | `geometry_rule` | no modifier to expire | E |
| 1196 | `approved_marked_effect` | no marker | C |
| 1253 | `has_task_skill` | a task kind is not a plain str | C |
| 1303 | `staff_handoff_on_copy` | no replacement | E |
| 1313 | `staff_handoff_on_copy` | the participants or the task are invalid | E |
| 1321 | `staff_handoff_on_copy` | no staff_operations_v1 | C |
| 1324 | `staff_handoff_on_copy` | staff_operations_v1 is another value | C |
| 1330 | `staff_handoff_on_copy` | no operation record | C |
| 1362 | `staff_handoff_on_copy` | the replacement isn't a qualified idle one at the active work | E |
| 1366 | `staff_handoff_on_copy` | the replacement is unavailable | E |
| 1376 | `staff_handoff_on_copy` | the replacement is unavailable | E |
| 1393 | `staff_handoff_on_copy` | no one matching pending completion | E |
| 1454 | `staff_handoff` | no marker | C |
| 1482 | `facility_repair` | the target is not a known facility | E |
| 1485 | `facility_repair` | no shared receipt state | C |
| 1543 | `set_staff_role` | the event does not reference an approved staff target | E |
| 1551 | `set_staff_role` | the worker has an active task | E |
| 1556 | `set_staff_role` | the worker has active work | E |
| 1630 | `add_structure` | the count is not a positive int | E |
| 1636 | `add_structure` | the target or the operation is invalid | E |
| 1649 | `add_structure` | the generated staff ID exists | E |
| 1664 | `add_structure` | the generated toilet ID exists | E |
| 1848 | `pending_has_marker` | a pending event is not a plain ScheduledEvent | C |
| 1874 | `check_state` | marked events without the state | C |
| 1878 | `check_state` | marked events without the state | C |
| 1888 | `check_state` | another schema version | C |
| 1900 | `check_state` | another schema version | C |
| 1937 | `check_state` | an effect ID the Python rule decides | C |
| 1943 | `check_state` | a scheduled event ID the Python rule decides | C |
| 1949 | `check_state` | an unknown scheduled event | C |
| 1954 | `check_state` | a scheduled event has no marker | C |
| 1956 | `check_state` | a marker the Python rule decides | E |
| 1958 | `check_state` | a marker of another program | C |
| 1963 | `check_state` | a marker that is no mapping | C |
| 1974 | `check_state` | an unknown effect | C |
| 2012 | `check_state` | an event type that isn't its operator's | C |
| 2017 | `check_state` | a modifier of an unknown facility | C |

## `rules/facility_queues.rs`（29）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 59 | `members_of` | a queue lists an unknown request | C |
| 86 | `request_node` | the queues have no requests | C |
| 94 | `queue_node` | no queue state | C |
| 95 | `queue_node` | no queues | C |
| 96 | `queue_node` | no such queue | C |
| 113 | `write_enqueue` | no queue state | C |
| 114 | `write_enqueue` | no requests | C |
| 135 | `write_start` | no such request | E |
| 155 | `write_finish` | no queue state | C |
| 195 | `transition_row` | the record's identity is taken | C |
| 221 | `transition_row` | the receipt's contract refuses it | C |
| 238 | `transition_row` | the record's values are not what an event takes | C |
| 265 | `independent_queues_required` | the Python rule's parse raises | C |
| 274 | `independent_queues_required` | not a tuple | C |
| 326 | `create_facility_queue_state` | the config has no independent service points | C |
| 330 | `create_facility_queue_state` | no floor plan | V |
| 371 | `choose_queue_id` | the needs are not a list | C |
| 375 | `choose_queue_id` | a need is not a str | C |
| 403 | `choose_queue_id` | a signed point has no queue | C |
| 440 | `queue_head_agent_ids` | no such facility | C |
| 462 | `independent_heads` | a queue lists an unknown request | C |
| 538 | `start_service` | no such request | E |
| 541 | `start_service` | no such queue | C |
| 544 | `start_service` | the request is not queued | C |
| 549 | `start_service` | no queue state | C |
| 564 | `finish_service` | no such request | E |
| 595 | `record_queue_transition` | not a queue request | C |
| 608 | `record_queue_transition` | no such queue | C |
| 612 | `record_queue_transition` | no queue state | C |

## `rules/handlers.rs`（85）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 115 | `number` | a number does not fit a float | C |
| 205 | `ensure_identity_free` | a generated event's identity is taken | C |
| 212 | `ensure_identity_free` | a generated event's identity is taken | C |
| 240 | `require_source` | an event without a source | C |
| 327 | `proximity_on` | the provenance or time is for the Python rule to judge | C |
| 334 | `proximity_on` | the payload has other fields | C |
| 358 | `proximity_on` | the participants or measurement are for the Python rule to judge | C |
| 365 | `proximity_on` | the coordinates are for the Python rule to judge | C |
| 369 | `proximity_on` | the coordinates are for the Python rule to judge | C |
| 376 | `proximity_on` | the distance is for the Python rule to judge | C |
| 380 | `proximity_on` | the sample is stale | E |
| 384 | `proximity_on` | the sample is stale | E |
| 416 | `ledger_event_of` | the shipment has no operational state | C |
| 426 | `ledger_event_of` | the delivery does not match its shipment | E |
| 506 | `facility_units` | the units are past 64 bits | C |
| 567 | `delivery_delay_on` | no operational state | C |
| 583 | `delivery_delay_on` | the shipment is no longer pending | E |
| 591 | `delivery_delay_on` | the shipment is no longer pending | E |
| 640 | `water_outage` | the payload has other fields | C |
| 644 | `water_outage` | no operational state | C |
| 648 | `water_outage` | the outage does not match its state | E |
| 652 | `water_outage` | the outage's target does not match its state | E |
| 667 | `water_outage` | the zone has no water station | C |
| 672 | `water_outage` | the target is not a water station | C |
| 676 | `water_outage` | the outage is already active | E |
| 691 | `water_restoration` | the payload has other fields | C |
| 695 | `water_restoration` | no operational state | C |
| 738 | `staff_unavailable_on` | the payload has other fields | C |
| 743 | `staff_unavailable_on` | no operational state | C |
| 749 | `staff_unavailable_on` | an unknown staff member | E |
| 754 | `staff_unavailable_on` | the end of the absence is invalid | C |
| 759 | `staff_unavailable_on` | the absence was already applied | E |
| 789 | `staff_unavailable_on` | the event sequence changed | C |
| 812 | `staff_unavailable_on` | the staff member owns several tasks | C |
| 847 | `staff_restoration` | the payload has other fields | C |
| 851 | `staff_restoration` | no operational state | C |
| 888 | `interrupt_support_task` | the task has no request | C |
| 894 | `interrupt_support_task` | the task has no person | C |
| 911 | `interrupt_support_task` | the task's ownership is inconsistent | C |
| 926 | `interrupt_support_task` | the timeout is inconsistent | C |
| 956 | `interrupt_support_task` | the event sequence changed | C |
| 971 | `interrupt_support_task` | the person has two trips to support | C |
| 987 | `interrupt_support_task` | the person's trip is inconsistent | E |
| 1096 | `cancel_service` | the cancellation is for the Python rule to judge | C |
| 1101 | `cancel_service` | the cancellation is for the Python rule to judge | C |
| 1104 | `cancel_service` | the task was already cancelled | E |
| 1108 | `cancel_service` | no active task | E |
| 1113 | `cancel_service` | the task is another kind | C |
| 1121 | `cancel_service` | the queue has no such person | C |
| 1125 | `cancel_service` | not a resource facility | C |
| 1128 | `cancel_service` | the facility has no such slot | C |
| 1141 | `cancel_service` | the cancellation does not match its reservation | E |
| 1213 | `service_cancellation` | resource service cancellation requires WorldState v6 | C |
| 1238 | `interrupt_for_staff` | the staff member owns several services | C |
| 1245 | `interrupt_for_staff` | the service has incomplete ownership | C |
| 1251 | `interrupt_for_staff` | the service has inconsistent state | C |
| 1262 | `interrupt_for_staff` | the service has inconsistent state | C |
| 1265 | `interrupt_for_staff` | not a resource facility | C |
| 1274 | `interrupt_for_staff` | the reservation is inconsistent | C |
| 1426 | `population` | the primitive is for the Python rule to judge | C |
| 1430 | `population` | the primitive is not in a program | E |
| 1435 | `population` | the primitive is not in a program | E |
| 1444 | `population` | the primitive is not in the approved program | E |
| 1492 | `infrastructure_state` | no infrastructure model | V |
| 1594 | `recalculate_capacity` | an unknown outage | E |
| 1604 | `recalculate_capacity` | a facility has no base units | C |
| 1650 | `until_of` | the duration is invalid | C |
| 1664 | `start_outage` | the outage does not match its state | E |
| 1668 | `start_outage` | the outage is already active | E |
| 1675 | `start_outage` | the payload has other fields | C |
| 1681 | `start_outage` | the outage has no valid affected facilities | C |
| 1685 | `start_outage` | the outage conflicts with a modifier | E |
| 1709 | `restore_outage` | the restoration does not match an active outage | E |
| 1714 | `restore_outage` | the restoration's type does not match its cause | E |
| 1733 | `start_stressor` | the stressor is for the Python rule to judge | C |
| 1746 | `start_stressor` | the zone's stressor limit was reached | E |
| 1770 | `end_stressor` | the end does not match an active stressor | E |
| 1790 | `interrupt_route` | the interruption is for the Python rule to judge | C |
| 1798 | `interrupt_route` | the route already has an interruption | E |
| 1803 | `interrupt_route` | no shipment records | C |
| 1813 | `interrupt_route` | the shipment cannot be delayed | E |
| 1824 | `interrupt_route` | the shipment cannot be delayed | E |
| 1832 | `interrupt_route` | the shipment cannot be delayed | E |
| 1891 | `restore_route` | the restoration does not match an active interruption | E |
| 1926 | `infrastructure` | an unknown event type | E |

## `rules/information.rs`（15）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 38 | `unsupported` | (a value) | — |
| 90 | `number` | a setting is out of range | C |
| 122 | `read` | a contact setting is out of range | C |
| 128 | `read` | the forgetting rate is out of range | C |
| 193 | `inputs` | the minute is not an int | C |
| 195 | `inputs` | the minute is too large | C |
| 198 | `inputs` | the minute or the seed is out of range | C |
| 209 | `inputs` | the zone neighbors are not str tuples | C |
| 315 | `transmission_probability` | the hazard is out of range | C |
| 386 | `plan` | the state has another schema | O |
| 401 | `plan` | a person is under another ID | C |
| 404 | `plan` | a person has no beliefs or no query pressure | C |
| 463 | `plan` | a contact's minutes do not fit | C |
| 474 | `plan` | the contact counter does not fit | C |
| 523 | `plan` | a belief left the row | C |

## `rules/information_events.rs`（39）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 38 | `runtime_state` | the model is not enabled | M |
| 42 | `runtime_state` | the model has no state | T |
| 46 | `runtime_state` | the world holds another document than the row's | C |
| 50 | `runtime_state` | an older state is upgraded in Python | O |
| 70 | `seed_of` | a negative seed | C |
| 185 | `prior_of` | a belief is for the Python rule to read | C |
| 214 | `rumor` | the payload has other fields | C |
| 225 | `rumor` | a truth status for the Python rule to judge | C |
| 237 | `rumor` | the person has no record | C |
| 240 | `rumor` | the rumor is for the Python rule to judge | C |
| 248 | `rumor` | an ID for the Python rule to judge | C |
| 315 | `correction` | the payload has other fields | C |
| 326 | `correction` | the correction is for the Python rule to judge | C |
| 330 | `correction` | an unknown message | E |
| 333 | `correction` | a message is for the Python rule to read | C |
| 338 | `correction` | the correction must reference its rumor | E |
| 341 | `correction` | the correction conflicts with the truth | E |
| 342 | `correction` | the correction conflicts with the truth | E |
| 366 | `correction` | the person has no beliefs | C |
| 381 | `correction` | a conflicting verified state | C |
| 412 | `correction` | a belief for the Python rule to judge | C |
| 416 | `correction` | the person has no record | C |
| 496 | `contact` | the kind or time is for the Python rule to judge | C |
| 505 | `contact` | the provenance is for the Python rule to judge | C |
| 515 | `contact` | the participants are for the Python rule to judge | C |
| 521 | `contact` | the belief is for the Python rule to judge | C |
| 525 | `contact` | an unknown message | E |
| 528 | `contact` | a message is for the Python rule to read | C |
| 531 | `contact` | a person has no beliefs | C |
| 534 | `contact` | the sender no longer knows the message | E |
| 537 | `contact` | a belief is for the Python rule to read | C |
| 557 | `contact` | the sampled basis is not current | E |
| 561 | `contact` | the duration is below the minimum | E |
| 565 | `contact` | the probability is invalid | C |
| 572 | `contact` | the correction conflicts with the truth | E |
| 576 | `contact` | a belief is for the Python rule to read | C |
| 579 | `contact` | the recipient already has as strong a belief | E |
| 619 | `contact` | a belief for the Python rule to judge | C |
| 622 | `contact` | the person has no record | C |

## `rules/metrics.rs`（6）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 108 | `snapshot_fields` | the working units do not fit an i64 | C |
| 126 | `snapshot_fields` | a staff queue names no task | C |
| 229 | `kpi_values` | the Python rule says what is wrong with this world | C |
| 233 | `kpi_values` | the Python rule says what is wrong with this minute | C |
| 266 | `kpi_values` | a resource is missing | C |
| 354 | `capture` | the snapshot has other fields | — |

## `rules/movement.rs`（59）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 134 | `edge_minutes` | an edge's minutes are not a plain duration | C |
| 183 | `reserved_bed_point` | the overlay is not a plain dict | C |
| 192 | `reserved_bed_point` | a reservation is not a plain dict | C |
| 231 | `check_idle_point` | a display point is not a usable point | C |
| 264 | `stationary` | a display point is not a usable point | C |
| 283 | `plain_point` | a point has other fields | C |
| 288 | `plain_point` | a point lacks a field | C |
| 293 | `plain_point` | a point is not a usable point | C |
| 306 | `points_of_journey` | a route point is not a usable point | C |
| 426 | `refused` | (a value) | — |
| 480 | `queue_slot_in` | no such agent | C |
| 491 | `queue_slot_in` | a position is not a usable point | C |
| 495 | `queue_slot_in` | a position is not a usable point | C |
| 535 | `given_point` | a point is not a Point2D | C |
| 539 | `given_point` | a point is not a usable point | C |
| 550 | `mobility_of` | a mobility is not a str | C |
| 555 | `mobility_of` | another mobility | M |
| 573 | `plan_in_room_route` | no destination | E |
| 601 | `route_is_physically_clear` | a str is not UTF-8 | C |
| 686 | `facility_approach_route` | no such agent or facility | C |
| 716 | `room_minutes` | no floor plan | V |
| 732 | `room_minutes` | the duration is too large | C |
| 761 | `approach_point` | no such agent or facility | C |
| 764 | `approach_point` | no floor plan | V |
| 772 | `approach_point` | a position is not a usable point | C |
| 790 | `approach_point` | a footprint's centre is not a plain sum | C |
| 820 | `approach_point` | not a plain sum | C |
| 871 | `facility_move_invalidates` | a move's minute is too large | C |
| 885 | `room_path_is_clear` | a position is not a usable point | C |
| 893 | `room_path_is_clear` | a route's length is not a plain sum | C |
| 977 | `plan_journey` | a journey has no plain duration | C |
| 990 | `plan_journey` | a route's length is not a plain sum | C |
| 1006 | `plan_journey` | a route's minutes are too large | C |
| 1025 | `plan_journey` | closed is not a bool | C |
| 1034 | `plan_journey` | a route has fewer zones | E |
| 1072 | `plan_staff` | a staff operation is not a plain dict | C |
| 1082 | `plan_staff` | no operation | E |
| 1084 | `plan_staff` | a staff operation lacks a field | C |
| 1102 | `plan_staff` | a route has fewer zones | E |
| 1128 | `queue_of` | the world's events are not in Rust | C |
| 1130 | `queue_of` | the event queue is not in Rust | C |
| 1139 | `advance_movement` | a minute is too large | C |
| 1244 | `route_points_of` | the points are not a tuple | C |
| 1249 | `route_points_of` | a route point is not a Point2D | C |
| 1253 | `route_points_of` | a route point is not a usable point | C |
| 1345 | `plan_start` | a route's length is not a plain sum | C |
| 1436 | `movement_model` | another movement policy | M |
| 1441 | `movement_model` | another movement source | M |
| 1450 | `movement_model` | a speed is missing | C |
| 1453 | `movement_model` | a speed is not positive | C |
| 1464 | `movement_model` | a slowdown is not a plain value | C |
| 1496 | `route_with_movement_time` | the route is not a ZoneRoute | C |
| 1498 | `route_with_movement_time` | not a tuple | C |
| 1499 | `route_with_movement_time` | not a tuple | C |
| 1535 | `movement_minutes` | the route's minutes are too large | C |
| 1542 | `movement_minutes` | no such agent | C |
| 1561 | `movement_minutes` | not a plain sum | C |
| 1565 | `movement_minutes` | not a plain sum | C |
| 1588 | `movement_minutes` | the duration is too large | C |

## `rules/movement_geometry.rs`（26）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 337 | `point_of` | a coordinate is too large | C |
| 343 | `tuple_of` | not a tuple | C |
| 358 | `flag_of` | not a bool | C |
| 383 | `point_in_value` | a point is not a dict | C |
| 389 | `point_in_value` | a point is not a usable point | C |
| 405 | `moved` | a moved point is not a usable point | C |
| 423 | `layout_of` | a zone has no polygon | C |
| 433 | `layout_of` | a route has no points | C |
| 451 | `layout_of` | the overlay's moves are not a dict | C |
| 459 | `layout_of` | a move is not a dict | C |
| 469 | `layout_of` | a moved service point has no point of its facility | C |
| 492 | `constraints_of` | an obstacle has no polygon | C |
| 498 | `constraints_of` | the partitions are not a dict | C |
| 504 | `constraints_of` | a partition is not a dict | C |
| 510 | `constraints_of` | a partition has another number of corners | C |
| 513 | `constraints_of` | a partition's source is not an identifier | C |
| 518 | `constraints_of` | a partition's ID is not an identifier | C |
| 524 | `constraints_of` | the overlay's moves are not a dict | C |
| 531 | `constraints_of` | a footprint has no polygon | C |
| 536 | `constraints_of` | a moved footprint without the Scenario's plan | C |
| 540 | `constraints_of` | a moved footprint has no point of its facility | C |
| 548 | `constraints_of` | a move is not a dict | C |
| 658 | `parsed` | the Python rule's parse raises | C |
| 688 | `of` | the plans are poisoned | C |
| 722 | `of` | the plans are poisoned | C |
| 746 | `zone_center` | the centre of a zone is not finite | — |

## `rules/overlay.rs`（163）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 117 | `required` | the state has no such field | C |
| 138 | `list_of` | a value is not a list | C |
| 145 | `int_of` | a value is not a plain int | C |
| 161 | `flag_of` | a flag is not a bool | T |
| 172 | `polygon_of` | no polygon | C |
| 176 | `polygon_of` | a polygon has no corners | C |
| 182 | `square` | a square Python decides | C |
| 255 | `register_space` | temporary_spaces_v1 is not installed | C |
| 259 | `register_space` | the record has no space_id | C |
| 263 | `register_space` | the record has no zone_id | C |
| 294 | `expire_space` | temporary_spaces_v1 is not installed | C |
| 301 | `expire_space` | not a bool | C |
| 357 | `validate_spaces` | the state has another shape | C |
| 385 | `validate_spaces` | a space's end event is another value | C |
| 410 | `validate_spaces` | a history record has another shape | C |
| 419 | `validate_spaces` | a history record has another value | C |
| 447 | `capacity_of` | a capacity out of range | C |
| 459 | `capacity_of` | a space's zone is another value | C |
| 464 | `capacity_of` | a space's active is not a bool | C |
| 476 | `capacity_of` | the spaces' state is not a plain dict | C |
| 480 | `capacity_of` | a capacity out of range | C |
| 498 | `capacity_of` | the overlay is not a plain dict | C |
| 546 | `nominal_of` | no such zone | E |
| 591 | `capacity_metrics_kernel` | a capacity out of range | C |
| 618 | `effective_total_zone_capacity` | a capacity out of range | C |
| 745 | `admission_point` | a room is another value | C |
| 796 | `admission_point` | a round Python decides | C |
| 816 | `admission_point` | a distance is not finite | C |
| 825 | `admission_point` | a room's zone is not a str | C |
| 886 | `available_temporary_room` | a room's capacity is not a plain int | C |
| 913 | `admit_to_temporary_room` | no overlay | V |
| 919 | `admit_to_temporary_room` | no such room | C |
| 926 | `admit_to_temporary_room` | the room has no point | C |
| 932 | `admit_to_temporary_room` | a source is not a str | C |
| 990 | `plan_bed` | a bed's positions without the movement state | T |
| 1010 | `plan_bed` | a position is not a usable point | C |
| 1033 | `plan_bed` | a reservation is another value | C |
| 1037 | `plan_bed` | no rooms | C |
| 1053 | `plan_bed` | no such agent | C |
| 1108 | `plan_bed` | a root Python decides | C |
| 1116 | `plan_bed` | not a plain sum | C |
| 1142 | `cell_of` | a cell of the grid is too far | C |
| 1153 | `write_bed` | no overlay | V |
| 1270 | `installed_point` | a grid point is not a usable point | C |
| 1289 | `place_facility` | no overlay | V |
| 1345 | `place_installed_facility` | the sources are not a tuple | C |
| 1474 | `model_of` | the contract refuses the parameters | C |
| 1485 | `point_attr` | a point is not a usable point | C |
| 1493 | `points_attr` | not a tuple | C |
| 1499 | `points_attr` | a point is not a usable point | C |
| 1527 | `edges_of` | not a bool | C |
| 1535 | `constraints_of_plan` | no constraints | C |
| 1539 | `layout_of_plan` | no floor plan | V |
| 1555 | `clear_of_rooms` | (a value) | — |
| 1565 | `clear_of_rooms` | (a value) | — |
| 1569 | `clear_of_rooms` | (a value) | — |
| 1575 | `clear_of_rooms` | (a value) | — |
| 1587 | `facility_is_free` | (a value) | — |
| 1594 | `facility_is_free` | (a value) | — |
| 1604 | `facility_is_free` | (a value) | — |
| 1607 | `facility_is_free` | a task's target is another value | C |
| 1656 | `service_staff_in_zone` | the service config is another mapping | C |
| 1680 | `service_staff_in_zone` | (a value) | — |
| 1691 | `footprint_fits` | the Python rule raises on an obstacle | C |
| 1704 | `footprint_fits` | (a value) | — |
| 1707 | `footprint_fits` | (a value) | — |
| 1714 | `footprint_fits` | (a value) | — |
| 1717 | `footprint_fits` | (a value) | — |
| 1726 | `check_change` | (a value) | — |
| 1738 | `check_change` | (a value) | — |
| 1741 | `check_change` | (a value) | — |
| 1762 | `check_change` | a route's ID is not a str | C |
| 1777 | `check_change` | (a value) | — |
| 1794 | `check_change` | (a value) | — |
| 1798 | `check_change` | (a value) | — |
| 1802 | `check_change` | (a value) | — |
| 1805 | `check_change` | (a value) | — |
| 1810 | `check_change` | (a value) | — |
| 1814 | `check_change` | (a value) | — |
| 1827 | `check_change` | (a value) | — |
| 1838 | `check_change` | (a value) | — |
| 1841 | `check_change` | (a value) | — |
| 1845 | `check_change` | a facility in a zone with no polygon | C |
| 1848 | `check_change` | (a value) | — |
| 1853 | `check_change` | (a value) | — |
| 1865 | `check_change` | (a value) | — |
| 1869 | `check_change` | (a value) | — |
| 1877 | `check_change` | (a value) | — |
| 1880 | `check_change` | (a value) | — |
| 1883 | `check_change` | (a value) | — |
| 1890 | `check_change` | (a value) | — |
| 1900 | `check_change` | a position is not a usable point | C |
| 1903 | `check_change` | (a value) | — |
| 1915 | `check_change` | (a value) | — |
| 1922 | `check_change` | (a value) | — |
| 1930 | `check_change` | a route point is not a usable point | C |
| 1935 | `check_change` | (a value) | — |
| 1950 | `check_change` | not a tuple | C |
| 1954 | `check_change` | (a value) | — |
| 1957 | `check_change` | (a value) | — |
| 1960 | `check_change` | (a value) | — |
| 1965 | `check_change` | (a value) | — |
| 1968 | `check_change` | (a value) | — |
| 1974 | `check_change` | (a value) | — |
| 1979 | `check_change` | (a value) | — |
| 1987 | `check_change` | (a value) | — |
| 1991 | `check_change` | (a value) | — |
| 2008 | `check_change` | (a value) | — |
| 2013 | `check_change` | (a value) | — |
| 2016 | `check_change` | (a value) | — |
| 2023 | `check_change` | a position is not a usable point | C |
| 2026 | `check_change` | (a value) | — |
| 2051 | `check_change` | (a value) | — |
| 2056 | `check_change` | (a value) | — |
| 2108 | `apply_change` | not a ScheduledEvent | C |
| 2111 | `apply_change` | the payload is not a dict | C |
| 2115 | `apply_change` | no overlay | V |
| 2118 | `apply_change` | no parameters | C |
| 2122 | `apply_change` | the end minute is another value | C |
| 2145 | `apply_change` | the parameters are another value | C |
| 2161 | `apply_change` | the marker is another value | C |
| 2190 | `apply_change` | no such facility | C |
| 2212 | `apply_change` | no such route | C |
| 2221 | `apply_change` | accessible is not a bool | C |
| 2223 | `apply_change` | no parameters | C |
| 2243 | `apply_change` | not a bool | C |
| 2246 | `apply_change` | not a bool | C |
| 2258 | `apply_change` | the rooms are still to be made | V |
| 2262 | `apply_change` | the room did not start | E |
| 2270 | `apply_change` | not a dict | C |
| 2283 | `apply_change` | another operator | C |
| 2327 | `stamped_is_good` | a record has another shape | C |
| 2373 | `check_overlay` | the overlay exceeds a limit | E |
| 2398 | `check_overlay` | a history record is not what the Python rule passes | C |
| 2424 | `check_overlay` | a room has another shape | C |
| 2435 | `check_overlay` | a room's end event is another value | C |
| 2466 | `check_overlay` | a bed lies outside its room | C |
| 2472 | `check_overlay` | a route's ID is not a str | C |
| 2483 | `check_overlay` | an active room lacks a route | C |
| 2494 | `check_overlay` | active rooms overlap | C |
| 2498 | `check_overlay` | an active room covers a facility | C |
| 2504 | `check_overlay` | a move has another shape | C |
| 2509 | `check_overlay` | a move of an unknown facility | C |
| 2512 | `check_overlay` | a move is not what the Python rule passes | C |
| 2517 | `check_overlay` | a service point of an unknown facility | C |
| 2520 | `check_overlay` | a service point leaves its zone | C |
| 2526 | `check_overlay` | a partition has another shape | C |
| 2533 | `check_overlay` | a partition in an unknown zone | C |
| 2536 | `check_overlay` | a partition is not what the Python rule passes | C |
| 2546 | `check_overlay` | an unknown route | C |
| 2549 | `check_overlay` | a route's overlay has no modifier | C |
| 2556 | `check_overlay` | a route modifier has another shape | C |
| 2561 | `check_overlay` | accessible is not a bool | C |
| 2564 | `check_overlay` | a route modifier is not what the Python rule passes | C |
| 2569 | `check_overlay` | the overlay and the graph disagree | C |
| 2573 | `check_overlay` | the overlay exceeds a limit | E |
| 2578 | `check_overlay` | a footprint of an unknown facility | C |
| 2581 | `check_overlay` | a footprint in an unknown zone | C |
| 2584 | `check_overlay` | a footprint leaves its zone | C |
| 2587 | `check_overlay` | an active room overlaps a footprint | E |
| 2591 | `check_overlay` | no constraints | C |
| 2636 | `check_overlay` | an admission is not what the Python rule passes | C |
| 2641 | `check_overlay` | admissions exceed a room | E |

## `rules/parity.rs`（1）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 84 | `num` | not an exact int or float | C |

## `rules/population.rs`（121）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 130 | `event_row` | the event has no row | C |
| 230 | `required_identifier` | a presence field is not an Identifier | C |
| 238 | `maybe_identifier` | a presence field is not an Identifier or None | C |
| 249 | `identifiers` | a list holds what is not an Identifier | C |
| 252 | `identifiers` | a list of Identifiers is something else | C |
| 259 | `minute_field` | a minute is not a plain non-negative int | C |
| 280 | `parse` | a presence record is not a mapping | C |
| 283 | `parse` | a presence record has another field | C |
| 287 | `parse` | a presence status is another value | C |
| 298 | `parse` | an archive path has another length | C |
| 322 | `check` | expected return precedes departure | E |
| 325 | `check` | temporary absence requires a retained state reference | C |
| 329 | `check` | admitted presence requires a zone | C |
| 404 | `transition_is_plain` | a presence transition is not a mapping | C |
| 410 | `transition_is_plain` | a presence transition has another schema | C |
| 417 | `transition_is_plain` | a presence transition has another action | C |
| 426 | `transition_is_plain` | a presence transition has another hash | C |
| 429 | `transition_is_plain` | a presence transition has other time semantics | C |
| 437 | `presence_is_plain` | the presence state is not a mapping | C |
| 440 | `presence_is_plain` | the presence state has another field | C |
| 443 | `presence_is_plain` | the presence state has another schema | C |
| 446 | `presence_is_plain` | the presence state lacks its people | C |
| 459 | `presence_is_plain` | the presence history is something else | C |
| 600 | `truth` | a flag is not a bool | T |
| 651 | `plain_point` | a point is not a mapping | C |
| 655 | `plain_point` | a point is not two finite floats | C |
| 663 | `points_of` | the points of a room are something else | C |
| 669 | `squared` | a square overflows | C |
| 775 | `available_room` | a room ID is a str subclass | C |
| 807 | `admission_point` | a room is not a mapping | C |
| 870 | `admission_point` | a point does not round | C |
| 893 | `admission_point` | a room's zone is not a str | C |
| 1215 | `plan_admit_pending` | no overlay | V |
| 1216 | `plan_admit_pending` | a room admission has no admissions to join | V |
| 1377 | `generated_context` | a household has more dependents than a context holds | V |
| 1478 | `plan_arrivals` | the alternate zones are not a list or tuple | C |
| 1492 | `plan_arrivals` | an approach route with alternate zones | V |
| 1499 | `plan_arrivals` | the route targets another zone | E |
| 1511 | `plan_arrivals` | alternate rooms need another overlay schema | V |
| 1534 | `plan_arrivals` | a context's member index is invalid | C |
| 1540 | `plan_arrivals` | a support kind is not known | C |
| 1548 | `plan_arrivals` | the population state is made on first use by the Python rule | T |
| 1553 | `plan_arrivals` | past the pending bound | E |
| 1557 | `plan_arrivals` | past the runtime bound | E |
| 1568 | `plan_arrivals` | past the history bound | E |
| 1594 | `plan_arrivals` | a stable ID repeats | C |
| 1707 | `plan_arrivals` | no overlay | V |
| 1709 | `plan_arrivals` | a room admission has no admissions to join | V |
| 1738 | `check_waiting_person` | a waiting arrival already has a presence record | E |
| 1754 | `plan_completion` | the arrival has no program ID | C |
| 1757 | `plan_completion` | the arrival has no effect ID | C |
| 1760 | `plan_completion` | no approved program | C |
| 1767 | `plan_completion` | the approach is not in an approved program | C |
| 1770 | `plan_completion` | the arrival lacks its program's provenance | C |
| 1775 | `plan_completion` | a route has no sources | C |
| 1803 | `plan_completion` | ScheduledEvent refuses the completion | C |
| 1893 | `apply_arrival_approach_completion` | another event type | C |
| 1896 | `apply_arrival_approach_completion` | a payload is not a mapping | C |
| 1900 | `apply_arrival_approach_completion` | the payload's fields do not match | C |
| 1910 | `apply_arrival_approach_completion` | a payload value is not a str | C |
| 1913 | `apply_arrival_approach_completion` | no approved program | C |
| 1921 | `apply_arrival_approach_completion` | the completion is outside its program | C |
| 1924 | `apply_arrival_approach_completion` | no population state | C |
| 1935 | `apply_arrival_approach_completion` | no such pending arrival | E |
| 1944 | `apply_arrival_approach_completion` | the completion does not match its arrival | E |
| 1954 | `apply_arrival_approach_completion` | the route is unknown | E |
| 1958 | `apply_arrival_approach_completion` | the route targets another zone | E |
| 1963 | `apply_arrival_approach_completion` | the route's provenance does not match | E |
| 1970 | `apply_arrival_approach_completion` | the approach has no presence | E |
| 2035 | `plan_retry` | a retry count is not an int | C |
| 2046 | `plan_retry` | provenance is not a mapping | C |
| 2061 | `plan_retry` | a program's marker is not two strs | C |
| 2066 | `plan_retry` | a payload is not a mapping | C |
| 2069 | `plan_retry` | ScheduledEvent refuses the retry | C |
| 2096 | `pair_of` | a pair key is not a list of strs | — |
| 2100 | `pair_of` | a pair key is not two people | — |
| 2157 | `check_queue_rows` | a pending event has no row | C |
| 2428 | `queue_departures` | include_initial is not a bool | C |
| 2431 | `queue_departures` | the population state is made on first use by the Python rule | T |
| 2453 | `queue_departures` | too few residents to leave | E |
| 2467 | `queue_departures` | a minute before the Run | E |
| 2531 | `traits_are_plain` | traits are not a mapping | C |
| 2535 | `traits_are_plain` | traits have another field | C |
| 2542 | `traits_are_plain` | a role is another value | C |
| 2547 | `traits_are_plain` | medical support is not a bool | C |
| 2556 | `context_is_plain` | a context is not a mapping | C |
| 2560 | `context_is_plain` | a context has a field a generated one lacks | C |
| 2565 | `context_is_plain` | a context has too many people | C |
| 2568 | `context_is_plain` | requires_review is not a bool | C |
| 2573 | `context_is_plain` | the review reasons are something else | C |
| 2578 | `context_is_plain` | the roles are something else | C |
| 2581 | `context_is_plain` | a guardian status is another value | C |
| 2589 | `context_is_plain` | a support kind is not a str | C |
| 2592 | `context_is_plain` | a support kind is unknown or repeats | C |
| 2596 | `context_is_plain` | the support kinds are something else | C |
| 2612 | `validate_population_dynamics_state` | the population state is not a mapping | C |
| 2633 | `validate_population_dynamics_state` | an initial ID is not a str | C |
| 2654 | `validate_population_dynamics_state` | a context is of an unknown person | C |
| 2658 | `validate_population_dynamics_state` | a context has no household | C |
| 2694 | `check_pending_items` | a pending arrival is not a mapping | C |
| 2714 | `check_pending_items` | a pending arrival's zone or ID is not a str | C |
| 2721 | `check_pending_items` | the alternate zones are not a list | C |
| 2726 | `check_pending_items` | an alternate zone is not a str | C |
| 2729 | `check_pending_items` | an alternate zone is invalid | C |
| 2737 | `check_pending_items` | pending care needs are not a list | C |
| 2741 | `check_pending_items` | a pending context is invalid | C |
| 2750 | `check_pending_items` | a pending care context disagrees | C |
| 2757 | `check_pending_items` | an approach's fields are something else | C |
| 2760 | `check_pending_items` | an approach's timing is invalid | C |
| 2772 | `check_pending_items` | an approach's route is unknown | C |
| 2775 | `check_pending_items` | an approach disagrees with its route | C |
| 2780 | `check_pending_items` | an approach's completion minute is invalid | C |
| 2785 | `check_pending_items` | an approach's completion event is not a str | C |
| 2789 | `check_pending_items` | an approach's completion event is missing | C |
| 2794 | `check_pending_items` | no queue | C |
| 2795 | `check_pending_items` | the queue is not in Rust | C |
| 2797 | `check_pending_items` | an approach's completion event is missing | C |
| 2804 | `check_pending_items` | an approach's completion event disagrees | C |
| 2807 | `check_pending_items` | an approach has a completion event it should not | C |
| 2814 | `check_pending_items` | an approach disagrees with the presence | C |
| 2818 | `check_pending_items` | no traits | C |

## `rules/population_lifecycle.rs`（63）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 322 | `check_archive` | the person already has an archive | E |
| 326 | `check_archive` | no such person | E |
| 332 | `check_archive` | a person's values do not fit a snapshot | C |
| 435 | `check_restore` | the retained person is missing | E |
| 438 | `check_restore` | a new record of all people for one person is the Python rule's | V |
| 628 | `busy` | no such person | E |
| 710 | `plan_relocation` | passage capacities read every trip under way | V |
| 713 | `plan_relocation` | no route | E |
| 716 | `plan_relocation` | no such person | E |
| 720 | `plan_relocation` | the route is not clear | E |
| 731 | `plan_relocation` | the route ends after the scenario | E |
| 735 | `plan_relocation` | a journey has no duration | C |
| 767 | `plan_relocation` | ScheduledEvent refuses the arrival | C |
| 786 | `plan_action` | no one is selected | E |
| 806 | `plan_action` | an expected return before the exit | E |
| 832 | `plan_action` | no valid destination | E |
| 857 | `plan_action` | no valid destination | E |
| 860 | `plan_action` | no row | T |
| 863 | `plan_action` | the person is in an active task | E |
| 866 | `plan_action` | the destination has no room | E |
| 880 | `plan_action` | a reunion needs a household and the presence contract | E |
| 889 | `plan_action` | the people are not co-located | E |
| 893 | `plan_action` | no one is selected | E |
| 897 | `plan_action` | another action | C |
| 1016 | `write_action` | the presence left | E |
| 1046 | `apply_presence_action` | the lifecycle is not installed | T |
| 1183 | `complete_presence_relocation` | the event names no one | C |
| 1186 | `complete_presence_relocation` | no lifecycle state | C |
| 1193 | `complete_presence_relocation` | the completion does not match its route | E |
| 1198 | `complete_presence_relocation` | the person is not at the destination | E |
| 1261 | `snapshot_is_plain` | a snapshot is not a mapping | C |
| 1265 | `snapshot_is_plain` | a snapshot has other fields | C |
| 1268 | `snapshot_is_plain` | a snapshot's profile is not a mapping | C |
| 1272 | `snapshot_is_plain` | a snapshot's profile is other | C |
| 1276 | `snapshot_is_plain` | no activity | C |
| 1277 | `snapshot_is_plain` | no mobility | C |
| 1281 | `snapshot_is_plain` | a snapshot's enum is another value | C |
| 1287 | `snapshot_is_plain` | a snapshot's value is not a number | C |
| 1304 | `check_lifecycle` | the lifecycle state is not a mapping | C |
| 1328 | `check_lifecycle` | the lifecycle state holds a value that is not finite | C |
| 1335 | `check_lifecycle` | an archived person is not a mapping | C |
| 1338 | `check_lifecycle` | an archived person is invalid | C |
| 1341 | `check_lifecycle` | an archived person's fields are other | C |
| 1344 | `check_lifecycle` | an archived person's information is other | C |
| 1347 | `check_lifecycle` | an archived person's information is other | C |
| 1349 | `check_lifecycle` | no agent | C |
| 1355 | `check_lifecycle` | a pending return is not a mapping | C |
| 1363 | `check_lifecycle` | a pending return's fields are other | C |
| 1366 | `check_lifecycle` | a pending return is invalid | C |
| 1374 | `check_lifecycle` | a pending relocation is not a mapping | C |
| 1377 | `check_lifecycle` | a pending relocation is invalid | C |
| 1388 | `check_lifecycle` | a pending relocation's fields are other | C |
| 1391 | `check_lifecycle` | a pending relocation is invalid | C |
| 1401 | `check_lifecycle` | the queue is not in Rust | C |
| 1406 | `check_lifecycle` | a pending relocation's task disagrees | C |
| 1409 | `check_lifecycle` | a pending relocation's event disagrees | C |
| 1416 | `check_lifecycle` | an archived tie is not a mapping | C |
| 1426 | `check_lifecycle` | an archived tie is invalid | C |
| 1431 | `check_lifecycle` | a retained message is not a mapping | C |
| 1434 | `check_lifecycle` | a retained message is invalid | C |
| 1439 | `check_lifecycle` | deferred beliefs are not a mapping | C |
| 1443 | `check_lifecycle` | no presence | C |
| 1448 | `check_lifecycle` | a pending return disagrees with the presence | C |

## `rules/proposals.rs`（22）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 163 | `plan_toilet` | the toilet's independent queue at once | V |
| 168 | `plan_toilet` | the toilet's queue at once | V |
| 208 | `plan_resource` | the service's queue at once | V |
| 238 | `held_bed` | the overlay is not a plain dict | C |
| 244 | `held_bed` | the bed reservations are not a plain dict | C |
| 348 | `apply_seek` | no such agent | C |
| 373 | `apply_seek` | the namespace is None | C |
| 387 | `apply_seek` | the rows have no such agent | C |
| 441 | `query_pressure_is_high` | no information state | T |
| 444 | `query_pressure_is_high` | the information state is for the Python rule | T |
| 447 | `query_pressure_is_high` | no query pressure | T |
| 450 | `query_pressure_is_high` | the query pressure is for the Python rule | T |
| 453 | `query_pressure_is_high` | the person has no query pressure | T |
| 476 | `apply_effect` | an action unknown here | V |
| 487 | `apply_effect` | no such agent | C |
| 491 | `apply_effect` | the rows have no such agent | C |
| 512 | `apply_effect` | a due minute out of range | C |
| 530 | `apply_effect` | the event's identity is taken | E |
| 560 | `apply_effect` | the event's values are not what an event takes | C |
| 576 | `apply_effect` | no resource service | C |
| 580 | `apply_effect` | not a trip | C |
| 600 | `apply_effect` | the rows have no such agent | C |

## `rules/psychology.rs`（34）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 42 | `unsupported` | (a value) | — |
| 70 | `people` | the deltas are not a tuple | C |
| 76 | `people` | an id is not a str | C |
| 80 | `people` | an id is not UTF-8 | C |
| 94 | `people` | the world and the deltas have different people | C |
| 227 | `close_day` | completed days do not fit | C |
| 269 | `plan_long_term` | the baselines are not a plain dict | C |
| 321 | `write_long_term` | the row changed under the plan | C |
| 367 | `update_long_term` | the minute is too large | C |
| 369 | `update_long_term` | the minute is negative or too large | C |
| 420 | `model_of` | the policy has no plain class name | H |
| 443 | `float_item` | a field is missing | C |
| 458 | `readable_dict` | a table of the policy is not a plain dict | C |
| 526 | `exact_bool` | a flag is not a bool | T |
| 581 | `read_body_rates` | the body formula is not one this kernel knows | M |
| 585 | `read_body_rates` | the body formula has no plain id | M |
| 617 | `read_body_rates` | a sleep debt rate is out of bounds | C |
| 964 | `read_view_people` | an id is empty | C |
| 1104 | `update_psychology` | the minute is negative or too large | C |
| 1114 | `update_psychology` | the body config is not a plain dict | C |
| 1150 | `update_psychology` | the view and the world have different people | C |
| 1157 | `update_psychology` | the view and the world have different people | C |
| 1160 | `update_psychology` | a person's id differs from its key | C |
| 1173 | `update_psychology` | a value of the mean reversion is out of bounds | C |
| 1219 | `update_psychology` | a math function would raise | C |
| 1259 | `body_mirror` | body_state_v1 is not the tree of its row | C |
| 1275 | `apply_deltas` | the deltas are not a tuple | C |
| 1300 | `apply_deltas` | a delta is not an AgentStateDelta | H |
| 1305 | `apply_deltas` | a field is missing | C |
| 1309 | `apply_deltas` | an id is not a str | C |
| 1312 | `apply_deltas` | an id is not UTF-8 | C |
| 1336 | `apply_deltas` | a body value is not a float | C |
| 1380 | `apply_deltas` | a delta names nobody in the world | C |
| 1401 | `apply_deltas` | a delta names nobody in the world | C |

## `rules/queues.rs`（41）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 58 | `exact_bool` | not a bool | C |
| 66 | `plain_minute` | not a plain minute | C |
| 87 | `read` | no such queue | C |
| 113 | `fifo_of` | a queue ID without the queues | C |
| 158 | `need_of` | a need is not finite | C |
| 176 | `selected_queue_model` | the model selection refuses | M |
| 177 | `selected_queue_model` | not a selection | C |
| 188 | `policy_of` | another queue model | M |
| 201 | `policy_of` | the queue config is not a dict | C |
| 218 | `policy_of` | the queue limits are not a dict | C |
| 238 | `policy_of` | the threshold is not normalized | C |
| 304 | `stale_stall` | the service state is not a plain dict | C |
| 307 | `stale_stall` | no stall_state | C |
| 310 | `stale_stall` | the stall_state is not a dict | C |
| 319 | `stale_stall` | a stall record is not a dict | C |
| 322 | `stale_stall` | a stall record has no agent | C |
| 366 | `prepare_facility_queue` | no such facility | C |
| 374 | `prepare_facility_queue` | a queue task has no agent | C |
| 390 | `prepare_facility_queue` | a start out of range | C |
| 392 | `prepare_facility_queue` | a wait too long for a float | C |
| 395 | `prepare_facility_queue` | no such agent | C |
| 418 | `prepare_facility_queue` | the rows' facility has no object | C |
| 479 | `prepare_independent_facility_queue` | no such facility | C |
| 486 | `prepare_independent_facility_queue` | a start out of range | C |
| 491 | `prepare_independent_facility_queue` | no such agent | C |
| 544 | `plan_trip` | a due minute out of range | C |
| 549 | `plan_trip` | a journey has no plain duration | C |
| 552 | `plan_trip` | no such facility | C |
| 555 | `plan_trip` | a facility with an empty ID | C |
| 562 | `plan_trip` | a person of the queue has no request | C |
| 584 | `plan_trip` | a request of the walk is not queued | C |
| 589 | `plan_trip` | a route point is not a usable point | C |
| 630 | `plan_trip` | the event's values are not what an event takes | C |
| 634 | `plan_trip` | the event's identity is taken | E |
| 684 | `write_plan` | no such agent | C |
| 745 | `slot_route` | a position is not a usable point | C |
| 753 | `slot_route` | not a plain sum | C |
| 780 | `progress_physical_queue_positions` | no floor plan | V |
| 820 | `progress_physical_queue_positions` | a position is not a usable point | C |
| 879 | `head_to_service` | no floor plan | V |
| 884 | `head_to_service` | a queue ID without the queues | C |

## `rules/resources.rs`（5）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 48 | `added` | a quantity past 64 bits | C |
| 52 | `taken` | a quantity past 64 bits | C |
| 56 | `required_field` | the payload lacks a field | C |
| 180 | `apply_planned` | a minute before the Run | E |
| 329 | `apply_planned` | the resources are not a dict | C |

## `rules/service.rs`（76）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 125 | `text` | the config lacks a field | C |
| 132 | `integer` | the config lacks a field | C |
| 139 | `float` | the config lacks a field | C |
| 216 | `service_list` | no service state | C |
| 220 | `service_list` | the service state lacks a list | C |
| 229 | `pending_reserves` | the pending events are not a tuple | C |
| 238 | `pending_reserves` | a pending reservation has no ID | C |
| 255 | `water_is_blocked` | no water_outages | C |
| 259 | `water_is_blocked` | an outage lacks a field | C |
| 261 | `water_is_blocked` | active is not a bool | C |
| 314 | `of` | the source is for the Python rule to judge | — |
| 346 | `audit_row` | the event's identity is taken | E |
| 362 | `audit_row` | the event's values are not what an event takes | C |
| 391 | `set_agent` | no such agent | C |
| 434 | `facilities_to_serve` | a row has no object | C |
| 452 | `serve_head` | a facility with independent queues | C |
| 492 | `record_independent_stall` | the stalls are not a list | C |
| 498 | `record_independent_stall` | a stall is not a tuple | C |
| 504 | `record_independent_stall` | a stall record has another shape | C |
| 510 | `record_independent_stall` | no service state | C |
| 514 | `record_independent_stall` | no stall state | C |
| 547 | `serve_one` | an unknown resource | E |
| 552 | `serve_one` | another minute | C |
| 564 | `serve_one` | a queue with no signed point | C |
| 568 | `serve_one` | no such queue | C |
| 571 | `serve_one` | the queue is empty | E |
| 574 | `serve_one` | a queue lists an unknown request | C |
| 584 | `serve_one` | no queue state | C |
| 587 | `serve_one` | a queue's point is another facility's | C |
| 597 | `serve_one` | no such facility | C |
| 603 | `serve_one` | a queue's request lost its task | C |
| 606 | `serve_one` | a request is missing from the facility's list | C |
| 612 | `serve_one` | the queue is empty | E |
| 621 | `serve_one` | a person with two queue tasks | C |
| 630 | `serve_one` | a person with no queue task | C |
| 642 | `serve_one` | an ID is for the Python rule to judge | C |
| 649 | `serve_one` | a balance past 64 bits | C |
| 708 | `serve_one` | an ID is for the Python rule to judge | C |
| 715 | `serve_one` | a quantity that is not positive | E |
| 719 | `serve_one` | a due minute out of range | C |
| 767 | `serve_one` | the event's values are not what an event takes | C |
| 781 | `serve_one` | a row has no object | C |
| 786 | `serve_one` | a row has no object | C |
| 788 | `serve_one` | a start out of range | C |
| 790 | `serve_one` | a wait too long for a float | C |
| 848 | `serve_one` | no such staff | C |
| 892 | `complete_event` | the payload has other fields | C |
| 894 | `complete_event` | the payload lacks a field | C |
| 904 | `complete_event` | the event does not match | E |
| 907 | `complete_event` | an unknown resource | E |
| 911 | `complete_event` | an ID is for the Python rule to judge | C |
| 916 | `complete_event` | an ID is for the Python rule to judge | C |
| 943 | `complete_event` | no such facility | C |
| 946 | `complete_event` | no such staff | C |
| 949 | `complete_event` | no such agent | C |
| 960 | `complete_event` | a need is not finite | C |
| 996 | `complete_event` | no such queue | C |
| 1007 | `complete_event` | no active task | E |
| 1009 | `complete_event` | a wait out of range | C |
| 1019 | `complete_event` | a row has no object | C |
| 1023 | `complete_event` | a row has no object | C |
| 1028 | `complete_event` | a row has no object | C |
| 1057 | `complete_event` | no such agent | C |
| 1078 | `complete_event` | no such staff | C |
| 1173 | `request` | another minute | C |
| 1191 | `request` | a queue ID of a facility without independent queues | C |
| 1199 | `request` | no accessible independent queue | E |
| 1205 | `request` | no accessible independent queue | E |
| 1212 | `request` | no such queue | C |
| 1218 | `request` | no such agent or facility | C |
| 1221 | `request` | not a resource service facility | C |
| 1227 | `request` | the person has an exclusive task | E |
| 1232 | `request` | the person is at a facility | E |
| 1240 | `request` | a counter past 64 bits | C |
| 1249 | `request` | a row has no object | C |
| 1259 | `request` | the request's ID is in use | C |

## `rules/social_dialogue.rs`（5）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 131 | `id_set` | the participants are neither a tuple nor a list | C |
| 180 | `of` | a person's value is out of bounds | C |
| 199 | `read_capture` | the minute is outside the run | E |
| 215 | `read_capture` | the body records are not a plain dict | C |
| 337 | `affect_of` | a body record is another kind of dict | — |

## `rules/social_interaction.rs`（31）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 47 | `field` | the payload lacks a field | C |
| 65 | `current_household` | the presence state's people are not a dict | C |
| 71 | `current_household` | a presence record is not a plain dict | C |
| 133 | `apply_event` | another type of interaction | V |
| 141 | `apply_event` | the event is not endogenous | E |
| 146 | `apply_event` | the event is not due now | E |
| 157 | `apply_event` | the event's provenance is for the Python rule to judge | C |
| 168 | `apply_event` | an action of a person on themselves | E |
| 204 | `apply_event` | the presence state is another kind of dict | C |
| 219 | `apply_event` | an unknown person | E |
| 222 | `apply_event` | two people of one profile | C |
| 241 | `apply_event` | an unknown person | E |
| 303 | `text_field` | an empty ID | C |
| 320 | `untyped_event` | the request or person is for the Python rule to judge | C |
| 329 | `untyped_event` | the action is for the Python rule to judge | C |
| 351 | `threshold_of` | the threshold is invalid | C |
| 383 | `other_typed_event` | the provenance or time is for the Python rule to judge | C |
| 391 | `other_typed_event` | the event is for the Python rule to judge | C |
| 399 | `other_typed_event` | a subclass of str | T |
| 403 | `other_typed_event` | the context is for the Python rule to judge | C |
| 412 | `other_typed_event` | the payload has other fields | C |
| 428 | `other_typed_event` | the complaint is for the Python rule to judge | C |
| 440 | `other_typed_event` | the action is for the Python rule to judge | C |
| 446 | `other_typed_event` | the payload has other fields | C |
| 452 | `other_typed_event` | cooperation requires two known people | C |
| 478 | `other_typed_event` | the payload has other fields | C |
| 490 | `other_typed_event` | the reaction is for the Python rule to judge | C |
| 497 | `other_typed_event` | the claim is no longer current | E |
| 500 | `other_typed_event` | the claim is for the Python rule to judge | C |
| 502 | `other_typed_event` | the person has no record | C |
| 505 | `other_typed_event` | the reaction is for the Python rule to judge | C |

## `rules/social_network.rs`（12）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 45 | `mapping_get` | a payload cannot be read | E |
| 82 | `body_part` | a part of the body state is not a plain dict | C |
| 98 | `body_state_of` | the body state is not a plain dict | C |
| 127 | `action_relief` | a body record is not a plain dict | C |
| 154 | `body_trust` | a body relationship is not a plain dict | C |
| 157 | `body_trust` | a body relationship has no trust | C |
| 202 | `apply` | a count is too large | C |
| 212 | `apply` | a count is too large | C |
| 260 | `write_record` | the record is not in the row | C |
| 320 | `record_observed_contacts` | a pair the kernel leaves to Python | V |
| 403 | `observe_event` | a contact of a person with themselves | E |
| 440 | `observe_event` | an interaction of a person with themselves | E |

## `rules/social_propagation.rs`（18）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 44 | `unsupported` | (a value) | — |
| 93 | `checked` | a minute does not fit | C |
| 233 | `plan` | a new stress is outside 0..1 | C |
| 271 | `channel_stress` | weights are not finite | C |
| 288 | `channel_stress` | shifts are not finite | C |
| 378 | `unit_stress` | a stress is outside 0..1 | C |
| 387 | `neighbors_of` | the zone neighbors are not a dict | C |
| 402 | `neighbors_of` | not UTF-8 | C |
| 481 | `update_social_propagation` | an id is not a str | C |
| 484 | `update_social_propagation` | an id is not UTF-8 | C |
| 490 | `update_social_propagation` | the view and the deltas have different people | C |
| 523 | `update_social_propagation` | the view and the world have different people | C |
| 529 | `update_social_propagation` | the view and the world have different people | C |
| 532 | `update_social_propagation` | a person's key is not their id | C |
| 535 | `update_social_propagation` | a stress is outside 0..1 | C |
| 558 | `update_social_propagation` | the seed is not an int | C |
| 669 | `plan_of_values` | (a value) | — |
| 677 | `plan_of_values` | a policy this kernel does not know | M |

## `rules/staff.rs`（28）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 79 | `mirror_held` | the world holds another document than the row's | C |
| 191 | `kind_of` | a care record is not a plain dict | C |
| 209 | `priorities_of` | llm is another dict | C |
| 215 | `priorities_of` | priorities are another dict | C |
| 314 | `assign_work` | the minute is not the world's | C |
| 608 | `consider_due_complaints` | a request has no row | C |
| 690 | `assign_none` | a request has no row | C |
| 706 | `assign_none` | the request is not the world's | C |
| 736 | `assign_none` | the minute does not fit a float | C |
| 753 | `assign_none` | a request has no row | C |
| 899 | `open_waits` | a request has no row | C |
| 911 | `open_waits` | the wait does not fit an i64 | C |
| 955 | `wait_snapshot` | the requests are not a list | E |
| 986 | `config_item` | the config lacks a key | C |
| 988 | `config_item` | the config is not a map | C |
| 995 | `zone_override` | the config is not a map | C |
| 1001 | `zone_override` | a table of the config is not a map | C |
| 1006 | `zone_override` | an entry of the config is not a map | C |
| 1023 | `threshold` | a threshold is not a number | C |
| 1055 | `update_operations` | a zone has no record | C |
| 1058 | `update_operations` | the time since the update does not fit an i64 | C |
| 1062 | `update_operations` | the time since the update does not fit a float | C |
| 1077 | `update_operations` | a zone has no record | C |
| 1098 | `every_fatigue_is_finite` | a fatigue is not finite | C |
| 1146 | `assign_operations` | a staff member has no record | C |
| 1210 | `assign_operations` | an uncertainty is not finite | C |
| 1228 | `assign_operations` | a stress is not finite | C |
| 1256 | `relief` | the minute does not fit an i64 | C |

## `rules/staff_dynamics.rs`（5）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 78 | `is_available` | available is not a bool | C |
| 288 | `update` | the row changed under the plan | C |
| 357 | `is_available_now` | the id is not a str | C |
| 621 | `validate_override` | the known IDs are not a set | C |
| 663 | `fatigue_value` | the formula's inputs are out of range | C |

## `rules/staff_operation_events.rs`（14）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 215 | `arrive` | another event | C |
| 224 | `arrive` | no staff operations | C |
| 240 | `arrive` | the arrival is for the Python rule to judge | C |
| 302 | `relief` | the relief's provenance or payload is for the Python rule to judge | C |
| 311 | `relief` | no staff operations | C |
| 319 | `relief` | the relief is stale | E |
| 321 | `relief` | an idle record | C |
| 336 | `relief` | the relief is stale | E |
| 340 | `relief` | an unknown kind of task | C |
| 348 | `relief` | the replacement lacks the skill | E |
| 372 | `relief` | the relieved operation lacks a pending completion | C |
| 599 | `complete` | another event | C |
| 624 | `complete` | no staff operations | C |
| 640 | `complete` | the completion is for the Python rule to judge | C |

## `rules/support.rs`（42）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 85 | `timeout_event` | a timeout without its IDs | C |
| 91 | `timeout_event` | no such request | E |
| 102 | `timeout_event` | a minute out of range | C |
| 112 | `timeout_event` | no such request or person | C |
| 121 | `timeout_event` | no such agent | C |
| 168 | `prior_of` | the namespace is None | C |
| 180 | `record_interrupted` | a timeout without its request | E |
| 214 | `record_started` | a staff trip without its request | C |
| 226 | `record_started` | the request's person is gone | C |
| 248 | `config_float` | the config is not a document | C |
| 253 | `config_float` | the config's table is not a document | C |
| 258 | `config_float` | a relief that is not a float | C |
| 319 | `complete_support_event` | a completion without its IDs | C |
| 322 | `complete_support_event` | a completion without its IDs | C |
| 336 | `complete_support_event` | no such request, person or staff | C |
| 356 | `complete_support_event` | a minute out of range | C |
| 395 | `complete_support_event` | the person has no body record | C |
| 429 | `complete_support_event` | a value is not finite | C |
| 459 | `complete_support_event` | a row has no object | C |
| 468 | `complete_support_event` | no such staff | C |
| 482 | `complete_support_event` | no such agent | C |
| 560 | `selected_care_formula` | the model selection refuses | M |
| 561 | `selected_care_formula` | not a selection | C |
| 569 | `selected_care_formula` | the formula refuses | C |
| 680 | `body_field` | another field of the body | C |
| 689 | `set_body_field` | another field of the body | C |
| 742 | `formula_care` | a dependent without a body record | C |
| 748 | `formula_care` | no such person | E |
| 767 | `formula_care` | a dependent is measured without the person's body | C |
| 774 | `formula_care` | a dependent without a body record | C |
| 856 | `formula_care` | a value is not finite | C |
| 862 | `formula_care` | more changes than a receipt holds | C |
| 928 | `plan_request` | no such agent | C |
| 940 | `plan_request` | a minute out of range | C |
| 951 | `plan_request` | the event's identity is taken | E |
| 976 | `plan_request` | the event's values are not what an event takes | C |
| 1004 | `write_request` | no such agent | C |
| 1022 | `support_kind_of` | another kind of dict | C |
| 1039 | `support_kind_of` | a subclass of str | T |
| 1065 | `household_of` | the presence state's people are for the Python rule | C |
| 1071 | `household_of` | a presence record is for the Python rule | C |
| 1078 | `household_of` | another kind of dict | C |

## `rules/tick.rs`（3）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 66 | `refuse` | (a value) | — |
| 432 | `accept` | another class of controller | H |
| 475 | `run_tick` | experiments/phases/driver.py has not installed the table | H |

## `rules/tick_validation.rs`（61）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 178 | `field` | a payload is not a mapping | C |
| 258 | `entity_rules_watching` | not a WorldState v6 | O |
| 261 | `entity_rules_watching` | no resource service config | C |
| 266 | `entity_rules_watching` | the clock or seed is the Python rule's | C |
| 287 | `entity_rules_watching` | a zone key is not its ID | C |
| 335 | `entity_rules_watching` | an agent key is not its ID | C |
| 400 | `entity_rules_watching` | a facility kind is not ASCII | C |
| 420 | `entity_rules_watching` | a facility is over capacity | C |
| 430 | `entity_rules_watching` | a resource facility uses the legacy queues | O |
| 433 | `entity_rules_watching` | another facility holds resource slots | C |
| 444 | `entity_rules_watching` | a facility queue is the Python rule's | C |
| 449 | `entity_rules_watching` | a facility service is the Python rule's | C |
| 468 | `entity_rules_watching` | a resource service is the Python rule's | C |
| 483 | `entity_rules_watching` | the event queue repeats an identity | C |
| 486 | `entity_rules_watching` | a pending event is not a plain ScheduledEvent | C |
| 514 | `entity_rules_watching` | a resource completion lacks a task ID | C |
| 529 | `entity_rules_watching` | a task key is not its ID | C |
| 565 | `entity_rules_watching` | a resource service task is the Python rule's | C |
| 573 | `entity_rules_watching` | a toilet task is the Python rule's | C |
| 580 | `entity_rules_watching` | a toilet service task is the Python rule's | C |
| 611 | `entity_rules_watching` | an idle staff member keeps work | C |
| 643 | `entity_rules_watching` | someone in a resource queue has no task | C |
| 648 | `entity_rules_watching` | someone at a resource has no task | C |
| 658 | `entity_rules_watching` | a toilet queue task has no entry | C |
| 666 | `entity_rules_watching` | a toilet service task has no slot | C |
| 669 | `entity_rules_watching` | a toilet service task has no completion | C |
| 678 | `entity_rules_watching` | a toilet service task's completion is the Python rule's | C |
| 685 | `entity_rules_watching` | a resource queue task has no entry | C |
| 699 | `entity_rules_watching` | a resource service task's facility is unknown | C |
| 702 | `entity_rules_watching` | a resource service task has no slot | C |
| 709 | `entity_rules_watching` | a resource service task has no completion | C |
| 720 | `entity_rules_watching` | a resource service task's completion is the Python rule's | C |
| 729 | `entity_rules_watching` | a resource service task has no reservation | C |
| 733 | `entity_rules_watching` | a resource service task's staff is unknown | C |
| 742 | `entity_rules_watching` | a resource service task's staff is the Python rule's | C |
| 749 | `entity_rules_watching` | no resource service state | C |
| 753 | `entity_rules_watching` | the resource service state lacks a field | C |
| 758 | `entity_rules_watching` | a resource completion names an inactive task | C |
| 762 | `entity_rules_watching` | the resource service state lacks a field | C |
| 778 | `entity_rules_watching` | a travel task has no completion | C |
| 781 | `entity_rules_watching` | a travel task's completion is late | C |
| 785 | `entity_rules_watching` | a travel event's destination is not a str | C |
| 788 | `entity_rules_watching` | a travel event's destination is unknown | C |
| 802 | `entity_rules_watching` | a staff support trip is the Python rule's | C |
| 824 | `entity_rules_watching` | a request's ID is the Python rule's | C |
| 828 | `entity_rules_watching` | a request's ID is the Python rule's | C |
| 860 | `entity_rules_watching` | an assigned request names no staff or task | C |
| 874 | `entity_rules_watching` | an assigned request's agent is unknown | C |
| 889 | `entity_rules_watching` | an assigned request's travel is the Python rule's | C |
| 895 | `entity_rules_watching` | an assigned request needs one completion | C |
| 902 | `entity_rules_watching` | an assigned request's completion is the Python rule's | C |
| 923 | `entity_rules_watching` | someone has duplicate open requests | C |
| 940 | `entity_rules_watching` | a task's agent is unknown | C |
| 943 | `entity_rules_watching` | a task's facility is unknown | C |
| 955 | `entity_rules_watching` | a staff support task is inconsistent | C |
| 959 | `entity_rules_watching` | a task's timing is the Python rule's | C |
| 981 | `entity_rules_watching` | a pending event is the Python rule's | C |
| 1034 | `route_capacities_pass` | the route config lacks its capacities | C |
| 1041 | `route_capacities_pass` | the route config is the Python rule's | C |
| 1045 | `route_capacities_pass` | a route capacity is the Python rule's | C |
| 1090 | `population_dynamics` | the population state lacks its traits | C |

## `rules/toilet.rs`（53）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 45 | `is_toilet` | a facility kind that casefold may change | C |
| 87 | `queue_up` | a queue ID of a facility without independent queues | C |
| 95 | `queue_up` | no accessible independent queue | E |
| 101 | `queue_up` | no accessible independent queue | E |
| 108 | `queue_up` | no such queue | C |
| 116 | `queue_up` | no such agent or facility | C |
| 119 | `queue_up` | not a toilet | C |
| 130 | `queue_up` | the queue task's ID is taken | C |
| 136 | `queue_up` | a row has no object | C |
| 145 | `queue_up` | the request's ID is in use | C |
| 200 | `request` | another minute | C |
| 232 | `facilities_to_serve` | a row has no object | C |
| 249 | `has_room` | a facility with independent queues | C |
| 253 | `has_room` | no such facility | C |
| 274 | `start_head` | a facility with independent queues | C |
| 313 | `start_one` | another minute | C |
| 331 | `start_one` | a queue with no signed point | C |
| 335 | `start_one` | no such queue | C |
| 338 | `start_one` | the queue is empty | E |
| 341 | `start_one` | a queue lists an unknown request | C |
| 352 | `start_one` | no queue state | C |
| 355 | `start_one` | a queue's point is another facility's | C |
| 364 | `start_one` | no such facility | C |
| 379 | `start_one` | no such facility | C |
| 383 | `start_one` | a queue's request lost its task | C |
| 386 | `start_one` | a request is missing from the facility's list | C |
| 391 | `start_one` | the queue is empty | E |
| 398 | `start_one` | a person with two queue tasks | C |
| 404 | `start_one` | a person with no queue task | C |
| 408 | `start_one` | a start out of range | C |
| 410 | `start_one` | a wait for the Python rule to judge | C |
| 423 | `start_one` | a due minute out of range | C |
| 437 | `start_one` | no such facility | C |
| 471 | `start_one` | the service's identity is taken | E |
| 474 | `start_one` | the service's sequence is taken | C |
| 526 | `start_one` | the event's values are not what an event takes | C |
| 531 | `start_one` | a row has no object | C |
| 544 | `start_one` | a row has no object | C |
| 569 | `start_one` | no such staff | C |
| 601 | `complete_event` | the completion lacks an ID | C |
| 604 | `complete_event` | the completion lacks an ID | C |
| 617 | `complete_event` | the task does not match | E |
| 622 | `complete_event` | the staff reservation is inconsistent | C |
| 629 | `complete_event` | the staff reservation is inconsistent | C |
| 633 | `complete_event` | no such facility or agent | C |
| 651 | `complete_event` | no such queue | C |
| 662 | `complete_event` | a wait out of range | C |
| 673 | `complete_event` | a row has no object | C |
| 681 | `complete_event` | the body state is None | C |
| 688 | `complete_event` | a need is not finite | C |
| 699 | `complete_event` | a row has no object | C |
| 713 | `complete_event` | no such staff | C |
| 727 | `complete_event` | no such agent | C |

## `rules/travel.rs`（25）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 73 | `read_graph` | the edges are not a tuple | C |
| 75 | `read_graph` | closed is not a bool | C |
| 120 | `closed_to` | the closed edges are poisoned | C |
| 137 | `shortest` | an unknown zone | E |
| 164 | `shortest` | a route's minutes past 64 bits | C |
| 204 | `route_for` | no such agent | C |
| 332 | `plan_travel` | no such agent | C |
| 343 | `plan_travel` | a journey has no plain duration | C |
| 346 | `plan_travel` | a due minute out of range | C |
| 353 | `plan_travel` | no such zone | E |
| 384 | `plan_travel` | the trip's identity is taken | E |
| 391 | `plan_travel` | a facility's trip with no facility | C |
| 421 | `plan_travel` | a support trip without its IDs | C |
| 450 | `plan_travel` | the event's values are not what an event takes | C |
| 497 | `plan_room_approach` | a journey has no plain duration | C |
| 500 | `plan_room_approach` | a due minute out of range | C |
| 512 | `plan_room_approach` | the approach's identity is taken | E |
| 544 | `plan_room_approach` | the event's values are not what an event takes | C |
| 710 | `queue_has_capacity` | the queue config has no schema | C |
| 713 | `queue_has_capacity` | another queue config | M |
| 726 | `queue_has_capacity` | a negative queue capacity | C |
| 767 | `select` | no such agent | C |
| 775 | `select` | a facility kind that casefold may change | C |
| 838 | `approach_route` | a floor plan without the movement state | T |
| 846 | `approach_route` | no such facility | C |

## `rules/travel_completion.rs`（19）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 55 | `required` | the walk lacks an ID | C |
| 67 | `read` | a route of other values | C |
| 156 | `check_queue_walk` | no queue state | C |
| 165 | `check_queue_walk` | an independent queue walk without its request | C |
| 170 | `check_queue_walk` | the queue no longer holds the walk | E |
| 175 | `check_queue_walk` | a queue without its list | C |
| 177 | `check_queue_walk` | an independent queue walk without its facility | C |
| 248 | `check_queue_walk` | the queue walk does not fit, which the Python rule says | V |
| 297 | `arrive` | the world's whole validation is Python's | H |
| 324 | `reject_room` | no such person | E |
| 383 | `check_sleep_bed` | the walk has no live reservation | C |
| 392 | `check_sleep_bed` | the walk has no journey | C |
| 398 | `check_sleep_bed` | the reservation has no bed | E |
| 408 | `check_sleep_bed` | the walk does not match its reservation | E |
| 433 | `check_relocation` | the relocation does not match its route | E |
| 482 | `apply_event` | the walk does not match its task | E |
| 495 | `apply_event` | the destination is not a zone | E |
| 500 | `apply_event` | a resource walk without its origin | V |
| 508 | `apply_event` | the route does not match its target | E |

## `rules/world_view.rs`（34）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 41 | `unsupported` | (a value) | — |
| 266 | `exact_i64` | an int is too large | C |
| 273 | `exact_text` | not UTF-8 | C |
| 296 | `text_tuple` | a view value is not a tuple | C |
| 321 | `field` | a record's field is missing | C |
| 334 | `presence_people_of` | the presence state's people are not a dict | C |
| 432 | `read_person` | a person's fields are not a dict | C |
| 468 | `person_contexts` | the cache of persons is not a dict | C |
| 492 | `person_contexts` | a people dict is another kind of dict | C |
| 514 | `person_contexts` | no entry for a person | C |
| 538 | `read_zone_conditions` | the demand modifiers are not a tuple | C |
| 590 | `active_stressors` | the infrastructure state has no stressors | C |
| 631 | `read_zones` | the zones are not a plain dict | C |
| 642 | `read_zones` | the cache of zones is not a dict | C |
| 693 | `read_zones` | a sum of stress rates overflows | C |
| 754 | `read_rows` | a minute is out of range | C |
| 762 | `read_rows` | a person is under another ID | C |
| 770 | `read_rows` | a minute is out of range | C |
| 814 | `read_rows` | a facility is under another ID | C |
| 872 | `read_information` | the claims or beliefs do not parse | C |
| 876 | `read_information` | a person has no query pressure | C |
| 971 | `read_facility_contexts` | the operational state is None | C |
| 985 | `read_facility_contexts` | the infrastructure state has no outages | C |
| 1037 | `cache_of` | the controller keeps no view cache | H |
| 1054 | `build` | the configs are not a plain dict | C |
| 1065 | `build` | the information state is not a dict | C |
| 1130 | `refresh` | a person of the prior view has left | C |
| 1201 | `hashed_config` | a config is missing | C |
| 1203 | `hashed_config` | a config is None | C |
| 1638 | `python_fields` | a copy of a dict is not a dict | C |
| 1704 | `from_python_view` | an id is not a str | C |
| 1707 | `from_python_view` | an id is not UTF-8 | C |
| 1709 | `from_python_view` | the view is not in ID order | C |
| 1713 | `from_python_view` | a person's fields are not a dict | C |

## `state/entities.rs`（2）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 1353 | `rows_json` | the entity store is tainted | C |
| 1518 | `write_field` | the world's map has no such ID | C |

## `state/events.rs`（14）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 479 | `text` | a payload holds a subclass of str | C |
| 481 | `text` | a str is not UTF-8 | C |
| 492 | `int` | an int is too large | C |
| 502 | `int` | an int is too large | C |
| 527 | `is_completion` | a kind is not a str | C |
| 584 | `event_id` | an ID is not a str | C |
| 594 | `event_type` | a type is not a str | C |
| 604 | `due_minute` | a minute is too large | C |
| 1157 | `of` | the event log is not in Rust | C |
| 1171 | `of` | the duration is not an int | C |
| 1342 | `schedule_row` | the event has no row | C |
| 1357 | `record_row` | the event has no row | C |
| 1390 | `settle_event` | the event has no row | C |
| 1423 | `saved_rows` | a record has no bytes | C |

## `state/facility_queues.rs`（1）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 76 | `held_mirror` | the world holds another queue state | C |

## `state/resource_ledger.rs`（2）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 386 | `held_mirror` | model_state is another kind of mapping | C |
| 403 | `world_inputs` | the world's maps are another kind of mapping | C |

## `state/resource_service.rs`（4）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 75 | `held_mirror` | model_state is another kind of mapping | C |
| 79 | `held_mirror` | the world holds another service state | C |
| 210 | `validate` | model_state is another kind of mapping | C |
| 235 | `validate` | the minute is not an int | C |

## `state/sample_namespace.rs`（1）

| 行 | fn | 理由 | 記号 |
|--:|---|---|---|
| 49 | `write_level` | no such person | E |
