# M4 レーン：設備の列（`domain/queueing.py` と `domain/movement.py` の列の部分）

毎 tick に動く列の規則を `native/src/rules/queues.rs` に移しました。結果は変わりません。Python の規則は比較の基準として残ります。呼び出し1回につき、計画してから書く kernel が1つで、コールバックはありません（列の並べ替えと滞留の記録の削除だけ、リストと dict 自身のメソッドを通します）。

**`facility_queues_v1` の状態と規則は移していません。** 下の調査のとおり7日分で0回です。

## 調査で決めた範囲

回数は7日分の project の経路とゲート全体の実数です。µs は `fork-b1` の 9600分から480 tick、スレッドの CPU 時間、基準2本とレーンを同時に3組（load average 13〜16）の幅です。

| 規則 | 7日分の回数 | ゲートの回数 | 基準 µs/tick | レーン µs/tick | 扱い |
|---|---:|---:|---:|---:|---|
| `prepare_facility_queue` | 90,720（列あり 19,832） | 144,247 | 47〜64 | 22〜24 | **kernel** |
| `progress_physical_queue_positions` | 18,556 | 22,120 | 46〜58 | 21〜22 | **kernel** |
| `progress_physical_queue_head_to_service` | 13,067 | 10,425 | 31〜39 | 20〜22 | **kernel** |
| `_schedule_queue_trip`（上の2つから） | 9,335 | 13,799 | 上の内数 | 上の内数 | **kernel の中** |
| `shared_passage_edge_loads` | 10,080 | 28,869 | 14〜18 | 19〜23 | Python |
| `physical_service_capacity` | 19,518 | 33,739 | 6〜7 | 8〜9 | Python |
| `_abandon_and_retry` | 4 | 168 | | | Python（kernel は断ります） |
| `ToiletServiceManager.request` | 2,464 | 2,893 | `cProfile` で 6.8 | 同じコード | Python |
| `complete_toilet_service` | 2,460 | 3,247 | `cProfile` で 4.9 | 同じコード | Python |
| `enqueue`、`start_service`、`mark_stall`、`choose_queue_id` | **0** | 146、136、417、146 | | | Python |
| `prepare_independent_facility_queue`、`_arbitrate_independent_facility` | **0** | 4,200、1,200 | | | Python |

**kernel にして、やめた2つ：**

- `shared_passage_edge_loads`：kernel は 13〜17 → 89〜123 µs/tick になりました（同時に動かした3組）。Python が予定したイベントすべてから行を読むためです（待っているイベントごとに `EventRow::from_event`）。Python の規則は `pending()` のタプルを `event_type` で読むだけです。
- `physical_service_capacity`：差が 1 µs/tick 未満でした（`Geo::of` の `_parsed_config` 2回で、Python の規則の分がほぼ消えません）。

Python のままの2つがレーンで少し増えるのは（上の表）、Rust が予定した行のオブジェクトを `pending()` が作るからと見ています（確かめていません）。

**`facility_queues_v1` を持たない理由：** 7日分は名前空間そのものがなく、ゲートでも 31,532 tick のうち 600 tick です。書き手を残して持ち主だけ宣言すると転送の費用が増えるだけで、書き手を移しても生成 Scenario での得は0です。独立した列を持つ Scenario が実際に使われるようになったら移します。読む kernel は `queue_id` があれば断るので、持ち主を変える作業はこのレーンと競合しません。

**残る列の Python（`fork-b1` の `cProfile`、µs/tick）：** `ToiletServiceManager.arbitrate` の本体 12、`independent_queues_required` 26（検証と `is_legacy_queue_checkpoint` から）、`shared_passage_edge_loads` 25、`physical_service_capacity` 12、`facility_has_independent_queues` 8、`agent_is_at_*` 12、`request` 7、`complete_toilet_service` 5。計 約 105 µs/tick、tick の約 1.7% です（`ResourceServiceManager.arbitrate` の本体は別レーンの `domain/resource_service.py` なので入れていません）。

## 順序に依存する枝

移動のレーンの決めたことを変えていません。kernel は呼ばれた時点の行を読み、書いてから戻ります。呼ぶ順は Python の `arbitrate` のループのままです。

