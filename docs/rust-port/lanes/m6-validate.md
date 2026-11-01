# M6 レーン：tick の終わりの検証を1回の呼び出しに

`native/src/rules/after_tick.rs` が、`TickValidationMemo.begin` の比べるものがすべて前回のままの tick で、`begin`、`validate`、`commit` を1回の呼び出しで行います。毎分の指標の `MetricsRecorder.capture` も Rust に移しました（`rules/metrics.rs` の `capture`）。**結果は変わりません。係数は1つも増えていません。**

kernel は文を持ちません。すべての規則が通るときだけ返し、記憶を進めます。それ以外は記憶を変えずに `Unsupported` を返し、Python の検証がこれまでどおり動いて、通すかエラーの文を出します。Python の規則は比較の基準として残ります。

## 効果

| 計測（スレッドの CPU 時間） | 基準 `6a7ad7a` | レーン |
|---|---:|---:|
| `tick_budget.py`（9600分から480 tick、5回、同時。load average 4〜5） | | **0.959**（0.912、0.959、0.983、0.993、0.955）。A/A 1.012（1.049、1.012、1.085、1.008、1.008）。状態は5回とも一致 |
| 同じ（3回。このときの commit は `5b24a82`、規則は同じ） | | 0.934（0.990、0.934、0.919）。A/A 0.979 |
| `validate_after_tick`（順に3組、480 tick。load average 約 5） | 309、342、313 µs/tick | 219、226、229 µs/tick（**約 −28%**） |
| `TickEngine.step`（同じ回） | 1,886、2,086、1,912 µs/tick | 1,703、1,768、1,787 µs/tick |

tick は約 4〜7% 短くなりました。A/A の揺れが同じくらいなので、これより細かくは言えません。残りの大半は Rust の本体です。

`cProfile`、240 tick（同じ分。時間は目安で、回数を見てください）：

| | 基準 | レーン |
|---|---:|---:|
| Python の関数の呼び出し（回/tick） | 801 | 440 |
| Python の本体の時間（µs/tick） | 427 | 210 |
| `state_mirror.py` の呼び出し（回/tick） | 63.6 | 32.4（下） |

## `validate_after_tick` の内訳（入る前）

`cProfile`、240 tick、累積 570 µs/tick。**58 µs の `commit` の `deepcopy` は分岐の最初の1回で、毎 tick ではありません。**

| 部分 | µs/tick | 中身 |
|---|---:|---|
| `validate` の Python の本体と、下の Python の規則 | 約 150 | 下の規則のほか、`rust_validated`、`rust_accepts`、`_rows_are_what_the_world_holds` の16回、11回、11回（36 µs） |
| Rust の kernel | 約 215 | `entity_rules` 108、`information.validate` 18、`shared_operations.validate_tick` 17（うち `find` 12）、`movement.validate` 14、`agent_dynamics.validate` 13、`effects.validate_state` 7、ほか |
| `validate_body_state` の Python | 約 45 | `historical_ids` を毎回作る、`records_since_previous_tick`、設定の JSON 9 |
| `pending()` | 18 | `validate_operational_events_state` と `validate_infrastructure_runtime_state` のために毎 tick 作る |
| `is_legacy_queue_checkpoint`、`validate_facility_queue_state` | 24 | `independent_queues_required` を2回 |
| `requests_to_check`、`_settle_requests` | 56 | `report_writes_to` 48回 |
| `begin` | 26 | |
| `commit` の `deepcopy` | 58（1回だけ） | |

## 形

- **`Plan`：** 通った Python の検証のあとに `core/tick_validation.py` の `_steady_plan` が作ります。設定から読む値（宣言された名前空間、必須の名前空間、モデルの有無など）です。設定が変わるまで作り直しません。`infrastructure` の設定がある、`resource_service` がない世界には作らず、毎 tick が Python です。作るときに設定が例外を出したら作りません（`operational_events` は状態があると tick の検証で読まないので、ここで失敗させると結果が変わります。最初に見落とし、テストを足しました）。
- **記憶：** Python のオブジェクトのままです。Python の検証と tick の途中の走査が同じものを読むからです。kernel は全部が通るまで書きません。
- **検証の kernel：** `_RUST_VALIDATORS` から呼びます。登録がない規則は kernel の中から Python の規則を呼びます（`validate_temporary_spaces_state`、`validate_geometry_overlay_state`、`validate_facility_queue_state`、`validate_population_lifecycle_state`）。**M6 overlay のレーンが検証の kernel を登録すれば、そのまま使います。**
- **`_rows_are_what_the_world_holds`：** 名前空間ごとに読み直すのをやめ、`follows` を1回、`holds` を名前空間ごとに Rust で行います。kernel の間に動く Python（`find`、`_parsed_config`）は書きません。
- **断る条件：** `install` の表の関数（`domain/simulator_state.py` の `_install_after_tick_kernel`）が定義のときのものでない、`python_rules` の印、`python_path_only`（shadow）、`use_python_entries`、`schema_version` が 6 でない、Python の規則が読むかもしれない形のすべて。
- **指標：** driver が `_MetricSink` のときに `metrics.capture` を直接呼びます。`capture_metric` が `capture_metric_as_defined` のままで、インスタンスに差し替えがなく、`metrics` が `MetricsRecorder` そのもののときだけです。`RunArtifactWriter` などほかの sink は Python です。
- **`_parsed_config`：** `movement_geometry.rs` がスコープの辞書を直接読みます。見つからない、スコープの外なら Python の関数です。

