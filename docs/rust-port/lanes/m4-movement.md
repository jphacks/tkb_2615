# M4 レーン：移動の規則（`domain/movement.py`）

毎 tick に動く移動の規則を `native/src/rules/movement.rs` に移しました。`movement_v1` の行はレーン I のものです。結果は変わりません。Python の規則は比較の基準として残ります。**イベントの予定は移していません。** `EventWriter` も `id_counters` も使いません。

## 計測で決めた範囲

`fork-b1` の 9600分から480 tick を cProfile で測りました（µs/tick、プロファイラの分だけ遅く出ます。基準の tick 全体 9,236、1回の値）。

| 規則 | 回/tick | 基準 | レーン | 扱い |
|---|---:|---:|---:|---|
| `_update_movement`（`advance_movement`） | 1 | 780 | 37 | **kernel** |
| `facility_approach_route` | 4.6 | 292 | 55 | **kernel**（設備の点へ行く場合） |
| `start_movement` | 2.0 | 222 | 37 | **kernel** |
| `route_is_physically_clear` | 2.7 | 162 | 2.4 以下 | **kernel** |
| `_queue_slot_position` | 2.1 | 150 | 9 | **kernel**（設備自身の列） |
| `route_with_movement_time` | 2.7 | 102 | 19 | **kernel** |
| `plan_in_room_route` | 6.8 | 102 | 1 | **kernel**（呼び出しは Python の残りの分だけ） |
| `validate_movement_runtime_state` | 1 | 95 | 検証の kernel | **kernel** |
| `finish_movement` | 1.6 | 82 | 11 | **kernel** |
| `progress_physical_queue_positions` | 1.6 | 247 | 68 | Python（葉の計算は kernel） |
| `progress_physical_queue_head_to_service` | 1.2 | 143 | 38 | Python（同上） |
| `_sync_movement_after_events` | 1 | 81 | 18 | Python（`finish_movement` は kernel） |
| `shared_passage_edge_loads` | 1 | 22 | 21 | Python |
| `physical_service_capacity` | 1.6 | 11 | 10 | Python |

レーン全体の tick は同じ計測で 6,378 µs です。部品の足し算で、全体の差として確かめたものではありません。「形が合うか」は M3 と同じで、**計画して書く形に合いました。** 一番重い `_set_stationary_position` の転送（約 100回/tick）は、ビットが同じなら書かないので、動かない人については 0 回になります。

**残る `domain/movement.py`（Python）の毎 tick の分は約 140 µs/tick、tick の約 2%** です（上の Python の行の足し算。`_sync_movement_after_events` を除き、中で呼ぶ kernel の分を含みます）。ほかに残るもの：

- `_schedule_queue_trip`（0.75回/tick）と `_replan_physical_queue_trip`：イベントを割り当てて予定します。
- 独立した FIFO（`facility_queues_v1` の `queue_id`）の枠と、測量された `service_point_id` への経路。`point_for_queue` と列の名前空間を読むので断ります。7日分は0件です。
- `facility_queue_slot_route`、`in_room_travel_minutes`、`agent_is_at_*`、`staff_position_snapshots`（`advance_movement` の外から呼ぶ分）、`agent_positions`、`create_movement_runtime_state`、`canonical_movement_config`。

## 順序に依存する枝

レーン I が見つけた2つの依存は、**kernel では再現せず、断ります。** kernel の計画は tick の始めの行だけを読むので、そこから順序は出てきません。断ると何も変えず、Python の規則がその tick を全部動かします。

| 枝（`advance_movement`） | kernel | 理由 |
|---|---|---|
| 列の旅の引き直し（行き先の変更、設備の移動、部屋の経路の閉塞。`_replan_physical_queue_trip` の4か所） | `Unsupported` | 引き直した順にイベント番号を取り、前の人が歩いたかどうかで枠が変わる |
| 歩く途中で列の枠に置く人（`QUEUED` で `target_id` があり、列の旅でない、または旅が切れた） | `Unsupported` | `_queue_slot_position` が前の人の位置を読む |

