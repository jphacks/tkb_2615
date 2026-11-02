# L2 レーン：職員の割り当ての残り

手順3の仕上げです。職員が仕事を取る tick も Rust が最後まで動かします。**結果は変わりません。新しい係数はありません。** 手順4の棚卸しで7日分の断り 724回のうち 718回がここでした。**7日分の断りは6回になり、残りはすべて他のレーンのものです（下）。**

## 移したもの

| もの | Rust | Python |
|---|---|---|
| 仕事を取るかの判断 | `rules/staff.rs` の `assign_none`（`None` で `assign_work` に渡す。断りません） | `StaffService.assign_pending` |
| 割り込みの作業の中断 | `preempt_worker` から `interrupt_operation` を直接呼ぶ | `_preempt_background_staff` |
| 職員の作業の開始 | `assign_operations`、`plan_operations`、`write_operations`、`open_tasks`、`workload_of` | `assign_staff_operations` |
| 交代の予定 | `relief`、`plan_relief` | `schedule_staff_operation_relief` |
| 経路を読む待ちの理由 | `kind_wait`、`routed_reasons`、`route_wait_reasons`、`RouteReads` | `support_wait_reason_codes`、`_route_candidates`、`_support_route_wait_reasons` |
| 通路の混み具合 | `rules/travel.rs` の `passage_edge_loads`、`active_edge_loads`、`current_edge` | `shared_passage_edge_loads`、`_active_route_edge_loads`、`_current_journey_edge` |
| 辺の容量のある経路 | `route_for`、`shortest`、`CapacityWait`、`write_capacity_wait` | `_route`、`_record_route_capacity_wait` |
| 支援の移動の予定 | `schedule_support_travel`（`segments.py` に1行） | `_schedule_staff_support_travel` |
| 受領記録と苦情の driver からの呼び出し | `support::record_assigned`、`staff::consider_due_complaints` を直接 | `_record_assigned_support`、`_consider_due_complaints` |

`rules/staff.rs` の `FxHashMap` で区域を回していた箇所（旧 1205行）は `BTreeMap` にしました。区域の ID の順で、`sorted(by_zone.items())` と同じです。今も `FxHashMap` なのは `update_operations` の区域の人数と `workload_of` の件数で、どちらも引くだけで順に回しません。

## 決めたこと

- **計画してから書きます。** 開始と交代は、行だけ読んで全員分を決めてから、Python の入れ物へ1項目ずつ書きます（M6 と同じ）。名前空間全体の書き込みはありません。決める途中の例外は `refused_by_python` で断りになり、何も変わらず Python の規則が同じ文を出します。
- **`assign_none` は断らず `None` を返します。** 依頼を取る tick は異常ではないからです。空いた職員、優先度のある依頼、書き込みを報告する依頼、`validate_state`、分の不一致はすべて `assign_work` に渡します。分の不一致だけは `assign_work` が断り、Python の規則が `ValueError` を出します。
- **辺の容量の待ちの記録は、計画の間は書きません。** `_select_toilet` は計画の中で `_route` を呼びます。そこで書くと、後で断ったときに Python の規則がもう一度書きます。`route_for` は `waits` に積み、書く段階で `write_capacity_wait` が書きます。旅の連番がその分だけ後になるので、`EventWriter` に `reserve` と `allocate_reserved` を足しました。
- **`assign_work` の `route_resolver`、`travel_scheduler` は callback のままです。** `StaffService` はテストなどで別の関数を受け取るからです。Run では `_route`、`_schedule_staff_support_travel` の kernel に届きます。
- **`_support_route_wait_reasons` のグラフは世界から Rust で読みます（`world_graph`）。** `ZoneGraph.from_states` が拒む値は断ります。

## 断りの回数（棚卸し。`SHELTERSIM_REFUSAL_CENSUS`）

| 理由 | 棚卸し ゲート / 7日分 | 今 ゲート / 7日分 |
|---|---:|---:|
| `a free worker can take a request` | 3,139 / 712 | **0 / 0** |
| `a qualified worker is free, so routes are read` | 2,313 / 4 | **0 / 0** |
| `a free worker can patrol` | 0 / 2 | **0 / 0** |
| `a staff member has a task` | 265 / 0 | **0 / 0** |
| `a free worker may repair` | 7 / 0 | **0 / 0** |
| `edge capacities look at every trip under way` | 3 / 0 | **0 / 0** |
| `a free worker can clean`、`can reach out`、`an urgent request may preempt`、`a pending request reports its writes`（テストだけ） | 0 / 0 | 文ごとなくなりました |

