# M6 レーン：`events/handlers.py` の残りのうち費用の大きい規則（トイレ、支援の依頼、ケアの受領記録）

dispatch が Python に渡していたイベントのうち、7日分に現れるものを表に足しました。7日分で Python に渡るイベントは **10,495件から8件**（`resource_delivery` だけ）になり、Rust の関数の断りは0件です。結果は変わりません。係数は1つも増えていません。Python の規則は比較の基準として残ります。

## 調査（`rust/m3-dispatch` の先頭 `98f10f1`）

関数の出入りに計測の枠を付けて7日分とゲートを動かしました。秒は枠を含むスレッドの CPU 時間です。7日分の全体は 93 秒（load average 約 36）で、この表の順に移しました。

| もの | 7日分 回数 | 秒 | 1回 µs | ゲート 回数 | 扱い |
|---:|---:|---:|---:|---:|---|
| `append_applied_care_transition` | 7,329 | 0.690 | 94 | 7,503 | **kernel** |
| `_apply_effect`：`seek_help` | 3,309 | 0.626 | 189 | 2,839 | **kernel** |
| `ToiletServiceManager.arbitrate` | 10,080 | 0.559 | 55 | 31,530 | **kernel** 3つ。ループは Python |
| `_after_arrival` | 11,537 | 0.600 | 52 | 9,805 | **kernel** |
| `toilet_service_complete`（`dispatch_one`） | 2,460 | 0.352 | 143 | 3,247 | **kernel**（下の2つを含む） |
| `resource_service_complete`（`dispatch_one`） | 4,008 | 0.346 | 86 | 2,027 | **表に追加** |
| `_record_request_interrupted` | 2,597 | 0.286 | 110 | 1,011 | **kernel** |
| `staff_support_complete`（`dispatch_one`） | 711 | 0.251 | 353 | 2,428 | **kernel** |
| `_schedule_return_to_initial_zone` | 2,460 | 0.172 | 70 | 3,247 | **kernel** |
| `_consider_due_complaints` | 10,080 | 0.176 | 17 | 31,530 | Python（下） |
| `_finish_stopped_journey` | 9,578 | 0.139 | 15 | 9,985 | **直接呼ぶ**（`sleep_bed` 以外） |
| `StaffService.request` | 3,309 | 0.126 | 38 | 3,129 | **kernel**（`seek` の中） |
| `support_request_timeout`（`dispatch_one`） | 3,308 | 0.119 | 36 | 3,512 | **kernel** |
| `_record_staff_support_started` | 635 | 0.078 | 123 | 1,832 | **kernel** |
| `Toilet.request` | 2,464 | 0.061 | 25 | 2,893 | **kernel** |
| `StaffService.assign_pending` | 10,080 | 0.586 | 58 | 31,530 | M4 の kernel のまま。依頼を取る tick（7%）は Python |
| `information_contact_update` | 0 | | | 5,339 | Python |
| 全体の書き換えを伴う `staff_restored`、`staff_unavailable`、`effect_program_*` など | 0 | | | 下 | Python |

## 移したもの

| もの | 場所 |
|---|---|
| `append_applied_care_transition`、`make_care_transition_record` の中身（pydantic の受領書を作りません）、`care_source_ids_for_agent`、`population_context_people` の1人分 | `native/src/rules/care_receipts.rs` |
| `record_shared_operation` のケアの計画と書き込みを切り出し | `rules/common_operations.rs` の `plan_record`、`write_record` |
| `ToiletServiceManager.request`、`_facilities_to_serve`、`_has_room`、`_start_head`、`complete_toilet_service`、`ToiletServiceCompletionHandler` の衛生度 | `native/src/rules/toilet.rs` |
| `StaffService.request`、`timeout_support_request`、`complete_support_task`、`StaffSupportCompletionHandler`（情緒の支援。ほかの種類は下）、`_record_request_interrupted`、`_record_staff_support_started`、`support_kind_for_agent`、`current_household_id` | `native/src/rules/support.rs` |
| `seek_help`、`seek_information` の `_apply_effect` | `native/src/rules/proposals.rs` の `apply_seek` |
| `_after_arrival` | `native/src/rules/travel_completion.rs` の `arrive` |
| `_schedule_room_approach`、`_schedule_return_to_initial_zone` | `native/src/rules/travel.rs` |
| 表の4行（`toilet_service_complete`、`resource_service_complete`、`support_request_timeout`、`staff_support_complete`）、`_finish_stopped_journey` の直接の呼び出し | `native/src/rules/dispatch.rs` |
| `ResourceServiceManager.complete` を `EventRef` から（行で予定されたイベントのオブジェクトを作りません） | `native/src/rules/service.rs` |
| `EventRef::int`、`payload_len`、`is_completion` | `native/src/state/events.rs` |
| 比較 | `benchmarks/rule_shadow/shadow_handlers.py` |
| テスト | `tests/integration/test_handler_rules.py` |