- `test_two_queue_replans_go_to_the_python_rule_and_keep_their_order`、`test_a_person_in_a_slot_in_the_walk_goes_to_the_python_rule`（`tests/integration/test_movement_rules.py`）：kernel が何も変えずに断ること、`kernel_first` を通した結果がビットまで Python の規則と同じことを確かめます。
- `tests/integration/test_movement_row.py` の2つのテストは、Python の規則の順序依存を今までどおり固定します（`__wrapped__` を直接呼びます）。この中で動く `_queue_slot_position` と経路の kernel は、Python の規則が書いた行を読みます。
- **1人の呼び出しの枠は kernel が決めます。** `finish_movement` と `_queue_slot_position` は呼ばれた時点の行を読みます。その間にほかの人は動かないので、順序の問題はありません。

ゲートと7日分は、断る枝を1回も通りませんでした（下の表）。**この枝の比較はテストだけです。**

## 間取りをデータとして読む

`rules/movement_geometry.rs` の `Geo::of(world)` です。

- **読むもの：** `_parsed_config` を kernel の呼び出しごとに Python で呼び（約 0.3 µs × 2。範囲の中ではキャッシュから返ります）、返ったオブジェクトの同一性と、上書きの `facility_points`・`partitions` の正準 bytes が同じなら、作った `Geo` を使い回します。
- **作るもの：** 違えば Python の `effective_geometry` と `effective_spatial_constraints` の結果のモデルを読みます。上書きの計算は Python のままで、壊れた設定は例外になり、kernel が `Unsupported` を返して Python の規則が同じ文を出します。
- **範囲の外：** `_parsed_config` が毎回新しいオブジェクトを返すので、毎回作り直します。設定をその場で書き換えても Python の規則と同じものを読みます（テストあり）。
- **持ち方：** プロセスに8個まで。`Plan` が Py のハンドルを持ちますが、行は持ちません。
- `bed_reservations`、`temporary_rooms`、`staff_operations_v1.staff`、`zones`、`zone_edges` は呼び出しの中で Python の dict と dataclass をそのまま読みます。`closed` は実行中に変わるからです。

## 任せるもの（`Unsupported`）

- 型が違う値：int の座標や `progress`（行が taint します）、int の設定値（`movement_model` の速度を含む）、`Point2D` でない点、タプルでない経路
- `due_minute <= started_minute`、0 分以下の辺、知らない辺、経路より短い `route_zone_ids`、絶対値が 1e100 を超える座標、2^53 以上の分
- 世界が行と別のものを持っている（`dict.__setitem__` で表を飛ばした場合）
- `SHELTERSIM_NATIVE=0` と、追跡の session

## モデルの担当の方に見ていただくコード

式と値は変えていません。Rust に移ったものです。

| Python | Rust |
|---|---|
| `advance_movement`、`start_movement`、`finish_movement`、`_set_stationary_position`、`_reserved_bed_point`、`_route_point`、`_point_along_polyline`、`_room_path_is_clear`、`_facility_move_invalidated_journey`、`staff_position_snapshots` の記録、`route_with_movement_time`、`in_room_travel_minutes`、`_max_route_density`、`_queue_slot_position`、`facility_approach_route`、`canonical_movement_config` の確認 | `rules/movement.rs` |
| `_zone_center`、`point_in_polygon`、`plan_in_room_route`、`_plan_in_room_route`、`route_is_physically_clear`、`_room_segment_clear`、`_point_walkable`、`_segment_*`、`_point_segment_distance`、`_polyline_length`、`_distance` | `rules/movement_geometry.rs` |

**Rust に書いた係数：**

- 速さの既定値 独立 72、介助 36、車椅子 48 m/分。混雑 0.12（人/m² あたり）、上限 2.5、混雑が始まる密度 0.5、基準 1.0。最短 1 分。`movement_model` があれば実行時にそちらを読みます。
- 通路の余裕 独立 0.25、介助 0.4、車椅子 0.45 m。経路の節点には +0.05 m。幅と出入口の許容差 1e-9、1e-8。
- 列の間隔 独立 0.6、介助 0.9、車椅子 1.0 m。探す範囲 64 周、横の順 0、1、-1、2、-2。
- 設備の外周の余裕 独立 0.30、介助 0.45、車椅子 0.50 m。
- 許容差と上限：1e-9、1e-10、1e-12、1e-18、1e-8（点の重なり）、節点のマイター 20.0 倍、丸め 8桁、凹んだ部屋の中心の押し込み 1e-7、1e-5、1e-3、1e-2、`MAX_REMEMBERED_ROOM_ROUTES` 4096。