- `_replan_physical_queue_trip` と `advance_movement` の断る枝は Python のままで、`_schedule_queue_trip` の Python の本体もその分だけ残ります。
- `test_movement_rules.py` と `test_movement_row.py` の順序のテストは、そのまま通ります。
- `--reversed-order` は55シナリオとも一致しました。Rust の中の行の順序は逆転の影響を受けないので、kernel の順序を試したものではありません。kernel の中で順序が効く場所は、`active_tasks` の順に読む `tasks`（人ごとに最後の1つ）と列の最初のタスク（`_schedule_queue_trip`）です。どちらも1人に1つのタスクなら順序に関係しません。

## 任せるもの（`Unsupported`、何も書く前）

- 独立した FIFO（`queue_id` あり）。
- 列を離れる人がいる（`should_abandon` が真）。その呼び出しは全部 Python の規則です。
- 別の queue モデル、範囲外や型の違う `facility_queues` の設定（`selected_models` が例外の場合を含む）。
- 列に同じ人が2回いる、ASCII でない設備の種類（`casefold` が変えるかもしれないため）、列の種類でない設備。
- 滞留の記録があり `facility_queues_v1` がある場合（先頭の決め方が別のため）。
- 型が違う値、taint した行や `metric_state`、一致するタスクがない（Python が自分の文で失敗します）、イベントの ID か順番がすでに使われている。

## モデルの担当の方に見ていただくコード

式と値は変えていません。

| Python | Rust（`rules/queues.rs`） |
|---|---|
| `prepare_facility_queue`、`_service_name`、`_drop_stale_resource_stall`（先頭の判定） | `prepare_facility_queue`、`service_of`、`stale_stall` |
| `NeedWeightedPhysicalQueuePolicy.__init__`、`priority`、`should_abandon`、`_service_need`、`create_queue_policy` の選び方 | `policy_of`、`priority`、`abandons`、`need_of` |
| `progress_physical_queue_positions`、`progress_physical_queue_head_to_service`、`facility_queue_slot_route`、`_schedule_queue_trip` | 同じ名前の kernel、`slot_route`、`plan_trip` |

**Rust に書いた係数：** 列を離れる待ち時間の既定値 トイレ 120、水 240、食事 360 分。残る必要度の既定値 0.8。待ちの重み 0.1、1440 分。枠に立っているとみなす距離 0.05 m。経路の点の重なり 1e-8（移動のレーンと同じ）。設定の範囲 1〜10080、1〜1440、0〜1。`retry_delay_minutes` の既定値 15 は Rust にありません（列を離れる場合は Python です）。

`movement.rs` の `facility_approach_route` と `start_movement` は、計画の部分を関数に分けただけです（`approach`、`room_minutes`、`plan_start`）。

## 計測

2026-11-01、WSL2、16コア、ほかのレーンと同時。基準は `54d64b1`（レーン M4 移動の先頭）。

| `tick_budget.py`（9600分から480 tick、3回） | 基準との比 | A/A | load average |
|---|---:|---:|---:|
| 最後のコード 1回目 | **0.971**（0.971、0.991、0.947） | 1.023（0.993、1.023、1.235） | 8〜16 |
| 最後のコード 2回目 | **0.968**（0.968、0.976、0.965） | 1.030（1.03、0.941、1.071） | 3〜5 |

状態はどの回も一致しました。**tick 全体の差は A/A の揺れより小さいので、「速くなった」とは言えません。** 確かな差は上の部品の表で、3つの kernel で約 80 µs/tick 減り、Python のままの2つで約 6 µs/tick 増えて、差し引き約 1.5% です。部品の足し算で、全体の差として確かめたものではありません。

7日分の素の回：実時間 144.8 秒、スレッドの CPU 時間 95.0 秒（ほかの確認と同時の1回で、基準とは同時に測っていません）。

## 結果

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 3,585件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,073件成功、1,518件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| ゲートの `rule_shadow`（全部の影） | 55シナリオ、問題0件、違い0件 |
| 7日分 | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致（素の回、影の回） |
| 7日分の `rule_shadow` | **移動と列の影だけ。** 全部の影は約 8,900 秒かかるため（レーン M4 移動の回） |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（指示どおり。`core/state_mirror.py` は変えていません） |

