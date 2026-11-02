# M6 レーン：`events/handlers.py` の残り

`events/handlers.py` のすべてのハンドラーと `domain/care_receipts.py` の残りに Rust の経路を作りました。**結果は変わりません。新しい係数はありません。** Python の規則は比較の基準として残ります。このレーンの目的は速さではなく、手順4で Python の規則を消せるようにすることです。

7日分で Python の dispatcher に渡るイベントは **8件から0件**になりました。ただし**7日分には、移した規則のほとんどが現れません。** 下の「比較の基準」のとおり、多くはゲートとテストでしか比べていません。

## 移したもの

| もの | Rust | Python |
|---|---|---|
| 近接の接触 | `rules/handlers.rs` の `proximity` | `ProximityContactHandler` |
| うわさ、訂正、情報の接触 | `rules/information_events.rs` | `InformationRumorHandler`、`InformationCorrectionHandler`、`InformationContactHandler` |
| `complaint_response`、`cooperation`、`information_reaction`、型のない社会イベント | `rules/social_interaction.rs` | `SocialInteractionHandler` |
| 資源の台帳（`resource_delivery` を含む） | `rules/handlers.rs` の `resource_ledger` | `ResourceLedgerHandler` |
| 設備の故障と復旧 | `facility_outage`、`facility_recovery` | 同名の handler |
| 配送の遅れ、断水と復旧 | `delivery_delay`、`water_outage`、`water_restoration` | 同名の handler |
| 職員の不在と復帰、支援の中断 | `staff_unavailable`、`staff_restoration`、`interrupt_support_task` | 同名の handler、`_interrupt_support_task` |
| インフラのイベント6組、依存の計算 | `infrastructure`、`dependents_of`、`capacities_of` | `InfrastructureEventHandler`、`dependent_facilities`、`effective_facility_capacities` |
| 資源のサービスの取り消しと中断 | `cancel`、`service_cancellation`、`interrupt_for_staff` | `ResourceServiceManager.cancel`、`interrupt_for_staff`、`ResourceServiceCancellationHandler` |
| 人の出入りのプリミティブの確認と写し | `population` | `EffectProgramPopulationHandler` |
| 職員の作業の到着、交代、完了、中断 | `rules/staff_operation_events.rs` | `StaffOperation*Handler`、`arrive_`、`handoff_`、`complete_`、`interrupt_staff_operation`、`_staff_operation_measurements` |
| 受領記録の残り | `rules/care_receipts.rs` | `make_facility_recovery_record`、`make_staff_handoff_record`、`make_staff_operation_record`、`make_staff_support_continuity_record`、`ingest_applied_shared_operation_records` |
| 選んだ式が決めるケア | `rules/support.rs` の `formula_care` | `StaffSupportCompletionHandler`、`CareOutcomeFormula`、`explicit_care_dependents` |
| 依頼の割り当て、割り込み、受領記録 | `rules/staff.rs` の `assign_work`、`preempt_worker`、`rules/support.rs` の `assign_staff_support` | `assign_pending`、`_preempt_background_staff`、`_assign_staff_support` |
| 苦情の期限 | `rules/staff.rs` の `consider_due_complaints` | `_consider_due_complaints` |
| 歩きの残り | `rules/travel_completion.rs`、`rules/dispatch.rs` | `sleep_bed`、`presence_relocation`、`_reject_room_approach`、`_finish_stopped_journey` の `sleep_bed` |
| ループの表 | `rules/dispatch.rs` の `other_event` | `dispatch_one` の手順 |

## 決めたこと