## 実体の規則はオブジェクトを読むままにしました

T レーンと M5 の判断を変えていません。`agent.__dict__["stress"] = 1.5` は行に届かないので、行で判定するとこの書き込みを見つけられません。行で判定しても健全だと示せる代わりの手段は見つかりませんでした（`PyDict_Watch` は stable ABI になく、T レーンの見積もりどおり実体の `__dict__` を作ると読みが遅くなります）。テスト `stress above 1 past the row` と shadow の probe が、この経路を kernel が断ることを確かめます。**なので `entity_rules` の約 108 µs（cProfile の値）は残ります。今の検証で最大の部分です。**

## 他の入口の結果

| 入口（driver の文書の値） | 結果 |
|---|---|
| `capture_metric` 65 | 移しました。残りは `snapshot_fields`、`metric_values`、`unsettled_support_requests` の本体です |
| `_parsed_config` 29 | Python の関数を呼ばなくなりました（スコープに入ったあと） |
| `assign_pending` 45、`_consider_due_complaints` 24、`_record_assigned_support` 4 | **M6 handlers rest のレーンが移しています。** 触っていません |
| `_person_entry` 23 | **毎 tick の費用ではありません。** 分岐のあとの最初の tick に100人分の100回で、続く479 tick は0回です（確かめました）。保存から戻すたびの費用で、移していません |
| `selected_models` 8回/tick（約 7 µs） | 移していません。`body_state.rs`、`queues.rs`、`support.rs` から呼びます |

### `state_mirror.py` の 63.6回/tick は誰か

| 呼び出し | 回/tick（前 → 後） | 誰が、なぜ |
|---|---:|---|
| 検証（`rust_validated`、`rust_accepts`、`_rows_are…`、`_table_holds…`） | 49 → 0 | `validate` の中。Rust に移しました |
| 記憶（`namespace_stamp`、`field_stamp`、`same_stamp`） | 13 → 2 | `begin`、`_refresh_config_scopes` |
| `TrackedDict`、`TrackedList` の `__deepcopy__` | 19.6（分岐のあと1回ずつ） | `_person_entry`、`commit`、`_refresh_config_scopes`。毎 tick ではありません |
| `IdMap.__setitem__`、`__delitem__` | 2.75、2.01 | kernel（`toilet.rs`、`service.rs`）が `tasks.set_item`、`del_item` で Python の上書きを通します |
| `TrackedList` の `append`、`remove`（と `__delitem__`）、`pop`、`sort`、`clear`、`[:] =` | 1.47、0.9、0.55、0.36、0.42、0.45 | 同じ kernel が設備の列、職員の作業、予約の ID を Python のメソッドで書きます |

**書き込みの約 10回/tick は移していません。** どれも kernel が Python の上書きを呼んで Rust の `map_set`、`forward` に戻る形で、`cProfile` で計 約 27 µs、本当は約 10 µs（tick の 0.5%、見積もり）です。`rules/` の中の各所の書き換えで、他のレーンの編集中のファイルに重なるので、やめました。手順4で Python の木がなくなると消えます。

## Python に残した規則（手順4で Rust が要るもの）

- **`core/tick_validation.py`：** `begin`、`commit`、`requests_to_check`、`unsettled_requests`、`_steady_plan` のすべて。`unsettled_requests` は `added_values` を 1 tick に3回（辞書の長さに比例、約 26 µs）呼びます。IdMap の世代で省けますが、`dict.__setitem__` が見えなくなるのでやめました。
- **`core/world.py`：** `validate` のすべて。kernel が断った tick と、保存の完全検証で動きます。
- **設定の解析：** `selected_models`、`simulator_signatures`、`declared_states`、各 config の `model_validate`、`independent_queues_required`、`operational_events_enabled`。`Plan` の入力です。
- **kernel が Python で呼ぶ規則：** `validate_temporary_spaces_state`、`validate_geometry_overlay_state`、`validate_facility_queue_state`、`validate_population_lifecycle_state`（上）。
- **kernel が断るもの：** `infrastructure` の設定がある世界、WorldState v5、`validate_body_state` の本体（`body_config_json` がない、いま世界にいない人の履歴）、`population_presence_v1` が変わった tick。
- **`experiments/metrics.py`：** `MetricsRecorder.capture`。ほかの sink と kernel が断ったときに動きます。