- **Python の変更：** `append_applied_care_transition` は何も返さなくなりました（`src/` にもテストにも読むコードはありません）。`ToiletServiceManager.arbitrate` は `_facilities_to_serve`、`_has_room`、`_start_head` に分けました。式は変えていません。
- **順序：** `staff_support_complete` は Python と同じく、先に番号を割り当て、イベント自身の記録のあとで受領記録の記録を足して射影します。
- **段階をまたぐ `Unsupported`：** 歩きの完了のあとの `arrive` と、トイレの完了のあとの歩いて帰る手順は、それぞれ別の kernel です。断ると、その手順だけ Python が動きます（`handler._after_arrival`、`after_complete`）。どちらも何も書く前に断ります。
- **LLM の hook：** `consider_support_request` と `_prioritize_support_request` は Python のままで、Python の手順と同じ場所で呼びます。借用は持ちません。`ValueError` は Python の `try` と同じく `support_request_rejected_count` になります。`record_support_request` も、閾値を超えたときに Python を呼びます。
- **依頼の `__setattr__`：** `SupportRequestState` は書き込みを報告する（`_write_listener`）ので、状態は `setattr` で書きます。`raw_setattr` を使うと tick の検証が読み直しません。`test_a_timeout_writes_through_the_request_s_listener` が固定します。
- **差し替え：** 表はクラスと `apply`、`after_complete` は controller の `_schedule_return_to_initial_zone`（`install_return_rule`）を、イベントごとに見ます。`_after_arrival` のクラスの属性と、管理者のインスタンスに置いた `request` も見ます。
- **`@pytest.mark.python_rules`：** 名前を `tests/support/python_rules.py` に足しました。

## 断るもの（`Unsupported`、何も書く前）

- 独立した FIFO（`facility_queues_v1`）：トイレの kernel すべてと `arrive`。
- 部屋の経路がない、または時間内に着けない `_after_arrival`（`_reject_room_approach`）。
- 検証つきの `request`、管理者に置いた `request`、`CounterRNG` でない乱数。
- Run の最後の分の依頼（`StaffService.request` がその場で閉じます）。7日分で1回です。
- `staff_support_complete`：選んだケアの式が決める種類（`hygiene_support` と `typed-care-support-response-v1` の種類）。**情緒の支援はどの式でも同じです。**
- 受領記録：`verified` でない直近の記録（復元と「写して差し替える」世界）、順序や同一性の違い。Python の規則が読んで同じ文を出します。
- 値：型が違う、ID が契約に合わない、i64 を超える整数など。

## Python に残した規則（手順4までに Rust の経路が要るもの）

回数は `dispatch_one` の呼び出しです（ゲート / 7日分）。

| 規則 | ゲート | 7日分 |
|---|---:|---:|
| `InformationContactHandler` | 5,339 | 0 |
| `ProximityContactHandler` | 918 | 0 |
| `SocialInteractionHandler` の `complaint_response`、`cooperation`、`information_reaction`、型のないもの（指示で挙げられたものですが、7日分0回なので後回しにしました） | 82 | 0 |
| `AgentTravelCompletionHandler` の `sleep_bed`、`presence_relocation` | 13 | 0 |
| `InformationCorrectionHandler`、`InformationRumorHandler` | 104、156 | 0 |
| `StaffUnavailabilityHandler`、`StaffRestorationHandler` と `_interrupt_support_task` | 89、90 | 0 |
| `StaffOperationArrival`、`Completion`、`Relief` | 15、14、0 | 0 |
| `ResourceLedgerHandler`（`resource_delivery`、`resource_loss` など） | 53、1 | **8** |
| `DeliveryDelayHandler`、`WaterOutageHandler`、`WaterRestorationHandler` | 3、4、25 | 0 |
| `FacilityOutageHandler`、`FacilityRecoveryHandler` | 9、25 | 0 |
| `InfrastructureEventHandler`（`power_*`、`extreme_*`、`equipment_*`、`environmental_*`） | 10 | 0 |
| `EffectProgram*`：**レーン M6 effects と population の範囲** | 179 | 0 |

ほかに：

