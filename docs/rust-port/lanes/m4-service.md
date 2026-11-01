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

## 計測

2026-11-01、WSL2、16コア、ほかのレーンと同時。基準は `641ca13`（レーン M4 資源の先頭）。コードは `519753f`（その後の変更はテスト、影、文書だけです）。

`tick_budget.py`（9600分から480 tick、3回）：**0.959**（0.957、0.959、1.058）。A/A 1.012（0.956、1.012、1.054）。状態は3つとも一致。load average 46〜54。**tick 全体の差は A/A の揺れと同じ大きさなので、これだけでは「速くなった」とは言えません。**

部品（同じ分岐から480 tick、基準2本とレーンを同時に1組、スレッドの CPU 時間、µs/tick、load average 約 11）：

| | 基準 | A/A | レーン | 1 tick の回数 |
|---|---:|---:|---:|---:|
| `TickEngine.step` | 3,411 | 3,567 | 3,232 | 1 |
| `_run_propose_phase` | 755 | 790 | 568 | 1 |
| `_apply_due_proposals` | 586 | 614 | 399 | 1 |
| `_apply_effect`（基準には手順がありません） | | | 177 | 9.31 |
| `_record_outcome` | 149 | 158 | 138 | 9.31 |
| `_route`（Python の手順。レーンでは kernel の中） | 74 | 78 | 0 | 2.67 |
| `_schedule_travel`（同） | 53 | 54 | 0 | 0.88 |
| `ResourceServiceManager.arbitrate` | 98 | 99 | 86 | 1 |
| 上の内数 `_serve_head` | | | 58 | 1.11 |
| `complete` | 30 | 29 | 18 | 0.39 |
| `request` | 10 | 11 | 8 | 0.40 |
| `toilet.arbitrate` | 31 | 32 | **39** | 1 |
| `EventQueue.pending` | 22 | 23 | **26** | 7.63 |
| `_run_dispatch_phase` | 636 | 658 | 642 | 1 |

負荷の高い回（load average 約 30、2組の中央値）でも `step` は 5,477 → 5,178 µs、`_apply_due_proposals` は 925 → 629 µs でした。

**kernel を残すかの確認：** どれも基準より減りました。一番差が小さいのは `arbitrate` で約 12%、`request` で約 20% です。`_serve_head` は1回約 52 µs で、1回 20 µs の目安より重いです。`pending_reserves` が Python の `pending()` のタプルを読むのと、提供の開始で Python のメソッド（`list.pop`、`ActiveTask(...)`）を呼ぶのが理由と見ていますが、内訳は測っていません。`QueueStore` に `resource_reserve` の索引があれば走査は要りません（`native/src/state/events.rs` はレーン M3 の範囲なので触っていません）。

**増えたもの：** `toilet.arbitrate` の +7 µs と `pending()` の +4 µs は、Rust が予定したイベントのオブジェクトを Python が読むときに作るからと見ています（確かめていません）。レーン M3 の dispatch が行を読むようになれば減るはずです。

7日分の素の回（負荷の下で1回）：スレッドの CPU 時間 139 秒。基準とは同時に測っていません。

## 結果

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 3,690件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,073件成功、1,623件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| ゲートの `rule_shadow`（全部の影） | 55シナリオ、問題0件、違い0件 |
| 7日分 | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致（素の回、影の回） |
| 7日分の `rule_shadow` | **移動、判断の効果、列、資源、提供の影だけ**（全部の影は遅いため）。違い0件 |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません。** `core/state_mirror.py` は登録の2行だけで、意味は変えていません |

| `rule_shadow`（kernel が動いた / 呼び出し） | ゲート | 7日分 |
|---|---:|---:|
| `apply_effect` | 60,035 / 63,473 | 84,155 / 87,464 |
| `serve_head`（開始 / 止まる） | 11,951 / 11,951（1,923 / 10,028） | 12,185 / 12,185（4,008 / 8,177） |
| `complete` | 1,955 / 2,027 | 4,008 / 4,008 |
| `request` | 2,397 / 2,478 | 4,016 / 4,016 |
| `facilities_to_serve` | 28,610 / 29,210 | 10,080 / 10,080 |
| `route` | 8,810 / 8,813 | 3,212 / 3,212 |
| `schedule_travel` | 6,663 / 6,663 | 3,095 / 3,095 |
| `record_request_outcome` | 153 / 153 | 0 / 0（手順はすべて kernel の中です） |
| `validate` 受け入れ / 断り（受け入れの25回に1回は Python の規則も通過） | 28,610 / 600 | 10,080 / 0 |
| 比べた値（この2つの影） | 3,515,311 | 61,808,019 |
| 比べたイベント / ログの記録 | 47,339 / 3,878 | 74,493 / 8,016 |
| 違い | 0 | 0 |

