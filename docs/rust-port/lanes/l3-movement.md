# L3 レーン：提案と移動の残り

手順4の前に、提案（`_apply_effect`）と移動で断っていた分を Rust に移しました。**結果は変わりません。係数も増えていません。順序も今のままです。** 順序依存をなくすのは手順4の並列のレーンの仕事で、ここでは触っていません。

## 移したもの

| もの | 場所 | 中身 |
|---|---|---|
| 列の旅の引き直し（`_replan_physical_queue_trip`） | `rules/movement.rs` | `advance_movement` の中で。イベント番号は歩く順 |
| 歩く途中で列の枠に置く人 | `rules/movement.rs` | 前の人が歩いた後の位置を読む |
| `_replan_journeys`、`_replan_staff_support_trip` | `rules/replan.rs`（新規） | 1人ずつ。下の節 |
| その場の依頼（独立した列、時間の掛からない移動、間取りなし） | `rules/proposals.rs`、`rules/travel.rs` | トイレと水・食事の `queue_up` を呼ぶ |
| その場で閉じる依頼（Run の最後の分の `seek_help`、`seek_information`） | `rules/proposals.rs`、`rules/support.rs` | 受領書2件（requested、interrupted）を含む |
| `resource_service` のない世界の水・食事 | `rules/proposals.rs` | 却下の指標だけ |
| `agent_is_at_queue_slot`、`agent_is_at_queue_trip_destination`、`agent_is_at_facility_service_point` | `rules/movement.rs` | `rules/travel_completion.rs` はもう Python を呼びません |
| `staff_position_snapshots`（`advance_movement` の外から呼ぶ分） | `rules/movement.rs` | |
| `movement_v1` のない世界 | `rules/movement.rs`、`rules/travel.rs` | Python の `model_state.get(KEY, {})` と同じ読み。`the world holds another movement state` の 92 回はこれでした |

**Python の分け方：** `_replan_journeys` は `_replan_in_python`（全部）と `_replan_stopped`（止めた後の新しい経路）に分けました。動きは同じです。`SEGMENTS` と driver に `_replan_journeys` を足しました（`DRIVEN_STEPS` にもあるので、テストの patch はこれまでどおり効きます）。

**`a value is not a str` の 1,781 回：** 断りではなく、Rust が型を確かめて捨てる判定でした（`exact_str(x).is_ok()` など）。断りを作らない `core/row.rs` の `str_value` に置き換えました（`care_receipts`、`handlers`、`tick_validation`、`travel_completion`）。`sleep_bed` と `presence_relocation` の歩きは `m6-handlers-rest` で移っていて、その判定が数えられていたものです。

## 今の順序で動かす作り

`advance_movement` は2回歩くことがあります。

1. **1回目（いつも）：** これまでと同じ、tick の始めの行だけを読む計画です。順序に依存する枝に着いたら、何も書かずにやめます。
2. **2回目（その tick だけ）：** 同じ順で歩き直します。`positions` のコピーを歩きながら更新し、枝はそれを読みます。イベント番号は歩く順に取り、行に書くのは全部決まってからです。

Python の規則が途中で例外を出す状態（列の task がないなど）は計画の段階で断るので、何も変わらず Python の規則が同じ例外を出します。**費用は枝に着いた tick だけです。** ゲートと7日分は1回も着きません（下）。

`_replan_journeys` は「計画してから書く」にできません。止めた後の状態（区域、向かっている人の数）を新しい経路が読むからです。そこで断りの意味を場所で分けます。

| 断った場所 | 次に動くもの |
|---|---|
| 最初の人を止める前 | `Unsupported`。Python の手順が全部 |
| 2人目以降を止める前 | `_replan_in_python` が残りの人 |
| 止めた後、新しい経路を書く前 | `_replan_stopped` がその人の残り |

エラーの文はこれで Python のままです。`_replan_staff_support_trip` の `ValueError` も同じ文です（テストあり）。

## `Unsupported` が残るもの

- 列の旅の引き直し：列の task がない、列にいない人の旅、`queue_id` の列がない、同じイベントを2つの旅が持つ（壊れた状態。Python が例外を出すか、検証を通りません）。
- その場の依頼：列が断る人（活動中、ほかの列にいる、タスク ID が重複）。**有効な Run で届く可能性があります。** 水・食事は L8c で Rust が `ValueError` の文を `detail` に書くようになりました（`l8-texts-saved.md`）。トイレは例外がそのまま出ます。
- `manager.request` を差し替えたテスト（`the manager's request is another function`）。手順4で消えます。
- 型が違う値、taint した行。これまでと同じです。

## Python に残した規則（手順4で Rust が要るもの）

