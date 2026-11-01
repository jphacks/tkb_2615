# M5 レーン：毎 tick の指標と、残りの毎 tick の検証

毎分の KPI の列（`MetricSnapshot.from_world`、`operational_kpi_values`）と、`WorldState.validate` の実体とイベントの規則を Rust に移しました。結果は変わりません。Python の規則は比較の基準として残ります。呼び出し1回につき kernel が1つで、コールバックはありません。指標は読むだけ、検証は「すべて通るか `Unsupported`」で、どちらも書きません。

| もの | 場所 |
|---|---|
| `snapshot_fields`、`kpi_values` | `native/src/rules/metrics.rs` |
| `entity_rules`、`population_dynamics` | `native/src/rules/tick_validation.rs` |
| 比較 | `benchmarks/rule_shadow/shadow_metrics.py` |
| テスト | `tests/integration/test_entity_rules_kernel.py` |
| 依頼ごとの待ち（`open_waits`。`wait_snapshot` と共用に切り出し） | `native/src/rules/staff.rs` |
| 予定イベントを行にせずに読む `QueueStore::pending_events` | `native/src/state/events.rs` |

## 入る前の tick（`fork-b1` の 9600分から480 tick）

レーンの開始点 `26771e0`、スレッドの CPU 時間、load average 2〜5 の1回です。`cProfile` なしで、関数の出入りに計測の枠があります。

| 部分 | µs/tick | tick に占める割合 |
|---|---:|---:|
| tick 全体 | 2,727 | |
| `validate_after_tick` | 612 | 22% |
| propose | 606 | 22% |
| dispatch | 533 | 20% |
| update | 487 | 18% |
| arbitrate | 184 | 7% |
| sample | 111 | 4% |
| metrics | 98 | 4% |

**指標は 4% で、検証が最大でした。** そのため検証を先に見ました。`validate_after_tick` の内訳は、`validate` の本体の実体の規則 約 270、`validate_simulator_extensions` 154（`validate_body_state` 37、`validate_population_dynamics_state` 34 を含む）、`TickValidationMemo.begin` 98、`commit` 16 です。

## 調べて決めたこと

µs/tick は基準とレーンを同時に動かした3組です（load average 40〜50 で、低負荷の約2倍の値です）。

| 規則 | 基準 | レーン | 扱い |
|---|---:|---:|---|
| `validate`（下を含む） | 893〜980 | 635〜726 | |
| 実体とイベントの規則 | `validate` の内数、下の差から約 460 | 209〜244 | **kernel** `entity_rules` |
| `from_world` | 149〜162 | 83〜92 | **kernel** `snapshot_fields` |
| `validate_population_dynamics_state` | 52〜56 | 0（kernel は `rust_accepts` の内） | **kernel** `population_dynamics`（待っている人と動的な文脈がない状態） |
| `effective_total_zone_capacity` | 40〜44 | 0 | `entity_rules` に。一時空間か部屋があれば Python |
| `validate_zone_environment_runtime_state` | 44〜50 | 4〜5 | **kernel ではなくメモ**（下） |
| `operational_kpi_values` | 13（測定ループ） | 2.4（同） | **kernel** `kpi_values`。Project の経路では動きません |
| `validate_effect_program_state` | 35〜42 | kernel で 29（load 約 40、別の1回） | **やめました**（下） |

**kernel にして、やめたもの：**

- **`validate_effect_program_state`：** 予定イベントごとに `provenance.get` を読むだけで、Python が約 16 µs（低負荷）です。kernel も同じだけオブジェクトを読むので、tick に対する割合で 0.73% → 0.63% と、別々の回の比較で揺れの中でした。持つ価値がないのでやめました。
- **イベントを行として読む最初の形：** `entity_rules` の最初の版は `QueueStore::row` で全部のイベントを行にしました。Python が予定したイベント（1 tick に約 11個）を初めて読むたびに、payload と provenance を全部写すので、1 tick に約 75 µs 増えました。今は行があれば行、なければオブジェクトの必要な項目だけを読みます。