driver の `kernels_refused` はゲート 6,280 → **2,734**、7日分 720 → **6**。7日分の6回は `someone gives up on the queue` 4（L4）、`the namespace has no row` 1（L1）、`a request closed at once` 1（L3）です。driver が動いた tick はゲート 28,892 / 31,530、7日分 10,080 / 10,080 で、変わりません。

**残るこのレーンの断り：** 職員のテストを棚卸しにかけると、`a fatigue is not finite`、`another staff operations formula`、`the minute is not the world's` の各1回だけで、3つとも壊れた状態か未知の式です。`preempt` の断りは0回です。ゲートと7日分では `rules/staff.rs` の断りは0回です。なお、テスト全部の棚卸しはしていません（職員の5ファイルだけ）。

## 比較

`benchmarks/rule_shadow/shadow_staff.py` を広げました。`assign_operations` と `relief` は、Python の規則が何か始めるなら、世界を2つ写して両方を動かし、結果と文書全体を比べます。`_route` は容量のある controller で同じことをします（`route_capacity`）。`snapshot_fields` の中で読む待ちの理由も比べます。単独で動かすときは `python_path_only` にします。

| `rule_shadow` | ゲート（呼び出し / kernel が動いた） | 7日分 |
|---|---:|---:|
| `assign_operations` | 31,530 / 31,530（断り0）。うち始めた回 7、始めた作業 7 | 10,080 / 10,080。**始めた回 0** |
| `relief` | 31,530 / 31,530。**交代を予定した回 0** | 10,080 / 10,080。始めた回 0 |
| `assign_none` | 31,530 / 28,391、`assign_work` へ 3,139 | 10,080 / 9,368、`assign_work` へ 712 |
| `assign_work`（`shadow_handlers_rest.py`） | 3,139 / 3,139、割り当て 2,391 | **比べていません**（下） |
| `wait_snapshot` | 全部の影 39,546 / 39,546 | 10,414 / 10,414 |
| `_route`（容量あり） | 9回。**待ちの記録まで行った回 0** | 0回 |
| 違い | **0** | **0** |
| 比べた値 | `assign_work` 215,390,504、`assign` 299,035、待ち 780,821 | 待ち 936,942、`assign_none` 5,465,020、`update` 977,760 |

**どの回か（はっきり書きます）：**

- **全部の影のゲートは `1b294c4` です。** 55シナリオ、問題0件、違い0件。提案（`apply_effect`）と指標の影はこの回だけです。その後の Rust の変更は `preempt_worker` が `interrupt_operation` を直接呼ぶ1か所だけで、ゲートはそこを通りません。
- **最後のコードの影のゲートは `staff` と `handlers_rest` の2つです。** `SHELTERSIM_RULE_SHADOW_ONLY` に `proposals` も入れましたが、`shadow_proposals` は `shadow_handlers` の準備が先に要るので起動時に落ち、続く `metrics` と一緒に動いていません。上の表の `wait_snapshot` のゲートの数は全部の影の回のものです。
- **7日分の影は `staff` だけ、`timeout 3600` の中で最後まで動きました**（`Error in sitecustomize` なし）。**7日分の `assign_work` 712回は影で比べていません。** ハッシュと M6 の3日分（315回）だけです。

**7日分は職員の作業が1回も始まりません。** 巡回の候補が出る2回も、期限が Run の終わりを超えて `continue` します。7日分で新しい経路が動くのは `assign_work` と、待ちの理由の経路（`assignment_not_attempted_at_sampled_minute` 23、`insufficient_run_time` 34）だけです。開始はゲートの7回とテスト、交代の予定、待ちの記録、割り込みはテストだけです。