- `_route` の辺の容量の分：**L2** です。`replan.rs` は `travel::route` が断ると `controller._route` を呼びます。L2 が終われば呼ばれなくなります。
- `_prioritize_support_request`（LLM）：L6 の Provider の範囲です。
- `create_movement_runtime_state`、`canonical_movement_config`、`agent_positions`（tick の外。世界を作るときと画面）。
- `shared_passage_edge_loads`（`staff_operations.py`、`population_lifecycle.py` の Python の分から）。
- `physical_service_capacity`、`facility_queue_slot_route`、`in_room_travel_minutes`、`_select_toilet`、`_select_resource_facility`、`_facility_queue_has_capacity`、`_facility_approach`、`_physically_closed_edges`：**Rust に同じ計算があります**（`service_capacity`、`slot_route`、`room_minutes`、`select`、`queue_has_capacity`、`approach_route`、`closed_to`）。呼び手は Python の規則（`_apply_effect`、`_route`、`_abandon_and_retry`、`reserve_sleep_bed`、`arbitrate` の断った分）だけなので、kernel の入口は足さず、呼び手と一緒に消えます。**読んで確かめました（検索）。**
- `reserve_sleep_bed` の Python の本体、`_replan_physical_queue_trip` などの Python の規則そのもの：比較の基準として残します（手順4で消します）。

## モデルの担当の方に見ていただくコード

式と値は変えていません。**新しい係数はありません。** 移した枝：

| Python | Rust | 中身 |
|---|---|---|
| `_replan_physical_queue_trip`、`advance_movement` の枝 | `rules/movement.rs` | 引き直しの順、指標の数え方（予定を諦めても引き直しには数える、今の動きのまま） |
| `_replan_journeys` | `rules/replan.rs` | 目的ごとの枝、`movement_replan_*` の数え方 |
| `_replan_staff_support_trip` | `rules/replan.rs` | 完了の予定し直し、`timeout_minute <= minute` なら未解決、それ以外は列に戻す |
| `StaffService.request` の閉じる枝 | `rules/support.rs` | `support_unresolved_count`、クールダウン |
| `agent_is_at_*` | `rules/movement.rs` | 着いたとみなす距離 **0.05 m**（`ARRIVED_WITHIN`。`queues.rs` の `SLOT_TOLERANCE` と同じ値の定数が2つあります） |

**Rust だけの条件：** 増えていません。`mathx` の外の浮動小数の計算も足していません（`distance` は既存）。

## 計測

2026-11-02、WSL2、16コア、ほかのレーンと同時。コードは `8b155df`。その後の変更はこの文書と `shadow_replan.py`（人のいない呼び出しを飛ばす、`python_path_only`）です。基準は `585d9f9`（`rust/m6-handlers-rest` の先頭）です。

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 4,533件成功、8件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,462件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件（driver は通りません） |
| `rule_shadow` のゲート（全部の影） | 55シナリオ、問題0件、違い0件 |
| 7日分 | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致。実時間 249 秒、スレッドの CPU 時間 114 秒（同時の1回） |
| 計測ありの7日分、影ありの7日分 | 同じ3つ。一致 |
| `first_difference.py --both`（**driver の経路**、55シナリオ） | 全シナリオ違いなし。220892 段階で digest が同じ。最終のハッシュも 55 個同じ |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（`core/state_mirror.py` は変えていません）。`test_tracked_mirror.py` は通ります |

**影の7日分は自分の影だけです（`SHELTERSIM_RULE_SHADOW_ONLY=handlers,service,movement,proposals,replan`、10,080分）。** 全部の影の7日分は 1,230 分で 118 秒/60分になり、上限の中で終わらないので止めました。Error in sitecustomize は0件です。

| 影 | ゲート | 7日分 |
|---|---:|---:|
| `advance_movement`（kernel が動いた / 呼び出し） | 28,410 / 28,410 | 10,080 / 10,080 |
| `advance_movement` が比べた値 | 6,177,556 | 6,818,634 |
| `advance.in_order_replans`（列の引き直しのある歩き） | 0 | 0 |
| `replan_journeys`（人のいる呼び出し。比べた世界） | 1 | 0 |
| `_apply_effect`（kernel が動いた / 呼び出し） | 60,344 / 63,473 | 84,155 / 87,464 |
| `_apply_effect` が比べた値 | 2,161,780 | 15,723,656 |
| その場の依頼を含む `stays`（toilet / 水 / 食事） | 652 / 376 / 24 | 2 / 4 / 1 |
| 違い | 0 | 0 |

`_apply_effect` の呼び出しと kernel の差は `seek` の分です（`shadow_handlers.py` の比較に渡り、ここでは数えません）。ゲートの全部の影の `facility_approach_route` 5,089回と `queue_slot_position` 8,711回の断り（`the world has no core`）は、影のコピーの上の Python の規則からの呼び出しだと見ています（読んだだけで、1回ずつ確かめていません）。