**1e100 の上限は Rust だけのものです**（Python の `Point2D` が例外を出すあふれを避けるため。超えたら Python が動きます）。

## 計測

2026-11-01、WSL2、16コア、ほかのレーンと同時。

`tick_budget.py`（9600分から480 tick、3回）：

| コード | 基準 | 基準との比 | A/A | load average |
|---|---|---:|---:|---:|
| 最後のコード（`045f78f`） | `408ad51` | **0.847**（0.846、0.855、0.847） | 1.000（0.963、1.016、1.000） | 約 20 |
| マージ前（`aa99c74`の次） | `ac275df` | 0.853（0.825、0.853、0.867） | 0.989（0.989、1.023、0.983） | 約 5 |

状態はどの回も3つとも一致。A/A の揺れの外で、約 15% 短くなります。7日分の project の経路は実時間 135.3 秒、スレッドの CPU 時間 103.4 秒です（ほかの確認と同時の1回で、基準とは同時に測っていません）。

## 結果

最後のコード `045f78f`（レーン H、F、社会関係、本体の状態を取り込んだ `408ad51` の上）の結果です。その後の変更はこの文書だけです。道具の確認だけは、マージ前の `2bd688d` のコピーです。

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 3517件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2073件成功、1450件 skip |
| `gate_all_paths.py --expect`（`rule_shadow` の回） | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| `SHELTERSIM_MOVEMENT_REVERSED=1`（`rule_shadow` の回） | 55シナリオ、問題0件、違い0件 |
| 7日分（素の回、`rule_shadow` の回） | どちらも world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致 |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（指示どおり） |

| `rule_shadow`（kernel が動いた / 呼び出し） | ゲート | 7日分 |
|---|---:|---:|
| `advance_movement` | 28,410 / 28,410 | 10,080 / 10,080 |
| `start_movement` | 28,283 / 28,283 | 23,970 / 23,970 |
| `finish_movement` | 10,140 / 10,140 | 9,578 / 9,578 |
| `route_with_movement_time` | 27,534 / 27,534 | 28,700 / 28,700 |
| `route_is_physically_clear` | 24,923 / 24,923 | 1,176 / 1,176 |
| `plan_in_room_route` | 131,582 / 131,582 | 75,004 / 75,004 |
| `queue_slot_position` | 76,979 / 78,876 | 21,374 / 21,374 |
| `facility_approach_route` | 44,760 / 46,137 | 50,889 / 50,889 |
| 検証（受け入れ / 呼び出し。受け入れはすべて Python の規則も通す） | 35,685 / 35,685 | 10,600 / 10,600 |
| `advance_movement` が比べた値 | 6,177,556 | 6,818,634 |
| `start_movement` が比べた値 | 9,494,964 | 16,949,834 |
| `finish_movement` が比べた値 | 3,083,996 | 6,673,358 |
| 行全体と木の比較 | 2,659 | 1,745 |
| 違い | 0 | 0 |
| taint | 0 | 0 |

呼び出しの数は `rule_shadow` の回のものです。読むだけの kernel の中には、影が Python の規則を動かしたことで増えた分があります。

**断り（ゲート）：** `queue_slot_position` の独立した FIFO 1,897回、`facility_approach_route` の測量された点 1,285回、同じく「世界が別の移動の状態を持つ」92回（世界を写して差し替える Scenario と見ていますが、その段階は追っていません）。**`advance_movement`、`start_movement`、`finish_movement` は断り0回です。** 7日分は移動のどの kernel も断り0回です。int の座標による taint はゲートでも7日分でも0回です。

**枝に届いた数（`advance_movement` が tick の始めに持つ旅、延べ）：**