- **Python の入れ物を通します。** レーン M6 effects と同じです。これらのイベントはまれなので、規則は Python の手順の順に Python の木とオブジェクトを読み書きし、`model_state` の書き込みは1項目ずつ転送します。名前空間全体の書き込みはありません。
- **成功以外は Python の規則が言います。** 抑止と失敗は、世界を変えずに `Unsupported` を返し、Python の規則が最初から動いて同じ文を出します。
- **最初の変更より後の例外は通します。** Python の規則と同じ操作なので、同じ例外です。`cancel` の解放だけは `ValueError` を Python と同じ文の `EventApplicationError` にします。受領記録の契約に合わない値は、Python の `make_*_record` を名前で呼んで、その文で失敗します。
- **写して差し替える流れは変えていません。** `_apply_operational_transaction` と同じ順に、`from_dict(to_dict())`、検証、`__dict__` の差し替えを呼びます。
- **ループ：** 印のないイベントは `other_event` が `handler.apply`（kernel か Python の規則）を呼びます。EffectProgram の印があるイベント、差し替えられた handler、列で置き換わったイベントは `dispatch_one` です。
- **`assign_pending`：** 誰も始めない tick は今までの `assign_none` です。始める tick は `assign_work` で、経路と旅は渡された callback を Python と同じ順で呼びます。
- **`effects.rs`：** `make_staff_handoff_record` と `make_facility_recovery_record` の呼び出しを Rust の関数に替えました。

## 比較の基準（はっきり書きます）

**7日分で比べられるのは `assign_work` と `consider_due_complaints` だけです。それも3日分です（下）。** ほかはゲートとテストだけで、次の規則はゲートにもなく、**テストの世界でしか比べていません：** `staff_operation_relief`（2回）、`supply_route_interruption` と `supply_route_restored`（各2回）、`resource_service_cancel` のイベント（1回）、`staff_shift_change`（1回）、`resource_reserve`、`resource_release`、`resource_consume`。

### 影（`benchmarks/rule_shadow/shadow_handlers_rest.py`）

Python の規則と kernel を別の写しで動かし、結果と正準の文書を JSON の文字列で比べ、kernel の写しの行と木の一致も見ます。

| 回 | 比べた値（このレーンの影） | 違い | Python が適用したのに断った |
|---|---:|---:|---:|
| ゲート（`dispatch,handlers,staff,handlers_rest`、`df84748`） | 534,165,744 | 0 | 0 |
| テスト約700件の下（`handlers_rest` だけ、`df84748`） | 9,812,951 | 0 | 0 |
| 3日分（`--duration 4320`、`handlers_rest` だけ、`2f546a1`） | 800,967,894 | 0 | 0 |

**7日分の影は終わっていません。** 広い組（`dispatch,handlers,staff,handlers_rest`）は約5時間で6960分までで、調整役が止めました。`handlers_rest` だけも `timeout 3600` で4440分までで切れ、強制終了で記録が残りませんでした。そこで3日分を動かしました。

**3日分で実際に比べたのは次だけです：** `assign_work` 315回（うち割り当て309件）、`consider_due_complaints` 4,320回（うち policy に尋ねた1,185件）。ハンドラーの kernel は1回も動いていません。`resource_delivery` もこの3日分にはありません。3日分のハッシュ（world `9125fe335537`）は、比べる基準がありません。**7日分の `resource_delivery` 8件は、kernel が動いた回数とハッシュだけで、影では比べていません。** ゲートでは54回比べました。

**ゲートの呼び出し（呼び出し / kernel が動いた）：**

| 規則 | 回数 | | 規則 | 回数 |
|---|---:|---|---|---:|
| `information_contact_update` | 5,339 / 5,032 | | `staff_unavailable` | 89 / 88 |
| `proximity_contact_update` | 918 / 916 | | `staff_restored` | 90 / 90 |
| `information_rumor` | 156 / 156 | | `staff_operation_arrive` | 15 / 15 |
| `information_correction` | 104 / 104 | | `staff_operation_complete` | 14 / 14 |
| `complaint_response` | 32 / 32 | | `resource_delivery`、`resource_loss` | 53 / 53、1 / 1 |
| `cooperation` | 2 / 2 | | `facility_outage`、`facility_recovery` | 9 / 9、25 / 24 |
| `information_reaction` | 22 / 22 | | `water_outage`、`water_restored` | 4 / 4、25 / 25 |
| 型のない社会イベント | 26 / 26 | | `delivery_delayed` | 3 / 3 |
| `sleep_bed`、`presence_relocation` | 12 / 12、1 / 1 | | `power_outage`、`power_restored` | 2 / 2、2 / 2 |
| `assign_work` | 3,139 / 3,139 | | `equipment_failure`、`equipment_repaired` | 1 / 1、1 / 1 |
| `ingest` | 1,988 / 1,988 | | `extreme_heat`、`extreme_cold`、`environmental_stressor_end` | 1 / 1、1 / 1、2 / 2 |
| `_reject_room_approach` | 7 / 7 | | `effect_program_population` | 92 / 38 |
| `population_approach_complete` | 8 / 8 | | | |

