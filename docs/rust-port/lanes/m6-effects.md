# M6 レーン：EffectProgram のイベントの実行

`events/effects.py` のうち tick で動く規則を Rust に移しました。ガード、5つのハンドラー、変更の重なりの読み取り、`validate_effect_program_state`、それと `effect_program_v1` の行です。**結果は変わりません。係数は増えていません。** Python の規則は比較の基準として残ります。

**このレーンの価値は速さではありません。** 手順4で Python の規則を消すために、操作の種類ごとの実行を Rust に揃えることです。tick の差は `tick_budget.py` の揺れの中です。

**比較の基準について、はっきり書きます。7日分には介入のイベントが1件もありません。** 7日分で比べられるのは、印のないイベントの `should_apply_guard`（常に true）と `validate_state`（名前空間がない形）だけです。**ハンドラー、ガードの式、名前空間がある形の検証は、ゲートとテストでしか比べていません。** そのうえ、ゲートでも各操作は数回です（下の表）。

## 調査：何がどれだけ動くか

Python のイベントの処理を包んで数えました（`98f10f1`、ゲートは55シナリオの1回ずつ）。

| 操作 | イベント | ゲート | 7日分 |
|---|---|---:|---:|
| `set_facility_availability` | `effect_program_apply` / `_expire` | 3（うちガードで抑止1）/ 3（うち抑止1） | 0 |
| `facility_outage` | 同 | 1 / 1 | 0 |
| `set_ambient_noise` | `effect_program_environment` / `_expire` | 2 / 2 | 0 |
| `modify_demand_rate`、`set_lighting`、`set_privacy`、`set_temperature`、`set_ventilation` | 同 | 各 1 / 1 | 0 |
| `add_temporary_space` | `effect_program_temporary_space` / `_expire` | 1 / 1 | 0 |
| `move_facility`、`transfer_equipment` | `effect_program_geometry` | 各 1 | 0 |
| `set_partition` | `effect_program_geometry` / `_expire` | 1 / 1 | 0 |
| `set_route_access` | 同 | 4 / 3 | 0 |
| `temporary_room_geometry` | 同 | 2 / 2 | 0 |
| `add_staff` | `effect_program_structure` | 16 | 0 |
| `staff_task_order` | 同 | 12 | 0 |
| `set_staff_role` | 同 | 8 | 0 |
| `add_toilet`、`facility_repair`、`staff_handoff` | 同 | 各 1 | 0 |
| （以下はこのレーンのハンドラーではない） | | | |
| `add_evacuees`、`depart_evacuees`、`population_presence` | `effect_program_population`、`population_approach_complete` | 100 | 0 |
| `deliver_resource`、`modify_supply_schedule`、`set_staff_availability`、`water_outage` | `resource_delivery` ほか | 8 | 0 |

毎 tick に動くのは2つだけです。

| 呼び出し | ゲート | 7日分 |
|---|---:|---:|
| `should_apply_effect_guard`（`dispatch_one` ごと。ほぼすべて印なし） | 19,214 | 10,495 |
| `validate_effect_program_state` | 34,529 | 10,431（毎分 10,080、完全検証 351） |

1回のハンドラーは 45〜160 ms で、ほとんどが世界の写しと検証です。

## 移したもの

| もの | Rust | Python |
|---|---|---|
| 行 | `state/effect_program.rs`（`OWNED_NAMESPACES` に1行。自由な形の `compiler_provenance`、`assumptions` は `V`） | |
| ガード | `rules/effects.rs` の `should_apply_guard`（`GuardExpression` の形の確認と評価） | `should_apply_effect_guard` |
| 設備 | `facility` | `EffectProgramFacilityHandler.apply` |
| 環境 | `environment` | `EffectProgramEnvironmentHandler.apply` |
| 一時空間 | `temporary_space` | `EffectProgramTemporarySpaceHandler.apply` |
| 床図 | `geometry`（`apply_geometry_change` の前まで） | `EffectProgramGeometryHandler.apply` |
| 構造 | `structure`（`add_staff`、`add_toilet`、`set_staff_role`、`facility_repair`、`staff_handoff`、`staff_task_order`） | `EffectProgramStructureHandler.apply` |
| 読み取り | `has_active_modifier`、`has_pending_modifier` | `has_active_facility_modifier`、`has_pending_facility_modifier` |
| 検証 | `validate_state`（毎 tick の形と履歴を読む形） | `validate_effect_program_state` |
| `active_facility_outage` | `active_outage`（読むだけ） | `domain/infrastructure.py` |
| `add_staff`（職員の動的状態）、`add_staff_operations_agent`、`_idle_record`、`_staff_has_task_skill` | `structure` の中 | 同名 |

