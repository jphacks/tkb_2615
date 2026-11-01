# M6 レーン：形状の上書き、仮設の空間、独立した列

`domain/temporary_spaces.py`、`domain/geometry_overlay.py`、`domain/facility_queues.py` の規則を Rust に移しました。`facility_queues_v1` は Rust の型付きの行が持ちます。列と提供の kernel は `queue_id` を断らなくなりました。`_apply_effect` の仮設の部屋の `sleep` も kernel で動きます。**結果は変わりません。係数の値は変えていません。** Python の規則は比較の基準として残ります。

**このレーンの価値は速さではありません。** 手順4で Python の規則を消すために、規則ごとの Rust の経路を揃えることです。

**比較の基準について、はっきり書きます。7日分は、ここで移した規則のほとんどに届きません。** 7日分に部屋も独立した列もなく、床図の変更もありません。7日分で実際に比べたのは、読むだけの `capacity_metrics`、`effective_total_zone_capacity`、`facility_has_independent_queues`、`queue_head_agent_ids`、`independent_queues_required`、`is_legacy_queue_checkpoint`、`release_sleep_bed`（持っていない人だけ）、空の状態の3つの検証だけです。**部屋、ベッド、受け入れ、設備の置き場所、床図の変更と期限、一時空間の書き込み、独立した列のすべては、ゲートとテストでしか比べていません。** しかもゲートでの回数は少なく、下の表のとおりです。

## 移したもの

| もの | Rust | Python（比較の基準） |
|---|---|---|
| 一時空間 | `rules/overlay.rs` | `register_`、`expire_temporary_space`、`capacity_metrics`、`effective_zone_capacity`、`effective_total_zone_capacity`、`incoming_travelers`、`validate_temporary_spaces_state` |
| ベッドと受け入れ | 同 | `reserve_sleep_bed`、`release_sleep_bed`、`available_temporary_room`、`admit_to_temporary_room`、`release_temporary_room_admission`、`room_admission_point` |
| 設備の置き場所 | 同 | `installed_facility_placement_enabled`、`installed_facility_point`、`place_installed_facility` |
| 床図の変更 | 同 | `validate_geometry_change`、`apply_geometry_change`、`validate_geometry_overlay_state`、`_require_service_staff_in_zone`、`_zones_are_connected` と `region/facility.py` の幾何の部品 |
| 実効の間取り | `rules/movement_geometry.rs` の `Geo::of` | `effective_geometry`、`effective_spatial_constraints`。今まで Rust から呼んでいました。今は Rust が作ります。サービス点も持ちます |
| 行 | `state/facility_queues.rs`（`OWNED_NAMESPACES` に1行） | |
| 列の規則 | `rules/facility_queues.rs` | `independent_queues_required`、`is_legacy_queue_checkpoint`、`create_facility_queue_state`、`validate_facility_queue_state`、`choose_queue_id`、`queue_ids_for_facility`、`facility_has_independent_queues`、`queue_head_agent_ids`、`request_for_task`、`request_for_agent`、`queue_agent_ids`、`enqueue`、`start_service`、`finish_service`、`mark_stall`、`record_queue_transition` |
| 独立した列の移動 | `rules/queues.rs`、`rules/movement.rs` | `prepare_independent_facility_queue`、`progress_physical_queue_*` の `queue_id`、`_queue_slot_position` の `queue_id`、`facility_approach_route` の `service_point_id` |
| 独立した列の提供 | `rules/service.rs` | `request`、`complete`、`_facilities_to_serve` の独立した列、`_serve_independent_head`、`_record_independent_queue_stall` |
| 独立した列のトイレ | `rules/toilet.rs` | `request`、`_serve_independent_head`、`_facilities_to_serve`、`ToiletServiceCompletionHandler.apply` と `complete_toilet_service` の独立した列と職員の予約 |
| 着いたあと | `rules/travel_completion.rs` の `arrive` | `_after_arrival` の独立した列の枝。`facility_has_independent_queues` も直接呼びます |
| 判断の効果 | `rules/proposals.rs` の `plan_sleep` | `_apply_effect` のベッドの枝。独立した列の世界の旅（断るのは同じ区域ですぐ列に入る場合だけ） |

