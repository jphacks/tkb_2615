# L7b レーン：WorldState v5 の tick の検証

手順3の終わりのレーンです。L9 で分かったとおり、**WorldState v5 は古い保存の形ではありません。** 資源の運用（`resource_service`）のない Scenario は今も v5 で始まります（`WorldState.from_scenario` の `schema_version=(6 if "resource_service" in scenario.model_configs else 5)`）。L7 は v5 の tick の検証を Python に残し（ゲートの 2,320 tick）、M5 の `entity_rules` も v5 を呼んでいませんでした。棚卸しはこれを旧モデル（O）に分けていましたが、誤りでした。

このレーンで、**v5 の tick の検証を v6 と同じ Rust の道（`rules/after_tick.rs` の1回の呼び出しと、その中の `rules/tick_validation.rs` の `entity_rules`）にしました。** 結果とエラーの文は変わりません。係数は1つも増えていません。

## v5 と v6 の違い（検証で。読んで確かめた）

違うのは**資源の運用だけ**です。ほかの名前空間（体調、在籍、職員の作業、運営のイベント、環境など）と、その規則は版によりません。v5 の Scenario でも、生成した Scenario から `resource_service` を外せば、これらをすべて持つ v5 の世界になります（テストで使っています）。

| 項目 | v6 | v5 | Python の場所 |
|---|---|---|---|
| 設定 `resource_service` | 必ずある（正準の形） | ない | `validate`：`resource_service requires WorldState v6; v6 requires resource_service` |
| 名前空間 `resource_service_v1` | 必ずある。形と履歴を検証 | **あってはいけない** | `validate`：`resource_service state requires WorldState v6` |
| 名前空間 `resource_ledger_v1`（台帳） | 必ずある | **初期在庫（`initial_stock`）があるときだけ**。あれば同じ規則で検証 | `WorldState v6 requires a resource ledger` |
| 人の `needs.hydration_need`、`profile.initial_needs.hydration_need` | 0〜1 | **どちらも 0**。保存の文書にこの項目はない | `WorldState v5 requires hydration_need to be zero` |
| 設備の `resource_queue_agent_ids`、`resource_active_agent_ids` | 水と食事の設備だけが使う | **空**。保存の文書にこの項目はない | `WorldState v5 cannot contain resource service occupancy` |
| 水と食事の設備の普通の列（`queue_agent_ids`、`active_agent_ids`） | 使ってはいけない（棚卸しの文 `a resource facility uses the legacy queues`。「legacy」は普通の列のこと） | **使ってよい**（規則がない） | `if self.schema_version == 5: continue` |
| 資源の作業（`resource_service_queue`、`_water`、`_food`） | 列と台帳の予約と職員に対応 | **あってはいけない** | `resource service tasks require WorldState v6` |
| 予定の `resource_service_complete` | 作業か取り消しの一覧に対応 | 作業が今あればそのまま通る。なければ `model_state["resource_service_v1"]` を読んで **`KeyError`**（`StateInvariantError` ではない） | `_validate_entity_rules` |
| サービスの予約と台帳の予約の一致 | 確かめる | ない | `if self.schema_version == 6` |

Python の検証の外の違い（tick の規則）：`world_view`、`runner`（`ResourceServiceManager`）、`population.py` と `population_lifecycle.py`（v5 の人を受け入れるときに水分を 0 に）、`handlers.py`（資源の完了と取り消しは v6 だけ）、`operational_kpis.py`（v6 だけ）。このレーンでは変えていません。

## 変えたこと

| 場所 | 中身 |
|---|---|
| `native/src/rules/tick_validation.rs` `entity_rules` | 版を読み、v5 と v6 を受けます（ほかは `not a WorldState v5 or v6`）。v5 では：設定は None だけ（`a resource service config for a WorldState v5`）、台帳を読まない、2つの水分が float の 0.0、水と食事の設備の普通の列を断らない、資源の作業と資源の完了のイベントを断る、サービスの状態を読まない。v6 は前と同じです |
| `native/src/rules/after_tick.rs` `check` | 版を読み、v5 と v6 を受けます。v6 は台帳とサービスの状態を前のとおり求めます。v5 はサービスの状態があれば断り、台帳はあるときだけ検証器にかけます |
| `src/sheltersim/core/tick_validation.py` `tick_plan` | `resource_service` がなければ資源の設定を None に（前は `KeyError` で、v5 は呼ばれる前に断っていました）。設定があれば前のとおり正準かを確かめます |
| `benchmarks/rule_shadow/shadow_metrics.py` | v5 の世界も設定 None で比べます。壊した写しに「v5 の水分」を足しました |