**断り（計測、`or_rule!` とも）：**

| 理由 | ゲート 基準 → レーン | 7日分 基準 → レーン |
|---|---:|---:|
| `a value is not a str`（判定のみ） | 1,781 → 0 | 0 → 0 |
| `no floor plan: a request at once` | 92 → 0 | 0 → 0 |
| `the world holds another movement state` | 92 → 0 | 0 → 0 |
| `the service's independent queue at once` | 12 → 0 | 0 → 0 |
| `a request closed at once` | 2 → 0 | 1 → 0 |
| `kernels_refused` の合計（ほかのレーンの分を含む） | 3,329 → 3,223 | 基準は未測定 → 7 |

7日分の基準の数は `step4-plan.md` の表です（この木で測り直していません）。残りの断りに `movement.rs`、`proposals.rs`、`replan.rs` のものはありません。同じ範囲の `edge capacities`（ゲート 3）は L2 です。

**ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）：** `domain/movement.py` 994行のうち 806行（81%）/ 209行（21%。M4 の時点は 47%）、`phases/propose.py` 359行のうち 293行（82%）/ 118行（33%。同じく 58%）、`phases/update.py` 187行のうち 132行（71%）/ 64行（34%）。Rust の行の網羅は測っていません。

**ゲートに届かず、テストだけが比べるもの：** 順序に依存する枝の全部（`test_movement_walk_order.py`、乱数の40個の歩きを含む）、`replan_journeys` の支援以外の枝（`test_replan_journeys.py`）。

**予算：** `tick_budget.py`、`fork-b1` の 9600分から 480 tick、3回、基準 `585d9f9`。**1.005**（1.037、1.005、0.997）。A/A 1.051（1.051、1.052、1.041）。状態は3つとも一致。load average 約 20〜30。上限の中です。毎 tick の道は変えていません（`Changes` の `Vec` と、枝の判定の1回の比較だけ）。部品ごとの費用は測っていません。

### 感度（コピーで変更）

| 変更 | テスト | 影とハッシュ（ゲート） |
|---|---|---|
| 歩いた位置を読まず tick の始めの行を読む | 40件失敗（`test_movement_walk_order.py`） | 見えません。枝に届きません |
| 着いたとみなす距離 0.05 → 0.0505 | 1件失敗 `test_having_arrived_at_the_end_of_a_trip` | 影0件。ハッシュは未確認（ゲートが影の下で時間切れ） |
| 歩く順を逆に | 41件失敗 | 影45件 |
| 支援の予定し直しの開始 +1 分 | 2件失敗 | 動かしていません |
| その場で閉じる依頼のクールダウン +1 分 | 2件失敗 | 影2件 |
| `walk` の指標を `movement_replan_blocked_count` に | 7件失敗 | 影0件、ハッシュ問題0件。**ゲートの1回の引き直しは支援の枝で、ここを通りません** |
| 支援の枝の指標を同じく | | 影1件（`the worlds`）、ハッシュが `geometry/support-trip-closed` で不一致 |

**0.05 m はテストだけが見ています。** 影の全部を動かしたコピーのゲートは、負荷の下で時間切れになった回があります（上の未確認）。

## 確かめていないこと

- **Windows と macOS では動かしていません。** 新しい C API はありません。
- **順序に依存する枝を比べるのはテストだけです。** ゲートにも7日分にも届かないので、`rule_shadow` は数えるだけです（`advance.in_order_replans`）。独立した列の引き直しは決まった形の4テストと、自分の列の乱数のテストが見ています。乱数のテストは独立した列を作りません。
- `_replan_journeys` のゲートの比較は1回で、枝の大半はテストだけです。
- 水・食事の独立した列に対する引き直しのテストはありません（`resource` の分岐は `purpose` の接頭辞で読むだけで、`write_plan` は列のレーンの既存のものです）。
- `tracked_mirror_gate.py` は動かしていません（指示どおり。`core/state_mirror.py` は変えていません）。

## レーンとの重なり

- **L2：** `rules/travel.rs` は `route` を `pub(crate)` にしたことと、`approach_route` の間取りなしの分だけです。`staff.rs` は触っていません。`_route` の辺の容量が移ったら、`replan.rs` の `route_of` の Python の呼び出しは消せます。
- **L4：** `rules/queues.rs` の `plan_trip` に `sequence` の引数を足し、`Plan`、`Route`、`plan_trip`、`write_plan`、`slot_route`、`BLOCKED_METRIC` を `pub(crate)` にしました。
- `core/row.rs` に `str_value`、`rules/{care_receipts,handlers,tick_validation}.rs` の 1〜3 行。`rules/overlay.rs` に `release_bed`。