## モデルの担当の方に見ていただくコード

**係数はありません。** Rust に写した検証の形だけです。`rules/after_tick.rs` に次があります。`validate_body_state` の履歴の項目、`validate_operational_events_state` と `_has_exact_schema` の形、`infrastructure` のない世界のイベントの型、`effect_program_environment` の接頭辞。イベントの型の集合は Python の定数を `Plan` で渡します。

**Rust だけの条件（超えたら Python が動きます）：** 正確な型でない値、履歴の actor がいま世界にいない、`resource_delivery` のイベントが予定にあって `operational_events_v1` がない（Python の規則は `shipment_id` があるときだけ止めます）。`rules/metrics.rs` の `capture`：i64 を超える分、float の分。

## 結果

2026-11-02、WSL2、16コア、ほかのレーンと同時です。`src/` と `native/` のコードは `94a4761`。その後の変更はテスト、`shadow_after_tick.py`、文書です。

| 確認 | 結果 |
|---|---|
| 全テスト（`2a5e866`） | Rust あり 4,200件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,127件 skip |
| `gate_all_paths.py --expect`（driver の回数つき） | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件。`tick_probe` が入るので driver は通りませんが、この kernel は通ります |
| 7日分（`--expect`、driver が動く回） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致。Run 104 秒（1回の参考値） |
| **`first_difference.py --both`：55シナリオすべて** | 段階の前後の digest 220,892か所、違い0件。最終のハッシュも同じ |
| 全部の影のゲート | 55シナリオ、問題0件、違い0件 |
| このレーンの影だけのゲート | 55シナリオ、問題0件、違い0件。probe 117回 |
| 7日分にこのレーンの影だけ（**driver が動く回**） | 同じハッシュ、違い0件。probe 90回 |
| `tracked_mirror_gate.py` | **動かしていません。** `core/state_mirror.py` は変えていません |
| `ruff`、`cargo clippy`（`disallowed_methods`） | 問題なし |

全部の影のゲートは最初 違い14件でした。このレーンの probe が壊した写しを kernel に渡すと、他の影（`rows_valid`、`validate`）が「Python が断ったものを受けた」と報告していました（13回の probe の設定 13件と、ストレス1件）。probe をこの影だけのときに限ったところ0件になりました。**kernel の違いではありません。** 消えたことで確かめましたが、14件を1件ずつ照らしてはいません。

### kernel が本当に動いた回数

| | ゲート | 7日分 |
|---:|---:|---:|
| 呼び出し | 28,501 | 10,079 |
| 受けた | 26,607 | **10,079** |
| 断った | 1,894 | **0** |

7日分の残りの1回は最初の tick で、`Plan` がまだありません。ゲートの tick のうち呼び出しがない約 3,000回は、それぞれの最初の tick と、`Plan` のない世界（v5、`infrastructure`）です。

断りの理由（ゲート）：宣言された名前空間（`population_presence_v1` に別の場所の `archive_path` がある）646、`resource_service_v1` の kernel 598、`population_dynamics_v1` の kernel 348、`staff_operations_v1` の kernel 230、共有運用 42、人が変わった 28、履歴の人がいない 1、`population_presence_v1` の変更 1。

比べた値：受けるたびに記憶なしの Python の規則をすべて通し、記憶を別に計算して比べました。

| | ゲート | 7日分 |
|---|---:|---:|
| Python の規則が通った回数 | 26,607 | 10,079 |
| 読んだ人（延べ） | 749,687 | 1,007,900 |
| 比べた依頼（延べ） | 167,902 | 305,555 |
| 残した定員の区域（延べ） | 5,271 | 0 |

### 感度（`rules/after_tick.rs`、`rules/metrics.rs` をコピーで変更）

係数がないので 1% の変更はありません。規則を外しました。テストは `test_after_tick_kernel.py`、`test_metric_capture.py`、`test_tick_validation_routes.py`。ゲートと影は `--only generated_project`、このレーンの影だけです。