**足さなかった規則（重複のため）：** 「v5 の設備に資源の席がある」は、資源の席に資源の作業が要る対応の規則と、v5 の資源の作業を断る規則で必ず断ります。片方を持つと、もう片方を外してもテストが落ちないので、作業の規則だけにしました。`after_tick.rs` の「設定と版の不一致」も、`entity_rules` の設定の規則（v6 は設定あり、v5 はなし）が同じことを断るので持ちません。

## 規則と止めるテスト

ゲートには良い状態しか来ないので、規則ごとに壊れた状態のテストを置きました。v5 の世界は2種類です：fixture の古い形の Scenario（`baseline_100_7d` から 30 人、200 分。名前空間なし、トイレだけ。ゲートの v5 と同じ形）と、生成した Scenario から `resource_service` を外したもの（すべての名前空間、台帳、水と食事の設備）。

| 規則 | テスト |
|---|---|
| v5 の2つの水分が 0 | `test_entity_rules_kernel.py` `a hydration need`、`an initial hydration need`、`test_after_tick_kernel.py` の v5 の `a hydration need` |
| v5 の在庫（初期在庫なし）が負でない | `an inventory below 0 without a stock` |
| v5 の資源の席に作業がない | `a resource slot without its task`（v6 と共通の対応の規則） |
| v5 の資源の作業を断る | `a resource queue task`（席と作業がそろった状態。規則がないと kernel が受けます） |
| v5 の資源の完了のイベントを断る | `a resource completion`（Python は `KeyError`） |
| v5 に資源の設定がない | `test_a_v5_world_is_not_taken_with_a_resource_config`、after_tick の `a resource config`（文も同じこと） |
| v6 に資源の設定がある | `test_a_v6_world_is_not_taken_without_its_resource_config`、`test_the_resource_config_of_a_v6_world_is_needed`（水か食事を受けている人がいない世界で。いると設定を読む別の道で断るため） |
| v5 にサービスの状態がない | after_tick の `a resource service state`（文も同じこと） |
| v5 の台帳はあれば検証 | after_tick の v5 の `a ledger balance below 0` |
| v5 は台帳なしでよい | `test_a_v5_world_needs_no_ledger` |
| v5 の水と食事の設備は普通の列を使ってよい | `test_a_v5_water_station_may_use_the_ordinary_queue`（受けることを確かめる） |
| v6 と同じ規則（実体、イベント、在籍、体調、運営、環境、基盤、記憶など） | 既存の壊し方の表を v5 の世界にも掛けます：`test_the_rules_of_both_versions_turn_down_a_broken_v5_world`（実体の 34 通り。資源の席、サービスの予約、在庫の3つは v5 用に別に）、`test_a_broken_v5_state_is_left_to_the_python_check_with_the_memo_as_it_was`（tick の 35 通り + v5 の4通り） |
| ランダムな書き込み | v5 の世界に 300 回ずつ（実体の規則、tick の検証）。受けたら Python の規則も通ること |

### 感度（規則を v5 だけ外したコピー）

`<S>/speed/lane-l7b/sens/`。`native/` を別の `target` にコピーし、各規則を環境変数 `L7B_SENS=<名前>` で v5 だけ外せるようにして1回作り、上のテストを名前ごとに動かしました（`sens/summary.txt`）。

| 外した規則 | 失敗したテスト |
|---|---|
| （外さない） | 0（91件成功） |
| v5 の水分 | 4件 |
| v5 の最初の水分 | 3件 |
| v5 の資源の作業 | 1件 |
| v5 の資源の完了 | 1件 |
| v5 の設定なし | 3件 |
| v6 の設定あり | **最初は0件。** テストの世界で水を受けている人がいて、設定を読む別の道で断っていました。受けている人のいない世界に変えて2件 |
| v6 の普通の列の規則を v5 にも掛ける | 1件 |
| v5 のサービスの状態 | 2件 |
| v5 の台帳の検証 | 2件 |
| 共通：ストレスの範囲（v5 だけ外す） | 5件 |
| 共通：保留の依頼の期限のイベント（v5 だけ外す） | 1件 |
| 共通：在籍の検証器（v5 だけ外す） | 5件 |
| 共通：体調の設定（v5 だけ外す） | 3件 |