- `staff_support_complete` の型付きのケアと `hygiene_support`（ゲート 8）、`make_facility_recovery_record`、`make_staff_operation_record`、`make_staff_handoff_record`、`make_staff_support_continuity_record`、`ingest_applied_shared_operation_records`、`care_history_*`、`explicit_care_dependents`。
- `ToiletServiceManager._arbitrate_independent_facility`、`arbitrate` の Python のループ、独立した FIFO の `request` の枝、`_reject_room_approach`。
- `_consider_due_complaints`（7日分 10,080回、1回 17 µs）：待っている依頼の索引が Python の `_tick_memo` にあります。行を全部読むと、依頼が増えるので今より遅くなる見込みで、**作っていません**（見込みで、測っていません）。
- `_assign_staff_support`（依頼を取った tick のループ。手順は Python、受領記録は kernel）、`assign_pending` の割り当ての本体。
- `consider_*`、`_prioritize_support_request`（LLM の境界。Python のままの予定です）、`record_support_request`。
- `_finish_stopped_journey` の `sleep_bed`。

## モデルの担当の方に見ていただくコード

**新しい係数はありません。** 判断の規則が Python と Rust の2か所になりました。

| Python | Rust | 中身 |
|---|---|---|
| `ToiletServiceCompletionHandler` | `rules/toilet.rs` の `HYGIENE_RELIEF` | `max(0.0, need - 0.2)` |
| `_start_head`（元の `arbitrate`） | `rules/toilet.rs` の `start_head` | `min + int(draw * span)`、`min(.., max)`、乱数のキー。`min_service_minutes`、`max_service_minutes` は毎回読みます |
| `StaffSupportCompletionHandler`（情緒の支援） | `rules/support.rs` | `stress - relief`、`trust + relief * 0.25`、`reassurance + relief`、`positive_affect + relief * 0.5`、`negative_affect - relief * 0.5`。`hygiene_need - relief`（式の選択がないとき）。`relief` は設定から読みます |
| `StaffService.request`、`timeout_support_request` | `rules/support.rs` | 優先度 20、`min(minute + unresolved_after_minutes, duration - 1)`、受付の条件、`cooldown_minutes` |
| `support_kind_for_agent` | `rules/support.rs` の `support_kind_of` | 種類の選び方 |
| `care_source_ids_for_agent`、`CARE_RECEIPT_SOURCE_ID` など | `rules/care_receipts.rs` | 出典の ID、方策の版 |
| `_schedule_room_approach` | `rules/travel.rs` | 出典の ID（`ASM-SPATIAL-MOVEMENT-V2`）、方策の版 |

`mathx` を通すのは `py_max`、`py_min` です。

**Rust だけの条件（超えると Python）：** 待ち時間と旅の時間が 2^53 分未満。`relief` が厳密な `float`（int なら Python）。`SupportKind`、`Identifier` の判定は Python の正規表現より狭く、改行で終わる ID などは Python が判断します。

## 計測

2026-11-01、WSL2、16コア、ほかのレーンと同時。基準は `98f10f1`。

`tick_budget.py`（9600分から480 tick、3回。レーン `ace94c4`）：**0.974**（1.219、0.974、0.893）。A/A 1.007（1.034、0.920、1.007）。状態は3つとも一致。load average 約 13。1回目の 1.219 は揺れと見ていますが、原因は調べていません。**これだけでは速くなったとは言えません。**

7日分の丸ごとを基準と同時に動かした部品（µs/tick。枠はこの段階の入口だけ、スレッドの CPU 時間）：

| | 基準 | レーン | 比 | 基準 | レーン | 比 |
|---|---:|---:|---:|---:|---:|---:|
| | 1組目 `ace94c4`、load 約 35 | | | 2組目 `eb13832`（トイレの開始の前）、load 約 45 | | |
| `TickEngine.step` | 4,355 | 4,109 | 0.944 | 4,843 | 4,573 | 0.944 |
| `_run_dispatch_phase` | 913 | 794 | 0.869 | 994 | 856 | 0.861 |
| `_apply_due_proposals` | 597 | 536 | 0.898 | 631 | 570 | 0.903 |
| `_apply_effect`（1回 µs） | 28.0 | 22.1 | 0.789 | 30.1 | 23.6 | 0.784 |
| `_arbitrate_services` | 201 | 177 | 0.881 | 218 | 216 | 0.994 |

2つの組を同時に動かしたので、それぞれ1回ずつの比です。7日分の素の回（`ace94c4`）はスレッドの CPU 時間 74.6 秒、実時間 91.7 秒（load 約 6。基準とは同時に測っていません）。

**kernel が Python に勝つかの確認**（入口の計測、同時の1組。load 約 35。`use_python_entries` の回で、`__wrapped__` でなく kernel を呼びます）：

