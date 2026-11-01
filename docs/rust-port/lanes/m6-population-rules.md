# M6 レーン：人の出入りの規則

`domain/population.py` と `domain/population_lifecycle.py` の規則を Rust に移しました（`native/src/rules/population.rs`、`population_lifecycle.rs`）。状態は [m6-population.md](m6-population.md) で移してあります。**結果は変わりません。** 目的は速さではなく、手順4で Python の規則を消せるようにすることです。

## 比べられる範囲（先に読んでください）

- **7日分はこの規則にほとんど届きません。** 書き手は0回で、動くのは「することがない」入口と読むだけの規則です。7日分の一致は、書き手の正しさの根拠になりません。
- **ゲートが届く書き手は少数です。** `queue_arrivals` 9回、`queue_departures` 2回、`apply_arrival_approach_completion` 8回、`complete_presence_relocation` 1回、`apply_presence_action` 7回、`schedule_population_retry` 74回です。
- **テストだけが比べるもの：**
  - `admit_pending_returns` の書き込み（ゲートは待っている人が0で、毎回「することがない」）
  - 履歴の上限（どちらも 20,000件）、再試行の上限（1,440回）、部屋の人との距離（0.5 m）
  - 壊れた状態を断ること
- **Rust の行の網羅は測っていません。** 下の数は kernel ごとの回数で、枝ごとではありません。`apply_presence_action` の動作ごとの内訳も数えていません。
- ゲートのシナリオは足していません（fixture は変わりません）。テスト `tests/unit/domain/test_population_rules.py` が、ゲートの届かないところを同じ方法で比べます。

## 形

kernel は `kernel_first` の後ろにあり、呼び出しごとに1つです。

1. **計画：** Python の規則が例外を出す入力を、最初の書き込みの前にすべて断ります（`Unsupported`）。Python の規則がそのまま動き、今の文で失敗します。計画の中の例外もすべて断りに変えます。
2. **書き込み：** Python の規則と**同じ操作を同じ順で**、追跡のコンテナに対して行います。転送で名前空間の行に届きます。名前空間の丸ごとの書き込みはありません。ここで `Unsupported` が出たら不具合なので `RuntimeError` にします（Python の規則を重ねて動かさないため）。それ以外の例外は、同じ操作が Python で出すものです。

行に直接書かないのは、1回の Run で数回のイベントの規則で、行の型ごとの書き込みを持つ得がないからです。手順4でも追跡のコンテナが残る前提です。残らないなら、書き込みの段階を行への書き込みに変える必要があります。

順に効く判断（定員、部屋、連番）は計画の中で積み上げます（`Places`）。pydantic の検証は、受け入れが確実な文書だけを通す Rust の確認にしました（`presence_is_plain`、`Person::parse`、`context_is_plain`、`traits_are_plain`）。少しでも違えば断ります。Python が受け入れる入力も断ることがあります（`age_years` が float、点の座標が int など）。遅くなるだけで、結果は同じです。

**結果が同じになるよう省いた書き込み：** `admit_pending_arrivals` の `item["admitted_minute"]`（直後に一覧から外れます）、同じ値の `waiting_count`・`admitted_count`、`admit_pending_returns` と `_restore_ready_relationships` の、中身が変わらない一覧の書き直しです。

## 移したもの

| 規則 | kernel |
|---|---|
| `queue_arrivals`、`_generated_arrival_context`、`_schedule_arrival_approach_completion`、`_record_waiting_presence` | `population.queue_arrivals` |
| `admit_pending_arrivals`、`_admit_profile`、`_record_presence_transition`、`_choose_admission_target`、`_zone_has_room` | `population.admit_pending_arrivals` ほか |
| `apply_arrival_approach_completion`、`_record_approach_completed` | 同名 |
| `queue_departures`、`_depart_agent`、`_cancel_agent_events`、`_remove_information_state`、`_arrival_minute`、`_profile_record` | `population.queue_departures` |
| `schedule_population_retry` | 同名 |
| `validate_population_dynamics_state` | 同名（待っている人と文脈を含む） |
| `apply_population_presence_command` の動作の部分（`_apply_presence_action` に切り出しました）、`_archive_person`、`_restore_person`、`_schedule_relocation`、`_set_presence`、`_history`、`_program_presence_minutes` | `population_lifecycle.apply_presence_action` |
| `admit_pending_returns`、`complete_presence_relocation`、`current_household_id`、`_zone_has_room`、`validate_population_lifecycle_state` | 同名 |

Rust の読みとして足したもの（Python の規則は他の呼び手のために残ります）：`capacity_metrics` のうち入所が読む部分、`incoming_travelers`、`available_temporary_room`、`room_admission_point`、`admit_to_temporary_room`、`release_sleep_bed`、`release_temporary_room_admission`。経路は `travel.rs` の `world_route` です。

## Python に残した規則（手順4で Rust が要るもの）