**driver の経路：** `first_difference.py --both`（driver と `SHELTERSIM_NATIVE=0`）を55シナリオ全部で、段階の前後の digest 220,892か所、違い0件（`1b294c4`）。上と同じ理由でその後は再実行していません。割り込みは `test_an_urgent_request_preempts_a_routed_task_in_a_run` が、driver と Python の経路の最後の状態を比べます。**ゲートのシナリオは足していません**（優先度は Provider の境界からしか入らないので、Scenario で作れません）。

## 感度（コピーを1か所変更）

変更はスクラッチのコピー1か所。ゲートは `--only model__staff_operations --only operational`（`maintenance_reason` だけ `--only interventions`）で、全部は通していません。影は `staff` だけです。

| 変更 | テスト | ゲートのハッシュ | 影 |
|---|---:|---|---:|
| 巡回の緊急度 `* 0.5` を 1% | 1件失敗 | 一致 | 0 |
| ローテーションの 240分を 1% | 1件失敗 | 一致 | 0 |
| `round(1000 * fatigue)` を 1% | 2件失敗 | 一致 | 0 |
| 同点の順を逆に | 1件失敗（足した後） | 記録なし | 0 |
| `staff-travel-first-v1` を逆に | 5件失敗 | 一致 | 0 |
| 理由の書式 `:g` を外す | 1件失敗（足した後） | 一致 | 0 |
| 最も仕事の多い交代を選ぶ | 2件失敗 | 一致 | 0 |
| 待ちの run の終わり `>=` → `>` | 1件失敗 | KPI 不一致 | 4 |
| 到着中の人を数えない | 1件失敗 | KPI 不一致 | 4 |
| 容量 `>=` → `>` | 2件失敗 | 一致 | 0 |
| 通路の割合を 1% | 1件失敗（足した後） | 一致 | 0 |
| 待ちの連番を予約しない | 1件失敗（足した後） | 一致 | 0 |
| driver の受領記録を飛ばす | 1件失敗 | 異常終了 | 0 |
| 修理の理由の文を変える（`interventions`） | 3件失敗 | world、events 不一致 | 14 |

**「足した後」の4つは、最初はどの確認にも見えませんでした。** 変更がすべて通ったので、見えるテストを足しました。**係数の 1% は影とハッシュに届きません。** ゲートで作業が始まるのは7回で、閾値や順がぎりぎりの回がないからです。テストが両側の値で見ます。テストの列は `test_staff_assignment.py`、`test_staff_rules.py`、`tests/unit/domain/test_staff*.py` の結果です。

## 結果

2026-11-02、WSL2、16コア、ほかのレーンと同時。load average 23〜82。ネイティブは `ae01fe3`（その後の変更はテストと文書だけ）。

| 確認 | 結果 |
|---|---|
| 全テスト（`ae01fe3` の次の `b0c86fb`） | Rust あり 4,458件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,385件 skip |
| 全テスト（最後のコード `56b155b`） | Rust あり 4,461件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,388件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件（`tick_probe` が入るので Python の経路。driver の逆順は通っていません）。最初の回は `timeout 1800` で切れ、5400 にして通しました |
| 7日分（`--expect`、driver が動く回） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致 |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません。** `core/state_mirror.py` は変えていません（指示どおり） |

`tick_budget.py`（9600分から480 tick、3回、基準 `585d9f9`、レーン `1b294c4`）：**0.993**（0.993、1.097、0.986）。A/A 0.964（0.964、0.912、1.017）。状態は3つとも一致。load average 約 50〜80 で揺れが大きいです。上限の中です。**部品ごとの費用は測っていません。** 依頼を取る tick が kernel の中で終わるので、7日分では少し短くなるはずですが、測ってはいません。

## Python に残した規則（手順4で Rust が要るもの）

- **LLM の境界（指示どおり Python）：** `_prioritize_support_request`、`consider_complaint_response` など `consider_*`。
- **設定と契約（名前で呼びます）：** `create_staff_operations_formula`（`model_id` と `source_id` を読みます）。
- **ほかのモジュールの規則（名前で呼びます）：** `staff_position_snapshots`（`movement_v1` があるとき。開始の最後）。L3 の範囲です。
- **Python の規則が理由を言うもの：** 下の「Rust だけの条件」。`interrupt_operation` が断ったときの `interrupt_staff_operation.__wrapped__`。
- **`assign_work` の callback の Python の入口：** `_route`、`_schedule_staff_support_travel` のメソッド（中は kernel です）。
- **tick の検証：** 作業中の職員がいる `validate_staff_operations_state` は L7 です（`staff_operations_v1: its kernel did not take it`）。
- **このレーンで触っていないもの：** `request`、`complete_support_task`、`timeout_support_request` の Python の本体（kernel が先に動きます）、`order_staff_operation`、`validate_staff_operation_order`、`interrupt_staff_trips_on_closed_routes`、`canonical_staff_operations_config`、`create_staff_operations_state`、`add_staff_operations_agent`。