| 規則 | Python µs | kernel µs |
|---|---:|---:|
| `append_applied_care_transition`（Python の入口） | 128 | 44 |
| `_record_request_interrupted` | 153 | 42 |
| `_record_staff_support_started` | 179 | 49 |
| `StaffSupportCompletionHandler.apply` | 206 | 84 |
| `_after_arrival`：トイレ / 資源 | 71 / 68 | 37 / 50 |
| `Toilet.arbitrate` | 71 | 40 |
| `_schedule_return_to_initial_zone` | 93 | 73 |
| `ToiletServiceCompletionHandler.apply`（帰り道を含む） | 132 | 103 |
| **`SupportRequestTimeoutHandler.apply`** | **11.6** | **12.2** |
| **`ResourceServiceCompletionHandler.apply`（オブジェクトから）** | **70** | **75** |

**下の2行は kernel だけでは勝っていません。** 残した理由は、表に載ると `dispatch_one`、`EventDispatcher.settle`、`ingest_applied_shared_operation_records` の分がなくなることです（全体で1回 約 38〜47 µs）。実際に動く直接の経路は、行から読むこの枠の外なので、**単独では測っていません**。`_run_dispatch_phase` の 13% の減りに含まれます。`_consider_due_complaints` と `_finish_stopped_journey` の入口の値は変わっていません（`_finish_stopped_journey` は今は呼ばれない経路の値です）。

## 結果

コードは `ace94c4`（その後の変更はテスト1件の tick 数、`shadow_handlers.py`、文書だけです）。

| 確認 | 結果 |
|---|---|
| 全テスト（Rust あり、`81d7117` 相当） | 3,883件成功、6件 skip |
| 全テスト（`SHELTERSIM_NATIVE=0`、`ace94c4`） | 2,073件成功、1,816件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| 7日分（`--expect`） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致（素の回、影の回） |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません。** `core/state_mirror.py` は変えていません |

**テストを1件変えました：** `test_rules_over_real_ticks_keep_every_row_equal_to_its_object` は 25 tick で転送の書き込みが100回を超えることを見ていましたが、kernel が増えて92回になったので、40 tick にしました（閾値はそのままです）。

kernel が本当に動いた回数（直接呼ぶ素の回、`SHELTERSIM_DISPATCH_COUNT_ONLY=1`）：

| | ゲート | 7日分 |
|---|---:|---:|
| イベント Rust / Python | 72,100 / 8,411 | 91,254 / **8** |
| `arrive` 動いた / 断り | 9,582 / 141 | 11,537 / 0 |
| トイレの完了 動いた / 断り | 3,131 / 65 | 2,460 / 0 |
| 帰り道 Rust / Python | 3,131 / 0 | 2,460 / 0 |
| 資源の完了 動いた / 断り | 1,951 / 72 | 4,008 / 0 |
| 期限切れ 動いた（うち抑止）/ 断り | 3,286（2,310）/ 0 | 3,308（711）/ 0 |
| 支援の完了 動いた / 断り | 2,404 / 21 | 711 / 0 |
| `interrupted` 動いた / 断り | 974 / 37 | 2,597 / 0 |
| `started` 動いた / 断り | 1,812 / 20 | 635 / 0 |
| `_finish_stopped_journey` 直接 / Python | 9,973 / 12 | 9,578 / 0 |

**7日分の断りは0件です。** 残る Python の8件は `resource_delivery` です。`seek` の断りは7日分で1回（Run の最後の分）だけです。

断りの理由（ゲート）：独立した FIFO 134 + 65 + 71 + 600、`verified` でない直近の記録 37 + 20 + 4 + 81 + 241、ケアの式が決める種類 8、部屋の経路 7、タスクなし 1 + 10、Run の最後の分の依頼 2、`a value is not a str` 13（`sleep_bed`、`presence_relocation` の 13回と同じ数で、`facility_id` が None の歩きと見ています。シナリオは追っていません）。

`rule_shadow`（**`shadow_dispatch`、`shadow_handlers`、`shadow_proposals`、`shadow_service` だけ**。全部の影は時間がかかるので動かしていません）：