- **エラーの文のすべて。** 指示どおりです。`EventSuppressed` の道も含みます。
- **忙しい人の先送り：** `_defer_while_busy` と `queue_departures` の忙しい枝。断って Python が動き、Python から `schedule_population_retry` の kernel を呼んで `EventSuppressed` を出します。ゲートの断りはすべてこれです（下）。
- **`apply_population_presence_command` の外側：** コマンドの解析（pydantic。効果のコマンドの境界）、世界の写しと差し替え、最後の検証。設計の「写して差し替える」の難所で、手順3では変えません。`events/handlers.py` の写しも同じです（別のレーンのファイルです）。
- **初めて使うときの状態：** `_state` の `create_population_dynamics_state`（`PopulationSpec` を読みます）、`create_population_lifecycle_state`、`initialize_population_lifecycle_state`。名前空間がなければ kernel は断ります。
- **`_initial_dynamics`：** 保存された人に `agent_dynamics` の記録がないまま戻るとき、全員分を作る規則です。断ります。
- **通路の定員がある移動：** `max_agents_in_transit_by_edge` があると `relocate` を断ります（`shared_passage_edge_loads` が全部の移動を読むため。M4 の `route_for` と同じ断り）。
- **`validate_*` の広い入力：** 生成でない文脈の項目（`parent_ids` など）、int の座標。断って Python が読みます。
- **kernel から Python を呼ぶ場所。** 規則ではありませんが、消すなら置き換えが要ります。
  - `parsed_facility_geometry`、`DynamicArrivalPersonContext.model_validate`（Scenario とコマンドの境界）
  - `ZoneGraph.from_states`、`AgentProfile`・`AgentState`・`NeedState`・`ActiveTask` の作成、`copy.deepcopy`
  - `snapshot_is_plain` の `Activity`・`Mobility`
  - **書いたあとの呼び戻し（2か所）：** `queue_departures` のあとの入所と、`complete_presence_relocation` のあとの帰還の kernel が断ったら、その手順だけ Python の規則（`__wrapped__`）を呼びます。「コールバックなし」の例外です。書いたあとなので規則全体をやり直せないためです。ゲートでもテストでも、この道を通った回は数えていません。
- `temporary_spaces.py`、`geometry_overlay.py` の書き手と検証は、このレーンの外です（[m6-population.md](m6-population.md)）。

## モデルの担当の方に見ていただくコード

**新しい係数はなく、値も変えていません。** 同じ値が Python と Rust の2か所になりました（`rules/population.rs`）。

- 到着の生成：年齢 `5 + byte % 79`、子ども 18 未満、要介助 75 以上、欲求 `byte / 255 * 0.35`、`support_need` `byte / 255 * 0.5`、`stress_sensitivity` `byte / 255`
- 体の記録：`min(1440, fatigue * 360)`、`min(1, 0.25 + stress * 0.5)`、`positive_affect` 0.5
- 部屋：格子 0.75 m、人との距離 0.5 m、候補 256
- 上限：1イベント 1,000人、世帯 12人、待ち 10,000人、人の総数 10,000人、再試行 1,440回、履歴 20,000件

**Rust だけの条件：** 上の「受け入れが確実な文書」の確認と、ID が `Identifier` であることです。どれも断るだけで、結果を変えません。

## 確認

`739e3d3`、2026-11-02、WSL2、16コア、ほかのレーンと同時です。基準は `c3ab744`。その後の変更はテスト、文書、Rust のコメントだけです。

| 確認 | 結果 |
|---|---|
| `gate_all_paths.py --expect`（全部の影） | 55シナリオ、問題0件。**このレーンの影の違い0件** |
| `--reversed-order`（このレーンの影だけ） | 55シナリオ、問題0件、違い0件 |
| 7日分（`--expect`、素の回と、このレーンの影だけの回） | world `69aba6590f6b`、KPI `94c8cbb26041`。どちらも一致。`--expect` は events も見ます |
| `ruff`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（`core/state_mirror.py` を変えていないため） |

`tick_budget.py`（9600分から480 tick、3回）：レーン **1.020**（0.970、1.020、1.039）、A/A 1.077（1.080、1.077、1.070）、状態は一致、load average 7〜15。7日分の規則は読むだけなので、差は揺れの中です。

待っている人がいる tick の費用（1回の測定、待ち200人、人20人、load average 約 6）：kernel 336 µs、Python の規則 452 µs。**この Python の値は、規則の中で `_choose_admission_target` の kernel を呼ぶので、純粋な Python ではありません。**

### kernel が動いた回数と比べた量

影は、世界を写して Python の規則を写しで動かし、kernel を世界で動かして、正準 JSON と戻り値を比べます。写しの中ではこの2つのモジュールの kernel を断らせるので、すべて Python です。