**環境の設定：** 毎 tick に pydantic で読み直していました。`tick_memo` に、設定と区域が変わらないあいだ読み直さないよう足しました。形の確認（`isinstance` 3回）は Python のままです。`core/tick_validation.py` の冒頭の説明と同じ理屈で、保存の完全検証は毎回読みます。

## 検証の kernel がオブジェクトを読む理由

`entity_rules` は行ではなくオブジェクトを読みます。`agent.__dict__["stress"] = 1.5` は行に届かず、規則はこれを見る必要があるからです（[t-tick-validation.md](t-tick-validation.md)）。**なので kernel は思ったほど速くありません。** 属性を 1 tick に約 2,500回（数えた見積もり）読み、tick の中ではキャッシュが冷えています。測定ループで 56 µs（load average 約 5）、実際の tick で 148〜244 µs（load average 30〜50）です。行で判定すればもっと速いはずですが（試していません）、この書き込みを tick で見つけられなくなります。これは判断なので、変えていません。

イベントは行があれば行から読み、なければオブジェクトから読みます。

## Python に残した規則（手順4で Rust が要るもの）

- **`core/world.py`：** `_validate_entity_rules`、`_validate_event_rules`（保存の完全検証、WorldState v5、kernel が断ったとき）、`resource_service_v1` の検証、`_validate_zone_capacity`（一時空間か部屋があるとき、定員を超えるとき）、`validate` の前半の確認。
- **`core/tick_validation.py`：** `TickValidationMemo` のすべて。
  - **`begin` が今の検証で最大です（低負荷で約 97 µs）。** `model_configs ==`（JSON で 124 KB の比較）と `saved_json(population_presence_v1)`（21 KB の書き出し）が中心で、測定ループでどちらも約 55 µs（load average 約 30）です。どちらも C のコードの中で、kernel にしても速くなりません。`population_presence_v1` を Rust が持てば世代の比較で消えます（M6）。`model_configs` は別の手段が要ります。
- **`domain/population.py`：** 待っている人（`pending_arrivals`）か動的な文脈（`profile_context`）がある状態の検証。7日分にはありません。
- **`domain/body_state.py`：** `validate_body_state` の設定と履歴の確認（約 36 µs）。`historical_ids` を毎 tick 作るのが中心です。
- **`events/effects.py`：** `validate_effect_program_state`（上）。
- **そのほかの検証：** `domain/environment.py`、`domain/infrastructure.py`、`domain/operational.py`、`domain/facility_queues.py`、`domain/temporary_spaces.py`、`contracts/runtime.py` の検証。どれも数 µs です。
- **`experiments/metrics.py`：** `MetricsRecorder.capture` の分の確認、`to_dict`、`from_dict`。`_snapshot_fields`（資格のある空いた職員がいる依頼、ASCII でない設備の種類、行の taint）、`_support_wait_snapshot`。
- **`experiments/operational_kpis.py`：** `operational_kpi_values`（人がいない世界、taint、台帳の行がないとき）。
- **`experiments/phases/metrics.py`：** `finalize_active_actions` の呼び出し（Run の終わりに1回。kernel は F レーンにあります）。

## モデルの担当の方に見ていただくコード

**式と値は変えていません。**

| Python | Rust |
|---|---|
| `MetricSnapshot.from_world` の列（平均、比、`stress >= 0.7`、`"toilet" in kind.casefold()`、未解決の依頼） | `rules/metrics.rs` の `snapshot_fields` |
| `operational_kpi_values` の列 | `kpi_values` |
| `validate` の実体とイベントの規則（定員、列、作業、職員、依頼、イベント） | `rules/tick_validation.rs` の `entity_rules` |
| `validate_population_dynamics_state`（待っている人がいない場合） | `population_dynamics` |

**Rust に書いた係数：** 高ストレスの閾値 0.7。検証の値：経路の容量 1〜100、資格の種類の文字列、`support_request_timeout` の待ちの理由（4種類）、`population_dynamics_v1` の `policy_version`、`source_id`。