| | ゲート | 7日分 |
|---|---:|---:|
| 部屋の中 | 18,330 | 14,392 |
| 辺の上 | 18,722 | 19,077 |
| 列（`queue_id`） | 448 | 0 |
| 職員の支援 | 2,355 | 1,313 |
| 引き直しを返した | 1 | 0 |

### 影の感度（コピーで 1% 変更。マージ前の `2bd688d`）

| 変更 | 影（`long_generated`） | ハッシュ | テスト |
|---|---|---|---|
| 辺の進み具合を 1.01 倍 | 279件 | world が不一致 | |
| 混雑を掛けた時間を 1.01 倍 | 541件 | world、events、KPI が不一致 | |
| 列の間隔 0.6 → 0.606 | 5,719件 | world、events、KPI が不一致 | |
| 混雑の既定値 0.12 → 0.1212 | 0件 | 一致 | `test_a_route_s_minutes_are_python_s` が失敗 |
| 速さの既定値 72 → 72.72 | 0件 | 一致 | |
| 速さの既定値 36 → 36.36 | | | 同じテストが失敗 |
| 設備の外周 0.30 → 0.303 | 0件 | 一致 | `test_the_walk_to_a_facility_with_a_footprint_is_python_s` が失敗 |
| 部屋の進み具合を 1.01 倍 | 0件 | 一致 | 部屋の歩行のテストが失敗 |
| 通路の余裕 0.25 → 0.2525 | 0件 | 一致 | 乱数の間取りのテストが失敗 |

**影で0件の5つは、ゲートのこのシナリオに届かない値です。** 既定値は生成 Scenario が全部書くので読まれません。ここが影の穴で、テストが補います。テストの列が空の行（72.72）はテストで試していません。

乱数の間取りのテスト（`test_random_floor_plans_are_python_s_bit_for_bit`）は、凹んだ部屋、壁、出入口、障害物の250個の間取りのうち受け入れられたものを `repr` で比べます。

## 網羅

`gate_all_paths.py --coverage` の `domain/movement.py`（kernel ありとなしの合計 / kernel ありだけ）：988行のうち 800行（81%）/ 468行（47%）。Rust の行の網羅は測っていません。上の枝の件数と、`test_movement_rules.py` が比べる枝で見ています。

ゲートに届かず、テストだけが比べるもの：

- 順序の節の2つの枝、`test_a_config_edited_outside_a_scope_is_seen_at_the_next_call`、ベッドの添字が範囲外や bool のとき、歩く人の記録が消えた人。
- **設備の外周（`equipment_footprints`）への経路。** `long_generated` では 0.30 を 1% 変えても差が出ません。`test_the_walk_to_a_facility_with_a_footprint_is_python_s` が比べます。
- 部屋の中の歩行で進み具合が 1 未満の場合（`long_generated` の部屋の旅はほぼ 1 分で、進み具合は 1.0 です）。`test_a_room_walk_follows_its_points_and_stops_at_what_blocks_them` が比べます。
- `movement_model` がないときの既定値（生成 Scenario は全部書いています）。`test_a_route_s_minutes_are_python_s` が比べます。

## 確かめていないこと

- **Windows と macOS では動かしていません。** 新しい C API は `PyDict_DelItem` だけで、stable ABI の中です。`hypot`、`fsum`、`round`、`sum` は `mathx` で、libm の `exp`、`pow` は使いません。
- **順序に依存する枝を kernel は動かしません。** 手順4の並列化には、この枝の扱いがまだ残ります。
- `SHELTERSIM_MOVEMENT_REVERSED=1` の回の `advance_movement` は Python の規則です。kernel の計画は順序を持たないという上の説明は、コードを読んだものとテストで、kernel を逆順に動かしてはいません。
- 記録の丸ごとの置き換えは Python と同じです。**ビットが同じ記録は置き換えません。** 記録のオブジェクトを tick をまたいで持つコードは `src/` に見つかりませんでした（検索だけで確かめました）。キーの順序は、復元後の記録で Python と違うことがあります（結果には影響しません）。
- `tracked_mirror_gate.py` は動かしていません（指示どおり。`core/state_mirror.py` は変えていません）。