**断り：** 7日分は `seek_help` の 3,309回だけで、これは Python の手順です（上）。ゲートは `seek_help` 2,839、`seek_information` 290、時間の掛からない移動 92、独立した FIFO 149 + 71 + 81 + 600、仮設の部屋の `sleep` 68、辺の容量 3、タスクのない `complete` 1（Python が抑制します）です。

**枝に届いた数（ゲート）：** 行動は `seek_*` を除くすべて。trip の成否 `moves` / `stays` は、`request_toilet` 2,838 / 650、`fetch_water` 1,138 / 375、`collect_meal` 1,164 / 24。`route` が None 102回。

**影の最初の版の失敗：** 影は `_apply_effect` の Python の手順をコピーの上で動かします。最初の版は `toilet_service` や `facility_queues_v1` のコピーがなく、断る枝で本物の世界に書いてゲートの影の回が落ちました（kernel の問題ではありません。素のゲートは一致していました）。直したあとのゲート全体が上の結果です。7日分の影はこの修正の前の回ですが、修正した枝（独立した FIFO、時間の掛からない移動、ベッド）は7日分にありません。

### 影の感度（コピーで変更。`--only generated_project`）

| 変更 | 影 | ゲートのハッシュ | テスト |
|---|---|---|---|
| 給水の `relief` を 1.01 倍（`hunger_relief` と同じ式） | 20件以上 | 不一致 | 3件失敗 |
| 待ち時間の合計を 1.01 倍 | 20件以上 | 不一致（world のみ） | 7件失敗 |
| 社会の優先度 35 → 36 | 20件以上 | 不一致 | 5件失敗 |
| 施設の負荷から向かっている人を外す | 20件以上 | 不一致 | 0件 |
| 既定の分 5 → 6 | 0件 | 一致 | 1件失敗（設定に値があるので Run では読まれません） |
| 区域の定員の `>=` → `>` | 0件 | 一致 | 1件失敗 |
| 負荷と時間の順を入れ替える | 0件 | 一致 | **最初は0件。** `test_the_lower_load_goes_before_the_shorter_walk` を足して1件失敗 |

## 網羅

`gate_all_paths.py --coverage`（kernel ありとなしの合計 / kernel ありだけ）：`domain/resource_service.py` 477行のうち 409（86%）/ 350（73%）、`experiments/phases/propose.py` 359行のうち 293（82%）/ 207（58%）、`experiments/phases/arbitrate.py` 38行（100%）/ 同じ、`policies/space.py` 62行のうち 55（89%）/ 同じ、`domain/care_receipts.py` 164行のうち 140（85%）/ 同じ。Rust の行の網羅は測っていません。上の枝の件数と `test_service_rules.py` で見ています。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 新しい C API はありません。`hypot` などの浮動小数の関数は増えていません。
- **RSS は測っていません。** `resource_service_v1` の履歴が行と木の両方にあるぶん増えます（7日分で約1万件の型付きの行）。
- **イベントの連番：** 計画は予定のイベントの `sequence` を調べません（`QueueStore` に問い合わせがなく、列のレーンと同じです）。`validate` が `id_counters["event"]` は予定と記録の最大より大きいと確かめているので、検証の通った世界では起きません。起きたら `schedule` が Python と同じ型の `DuplicateEventError` を出します。
- **`shortest` の順序：** `Arc<str>` の比較（バイト順）で Python の tuple の比較（コードポイント順）を置き換えています。UTF-8 では同じ順です。ASCII でない ID はテストしていません。
- **`_physically_closed_edges`：** Rust は実効の間取りの `routes` を見ます。Python は生の設定の `routes` を見ます。ID が同じ前提で、`effective_geometry` は `routes` を変えません。`test_every_route_is_python_s_for_every_mobility` と影の範囲で確かめています。
- **`policy_version` ではなく `ShortestTravelTimePolicy` の型名で判断します。** 同じ名前の別のクラスには気づきません。
- **`controller._route_rows` の属性：** グラフのオブジェクトが同じ間は読み直しません。`ZoneGraph` は frozen です。`edges` の中の値を書き換えるコードは `src/` にありません。
- **別名：** `world.model_state["resource_service_v1"]` は置き換えられなくなりました。`src/` で持ち続ける形は調べた範囲でありません。

## レーンとの重なり

- `experiments/phases/segments.py`：登録の3行。`core/state_mirror.py`：`OWNED_NAMESPACES` と `_HASHED_BY_PARTS` の2行。
- `events/handlers.py`、`experiments/phases/dispatch.py`、`native/src/state/events.rs`（M3）、`domain/staff.py`、`staff_operations.py`、`common_operations.py`（M4 職員）は変えていません。
- `rules/staff_dynamics.rs`：`staff_status`、`is_available`、`is_available_now` を `pub(crate)` にしました（3行）。`rules/agent_dynamics.rs`：`agent_knows_facility` を同じく（1行）。職員のレーンが同じ行を変えると衝突します。