## モデルの担当の方に見ていただくコード

**新しい係数はありません。** 判断の規則が Python と Rust の2か所になりました。

| Python | Rust | 中身 |
|---|---|---|
| `assign_staff_operations` の順 | `workload_of` | 240分、`round(1000 * fatigue)`、ID |
| 候補 | `open_tasks` | 既定の閾値 0.85、0.45、0.6、`1 - 衛生度`、不足数、平均の不確かさ、`平均ストレス * 0.5` |
| `rank_key` | `plan_operations`、`ranks_above` | `staff-travel-first-v1` は移動時間が先。同点は種類、区域、対象 |
| 理由の文 | `g_text` | `:g` の書式（`format_g` でテスト） |
| `schedule_staff_operation_relief` | `plan_relief` | 次の分の勤務、残り時間、最も仕事の少ない交代 |
| `_support_route_wait_reasons` | `route_wait_reasons` | 既定の支援時間 10分、区域の定員、到着中の人数 |
| `_current_journey_edge` | `current_edge` | `elapsed / duration * total` |
| `_route` の容量 | `shortest`、`route_for` | `負荷 >= 容量` の辺を通らない |

**Rust だけの条件（超えると Python が動きます）：** 2^53 以上の分、移動時間、不足数。i64 を超える分。有限でない fatigue、不確かさ、ストレス。`1000 * fatigue` が i64 に収まらないとき。`staff-urgency-first-v1`、`staff-travel-first-v1` 以外の式。キューのイベントの `route_edge_ids` がリストでないとき。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。`EventWriter` に `Cell` を足しました。
- **ゲートと7日分で届かない枝があります：** 交代の予定、辺の容量の待ちの記録、計画の中の待ち、割り込み。**テストの小さな世界でだけ Python と比べています。** 実の Run の形では比べていません。
- **`passage_edge_loads` は `movement_v1` の旅、キューの旅、職員の旅の順に数えます。** 数えるだけなので順序は結果に出ません。`route_edge_ids` がリストでない値は断ります（Python は `_active_route_edge_loads` で読み飛ばします）。
- **`EventWriter::reserve` は計画の途中の連番の予約です。** ほかのレーンが `select_*` の後で同じ `writer` に別のイベントの連番を計画すると、順が崩れます。`test_a_trip_chosen_past_a_full_edge_takes_its_sequence_after_the_wait` が見るのはトイレの経路だけです。資源の経路はテストがありません。
- **`RouteRows` を `SpaceRouter` の属性に置きました（`_route_rows`）。** controller と同じ置き方です。router を deepcopy するコードがあると失敗しますが、今はありません。
- **断ったときに `_consider_due_complaints` の Python の規則へ戻る枝は、ゲートと7日分で0回です。**
- 行の網羅は測っていません。Rust の行の網羅も測っていません。
- メモリは測っていません。

## レーンとの重なり

- **L3 の範囲を編集：** `rules/proposals.rs`（`TripEffect.waits`、`select_*` の引数、書く段階の3行）。L3 が同じ行を変えると衝突します。
- **ほかに編集：** `state/events.rs`（`EventWriter`）、`rules/support.rs`（`record_assigned` の切り出し）、`rules/tick.rs`（`arbitrate` の2か所）、`rules/staff_operation_events.rs`（公開だけ）、`experiments/phases/driver.py`、`segments.py`（各行の追加）、`tests/support/python_rules.py`、`tests/integration/test_service_rules.py`（テスト1件）。
- `DRIVEN_STEPS` に `_record_assigned_support`、`_consider_due_complaints` を足しました。これらを patch すると、その tick は Python の経路を通ります。