**影の感度：** 同じコピーで、影の壊した写しを 20 回に1回にし（ふだんは 997 回に1回で、ゲートの v5 の Run は短いので一度も当たりません）、ゲートの v5 の Scenario に掛けました。水分の規則を外すと「the kernel took a v5 hydration need」、ストレスの規則を v5 だけ外すと「the kernel took stress 1% over」が、それぞれ違い 116 件として出ました。外さないコピーでは違い0件です。ハッシュはどちらも一致したままです（壊した写しは結果に入らないため）。

## 断りの実測（`refusal_census.py`、ゲート）

基準は出発点の `b76bee1`（別の worktree と別の `target`）、後はこのレーンの `9fcec94` です。

| 断り | 基準 | 後 |
|---|--:|--:|
| `not a WorldState v6`（`after_tick.rs`） | **2,320** | **0** |
| `the Python rule refuses the change`（`overlay.rs`、介入の検証） | 5 | 5 |
| `a call raised StateInvariantError`（`effects.rs`） | 2 | 2 |
| `no active task`（`service.rs`） | 2 | 2 |
| `value is not a plain JSON value`（`records.rs`、受けた入口なし） | 1 | 1 |

v5 の 2,320 tick は、ゲートの `trace_levels`（540。古い形の Scenario）、`operational`（200。`failure_day3`）、`branch_commands`（620。`h00_small_replay`）、`model__psychology__bounded-stress-v1`、`causal-stress-v1`、`mean-reverting-distress-v1`、`life-state-feedback-v1`（各 240）でした。残りの4行は tick の検証の外の、ほかのレーンの kernel です。tick の数はゲート 31,530 で同じ、driver の `kernels_refused` は前後とも0です。

## 影（このレーンの2つ、ゲート全体）

| | 基準（L7 の報告） | 後 |
|---|--:|--:|
| `after_tick` 呼び出し / kernel が受けた | 31,530 / 29,210 | 31,530 / **31,530** |
| `after_tick` の断り | v5 の 2,320 | **0** |
| `after_tick` の違い | 0 | 0 |
| `after_tick` 壊した写し / 断った | 196 / 196 | 208 / 208 |
| `entity_rules` 呼び出し（v5 / v6） / 受けた | 29,210（0 / 29,210）/ 29,210 | 31,530（**2,320** / 29,210）/ 31,530 |
| `entity_rules` 比べた値（延べ） | — | 2,940,720 |
| `entity_rules` の違い | 0 | 0 |

壊した写しを 20 回に1回にした v5 の Scenario だけの回（外さないコピー）：`after_tick` 4,020 呼び出しで壊した写し 1,266、すべて断り。`entity_rules` 4,020 呼び出し（v5 は 2,320）で壊した写し 1,428、すべて断り。違い0件。

## 結果

2026-11-03、WSL2、16コア、ほかのレーン（L8b、L8c）と同時です。コードは `9fcec94`（Rust と `src/`）と、その後のテストと文書の変更です。

| 確認 | 結果 |
|---|---|
| 全テスト（Rust あり） | 4,821件成功、8件 skip、失敗0。`<S>/speed/lane-l7b/tests-native.log` |
| 全テスト（`SHELTERSIM_NATIVE=0`） | 2,093件成功、2,736件 skip、失敗0。`tests-python.log` |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件（素の回、断りの計測の回、このレーンの影の2回） |
| `--reversed-order` | 55シナリオ、問題0件 |
| 7日分（`--expect`、断りの計測あり） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致。10,080 tick、断り0、`kernels_refused` 0 |
| `first_difference.py --both`（ゲートの v5 の Scenario すべて） | `trace_levels`、`operational`、`branch_commands`、`model__psychology__` の `bounded-stress-v1`、`causal-stress-v1`、`mean-reverting-distress-v1`、`life-state-feedback-v1`：段階の digest 計 26,906 か所、違い0件、最終のハッシュも同じ |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（`core/state_mirror.py` を変えていないため、指示どおり） |

基準のビルドは `b76bee1` の別の worktree と別の `target` です（`<S>/speed/lane-l7b/base`）。