`events/effects.py` のハンドラーの kernel（`rules/effects.rs`）は、`apply_geometry_change`、`place_installed_facility`、`register_`、`expire_temporary_space` を名前で呼ばず、Rust の関数を直接呼びます。

## 決めたこと

**Python の手順の分割：** 水と食事、トイレの `_arbitrate_independent_facility` の1回分を、それぞれ `_serve_independent_head` に切り出しました（計算は同じ）。ループと `prepare`、`progress` の順は Python のままで、`arbitrate` と同じ形です。

**読み書き：** `facility_queues_v1` は行を読み、木のメソッドで書きます（転送で行に届きます）。`geometry_overlay_v1`、`temporary_spaces_v1` は `V` の行のままで、木を読み、木のメソッドで書きます。イベントはまれで、名前空間全体の書き込みはありません。手順4の前に行を読む形へ変える余地はありますが、この手順では費用になりません。

**計画してから書く：** すべての kernel は、Python が断る値、1つでも迷う値で何も書く前に `Unsupported` です。文は Python が出します。`reserve_sleep_bed` は Python では断る前に `bed_reservations` を `setdefault` します。キーがない古い形は kernel が断ります。

**契約：** `model_validate`（`RouteAccessParameters`、`MoveFacilityParameters`、`PartitionParameters`、`EquipmentTransferParameters`、`TemporaryRoomGeometry`、`FacilityQueueTransitionReceipt`）と `TemporaryRoomGeometry.model_dump` は、Python の規則と同じ場所で Python を呼びます（M6 効果のレーンと同じ、設計で Python に残すもの）。`validate_geometry_overlay_state` の `_GeometryOverlayState` だけは、毎回呼ぶ理由がないので形を Rust で確かめ、確かでないものは断ります。

**影の分け方：** `shadow_overlay.py` は、書く規則を世界の写し2つで比べます。`domain/facility_queues.py` の書く規則は別の規則の途中で呼ばれ、世界がまだ検証を通らないので、名前空間の写しの上で比べます。ハンドラーから直接呼ばれる分は `shadow_effects.py` と一緒に動かしたときに比べられます（Python のハンドラーの中の呼び出しがこの影を通るため）。`shadow_proposals.py` は仮設の部屋のある `sleep` を Python 先の比較に移しました。kernel が先だと移動の状態を変えてしまい、比べられないためです。

## Python の規則で見つけたこと（変えていません）

- **`validate_geometry_change` の `transfer_equipment`：** 足跡のある設備で障害物（仕切りを含む）が1つでもあると、`item.facility_id` が `SpatialObstacle` にないので `AttributeError` になります。「重なり」の判定に届きません。kernel は断るので Python と同じ例外です。判断はモデル担当の方に任せます。

## Python に残した規則（手順4で Rust が要るもの）

- **`domain/facilities.py`：** `arbitrate` と `_arbitrate_independent_facility` の Python のループ。
- **`domain/queueing.py`：** `_abandon_and_retry`。列を離れる人がいる `prepare_*` は全部 Python です（独立した列でも同じ）。
- **`domain/resource_service.py`：** `cancel`、`interrupt_for_staff`（`requeued` を含む）、`arbitrate` のループ、`_arbitrate_independent_facility` のループ。
- **`domain/movement.py`：** `physical_service_capacity`（Python の呼び出しの分。提供の kernel の中は Rust）、`_replan_physical_queue_trip`、`agent_is_at_queue_slot` など。
- **`experiments/phases/propose.py`：** 同じ区域で独立した列にすぐ入る枝（`toilet_service.request`、`resource_service.request` を呼びます。時間の掛からない移動と同じ断り）。`experiments/phases/update.py` のベッドの再計画（`reserve_sleep_bed` は kernel、手順は Python）。
- **`domain/geometry_overlay.py`：** `effective_geometry`、`effective_spatial_constraints`、`parsed_facility_geometry`、`_parsed_config`、`create_geometry_overlay_state`。pydantic のモデルを返すので、Python の呼び手が残る間は要ります。Rust の規則は `Geo` を読みます。`_validate_*`、`_polygons_overlap` などの部品は `rules/overlay.rs` に同じものがあります。
- **`domain/facility_queues.py`：** `service_points`、`point_for_queue`（同じくモデルを返す）。
- **`create_temporary_spaces_state`：** 世界を作るときの定数の dict。
- **エラーの文：** すべて。`Unsupported` のあと Python が出します。
- **重なり：** `rules/population.rs` は受け入れに要る `temporary_spaces.py`、`geometry_overlay.py` の読みを自分でも持ちます（別レーンが同時に書きました）。比較はどちらも0件ですが、手順4の前に `rules/overlay.rs` に寄せるのがよいと思います。寄せていません。

