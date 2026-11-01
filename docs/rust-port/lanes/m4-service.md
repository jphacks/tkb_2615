# M4 レーン：水と食事の提供と、判断の効果（`resource_service_v1`、`_apply_proposal`）

M4 の資源と列のレーンが残したものを移しました。`resource_service_v1` は Rust の行が持ちます。`arbitrate`、`complete`、`request`、`_apply_proposal` の本体の多く、`_route`、`_schedule_travel` は1回の呼び出しが1つの kernel で、計画してから書きます。コールバックはありません。結果は変わりません。Python の規則は比較の基準として残ります。

## 調査で決めた範囲

基準の木（`641ca13`）に回数と時間の計測を掛けて7日分を動かしました（load average 約 10、`TickEngine.step` 34.0 秒、計測の枠の分を含みます）。

| もの | 回数 | 秒 | 1回 µs | 扱い |
|---|---:|---:|---:|---|
| `_apply_proposal`：`help_others` | 46,777 | 1.90 | 40.6 | **kernel** |
| `sleep`（仮設の部屋なし） | 14,585 | 0.96 | 65.9 | **kernel**（`reserve_sleep_bed` は 0.58 秒で、人の全員を読むだけで None でした） |
| `socialize` / `care_family` | 9,760 / 300 | 0.40 / 0.01 | 41 / 40 | **kernel** |
| `wait` / `rest` | 5,098 / 1,148 | 0.13 / 0.03 | 25 / 26 | **kernel** |
| `fetch_water` / `collect_meal` | 3,020 / 1,001 | 0.76 / 0.16 | 252 / 162 | **kernel** |
| `request_toilet` | 2,466 | 0.71 | 288 | **kernel** |
| `seek_help` | 3,309 | 0.80 | 242 | Python（下） |
| 上の内数 `_record_outcome` | 87,464 | 1.49 | 17 | レーン J の kernel のまま |
| 上の内数 `_route` | 28,620 | 0.85 | 30 | **kernel**（ほかの段階の呼び出しも） |
| 上の内数 `_schedule_travel` | 9,582 | 0.57 | 60 | **kernel**（ほかの段階の呼び出しも） |
| `ResourceServiceManager.arbitrate` | 10,080 | 1.26 | 125 | 空の列は呼ばれません。1回分が **kernel**。ループは Python |
| `complete` / `request` | 4,008 / 4,016 | 0.33 / 0.14 | 82 / 35 | **kernel** |
| `_assign_staff_support` | 10,080 | 1.28 | 127 | Python（`staff.assign_pending` が 1.12 秒。職員のレーンのものです） |
| `_consider_due_complaints` | 10,080 | 0.13 | 13 | Python（下） |
| `append_applied_care_transition`（`seek_help`、`_assign_staff_support` から） | 4,097 | 0.59 | 144 | Python（下） |

**`record_decision` の前の分け方：** `_apply_proposal` は `_apply_effect`（世界に何をするか。`(target_id, next_decision_at, committed)` を返します）と `_record_outcome` の2つの手順になりました。kernel も2つです。

## 移したもの

| もの | 場所 |
|---|---|
| 行（`ServiceRow`、`RequestRecord`、`StallRecord`、`Stall`）と毎 tick の検証 `validate` | `native/src/state/resource_service.rs` |
| `facilities_to_serve`、`serve_head`、`complete`、`request`、`record_request_outcome` | `native/src/rules/service.rs` |
| `apply_effect`（`_apply_effect`） | `native/src/rules/proposals.rs` |
| `route`（`_route`、`ShortestTravelTimePolicy.route`、`_physically_closed_edges`）、`schedule_travel`、施設の選択（`_select_toilet`、`_select_resource_facility`、`_facility_queue_has_capacity`） | `native/src/rules/travel.rs` |
| 社会の文脈の値（`context_value`）、`movement_minutes`、`head_to_service`、`apply_planned` | 既存の `social_dialogue.rs`、`movement.rs`、`queues.rs`、`resources.rs` から切り出し |
| 比較 | `benchmarks/rule_shadow/shadow_proposals.py`、`shadow_service.py` |
| テスト | `tests/integration/test_service_rules.py` |

