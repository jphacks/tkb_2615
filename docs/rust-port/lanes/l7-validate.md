# L7 レーン：tick の検証の残り

手順4の L7 です。WorldState v6 の毎 tick の検証（`WorldState.validate_after_tick`）は、**最初の tick も含めて毎 tick、`native/src/rules/after_tick.rs` の1回の呼び出し**になりました。前に通った検証の記憶（`TickMemo`）も Rust が持ちます。**結果は変わりません。係数は1つも増えていません。** 決定 D3 のとおり、tick の外の `validate_*`（読み込みの完全検証、介入の承認）は Python のままです。

## 断りの実測（`refusal_census.py`）

基準は `rust/l4-queues` の `20807e9`（このレーンの出発点）、後はこのレーンの `21210c6` 以降です。

| `after_tick.rs` の断り | 基準 ゲート | 後 ゲート | 後 7日分 |
|---|--:|--:|--:|
| `a declared namespace has no kernel that takes it`（`population_presence_v1`） | 646 | 0 | 0 |
| `staff_operations_v1: its kernel did not take it`（作業中の職員） | 234 | 0 | 0 |
| `the shared operations are the Python rule's`（職員の作業の受領記録） | 49 | 0 | 0 |
| `the people changed` | 28 | 0 | 0 |
| `a body history actor is no longer here` | 1 | 0 | 0 |
| `the presence state changed` | 1 | 0 | 0 |
| **数えられていなかった分**：`Plan` のない tick（各 Run の最初の tick、設定を書いた後、`infrastructure` の設定がある世界、`zone_environment` の設定が通る前）。Python は kernel を呼ばなかった | 709 | 0 | 0 |
| `not a WorldState v6`（**v5 は L9**） | 呼ばれず 2,320 | 2,320 | 0 |

ゲートの tick 31,530 のうち、kernel が受けたのは **29,210（v6 の全部）**、断ったのは v5 の 2,320 だけです（別の数え方 `after_tick.stats()` とゲートの各シナリオの集計、`probe.py`）。7日分は 10,080 tick すべて kernel が受け、断りは0回です（計測の `kernels_refused` も0）。

ゲートに残る他の断り（`no active task` 19 など）は他のレーンの kernel で、この表の外です。`a marker's guard the Python rule decides`（2）は `rules.effects.validate_state` の Python の入口から、つまり tick の外の介入の検証からで、tick の検証からではありません。

## 形

- **記憶：** `TickMemo`（pyclass）。`world.__dict__["_tick_memo"]` に入ります。設定の世代と `Plan`、人と区域の ID、設備の区域、4つの名前空間の世代、支援の依頼の `ValuesSnapshot` と開いている依頼と書かれた依頼、体調の保存された設定、前回の「変わっていない」の判定。写した世界は新しい記憶から始まります（`__deepcopy__`）。
- **断ったとき：** 記憶なしの `validate(validate_event_history=False)`。`SHELTERSIM_NATIVE=0` と同じ規則と文です。記憶は設定の `Plan` 以外は変えません。検証が失敗したら記憶を捨てます。
- **設定：** 変わったときだけ Python の契約が読みます（`core/tick_validation.py` の `tick_plan`）。pydantic のモデルと `canonical_*`、`selected_models`、`declared_states`、`simulator_signatures` で、どれも D3 で残るものです。読めない設定は例外で、kernel は断り、Python の検証が文を出します。体調の保存された設定も、JSON が変わったときだけ `body_config_ids`（`validate_body_config`）が読みます。設定が名指しする人が今いるか履歴にいるかは毎 tick Rust が見ます。
- **`TickValidationMemo`、`_steady_plan`、`begin`、`commit`、`requests_to_check`、`unsettled_requests` は消しました。** `unsettled_support_requests` は `TickMemo.unsettled_requests` を読みます。Python の検証から `tick_memo` の引数を外しました（`world.validate`、`validate_declared_states`、`validate_simulator_extensions`、`validate_body_state`、`validate_shared_operations_state`、`validate_zone_environment_runtime_state`、`validate_resource_ledger_state`、`resource_metric_values`）。