## モデルの担当の方に見ていただくコード

**値は変えていません。新しい係数はありません。** 同じ値が Rust にも書かれました。

| Python | Rust（`rules/overlay.rs` など） | 値 |
|---|---|---|
| `reserve_sleep_bed` | `plan_bed`、`cell_of` | ベッドを使っている半径 0.75 m、経路の候補 256、距離 ≤ 1e-8 なら 0 分 |
| `room_admission_point` | `admission_point` | 格子 0.75 m、候補 256、居住者との距離 0.5 m、丸め 8桁 |
| `installed_facility_point`、`facility_grid_point` | `installed_point`、`grid_point` | 最小の間隔 0.6 m、格子の成長 16、0.35、0.68 + 0.28 |
| `validate_geometry_change` | `check_change` と部品 | 壁から 0.5 m、経路の余裕 0.5 m、1e-8、1e-9 の許容 |
| `capacity_metrics` | `capacity_of` | 一時空間と部屋の定員の足し方、退出後の保持 |
| 上限 | 各所 | 空間 256、履歴 20,000、上書き 256、予約と受け入れ 10,000、要求 100,000、定員 1,000 / 1,000,000 |
| `choose_queue_id`、`_serve_independent_head` | `choose_queue_id`、`serve_one` | 負荷と ID の順、提供点の容量 |

`**` は `mathx::pow`、`hypot`、`sum`、`round`、`max`、`min` は `mathx` です。`math.sqrt`（格子の列の数）だけは `f64::sqrt` で、IEEE で正しく丸められる演算なので同じ値です。

トイレの独立した列（`rules/toilet.rs`）は、提供点の容量、施設の上限、抽選のキーに列 ID を足す形、職員の技能と予約も Rust になりました。

**Rust だけの条件（超えたら Python が動きます）：** 座標の絶対値が 1e100 を超える（既存の `usable`）、グリッドのセルが 2^52 以上、i64 を超える int、`Point2D` の座標が int（Python は float に変換します。kernel は断るだけです）、`bed_reservations` のキーがない古い形、部屋の `active` が bool でない、`Identifier` の `$` が許す末尾の改行、taint した行。

## 確認

2026-11-02、WSL2、16コア、ほかのレーンと同時です。基準は `ee92b96`（`feature/rust-port` の先頭。#341、#342、#343 を含みます。統合の衝突は `rules/proposals.rs`、`rules/service.rs`、`native/README.md`、`docs/architecture.md` でした）。全テストと lint は最後のコミットの値です。ゲート、影、7日分、予算は `b809d24` の値で、そのあとの変更はテストの1ファイルとこの文書だけです。

| `tick_budget.py`（9600分から480 tick、3回） | 基準との比 | A/A | load average |
|---|---:|---:|---|
| 統合のあと（基準 `ee92b96`） | **1.010**（0.947、1.010、1.044） | 1.028（1.050、0.979、1.028） | 44〜54 |
| 統合の前（基準 `ebfd8a0`、`6646b33`） | 0.984（0.984、1.048、0.930） | 0.995（0.995、0.996、0.982） | 6〜36 |