## 速さ

**v5 の tick の検証：** `<S>/speed/lane-l7b/v5_check_cost.py`。Run の中で `validate_after_tick` のスレッドの CPU 時間を測り、基準とこのレーンを同時に動かしました（240 tick ずつ、3回、load average 4〜11）。

| v5 の Scenario | 基準（Python の検証） | 後（kernel） |
|---|--:|--:|
| 生成した Scenario から `resource_service` を外したもの（30 人） | 578〜606 µs | 118〜120 µs |
| 古い形（`baseline_100_7d` の 30 人） | 191〜210 µs | 57〜61 µs |

**予算（`tick_budget.py`、`fork-b1` の 9600 分から 480 tick、5回）：** 基準との比 **0.973**（0.973、1.052、0.970、0.948、1.075）、A/A 0.993（0.995、0.985、0.979、1.023、0.993）、状態は5回とも一致。load average 4〜23。この Run は v6 なので、v6 の道に足した版の読み取りと分岐だけを測っています。

## Python に残した規則（手順4で要るもの）

- **tick の検証の v5 の分はありません。** kernel が断ったときの `validate(validate_event_history=False)` は、v6 と同じく比べる基準と文の道です（L10 で Rust のエラーに）。
- **Python の速い道 `native.world_agents_valid`** は v6 だけです（`core/world.py` の `_validate_entity_rules`）。Python の規則の中の話で、kernel の道には関係ありません。変えていません。
- **`operational_kpi_values`** は v5 で `ValueError` を出します（`resource-service WorldState v6` が要る）。`rules/metrics.rs` の `kpi_values` も v5 を断って Python に任せます。Project の経路では呼ばれません。手順4では Rust がこのエラーを出す必要があります。
- **v5 の世界の資源の完了のイベント**は、Python では作業があれば通り、なければ `KeyError` です。kernel はどちらも断ります。手順4で Rust の文を決めるとき、この `KeyError` をどうするか決める必要があります（下）。

## モデルの担当の方に見ていただくもの

**係数はありません。式も変えていません。** Python の `validate` と `_validate_entity_rules` の v5 の枝を Rust に写しました（上の表）。

**Rust だけの条件（超えたら Python が動きます）：**
- v5 の水分が float でない（int の 0 は Python の `!= 0.0` では通りますが、kernel は断ります）。
- v5 の世界の `resource_service_complete` のイベントは、いつも断ります。Python は今ある作業を名指すなら通し、そうでなければ `KeyError` です。**`StateInvariantError` でない例外が出るのは Python の検証の不具合に近いです。** 有効な Run では起きません（v5 は資源の完了を予定しません）。
- v5 に資源の設定を渡す、v6 に渡さない（どちらも Python では `validate` の先頭で文が出ます）。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。
- **ゲートの v5 は古い形の Scenario だけです**（名前空間なし、トイレだけ）。台帳と水と食事の設備を持つ v5 の世界（資源の運用のない生成の Scenario）は、テストの Run（360 分、20 人）だけで通しています。ゲートと7日分にはありません。
- **v5 の水と食事の設備の普通の列**は、テストで作った状態だけです。今の tick の規則が v5 でそこに人を並べるかは確かめていません。
- **7日分は v6 だけです**（`generated` の Scenario）。v5 の tick はゲートとテストだけです。
- `tracked_mirror_gate.py` は動かしていません（`core/state_mirror.py` を変えていないため、指示どおり）。
- Rust とPython のゲートの行の網羅は測っていません。上の延べの件数と、壊した状態のテストで見ています。

## レーンとの重なり

- `native/src/rules/tick_validation.rs`、`after_tick.rs`、`core/tick_validation.py`：L7、M5 のファイルで、今動いているレーン（L8b、L8c）の場所ではありません。**エラーの文は1つも変えていません**（`core/errors.rs`、`core/kernels.py` は触っていません）。
- 文書：`step4-plan.md`（L7b の行、O の訂正、L9 の6番）、`step4-refusal-sites.md`（2行の記号）、`l9-view-only.md`、`l7-validate.md`、`m5-metrics.md`（訂正の1文ずつ）、`architecture.md`、`native/README.md`。
- テスト：`test_after_tick_kernel.py`、`test_entity_rules_kernel.py`（v5 の節を足しました）。影：`shadow_metrics.py`。