| `rule_shadow`（kernel が動いた / 呼び出し） | ゲート | 7日分 |
|---|---:|---:|
| `prepare_facility_queue` | 144,080 / 144,247 | 90,716 / 90,720 |
| `progress_physical_queue_positions` | 21,288 / 22,120 | 18,556 / 18,556 |
| `progress_physical_queue_head_to_service` | 10,056 / 10,425 | 13,067 / 13,067 |
| 比べたイベント | 13,326 | 9,335 |
| 比べた値 | 18,704,110 | 35,761,963 |
| 違い | 0 | 0 |

**断り：** 7日分は `prepare_facility_queue` の4回だけで、すべて列を離れる人です（調査の `_abandon_and_retry` 4回と同じ）。ゲートは列を離れる人 167回（調査の `_abandon_and_retry` は 168回で、差の1回は `prepare_independent_facility_queue` の経路と見ていますが、追っていません）、独立した FIFO 1,201回です。

**枝に届いた数：**

| | ゲート | 7日分 |
|---|---:|---:|
| 列が空（`prepare`） | 112,273 | 70,888 |
| 並べ替え | 1,108 | 465 |
| 滞留の記録の削除 | 55 | 166 |
| 枠へ歩く旅 / 設備の点へ歩く旅 | 8,381 / 4,945 | 2,737 / 6,598 |
| `physical_queue_route_blocked_count` が増えた（枠 / 点） | 1,165 / 4 | 0 / 0 |

`SHELTERSIM_MOVEMENT_REVERSED` の回は動かしていません（このレーンの kernel に関係しません）。

### 影の感度（コピーで変更）

| 変更 | 影 | ゲートのハッシュ | テスト |
|---|---|---|---|
| 待ちの重み 0.1 → 0.101 | 0件（`long_generated`） | 一致 | 5件失敗 |
| 1440 → 1454.4 | 0件 | 一致 | 4件失敗 |
| トイレの既定 120 → 121 | 0件 | 一致 | 1件失敗 |
| 水の既定 240 → 242 | 0件 | 一致 | 1件失敗 |
| 食事の既定 360 → 364 | 0件 | 一致 | 1件失敗 |
| 残る必要度の既定 0.8 → 0.808 | 0件 | 一致 | 3件失敗 |
| 枠の距離 0.05 → 0.0505 | 0件 | 一致 | 1件失敗 |
| 枠までの距離を 1.01 倍 | 0件 | 一致 | 最初は0件。`test_a_walk_s_minutes_turn_over_where_python_s_do` を足して1件失敗 |
| 優先度を逆にする（道具の確認、1% ではない） | **940件**（ゲート全体） | 不一致 | 8件失敗 |
| 列を離れる境界を1分ずらす（設定の値の読みの確認、1% ではない） | **32件**（ゲート全体） | 不一致 | 4件失敗 |

**1% の変更は、8つすべて影とハッシュに届きません。テストだけが見つけます。** 既定値は生成 Scenario が設定に全部書くので読まれません。重みは同点に近い2人の順序を変えるときだけ効き、`long_generated` にそのような2人はいません。距離の 1% は分への切り上げに隠れます。テストは、変わる境のすぐ両側の値を作って比べます。影が比較できていることは下の2行で確かめました。`long_generated` 以外のシナリオで 1% を試してはいません。

## 網羅

`gate_all_paths.py --coverage`（kernel ありとなしの合計 / kernel ありだけ）：`domain/queueing.py` 142行のうち 119行（84%）/ 110行（77%）、`domain/facilities.py` 211行（86%）/ 同じ、`domain/facility_queues.py` 168行（86%）/ 同じ、`policies/queueing.py` 56行（86%）/ 52行（80%）、`domain/movement.py` 802行（81%）/ 462行（47%）。Rust の行の網羅は測っていません。上の枝の件数と `test_queue_rules.py` で見ています。

## 確かめていないこと

- **Windows と macOS では動かしていません。** 新しい C API はありません。`hypot`、`fsum`、`max`、`min` は `mathx` で、libm の `exp`、`pow` は使いません。
- 列を離れる判定と設定の読みは、Python の `create_installed_model` を通しません。同じ ID で別の factory を登録すると kernel は気づきませんが、`registry.py` が同じ ID の別の定義を断ります。
- 列の Python の `queue[:] = ...` と滞留の `del` は、リストと dict 自身のメソッドを呼びます。レーン M4 資源が `resource_service_v1` を持つようになっても、転送は上書きしたメソッドを通るので動くはずですが、統合した状態では試していません。
- 7日分の影は移動と列だけです。ほかの影はゲートだけで動かしました。