## 移した規則

| 規則 | Rust | 止めるテスト（`tests/integration/test_after_tick_kernel.py`） |
|---|---|---|
| `validate_presence_state`（宣言された名前空間） | `rules/tick_validation.rs` の `validate_presence`。契約の解析は `rules/population.rs` の `presence_is_plain` | `a presence record from the future`、`not admitted`、`dropped`、`zone unknown`、`archive path to nothing` |
| `validate_population_lifecycle_state` を検証器に | `rules/population_lifecycle.rs` の `validate`。保留の移動の作業はオブジェクトから読むように直しました | `a lifecycle field added`、`a lifecycle person who is here` |
| `validate_staff_operations_state` の作業中の記録 | `state/staff_operations.rs`。作業、職員、辺、予定のイベントを読みます。**職員と作業はオブジェクトから**（T レーンの判断のまま） | `test_a_staff_task_that_disagrees_is_left_to_the_python_rule` 9通り（`busy_until` と `work_queue` と作業の `staff_id` は `__dict__` への書き込み） |
| `_validate_staff_operation_receipt`（最近の職員の作業の受領記録） | `staff_operations::completion_agrees`。`validate_tick` が毎 tick 呼びます | `test_a_staff_receipt_without_its_completion_is_left_to_the_python_rule` 4通り |
| 受領記録の「確かめた」印 | 追記した kernel（`record_other` も）と、履歴の全体を再生して通った Python の検証が付けます（`history_passed`） | ゲートの `fork_resume` などの保存からの復元で断りが0回 |
| 人が変わった tick | 依頼をすべて読み直し、記憶を作り直します | `test_a_settled_request_of_someone_who_left_is_found_when_the_people_change`（一時退所の後、`object.__setattr__` で書いた依頼） |
| `validate_infrastructure_runtime_state` の tick の分 | `after_tick.rs` の `infrastructure` | `test_a_world_with_infrastructure_is_the_kernel_s` |
| `validate_zone_environment_runtime_state` の基準の区域 | `after_tick.rs` | `an environment baseline of an unknown zone` |
| `validate_body_state` の設定（過去の住民）と履歴の人 | `after_tick.rs` の `historical_ids` | `the body config names someone unknown`、`an actor nobody knows`、`test_a_body_record_of_someone_in_the_population_s_history_is_taken` |
| `_validate_zone_capacity` | `rules/overlay.rs` の `total_zone_capacity` を直接 | `test_a_zone_over_its_capacity_is_left_to_the_python_rule`、`at_its_capacity_is_taken` |

**ランダムな書き込み：** 400回、19種類の入れ物に書いて、kernel が受けたら Python の規則も通ることを確かめます（`test_random_writes_taken_by_the_kernel_pass_the_python_rules`）。

## 感度（規則を1つずつ外したコピー）

`<S>/speed/lane-l7-validate/sens/run.py`。`native/` をコピーして規則を1つ外し、別の `target` で作り、`test_after_tick_kernel.py`、`test_staff_rules.py`、`test_population_row.py`、`test_tick_validation.py` を動かしました。係数はないので 1% の変更はありません。

| 外した規則 | 失敗したテスト |
|---|---|
| 在籍の記録の時刻が今より後 | 1件（`a presence record from the future`） |
| 在籍の「受け入れ済み」と世界の人の一致 | 2件（`not admitted`、`test_tick_validation.py` の `presence`） |
| 在籍の `archive_path` をたどる | 2件 |
| 作業中の職員の `busy_until`（`__dict__` への書き込み） | 1件 |
| 作業の `due_minute` = 開始 + 移動 + 設定の時間 | 最初は**0件**。設定の時間を変えるテストを足して1件 |
| 職員の作業の受領記録と完了の一致 | 4件 |
| **人が変わったら依頼を全部読み直す** | **0件（足したテストでも0件）**。下の「確かめていないこと」 |
| 体調の設定の過去の住民 | 最初は**0件**（設定を変えるテストは Python の契約が先に止める）。人が抜けた後に履歴からも消すテストを足して1件 |
| 基盤の能力の範囲 | 1件 |
| 環境の基準の区域 | 2件 |
| 区域の定員 | 1件 |