| 変更 | テスト | ゲートのハッシュ | 影 |
|---|---:|---|---:|
| 記憶を `minute` に進めない | 1件失敗 | 一致 | **20件以上** |
| `written` を片方に報告する | 1件失敗 | 一致 | **20件以上** |
| `_requests` を伸ばさない | 1件失敗 | **不一致** | **20件以上** |
| 設定の世代を見ない | 2件失敗 | 一致 | 1件（probe） |
| `capture` の資源の行を足さない | 1件失敗 | **不一致** | 0件 |
| `kernel` の no を yes とする | 33件失敗 | 一致 | 0件 |
| 木と行の同一性を見ない | 11件失敗 | 一致 | 0件 |
| その他 19通り（比べる項目、履歴の各項目、イベントの型、スキーマ、差し替えの表、`written` の消去、reopened、`capture` の順序と遅れた台帳など） | それぞれ1件以上失敗 | 一致 | 0件 |
| 順序を written, open, added に | **0件** | 一致 | 0件 |

最後の1つは結果を変えません。`checked` の順序が `_open_requests` に現れるのは written に開いた依頼があるときで、そのときは reopened の処理が辞書の順に作り直します（読んで確かめました）。**最初の版のテストは9通りを見逃しました**（設備の区域、台帳、体の履歴3通り、reopened、`_requests`、`minute`、人）。体の履歴は木を追跡の経由で壊していて、規則に届く前に行が断っていたためで、`dict.__setitem__` で壊す形に直しました。人の比較は他の kernel も断るので、理由の文で確かめます。直したあと、この9通りを変更し直して、テストが失敗することを確かめました。

**検証の規則を外しても、ハッシュと影にはほとんど見えません。** 良い状態しか来ないからです。見つけるのはテストの壊した状態と、ランダムな書き込み（300回。kernel が受けたら Python の規則も通ること）です。

### ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ / 行数）

`core/tick_validation.py` 165 / 165 / 185、`core/world.py` 695 / 687 / 905、`domain/simulator_state.py` 172 / 167 / 188、`domain/body_state.py` 287 / 114 / 327、`domain/temporary_spaces.py` 81 / 71 / 92、`experiments/metrics.py` 125 / 124 / 144。Rust の行の網羅は測っていません。

## 確かめていないこと、危ないところ

- **弱くなったところが1つあります。** `validate_body_state` は毎 tick に `population_dynamics_v1` から `historical_ids` を作り、hash できない ID があれば `TypeError` で止まりました。kernel は作りません。世代が同じなら前回通ったものと同じと見ます。木の節点への `dict.__setitem__` でこの値を入れると、次の完全検証まで止まりません。`core/tick_validation.py` の説明と T レーンに書いた穴と同じ種類で、`src/` にこの書き込みはありません。テストはありません。
- **`entity_rules` の中で Python が動かないという前提は M5 のままです。**
- **`install` の表は手で持ちます。** 表にない Python の関数を kernel が飛ばすようになると、その patch は届きません。`validate` が呼ぶ関数を足すときは表と `after_tick.rs` も変えます。**`validate` に規則を足すと、`after_tick.rs` にも足さないと kernel が通します。** 比べるのは shadow とテストで、書き忘れを機械的には止めません。
- **Windows と macOS では動かしていません。** 足した C API は `PyObject_GenericSetAttr`（stable ABI）だけです。
- **7日分の段階ごとの比較はしていません。** 7日分はハッシュ3つと、このレーンの影です。
- 低負荷の tick の比は測っていません。load average 4〜5 が一番低い回です。内訳の表は `cProfile` の1回です。
- **`Plan` に持つ `resource_config` と設定の値は、`model_configs` の世代が同じ間だけ正しいものです。** 世代は `dict.__setitem__` を見ません。これは入る前の `configs_unchanged` と同じです。

## レーンとの重なり

- `native/src/rules/movement_geometry.rs`（M6 overlay）：`parsed` と `PythonParts` だけです。
- `native/src/rules/tick.rs`（M6 handlers rest、overlay）：`metrics`、`Table`、`install` の引数、`path_is_python_only`。
- `experiments/phases/driver.py`：`DRIVEN_RULES` に4行と `install` の引数。
- `native/src/rules/resources.rs`：`metric_values` に `pub(crate)`。
- `native/src/rules/tick_validation.rs`：`entity_rules_watching` に切り出し。`core/tracked.rs`：`namespace_table_type`。
- `domain/simulator_state.py`、`core/world.py`、`core/tick_validation.py`、`experiments/timeline.py`、`benchmarks/rule_shadow/shadow_tick.py`。
- `events/handlers.py`、`domain/care_receipts.py`、`domain/geometry_overlay.py`、`domain/temporary_spaces.py`、`domain/facility_queues.py` は触っていません。