**Rust だけの条件（超えたら Python が動きます）：** ASCII でない設備の種類、i64 を超える int、正確な型でない値（bool、str の継承、`1.0` と `1` の違いを含む）。`payload["quantity"]` が float の `1.0` のイベントは Python の `==` なら通りますが、kernel は断ります。

## 確認

`98b609a`、2026-11-01、WSL2、16コア、ほかのレーンと同時です。基準は `26771e0`。その後の変更は、この文書と `gate_all_paths.py` の `TICK_SIDE`（網羅の集計の対象）だけです。

| `tick_budget.py`（9600分から480 tick、3回） | 基準との比 | A/A | load average |
|---|---:|---:|---|
| `98b609a` | **0.915**（0.969、0.897、0.915） | 1.052（1.017、1.052、1.057） | 15〜35 |
| `5a086fc`（同じ規則） | 0.889（0.921、0.889、0.820） | 1.050（1.111、1.050、0.981） | 40〜55 |

状態はどの回も3つとも一致しました。A/A の揺れを考えると、短くなったのは約 8〜11% です。

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 3,752件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,073件成功、1,685件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件。全部の影の回も同じ |
| `--reversed-order` | 55シナリオ、問題0件。このレーンの影の回も同じ（3変種の合計で `entity_rules` 87,630回、`snapshot_fields` 92,286 / 95,271回、`kpi_values` 43,260回、断りの理由は下と同じ、違い0件） |
| 7日分 | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致（素の回、影の回） |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（指示どおり）。`core/state_mirror.py` は変えていません |

**全部の影を掛けた `--reversed-order` は動きません。** `tick_probe.py` の `reversed_within_phase` が包んだ `assign_pending` には `__wrapped__` がなく、`shadow_staff.py` が `AttributeError` で止まります（55シナリオすべて）。M4 職員レーンの影から後の道具の問題で、このレーンのコードではありません。直していません。上の結果はこのレーンの影だけです。

| `rule_shadow` | ゲート（全部の影） | 7日分（このレーンの影だけ） |
|---|---:|---:|
| `entity_rules` 動いた / 呼び出し | 29,210 / 29,210 | 10,080 / 10,080 |
| 読んだもの（実体、依頼、予定イベントの延べ） | 2,135,025 | 2,310,260 |
| 検証した依頼 / 列の項目 / サービスの項目（延べ） | 185,071 / 131,886 / 29,120 | 305,574 / 23,665 / 34,764 |
| 定員を Python に任せた区域（延べ） | 5,320 | 0 |
| 壊した写しを断った回数 | 98 | 70 |
| `population_dynamics` 動いた / 呼び出し | 33,774 / 35,715 | 10,454 / 10,454 |
| `snapshot_fields` 動いた / 呼び出し | 30,762 / 31,757 | 10,415 / 10,417 |
| `kpi_values` 動いた / 呼び出し | 14,420 / 14,420 | 0（下） |
| 比べた値（`snapshot`、`kpi`） | 1,745,504、447,020 | 1,921,695、0 |
| 違い | 0 | 0 |

7日分は `generated` の `project` の経路で、`rule_shadow` の全部の影の7日分は動かしていません（約 2.5 時間かかるため）。**7日分の `kpi_values` は0回です。** Project の経路の `_MetricSink` は `operational_kpi_values` を呼びません。比較はゲートとテストだけです。

**断り：** `snapshot_fields` はどれも「資格のある空いた職員」で、`wait_snapshot` と同じ理由です（ゲート 995回、7日分 2回）。`population_dynamics` のゲート 1,941回はすべて、待っている人か動的な文脈がある状態です。`entity_rules` は0回です。WorldState v5（ゲートの 2,320 tick）は kernel を呼びません。

**ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）：** `experiments/metrics.py` 144行のうち 125行（87%）/ 124行（86%）、`experiments/operational_kpis.py` 33行のうち 31行（94%）/ 8行（24%。kernel が動くので Python の本体は通りません）、`core/tick_validation.py` 157行のうち 139行（89%）/ 同じ、`core/world.py` 894行のうち 684行（77%）/ 676行（76%）、`domain/environment.py` 236行のうち 206行（87%）/ 同じ、`domain/population.py` 486行のうち 412行（85%）/ 411行（85%）。`metrics.py` と `operational_kpis.py` を測るため、`gate_all_paths.py` の `TICK_SIDE` に足しました。 Rust の行の網羅は測っていません。上の延べの件数と、テストの壊した状態で見ています。

### 感度（コピーで変更。`--only long_generated`、影はこのレーンだけ）

| 変更 | 影 | ゲートのハッシュ | `test_entity_rules_kernel.py` |
|---|---:|---|---:|
| 閾値 0.7 → 0.707 | 6件 | KPI が不一致 | 1件失敗 |
| ストレス平均 ×1.01 | 20件以上 | KPI が不一致 | 3件失敗 |
| トイレの平均待ち ×1.01 | 20件以上 | KPI が不一致 | 3件失敗 |
| KPI の空腹の平均 ×1.01 | 20件以上 | **一致**（ゲートのハッシュはこの列を見ません） | 1件失敗 |
| 上限を 1.01 に（`is_unit`、1%） | 4件（壊した写しの確認） | 一致 | 2件失敗 |
| 列の人の行き先を見ない（1% ではない） | 0件 | 一致 | 最初は0件。`queued target`、`served target` を足し、1件失敗 |
| 待っている依頼の期限を見ない（1% ではない） | 0件 | 一致 | 1件失敗 |
| 定員を超えた区域を Python に任せない（1% ではない） | 0件 | 一致 | 1件失敗 |
| 最初の人の ID の重複を見ない（1% ではない） | 0件 | 一致 | 1件失敗 |

**検証の規則を外しても、影とハッシュには見えません。** ゲートには良い状態しか来ないからです。見つけるのは壊した状態のテストで、`test_random_writes_taken_by_the_check_pass_the_python_rules` は値をランダムに書いて、kernel が受け入れたものを Python の規則に通します（400回）。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。`py_sum` は `mathx` です。`libm` は使いません。
- **`entity_rules` の規則を1つずつ外した確認は、すべての規則ではしていません。** 上の3つ（と `population_dynamics` の1つ）だけです。`BREAKS` の 37 通りが kernel の断りを見ますが、Python の規則のすべての枝に対応していることは確かめていません。ランダムの書き込みが補いますが、400回です。
- **`entity_rules` の中で Python のコードが動かないのは、属性が普通の属性であることに頼っています。** `__getattr__` を持つ別のクラスを実体に入れた場合は別です（`src/` にはありません）。
- **KPI の kernel は行を読みます。** `agent.__dict__` の書き込みは見えません（行が古い値を持ちます）。`wait_snapshot` と同じです。指標は tick の検証より先に動くので、その分の KPI は古い値です。`src/` にこの書き込みはありません。
- **`entity_rules` は `resource_ledger_v1` の予約を行から読みます。** 木の節点への `dict.__setitem__` は見えません（T レーンで記録した性質）。
- **低負荷での tick の比は測っていません。** どの回も load average 13 以上です。

## レーンとの重なり

- `native/src/state/events.rs`（M3 dispatch のレーン）：`PendingEvent`、`PendingSource` と `QueueStore::pending_events` を足しました。既存の関数は変えていません。
- `native/src/eventlog.rs`：`max_realized_sequence`、`event_ids_at` を足しました。
- `core/world.py`：`validate` の本体を `_validate_entity_rules`、`_validate_event_rules`、`_validate_zone_capacity` に分けました。中身は `_validate_zone_capacity` の切り出しのほか変えていません。
- `native/src/rules/staff.rs`：`wait_snapshot` の本体を `open_waits` に切り出しました。