ゲートのハッシュと影には見えません。良い状態しか来ないからです。影の probe（壊した写し。kernel が受けたら Python も受けること）はゲート 196回、7日分 140回で、kernel はすべて断りました。

## Python に残した規則（手順4で要るもの）

- **WorldState v5 の tick：** すべて Python です（ゲートの 2,320 tick）。**旧モデルを tick の経路から外すのは L9 です。** 列の旧方式の印（`facility_queues_legacy_mode_v1`）も同じで、kernel は断ります。
- **設定の契約：** `tick_plan`、`body_config_ids` が呼ぶ pydantic と `canonical_*`。設定が変わったときだけです。D3 と step4-plan の4節（契約の検査は残す）に従いました。
- **`model_configs` を丸ごと代入した世界：** ミラーがないので毎 tick Python です。`src/` にはありません。
- **kernel が断ったときの `validate`：** 壊れた状態と patch のときの文を出す道です。手順4で `kernel_first` と同じく Rust のエラーに替わります（L10）。
- **kernel の中の Python の契約の呼び出し：** `record_other` の `recent_receipts_hold`（pydantic）は前からあります。

## モデルの担当の方に見ていただくコード

**係数はありません。** Python の検証の形を Rust に写しました：`validate_presence_state`、`validate_staff_operations_state` の作業中の記録、`_validate_staff_operation_receipt`、`validate_infrastructure_runtime_state` の基準の能力、`validate_zone_environment_runtime_state` の基準の区域、`validate_body_state` の過去の住民と履歴の人、`effective_total_zone_capacity` との比較。

**Rust だけの条件（超えたら Python が動きます）：** 正確な型でない値（str の継承、`busy_until` の float、bool）、`Identifier` の末尾の改行、i64 を超える分、ミラーのない設定、v5。

## 結果

2026-11-02、WSL2、16コア、ほかのレーン（L8 の2つ）と同時です。コードは `21210c6` とその後のテストと文書の変更です（Rust と `src/` は同じ）。

| 確認 | 結果 |
|---|---|
| 全テスト（Rust あり） | 最後の回 4,680件成功、8件 skip、1件失敗：`test_refusal_census.py::test_a_run_of_the_driver_shows_its_fallbacks`。基準の #353 でも落ちる、L2〜L4 で消えた fallback を期待するテストで、コーディネーターが #353 で直します。触っていません。ログ `<S>/speed/lane-l7-validate/tests2.log`、最後の回は `tests-final.log` |
| 全テスト（`SHELTERSIM_NATIVE=0`） | 2,078件成功、2,609件 skip、失敗0。`tests-python.log` |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件（素の回、断りの計測の回、このレーンの影の回） |
| `--reversed-order` | 55シナリオ、問題0件 |
| 7日分（`--expect`） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致（計測の回、影の回） |
| `first_difference.py --both` | `population`、`arrivals`、`interventions`、`geometry`、`conditions`、`operational`、`generated_project`、`fork_resume`：段階の digest 計 110,850 か所、違い0件、最終のハッシュも同じ |
| このレーンの影（`after_tick`、`population`）ゲート | 呼び出し 31,530、kernel が受けた 29,210、断り v5 の 2,320。違い0件。比べた依頼 185,071、人が変わって受けた tick 28、依頼を全部読み直した tick 2,776、作業中の職員を読んだ 265、記憶の「変わっていない」の確認 2.5 GB の bytes |
| 同じ影の7日分 | 呼び出し 10,080、受けた 10,080、断り0、違い0件、比べた依頼 305,574。`Error in sitecustomize` 0件 |
| `tick_budget.py`（9600分から480 tick、5回） | 基準との比 **0.983**（0.952、0.989、0.968、0.983、1.003）。A/A 0.995（1.067、0.995、0.991、0.954、1.023）。状態は5回とも一致。load average 23〜50 |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（`core/state_mirror.py` を変えていないため、指示どおり） |