状態はどの回も3つとも一致。揺れの中で、速くも遅くもなったとは言えません。

**部品（同じ世界、1回 µs、スレッドの CPU 時間、2000回を5組の最小、load average 約 30）：**

| 呼び出し | Python | Rust | 7日分の回数 |
|---|---:|---:|---:|
| `effective_total_zone_capacity` | 0.63 | 0.21 | 2,808 |
| `capacity_metrics` | 0.38 | 0.44 | 5,392 |
| `incoming_travelers` | 5.40 | 0.20 | Rust の中の回を除く |
| `release_sleep_bed` | 0.41 | 0.23 | 3,309 |
| `facility_has_independent_queues` | 0.13 | 0.10 | 19,832 |
| `validate_temporary_spaces_state` | 0.63 | 0.60 | 10,837 |
| `validate_geometry_overlay_state` | 228 | 279 | 758（保存だけ）。**遅くなります**（7日分で 0.17 → 0.21 秒） |
| `independent_queues_required`、`validate_facility_queue_state` | 156〜161 | 160〜163 | 768、10,837（scope の外の値。parse の費用で、scope の中は別） |

部品は統合の前の値です。

| 確認 | 結果 |
|---|---|
| 全テスト | Rust あり 4,131件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,073件成功、2,064件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件。全部の影の回も同じ |
| `--reversed-order` | 55シナリオ、問題0件 |
| 7日分（`--expect`） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致（素の回、影の回） |
| 7日分の影 | **このレーンと依存するものだけ**（`shadow_effects`、`shadow_handlers`、`shadow_movement`、`shadow_overlay`、`shadow_proposals`、`shadow_queues`、`shadow_service`）。違い0件 |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（指示どおり。`core/state_mirror.py` は登録の1行だけ） |

### kernel が動いた回数（影の回）

| kernel | ゲート 呼び出し / 動いた | 7日分 呼び出し / 動いた |
|---|---:|---:|
| `apply_geometry_change` | 15 / 15（世界の比較 15回） | 0 |
| `validate_geometry_change` | 14 / 9（5は Python も断る） | 0 |
| `place_installed_facility`、`installed_facility_point` | 2 / 2、2 / 2 | 0 |
| `register_`、`expire_temporary_space` | 1 / 1、1 / 1 | 0 |
| `apply_effect` の `sleep`（部屋あり） | 歩く 14、留まる 54、断り 0 | 0 |
| `release_sleep_bed` | 200 / 200 | 1 / 1 |
| `release_temporary_room_admission` | 13 / 13 | 0 |
| `available_temporary_room`、`incoming_travelers` | 84,796 / 84,796、84,944 / 84,944 | 0 |
| `capacity_metrics` | 3,632 / 3,632 | 5,392 / 5,392 |
| `effective_total_zone_capacity`、`effective_zone_capacity` | 362,988 / 362,988、84,952 / 84,952 | 6,032 / 6,032、0 |
| `facility_has_independent_queues` | 32,673 / 32,673 | 19,832 / 19,832 |
| `queue_head_agent_ids` | 7,874 / 7,874 | 418 / 418 |
| `validate_overlay` 受け入れ | 19,686 | 758 |
| `validate_spaces` 受け入れ | 46,948 | 10,837 |
| `facility_queues.validate` 受け入れ | 46,948 | 10,837 |
| `prepare_independent_facility_queue` | 818 / 818（世界の比較） | 0 |
| 水と食事の `serve_independent_head` | 318 / 318（世界の比較） | 0 |
| `record_independent_stall` | 280 / 280（世界の比較） | 0 |
| トイレの `serve_independent_head` | 372 / 372（世界の比較） | 0 |
| トイレの `complete`（`shadow_handlers`） | 3,247 / 3,247 | 同レーンの表 |