すべて `kernel_first` で入れ替えます。`__wrapped__` が Python の規則です。`core/world.py` の検証の場所は `rust_validated` を通らず、この関数を呼ぶだけにしました。M5 が「kernel の得がない」としてやめた検証は、速さのためではなく手順4のために移しました。

## 決めたこと

**写して差し替える流れは変えていません。** `step3-design.md` の決定どおりです。Rust の規則も `WorldState.from_dict(world.to_dict())`、`validate()`、`__dict__` の差し替えを同じ順で呼びます。1回あたりの費用も同じです。その場で書く形にしなかったのは、`candidate.validate()` の失敗で世界が変わらないこと（失敗したイベントとして記録されること）をそのまま保つためです。`facility_repair` だけは Python の規則がもともとその場で書くので、同じくその場で書きます。

**成功以外は Python の規則が言います。** 抑止と失敗はどちらも、世界を変えずに `Unsupported` を返します。写しの上の例外もすべてです。Python の規則は最初から動いて同じ文を出します。例外を通すのは、世界がすでに変わったあとの2か所だけで、Python の規則と同じです（`facility_repair` の `world.validate()`、差し替えのあとの受領記録）。

**スキーマと意味の検証は動かしていません。** LLM の出力はここに届きません。`validate_effect_program`、`schedule_effect_program`、pydantic の契約は Python のままです。実行時のパラメーターの再検証も、Rust の規則が Python の規則と同じ場所で `_PARAMETER_MODELS[op].model_validate` を呼びます。ガードだけは毎回呼ぶのを避けるため形を Rust で確かめ、確かでないものは `Unsupported` です。Rust が受け入れた式を契約も受け入れることは、乱数のテストと境界のテストで見ます。

**読み書きの多くは Python の入れ物を通します。** 行を持つのは `effect_program_v1` と、入力の読みに使う実体と資源台帳です。ほかの名前空間（`infrastructure_v1`、`zone_environment_v1`、`temporary_spaces_v1` など）はそのまま木を読み、書き込みは `set_item` で転送します。イベントはまれなので費用になりません。**名前空間全体の書き込みはありません。**

**設計からの変更：** `effect_program_v1` は設計では `V` の木でしたが、形が決まっているので型付きの行にしました。`step3-design.md` を直しました。

## Python に残した規則（手順4で Rust が要るもの）

- **`events/effects.py`：** `schedule_effect_program`（分岐のコマンド、承認の境界）、`validate_effect_program`、`available_primitives`、`_projected_facility_units`、`_toilet_placement_issues`。pydantic の契約（設計で Python に残すもの）。
- **抑止と失敗の文：** 5つのハンドラーとガードのすべての文。今は `Unsupported` のあと Python の規則が出します。
- **写して差し替える流れの部品：** `to_dict`、`from_dict`、`validate`。手順3の後で `core.fork()` に置き換える予定のものです。
- **名前で呼ぶ、ほかのモジュールの規則：**
  - `domain/geometry_overlay.py`：`apply_geometry_change`、`installed_facility_placement_enabled`、`place_installed_facility`（M6 population レーンの範囲）
  - `domain/temporary_spaces.py`：`register_temporary_space`、`expire_temporary_space`（同）
  - `domain/care_receipts.py`：`make_staff_handoff_record`、`make_facility_recovery_record`（M6 handlers レーンの範囲）
  - `domain/staff_operations.py`：`order_staff_operation`（作業のイベントの規則。M4 職員レーンの残り）
  - `domain/movement.py`：`staff_position_snapshots`
  - `domain/staff_dynamics.py`：`staff_dynamics_is_available`
  - `domain/agent_dynamics.py`：`announce_installed_facility`
  - `domain/environment.py`：`ScenarioZoneEnvironmentConfig.model_validate`
  - `events/queue.py`：`EventsById`、`pending()`（完全検証と環境の期限のイベント探し）