基準のビルドは `20807e9` の別の worktree と別の `target` です（`<S>/speed/lane-l7-validate/base`）。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。
- **同じ値の設定の書き込みも作り直し：** 前の Python の記憶は世代が変わっても等しければ同じと見ました。今は世代だけで、書かれたら `tick_plan` をもう一度呼びます（結果は同じ）。tick は設定を書きません。
- **記録の「確かめた」印は行にあります。** 行が木と違う（`dict.__setitem__` で木の節点に書いた）ときは T レーンの穴と同じです。
- **保留の移動の作業をオブジェクトから読むように変えた** `check_lifecycle` は、コマンドの計画（`leaving`）でも使われます。読むものが行からオブジェクトになっただけで、Python の規則と同じものを読みます。
- **`shadow_metrics.py` の実体の規則の影**は、今は `validate_after_tick` の後にすべての依頼で呼びます（前は記憶の依頼だけ）。
- 全部の影を掛けたゲートと `tracked_mirror_gate.py` は動かしていません（`core/state_mirror.py` は変えていません）。
- **「人が変わったら依頼を全部読み直す」を外しても、どのテストも落ちません。** テストで人を変えられるのは在籍のコマンド（一時退所など）だけで、これは世界を作り直すので依頼も新しいオブジェクトになり、もう1つの条件（辞書が同じオブジェクトで始まらない）で全部を読み直すからです。ゲートでは到着（`arrivals`）で人が変わって kernel が受けた tick が 28 あり、そのたびに Python の規則も通りました（影）。外しても結果が変わらない場合しか作れていない、ということで、規則が要らないとは言えません。
- 7日分には作業中の職員がいません（影の `staff_tasks_read` 0）。その規則はゲート（265回）とテストだけです。
- Python のゲートの行の網羅（`--coverage`）は測っていません。Rust の行の網羅も測っていません。上の影の延べの件数と、テストの壊した状態で見ています。

## レーンとの重なり（このレーンの外の小さな編集）

- `native/src/state/staff_operations.rs`（作業中の記録、`completion_agrees`）、`state/shared_operations.rs`（印、`history_passed`、職員の受領記録）、`rules/common_operations.rs`（`record_other` の印、3行）、`rules/population_lifecycle.rs`（`validate`、作業をオブジェクトから）、`rules/overlay.rs`（`total_zone_capacity`）、`rules/resources.rs` と `rules/metrics.rs`（`metric_values` の使わない引数）、`validation.rs`（`ValuesSnapshot` を crate に）。
- `contracts/presence.py`、`contracts/runtime.py`、`domain/body_state.py`、`domain/common_operations.py`、`domain/environment.py`、`domain/population_lifecycle.py`、`domain/resources.py`、`domain/simulator_state.py`、`experiments/metrics.py`：記憶の引数を外す、検証器の登録。**L8 のレーンが文を触るファイルと重なるかもしれません。** 文は変えていません。
- テスト：`test_after_tick_kernel.py`（書き直し）、`test_population_row.py`、`test_tick_validation.py`、`test_entity_rules_kernel.py`、`test_resources.py`、`test_tracked_write_lint.py` の許可の1行。
- 影：`shadow_after_tick.py`（書き直し）、`shadow_population.py`（kernel の後に比べる形に）、`shadow_metrics.py`。