各インフラのイベントは数回しかありません。

**影が直接の経路を見ないところ：** 影の回は Python の入口を通ります。`dispatch` の影なしで `handlers_rest` だけを選ぶと、ループは社会イベントと歩きに直接の Rust の関数を呼ぶので、`social.*` と `walk.*` は比べません。3日分がこの形です。ゲートの回は `dispatch` の影つきなので比べています。

### kernel が動いた回数（直接呼ぶ素の回、`SHELTERSIM_DISPATCH_COUNT_ONLY=1`）

| | ゲート | 7日分 |
|---|---:|---:|
| イベント Rust / Python | 79,025 / 1,486 | 91,262 / **0** |
| 表の残りの適用 / 抑止 / 失敗 | 6,512 / 308 / 2 | 8 / 0 / 0 |
| `arrive` 動いた / 断り | 9,589 / 134 | 11,537 / 0 |
| `staff_support_complete` 動いた / 断り | 2,412 / 13 | 711 / 0 |
| `_finish_stopped_journey` 直接 / Python | 9,985 / 0 | 9,578 / 0 |

**7日分の断りは0件です。** ゲートの Python の 1,486件は、列で置き換わったイベント 1,164件と、EffectProgram の印のあるイベントです（内訳は数えていません）。

kernel の断りの理由（ゲート）：情報の接触 `no longer in a contact zone` 304、`the sender's belief changed` 2、`the provenance does not match the sender` 1（どれも Python の抑止か失敗）。人の出入りの `EventSuppressed` 54。近接の古い標本 2。設備の復旧の超過 1。職員の不在の `StateInvariantError` 1（`known_stops`。Python も同じ例外です）。ほかは独立した FIFO と `verified` でない直近の記録で、M6 handlers のレーンと同じです。

### 感度（コピーを1か所変更）

| 変更 | ゲートのハッシュ | 影 | テスト |
|---|---:|---:|---|
| 巡回の信頼 `relief * 0.1` を 1% | 4件 | 4件 | 2件失敗 |
| 割り当ての期限 +1 | 2件 | 20件 | 3件失敗 |
| インフラの完了の優先度 10→11 | 1件 | 5件 | 1件失敗 |
| `hygiene-support-proportional` の式を 1% | **一致** | **0件** | 1件失敗 |
| 苦情の応答の差 0.02 を 1% | **一致** | **0件** | 1件失敗 |
| 協力の信頼 0.01 を 1% | **一致** | **0件** | 1件失敗 |
| 訂正の上限 0.95 を 1% | **一致** | **0件** | 1件失敗（4,000回の抽選） |
| うわさの信頼の重み 2.0 を 1% | **一致** | **0件** | 1件失敗（4,000回の抽選） |
| ケアの既定値 0.03 を 1% | **一致** | **0件** | 1件失敗（設定を外した世界） |

ハッシュと影の列は、`--only` で選んだシナリオだけの値です。**下の6つは、選んだシナリオのハッシュと影に届かず、テストが見ます。** 苦情、協力、ケアの式は `model__social`、`generated_project`、`model__care_outcome` だけで試しました。ゲート全体では試していません。抽選の 1% は、確率がその小さな幅に当たるときしか結果を変えないので、ゲートには見えません。

### 行の網羅（ゲート、kernel ありとなしの合計 / kernel ありだけ、`df84748`）

`events/handlers.py` 1124/1440（78%）/ 443（31%）、`domain/care_receipts.py` 142/166（86%）/ 94（57%）、`domain/staff.py` 303/353（86%）/ 200（57%）、`domain/staff_operations.py` 392/558（70%）/ 279（50%）、`experiments/phases/arbitrate.py` 38/38 / 22、`experiments/phases/dispatch.py` 138/143（97%）/ 73（51%）。Rust の行の網羅は測っていません。

## 結果

2026-11-02、WSL2、16コア、ほかのレーンと同時。`ee92b96` をマージした上です。