- **入口：** `rules/dispatch.rs` は印のあるイベントを `dispatch_one` に渡します。ハンドラーの kernel はそこから `kernel_first` で動きます。Rust の表に載せていません。
- **`events/handlers.py` の印のあるイベント：** `EffectProgramPopulationHandler`、`resource_delivery`、`delivery_delayed`、`staff_unavailable`、`staff_restored`、`water_outage`、`water_restored`（M6 handlers レーン）。

## モデルの担当の方に見ていただくコード

**新しい係数はありません。小数の計算もありません。** 判断の規則が2か所になりました。

| Python | Rust（`rules/effects.rs`） |
|---|---|
| ガードの比較（数は値で、真偽値は数と等しくない） | `compare_guard` |
| ガードが読む値（`target.*`、`zone.occupancy`、`resource.available_quantity`、`staff.available`） | `guard_field` |
| 設備の働く数：基準と重なりの最小 | `facility_rule` |
| 追加の上限：職員100人、設備200個 | `add_structure` |
| 新しい職員：`support`、疲労 0.0、`off_duty`。新しいトイレ：定員1、働く数1 | `add_one_staff`、`add_one_toilet` |
| 交代の条件と作業の種類ごとの技能 | `staff_handoff_on_copy`、`has_task_skill` |
| 修理の条件 | `facility_repair` |
| 演算子の表、段階と演算子ごとのイベントの種類 | `OPERATORS`、`expected_event_type`。テストが Python の表と比べます（`EFFECT_EVENT_TYPES` に切り出しました） |

**Rust だけの条件（超えたら Python が動きます）：** 2^53 を超える int と小数の比較、i64 を超える int、正確な型でない値（`bool`、`str` の継承、1 と 1.0 の違い）、`ScheduledEvent` でないイベント、`source_id` のない一時空間のイベント、taint した行。

## 確認

MEASUREMENTS

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。
- **各操作の比較の回数は少ないです。** `staff_handoff`、`facility_repair`、`add_toilet`、`move_facility`、`transfer_equipment` はゲートで1回ずつです。入力の形を広く試すのは、テストの中のガードと検証だけです。ハンドラーの入力を乱数で試してはいません。悪い値は決めた数の例で見ています。
- **`staff_handoff` の成功はゲートの1回とその影だけが比べます。** テストは断る場合だけです。
- **実体を行から読むガードは、`facility.__dict__` への直接の書き込みを見ません。** ほかの行を読む規則と同じです。`validate_state` はオブジェクトを読みます。
- **間違った kernel の結果は、検証が捕まえると Python に落ちて静かに直ります。** 下の感度の表のとおりで、影の `refused_a_success` が増えるだけです。今の回は0件です。
- **`with_row` の中で Python を呼ぶ箇所があります**（`check_state` の `EventsById`、設備の読み取り）。読むだけで、このミラーには触りません。
- **`tracked_mirror_gate.py` は動かしていません**（指示どおり。`core/state_mirror.py` は登録の1行だけです）。追跡する回は Python の規則が動きます。

## レーンとの重なり

- **ファイルの編集：** `core/state_mirror.py`（登録の1行）、`core/world.py`（検証の呼び出しの3行）、`tests/support/python_rules.py`、`tests/support/tracked_mirror_plugin.py`、`benchmarks/rule_shadow/sitecustomize.py`（1行）。
- **`events/handlers.py`、`domain/care_receipts.py`、`domain/population*.py`、`domain/temporary_spaces.py`、`domain/geometry_overlay.py` は編集していません。** ただし名前で呼ぶので、これらのレーンが関数の名前や引数を変えると、Rust の呼び出しが例外になって `Unsupported` になり、Python の規則が動きます。ハッシュは通るので、`effects.stats()` の断りの数で気づくことになります。