| kernel が動いた / 呼び出し | ゲート | 7日分 |
|---|---:|---:|
| `after_arrival` | 9,664 / 9,818 | 11,537 / 11,537 |
| `append_transition` | 2,206 / 2,447 | 789 / 789 |
| `seek` | 3,046 / 3,129 | 3,308 / 3,309 |
| `timeout`（うち `EventSuppressed` の文の比較） | 3,512 / 3,512（2,500） | 3,308 / 3,308（711） |
| `staff_support_complete` | 2,406 / 2,428 | 711 / 711 |
| `record_interrupted` | 974 / 1,011 | 2,597 / 2,597 |
| `record_started` | 1,812 / 1,832 | 635 / 635 |
| `toilet_complete` | 3,182 / 3,247 | 2,460 / 2,460 |
| `toilet_start`（開始 / Run の終わり） | 3,184 / 3,184（3,132 / 52） | 2,461 / 2,461（2,460 / 1） |
| `schedule_return` | 3,247 / 3,247 | 2,460 / 2,460 |
| `toilet_has_room` | 22,739 / 22,739 | 9,604 / 9,604 |
| 比べた値 | 10,232,244 | 84,548,609 |
| 比べたイベント / ログの記録 | 13,709 / 6,779 | 13,284 / 7,329 |
| 違い | 0 | 0 |

- **`toilet_request` の kernel** は影の回で 65回呼ばれ、すべて断りました（独立した FIFO）。影の回の `arrive` は Rust の関数を直接呼ぶからです。比較は `after_arrival` の中と `test_a_toilet_request_is_python_s` です。
- **Provider の hook がある `seek` 181回は、結果だけを比べました**（`seek.with_provider_hooks_result_only`、ゲートの `llm_tape` など）。hook がイベントを予定するので、hook を除いた Python の写しとは合いません。最初の影はこれを違い52件と報告しました。規則の問題ではありません。これらの回はハッシュが見ます。
- **`Staff.request` の単独の kernel はありません。** `seek` の中で比べます。

### 影の感度（コピーで変更。`--only generated_project`）

| 変更 | 影 | ゲートのハッシュ | `test_handler_rules.py` |
|---|---:|---|---:|
| 衛生度の差 0.2 を 1% | **0件** | **一致** | 1件失敗 |
| トイレの時間の `draw * span` を 1% | 16件 | 不一致 | **0件** |
| `trust` の 0.25 を 1%（`0.2525`） | 20件以上 | world、events が不一致 | 1件失敗 |
| `positive_affect` の 0.5 を 1%（`0.505`） | 20件以上 | 不一致 | 1件失敗 |
| 期限切れの優先度 20 → 21 | 20件以上 | world、events が不一致 | 11件失敗 |
| クールダウンを +1（期限切れ） | 20件以上 | 不一致 | 1件失敗 |
| 受領記録の `detail` に1文字 | 20件以上 | world、events が不一致 | 15件失敗 |
| 部屋の接近の方策の版の文字列 | 20件以上 | world、events が不一致 | **0件** |

**衛生度の 1% は影とハッシュに届きません。** `generated_project` でトイレの完了の時点の `hygiene_need` が 0.2 に届かず、どちらの値でも 0 になるからと見ています（値は確かめていません）。テストが `0.75` などで見ます。逆に、トイレの時間と接近の版はテストに届かず、影とハッシュが見ます。

**ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）：** `events/handlers.py` 1105/1421（78%）/ 1010（71%）、`domain/care_receipts.py` 141/165（85%）/ 138（84%）、`domain/facilities.py` 227/262（87%）/ 191（73%）、`domain/staff.py` 303/353（86%）/ 284（80%）、`experiments/phases/dispatch.py` 138/143（97%）/ 88（62%）。Rust の行の網羅は測っていません。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。`sha2` はすでに依存にあります。libm は使いません。
- **直接の経路の `walk_home` と `record_*`、`staff_support_complete` は、影の回では動きません。** 影は Python の入口を通って同じ kernel を呼びます。直接の経路はハッシュと回数で見ています。`staff_support_complete` は、直接の経路（受領記録を Rust で書く）と入口の経路（Python の記録を返す）で、最後の書き込みが違います。7日分とゲートのハッシュが一致するので同じと見ていますが、比較は別にしていません。
- **`resource_service_complete` の行からの読み**も影の回では動きません（オブジェクトから読みます）。同じくハッシュで見ています。
- **`person_context` は `population_dynamics_v1`、`population_context_v1`、`population_presence_v1` を木から読みます。** レーン M6 population がこの木を切り離すか形を変えると `Unsupported` になり、Python が動きます。結果は変わらず、遅くなるだけです。
- `arbitrate` の `started` は Python の `find` でオブジェクトを作ります（資源と同じです）。`arbitrate` の戻り値を読むのはテストだけで、作らない形にはしていません。
- `_consider_due_complaints` を kernel にしない判断は見込みで、試していません。
- 部品の表の負荷は高く、値は低負荷の約 2 倍です。比だけを見てください。