世界の比較の値は全部で 111,111,961 個です。`rows_verified` は 5,402回。統合の後は、`enqueue`、`start_service`、`mark_stall`、`record_queue_transition` が kernel の中から直接呼ばれるので、名前では影を通りません。1回分の kernel の世界の比較に含まれます（統合の前の回では 65、65、242、195回を名前で比べ、違い0件でした）。

**断り：** 7日分でこのレーンの kernel の断りは0件です。ゲートは `validate_geometry_change` の 10（影と `stats` の両方で数えた5回で、すべて Python も断ります）と、`fetch_water` の「独立した列にすぐ入る」12 です。M4 と M6 handlers の「独立した FIFO」の断りはなくなりました。`Python が成功したのに断った` は 0 です。

**違い：** ゲート 0件、7日分 0件。

**影の作りで起きたこと：** 最初の全部の影の回で `split_queues` が落ち、`shadow_proposals` が32件の違いを出しました。どちらも影の問題でした。書く規則が途中の世界で `to_dict` を呼んだことと、kernel が書いたあとの移動の状態を Python が読んだことです。素のゲートは一致していて、直したあとの回が上の値です。**7日分の影の1回目は `sitecustomize` の読み込みの順で `IndexError` になり、影がほとんど動かないのにハッシュは通りました。** `Error in sitecustomize` は標準エラーに1行出るだけなので、影を絞るときは見る必要があります。上の値は直した回で、ファイルごとの出力を確かめました（7プロセス、7つの影）。

### ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）

`domain/geometry_overlay.py` 811行のうち 576（71%）/ 260（32%）、`domain/temporary_spaces.py` 103行のうち 92（89%）/ 32（31%）、`domain/facility_queues.py` 215行のうち 188（87%）/ 53（25%）、`domain/queueing.py` 143行のうち 120（84%）/ 87（61%）、`domain/resource_service.py` 485行のうち 419（86%）/ 216（45%）、`domain/facilities.py` 246行のうち 211（86%）。Rust の行の網羅は測っていません。

### 感度

スクラッチのコピーで1か所ずつ変えました（デバッグビルド）。ハッシュと影は全部のシナリオではありません：ハッシュは `geometry`、`split_queues`、`interventions` など、影は `geometry` か `split_queues` です。「未実行」は、テストが見つけたので回さなかったものです。

| 変更 | ハッシュ | 影 | テスト |
|---|---|---|---|
| ベッドの半径 0.75 → 0.7575 | 一致 | 0件 | 1件失敗 |
| 受け入れの半径 0.5 → 0.505 | 一致 | 0件 | 1件失敗 |
| 受け入れの格子 0.75 → 0.7575 | 一致 | 0件 | 2件失敗 |
| 置き場所の間隔 0.6 → 0.606 | 一致 | 0件 | **最初は0件。** テストを足して2件失敗 |
| 壁の余裕 0.5 → 0.505 | 一致 | 0件 | **最初は0件。** テストを足して1件失敗 |
| 格子の 0.68 → 0.6868 | **不一致**（`add-toilets`） | 0件（`geometry` には届きません） | 3件失敗 |
| 格子の成長 16 → 17 | 一致 | 0件 | **最初は0件。** テストを足して1件失敗 |
| 空間の上限 256 → 258 | 一致 | 0件 | 1件失敗 |
| 空間の履歴 20,000 → 20,200 | 未実行 | 未実行 | 1件失敗 |
| 床図の履歴 20,000 → 20,200 | 未実行 | 未実行 | 1件失敗（足したテスト） |
| ベッドの経路 256 → 258 | 未実行 | 未実行 | 1件失敗（足したテスト） |
| 受け入れの候補 256 → 258 | 未実行 | 未実行 | **最初は0件。** テストを足して2件失敗 |
| 部屋の保持を `min` から定員へ（1% ではない） | 一致 | **20件以上** | 1件失敗 |
| 要求の上限 100,000 → 101,000 | 未実行 | 未実行 | **0件。** kernel は「列にある要求の数 = 要求の数」も確かめ、1人1要求なので、この上限だけが効く状態を作れません。**どの確認にも見えません** |
| 記録の順番を +1（1% ではない） | 一致 | 0件（名前で通らないため） | 3件失敗 |
| 短い列でなく長い列を選ぶ（1% ではない） | **不一致** | **20件以上** | 1件失敗 |
| 水と食事の提供点の `>=` → `>`（1% ではない） | **落ちる** | 3件 | **0件** |
| 列の旅の順番を +1（1% ではない） | **不一致** | 20件 | 8件失敗 |
| トイレの提供点の `>=` → `>`（1% ではない） | **不一致** | 20件 | 1件失敗 |
| トイレの抽選のキーから列を除く（1% ではない） | **不一致** | 20件 | 2件失敗 |