| 確認 | コード | 結果 |
|---|---|---|
| 全テスト（Rust あり） | `b6e4259` | 4,117件成功、6件 skip |
| 全テスト（`SHELTERSIM_NATIVE=0`） | `b6e4259` | 2,073件成功、2,050件 skip |
| `gate_all_paths.py --expect` | `b6e4259` | 55シナリオ、問題0件 |
| `--expect`、影つき | `df84748` | 55シナリオ、問題0件、違い0件 |
| `--reversed-order`（影なし） | `df84748` | 55シナリオ、問題0件 |
| 7日分（`--expect`） | `b6e4259` | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041` 一致 |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | `b6e4259` | 問題なし |
| `tracked_mirror_gate.py` | | **動かしていません。** `core/state_mirror.py` は変えていません |

`df84748` のあとの Rust の変更は、テスト用の関数2つ（`claim_strength_of`、`acceptance_of`）だけです。

`tick_budget.py`（9600分から480 tick、3回、基準 `ee92b96`、レーン `df84748`）：**1.057**（1.057、1.066、1.002）。A/A 1.032（0.961、1.032、1.032）。状態は3つとも一致。load average 約 10。上限の中です。**わずかに遅い可能性がありますが、原因は調べていません。** 毎 tick の呼び出しが増えたのは `assign_pending` の入口と `_consider_due_complaints`、`_assign_staff_support` です。部品ごとの費用は測っていません（速さのレーンではありません）。

## Python に残した規則（手順4で Rust が要るもの）

- **文：** すべてのハンドラーの抑止と失敗の文。今は `Unsupported` のあと Python の規則が出します。
- **`EventDispatcher.dispatch_one`：** EffectProgram の印のあるイベント（ガードが `effect_event_guard_or_suppress` です）、差し替えられた handler、列で置き換わったイベント。
- **契約と設定（名前で呼びます）：** `InformationSpreadConfig.from_mapping`、`canonical_proximity_contact_config`、`ScenarioInfrastructureSpec.model_validate`、`selected_models`、`create_care_outcome_formula`、`create_staff_operations_formula`、`_operation_record_adapter`、`StaffSupportContinuityReceipt`。
- **ケアでない受領記録の `record_shared_operation`：** `ingest` と写しの流れから名前で呼びます。
- **ほかのモジュールの規則（名前で呼びます）：** `staff_position_snapshots`、`finish_movement`、`apply_resource_operation`、`admit_pending_arrivals`、`queue_arrivals`、`queue_departures`、`admit_pending_returns`、`apply_population_presence_command`、`apply_arrival_approach_completion`、`complete_presence_relocation`、`staff_dynamics_is_available`、`staff_dynamics_on_shift`、`facility_queues` の `enqueue`、`finish_service`、`request_for_task`、`record_queue_transition`。多くはそれぞれのレーンの kernel が先に動きます。
- **`ResourceServiceManager` の小さな部品（名前で呼びます）：** `_audit_event`、`_provenance`、`_next_queue_task_id`、`_ensure_event_identity_free`、`_cancelled_task_ids`、`_service_reservation_ids`、`_remove_service_reservation`、`_clear_queue_stall`、`_warehouse_id`、`_record_internal_event`、`_release_staff`、`_increment_metric`。
- **`assign_work` の callback：** `route_resolver`、`travel_scheduler`。入口は Python のメソッドで、中は `_route`、`_schedule_travel` の kernel です。
- **写して差し替える部品：** `to_dict`、`from_dict`、`validate`、`validate_simulator_extensions`（`core.fork()` まで）。
- **tick の外：** `care_history_for_agent`、`care_history_by_agent`。
- **LLM の境界：** `consider_complaint_response`、`consider_*`、`_prioritize_support_request`、`record_support_request`。Python のままの予定です。
- **このレーンで触っていないもの：** `interrupt_staff_trips_on_closed_routes`、`ToiletServiceManager._arbitrate_independent_facility`、`arbitrate` の Python のループ、独立した FIFO の `request` の枝（`facility_queues` はレーン M6 overlay の範囲です）。

## モデルの担当の方に見ていただくコード

**新しい係数はありません。** 判断の規則が Python と Rust の2か所になりました。

| Python | Rust | 中身 |
|---|---|---|
| `_apply_typed_interaction`、型のないイベント | `rules/social_interaction.rs` | 苦情 0.02、0.01、0.01、0.005。協力 0.02、0.01。安心 0.03、0.01、0.005。理由の表 |
| `InformationCorrectionHandler`、`InformationContactHandler` | `rules/information_events.rs` の `acceptance_probability` | `min(0.95, max(0.0, p + 2.0 * a * (trust - 0.5) + 2.0 * b * (reliability - 0.5)))`、抽選のキー |
| `_sample_claim_belief_strength` | `sample_strength` | `min(1.0, max(0.0, ...))`、抽選のキー、0.5、1.0、0.0 |
| `_claim_is_misbelieved`、順位、問い合わせの圧 | `misbelieved`、`contact`、`correction` | 閾値 0.5、`min(1.0, ...)`、`max(0.0, ...)` |
| `complete_staff_operation` | `rules/staff_operation_events.rs` の `complete` | `relief * 0.5`、`* 0.25`、`* 0.1`、`min`、`max` |
| `interrupt_staff_operation` | `interrupt_operation` | 経路の途中で最も近い区域（`progress < 0.5`） |
| `CareOutcomeFormula` | `rules/support.rs` の `formula_deltas`、`hygiene_after` | 既定値 0.03、0.02、0.04、0.03、0.02、0.03、0.05、0.02、0.01、比例と減算の式 |
| `effective_facility_capacities`、`dependent_facilities` | `rules/handlers.rs` | 依存の広がり、働く数 |
| 環境ストレスの上限、優先度 | `start_stressor`、`schedule_completion` など | 区域に16、優先度 10、20、-10、5 |
| `_preempt_background_staff`、`assign_pending` | `rules/staff.rs` | 候補の条件、公平の繰り上げ |

`mathx` を通すのは `py_max`、`py_min`、`hypot`、`py_is_close` です。

**Rust だけの条件（超えると Python）：** 厳密な型でない値（`bool`、`str` の継承、int の `stress`）。2^53 を超える数。i64 を超える int。読み込みの古い `information_spread_v1`（版1）。ケアの変化が32件を超えるとき。`relief` が厳密な `float` でないとき。`Unsupported` になった行。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。
- **7日分の影は終わっていません。** 上のとおり、3日分で比べたのは2つの規則だけです。
- **各イベントの比較の回数は少ないです。** `cooperation` 2回、`equipment_*` 1回ずつ。`staff_operation_relief` はテストの2回だけです。
- **`assign_work` の影は、controller を浅く写します。** dict、list、set の属性だけ写すので、入れ子の入れ物を共有します。今の回で違いは出ていませんが、影の作りの仮定です。
- **`_consider_due_complaints` の影は、尋ねた列だけを比べます。** policy の中身は比べません。
- **印のある `effect_program_population` は `dispatch_one` を通ります。** 印のない形は表から動きますが、ゲートの回数は分けていません。
- **影を1つだけ選ぶ回：** このブランチに `python_path_only` はなく、`handlers_rest` だけの回の数は0ではありません。driver のレーン（L5）をマージしたあとは、この影にも指定が要る可能性があります。**マージ後の形では確かめていません。**
- 部品の費用、メモリは測っていません。

## レーンとの重なり

- **編集したファイル：** `native/src/rules/effects.rs`（受領記録の呼び出し）、`resources.rs`、`service.rs`（公開だけ）、`state/information.rs`（公開だけ）、`domain/resource_service.py`、`domain/staff_operations.py`（`kernel_first` の行だけ）、`experiments/phases/segments.py`（2行）、`benchmarks/rule_shadow/sitecustomize.py`（`SHELTERSIM_RULE_SHADOW_ONLY`）、`benchmarks/rule_shadow/shadow_staff.py`、`tests/support/python_rules.py`、`tests/support/tracked_mirror_plugin.py`。
- **`domain/population.py`、`population_lifecycle.py`、`geometry_overlay.py`、`temporary_spaces.py`、`facility_queues.py` は編集していません。** 名前で呼ぶので、これらのレーンが名前や引数を変えると、kernel が例外で `Unsupported` になります。最初の変更より後なら例外がそのまま出ます。