- **`arbitrate` の作り：** ループは Python のままで、`_serve_head` が `progress_physical_queue_positions` の後の1回分です。予約の確認、水の断、容量、職員、在庫、Run の終わり、`head_to_service`、提供の開始までを計画してから書きます。列の旅（列のレーンの kernel）と kernel が分かれるのは、位置の旅を書いたあと同じ回が続く枝があり、1つの kernel にすると「書いた後に断る」ことになるからです。
- **`_facilities_to_serve`：** Rust は人のいる列の設備だけを返します。空の列の `prepare_facility_queue` は何もしないので、呼びません。
- **`resource_service_v1`：** `state_row!` の型付きの行です。設計の文書は履歴を正準 bytes としていますが、規則が読むのは新しい数件で、件数も 7日分で 8,034 + 1,415 件なので、まず型付きの `Vec` にしました。bytes にするのは手順3の後半の課題です。`_HASHED_BY_PARTS` に入れたので、保存は今までどおり木から履歴の bytes を作ります。
- **保存の前の検証：** `world.validate()` の完全検証は Python で履歴を読みます（`core/world.py` の条件に `validate_event_history` を足しました）。kernel が答えるのは tick の検証だけです。
- **イベントの行：** 社会の行動、旅、提供の完了は `EventRow` で予定します。Python が `pending()` などで読むときに初めてオブジェクトを作ります（レーン E の `to_py`）。`_schedule_travel` は `pending()` を読まなくなりました。
- **経路のグラフ：** `self.space.graph` が同じオブジェクトの間、行（`RouteRows`）を `controller._route_rows` に持ちます。閉じた辺は `Geo` の同一性とmobility ごとに覚えます。

## 断るもの（`Unsupported`、何も書く前）

- `seek_help`、`seek_information`：`StaffService.request`、受領書、情報の方策、LLM の順位付けを通ります。
- 時間の掛からない移動：その場で `toilet_service.request` か `resource_service.request` を呼ぶ枝です。
- 仮設の部屋がある `sleep`：ベッドの予約です。`temporary_rooms` が空なら kernel です。
- 独立した FIFO（`facility_queues_v1` がある世界）：`serve_head`、`complete`、`request`、`facilities_to_serve` と行動の trip。
- 辺の容量のある経路（`route_capacities`）と、別の route policy。
- `validate_state=True` の `request`、`_schedule_travel`。
- 値：型が違う、taint した行、イベントや旅の ID がすでにある、i64 を超える整数など。Python の規則が動き、同じ文を出します。

## Python に残した規則（手順4までに Rust の経路が要るもの）

- `ResourceServiceManager.cancel`、`interrupt_for_staff`、`_arbitrate_independent_facility`、`_record_independent_queue_stall`（7日分は0回。ゲートだけ）。`arbitrate` の Python のループ。
- `_apply_effect` の `seek_help`、`seek_information`、時間の掛からない移動、ベッドの予約（`reserve_sleep_bed` の本体、`release_sleep_bed`）、`_prioritize_support_request`（LLM）。
- `_route` の辺の容量の分（`_active_route_edge_loads`、`_record_route_capacity_wait`）。
- `experiments/phases/arbitrate.py`：`_assign_staff_support`（`care_receipt_for_request` と受領書）、`_consider_due_complaints`。`complaint` は要求を人ごとに読むだけで 13 µs/tick です。`unsettled_support_requests` の索引が Python にあるので、kernel は同じ費用になります。
- `domain/care_receipts.py` の残り全部：`append_applied_care_transition`、`make_*_record`、`ingest_applied_shared_operation_records`、`care_receipt_for_request`。費用の大半は pydantic の受領書と `record_shared_operation`（`domain/common_operations.py`、`shared_operations_v1`）で、**同時に動いているレーン `rust/m4-staff` の範囲**です。それが移ってから移します。
- `ToiletServiceManager.request`、`complete_toilet_service`（列のレーンの表のとおり）。
- エラーの文は Python が出します（手順4）。

## モデルの担当の方に見ていただくコード

式と値は変えていません。

| Python | Rust | 中身 |
|---|---|---|
| `ResourceServiceManager._serve_head`、`complete`、`request`、`_record_queue_stall`、`_record_queue_wait` | `rules/service.rs` | 順番、在庫の確認、`hydration_relief`・`hunger_relief` の `max(0.0, need - relief)`、待ち時間の指標 |
| `water_service_blocked`、`physical_service_capacity`、`staff_is_available` | 同じ | 断水の判定、1点につき1人 |
| `_apply_effect` | `rules/proposals.rs` | 社会の行動のイベント（優先度 35、`interaction_data`、既定の 5 分） |
| `_select_toilet`、`_select_resource_facility` | `rules/travel.rs` | 負荷（列 + 提供中 + 向かっている人）、時間、ID の順 |
| `ShortestTravelTimePolicy.route` | `rules/travel.rs` の `shortest` | 最短時間と ID の順 |
| `_schedule_travel`、`incoming_travelers` | `rules/travel.rs` | 区域の定員、タスクの種類 |

**Rust に書いた係数：** `SOCIAL_PRIORITY` 35、`DEFAULT_INTERACTION_MINUTES` 5、`ROUTING_SOURCE`、`SOCIAL_SOURCE`（出典の ID）。給水量、配食量、`service_minutes`、`hydration_relief`、`hunger_relief` は設定から毎回読みます。

**Rust だけの条件（ここを超えると Python）：** 待ち時間 2^53 分未満、旅の時間 2^53 分未満。`mathx` を通すのは `py_max` だけです（`max(0.0, x)`、最大の待ち時間）。