**1% の係数はほとんどテストだけが見ます。** ゲートの値はその境の近くを通らないからです。例外は格子の 0.68 で、`interventions` のハッシュも変わります。水と食事の提供点の `>=` はテストが見つけず、ハッシュと影だけです。

**影が0件の行はテストかハッシュで見ています。** 影は `geometry` のシナリオでの値で、そのシナリオに届かない規則は比べられません。影が比べられることは、下の4行の不一致で確かめました。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。
- **ゲートでの回数は少ないです。** 床図の変更は15回、`temporary_room_geometry` は2回、`transfer_equipment` と `move_facility` は1回ずつです。Python が断る多くの枝はテストの決めた例だけで、乱数の入力は試していません。`validate_geometry_change` の断りの比較は「Python も断る」までで、kernel は文を持ちません。
- **`interrupt_for_staff` の中から `enqueue` と `record_queue_transition` が呼ばれる形はテストにあります**が、kernel の比較はゲートの回数の中だけです。
- **`Geo::of` の変更は7日分の movement の全経路に影響します。** 7日分の `shadow_movement` と全ハッシュは一致しました。上書きがある間取りはゲートとテストだけです。
- **`state/resource_service.rs` の `validate`** は独立した列でも受け入れるようになりました。全部のゲートの影で違いはありません。
- **`rules/toilet.rs` の職員の予約のある完了** は、テストの1例と、職員の技能を求める提供点があるときだけ通ります。ゲートの `split_queues` に職員を求める点があるかは数えていません。
- **行の網羅は統合の前の値です。** 統合で `domain/facilities.py` の kernel も増えたので、Python の側の「kernel ありだけ」はさらに下がっているはずです。
- **感度の回は統合のあとの木とその前の木に分かれます。** 最初の9行の一部は統合の前の木の値です。
- **`validate_geometry_overlay_state`** は 20% ほど遅くなります（保存のときだけ、7日分で約 0.04 秒）。手順4の後は行を読む形にできます。
- **RSS は測っていません。**

## レーンとの重なり

- **統合しました：** `feature/rust-port`（`ee92b96`）を取り込みました。`rules/proposals.rs`、`rules/service.rs`、`native/README.md`、`docs/architecture.md` に衝突があったためです。
- **`events/handlers.py`、`domain/care_receipts.py`、`domain/population.py`、`domain/population_lifecycle.py` は編集していません。** `rules/population.rs` と `rules/population_lifecycle.rs` の中にある overlay の読みは、そのレーンのものです（上）。
- 編集したほかのレーンのファイル：`domain/facilities.py`（`_arbitrate_independent_facility` の切り出し）、`rules/toilet.rs`、`rules/travel_completion.rs`（M6 handlers）、`rules/effects.rs`（名前の呼び出しを直接の呼び出しに）、`core/state_mirror.py`（登録の1行）、`tests/support/python_rules.py`、`tests/support/tracked_mirror_plugin.py`、`tests/integration/test_effect_kernels.py`、`test_service_rules.py`、`test_queue_rules.py`、`test_installed_facility_announcement.py`、`benchmarks/rule_shadow/sitecustomize.py`（1行）、`shadow_queues.py`、`shadow_proposals.py`。
- **`native/README.md`、`docs/architecture.md`** は段落の追記が重なるかもしれません。