| kernel | ゲート 呼び出し / 動いた | 比べた bytes | 7日分 呼び出し / 動いた |
|---|---:|---:|---:|
| `admit_pending_arrivals` | 31,531 / 383（31,148 はすることなし） | 2,076,109,215 | 10,080 / 0（すべてすることなし） |
| `admit_pending_returns` | 31,532 / 0（すべてすることなし） | 0 | 10,080 / 0（同じ） |
| `queue_arrivals` | 9 / 9 | 3,942,612 | 0 |
| `queue_departures` | 56 / 2 | 1,815,652 | 0 |
| `apply_arrival_approach_completion` | 8 / 8 | 6,087,356 | 0 |
| `schedule_population_retry` | 74 / 74 | 47,628,471 | 0 |
| `apply_presence_action` | 27 / 7 | 3,500,969 | 0 |
| `complete_presence_relocation` | 1 / 1 | 447,514 | 0 |
| `current_household_id` | 84,196 / 84,196 | 値 84,196 | 104,410 / 104,410 |
| `validate_population_dynamics_state` | 36,896 / 36,896 | 値 36,896 | 素の validator が先に受けます |
| `validate_population_lifecycle_state` | 9,590 / 9,590 | 値 9,590 | 355 / 355 |

**断り：** ゲートは `queue_departures` の「忙しい」54回と `apply_presence_action` の「忙しい」20回だけです。**7日分は0回です。**

このレーンのテストと、人の出入りに触れる既存のテスト、合わせて26ファイルも影の下で動かしました。`current_household_id` など読む規則を含めて比べた数はゲートより多く、書き手は `queue_arrivals` 27回、`apply_presence_action` 29回、`admit_pending_returns` 24回、`complete_presence_relocation` 4回などです。影の違いは4件で、**このレーンの影のものは0件です。** 2件は `test_population_row.py` がわざと木の外に書くテストで、M6 の影が報告したものです。残り2件は `shadow_tick_validation` の `rows_valid`（`body_dynamics` の設定）で、基準でも出るかは確かめていません。影の下では、kernel が動いたことを数えるテストなど48件が失敗します。影を付けない回はすべて通るので、影が Python を動かすせいと見ていますが、1件ずつは確かめていません。

### 感度（コピーで変更）

| 変更 | ゲートのハッシュ（3シナリオ） | 影 | `test_population_rules.py` |
|---|---|---|---:|
| 欲求の 0.35 → 0.3535（1%） | **不一致** | 9件 | 16件失敗 |
| `support_need` の 0.5 → 0.505（1%） | **不一致** | 9件 | 16件失敗 |
| 体の 360 → 363.6（1%） | **不一致** | 22件 | 9件失敗 |
| 定員の `<` → `<=` | **不一致** | 2件 | 11件失敗 |
| 受領の ID の `\\0` → NUL | **不一致**（`population` だけで確認） | 動かしていません | 12件失敗 |
| 部屋の距離 0.25 → 0.2525（1%） | 一致 | 0件 | 1件失敗 |
| 再試行の上限 1,440 → 1,454（1%） | 一致 | 0件 | 1件失敗 |
| 待ちの履歴 20,000 → 20,200（1%） | 一致 | 0件 | 1件失敗 |
| 人の出入りの履歴 20,000 → 20,200（1%） | 一致 | 0件 | 1件失敗 |
| `retained` に 1 足す | 一致 | 0件 | 0件 |

最後の変更は結果を変えません。`retained` が効くのは定員を超えているときだけで、そのとき `has_room` はどの値でも偽です（`capacity_metrics` の他の値は入所の判断に使いません）。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。`mathx` の `pow`、`py_round`、`py_sum` を部屋の点で使います。
- 影は書く kernel の呼び出しごとに世界を写すので、影の回の時間は比較に使えません。
- 影の写しは `deepcopy(world)` です。`from_dict(to_dict())` と違い、`apply_population_presence_command` の本当の写しとは辞書の順序が違うことがあります。本当の順路はゲートのハッシュが見ます。
- `apply_population_presence_command` の写しの上で書き込みの途中に `RuntimeError` が出ると、写しごと捨てられます。普通の世界の `admit_pending_*` で出ると、途中まで書いて止まります。出たことはありません。
- `row` だけのイベントを予定するようになったので（`schedule_population_retry`、接近の完了、移動の到着）、Python の `ScheduledEvent` はハンドラーに渡すときに作られます。比べた範囲では同じです。

## レーンとの重なり

- `events/handlers.py`、`domain/care_receipts.py`、`events/effects.py` は変えていません。`handlers.py` が呼ぶ関数の名前と引数も同じです。
- `native/src/rules/movement.rs`、`travel.rs`、`agent_dynamics.rs` は関数を `pub(crate)` にしたものと、読むだけの `check_idle_point`、`world_route`、`check_add_agent` を足したものです。
- `benchmarks/rule_shadow/shadow_population.py`：読み込みをその場に移しました（新しい影より先に `population.py` を読み込まないため）。
- `tests/support/python_rules.py` に名前を足しました。`docs/architecture.md`、`native/README.md` は同じ段落で衝突するかもしれません。
