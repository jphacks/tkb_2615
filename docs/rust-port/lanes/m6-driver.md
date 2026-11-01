# M6 レーン：tick の順序を Rust に（driver）

`native/src/rules/tick.rs` が1回の tick の段階を順に動かします。kernel は直接呼び、Python の部分は部分ごとに1つの入口で呼びます。`TickEngine.step` から届き、`SHELTERSIM_NATIVE=0` は今までの Python の経路のままです。**結果は変わりません。係数は1つも増えていません。**

## 7日分で得になるか（結論）

**ほとんど得になりません。** 7日分では tick で約 1〜2%、Run 全体で 0.3% 以下で、揺れの中です。tick の時間の大半は kernel の本体で、境界そのものの費用は小さいからです。このレーンの価値は速さではなく、段階の順序を Rust に置いて、手順4で Python の段階を消せるようにしたことです。

| 計測（スレッドの CPU 時間） | 結果 |
|---|---|
| `tick_budget.py`（9600分から480 tick、5回。基準 `db12881`、レーン `725a3b6`） | **0.972**（0.960、0.987、0.972、0.937、0.983）。A/A 1.019（1.019、1.010、0.998、1.021、1.019）。状態は5回とも一致。load average 約 12〜15 |
| 同じ（`fe3633f`。load average 3〜53） | 0.961（0.961、0.980、0.890、0.954、0.982）。A/A 1.014（1.115、1.018、1.014、0.976、0.973）。状態は5回とも一致 |
| 7日分の丸ごとを基準2回と同時に（1組目。load 約 40〜60） | `TickEngine.step` 3,872、3,873 → 3,799 µs/tick（**0.981**）。Run 全体 122.5、122.5 → 122.1 秒 |
| 同じ（2組目） | `TickEngine.step` 3,677、3,737 → 3,685 µs/tick（**0.994**）。Run 全体 119.6、120.8 → 119.8 秒 |

7日分の2組はレーン `fe3633f`、ハッシュはどれも `--expect` と一致しました。基準の側は `phase_observer` を読まないので段階の時間がありません。

7日分のレーンの側は段階の時間を測る `phase_observer` の分（1 tick に14回の Python の呼び出し）を含みます。`tick_budget.py` には含みません。分岐の 0.961 と7日分の 0.98〜0.99 の差のどこまでがこの費用かは、測っていません。

## 境界の費用（入る前と後）

`fork-b1` の 9600分から 240 tick、`cProfile`、同じ tick です。`cProfile` は Python の呼び出しを重く見せるので、回数を見てください。時間は目安です。

| | 基準 | driver |
|---|---:|---:|
| Python → Rust の呼び出し（回/tick） | 138.8 | 87.3 |
| うち `segments.py` の包み | 37.0 | 0.13 |
| うち `kernel_first` の包み | 21.3 | 6.3 |
| うち `driver.py` | 0 | **1.0** |
| うち `state_mirror.py`（検証の `holds`、`follows`、転送の書き込み） | 63.6 | 63.6 |
| Rust → Python の呼び出し（回/tick） | 73.4 | 76.1 |
| Python の関数の呼び出し（回/tick） | 960.5 | 801.4 |
| Python の本体の時間（µs/tick） | 478 | 427 |
| `experiments/` の Python の本体（µs/tick） | 99.6 | 25.3 |
| `__call__`、`_run_phase`、`segments.run`、`ContextScope` | 7回の入口で計 約 70 µs | 1回の入口 |

**段階の入口を越える回数は 58回/tick から7回/tick（`driver.py` 1回と `kernel_first` の 6.3回）に減りました。** ただし `state_mirror.py` の 63.6回はほぼ tick の検証と転送の書き込みで、減っていません。**1 tick に1回の境界という状態にはまだなっていません。** Python の部分が残っているからです（下）。

**保存の間隔ごとに1回にはしませんでした。** 残る1回の呼び出しは 1 µs 未満で、まとめても得がありません。`TickEngine.step` の時計の確認と、保存の前後の処理を動かす必要があり、危ないところが増えます。

## 形

- `TickEngine.step` は `phase_handler.run_tick(world, minute, rng, phase_observer)` を先に呼び、false なら今までどおり `phase_handler` で段階を回します。`run_tick` は `experiments/phases/driver.py` が、Rust があるときだけ controller に付けます。
- **断る条件（何も変える前に `Unsupported`）：** `python_path_only`（`rule_shadow`）、`use_python_entries`、controller の派生クラス、別の世界、`artifacts.trace_level` が `off` でない、`offline` でない router（Provider を呼べる）、`DRIVEN_STEPS` の手順か `DRIVEN_RULES` の規則が定義のときのものでない（クラスと controller のインスタンスの両方を tick ごとに見ます）。`TraceLevel.FULL` のエンジンは `step` が `run_tick` を呼びません。
- `tick_probe.install` と `PhaseProfiler` は `phase_handler` を包むので `run_tick` がなく、Python の経路です。`tick_probe.install(..., on_driver=True)` は `TickEngine.phase_observer` で見て、driver を残します。`first_difference.py` の Rust の側はこれを使います。
- **手順：** kernel を直接呼び、`Unsupported`（何も変えていない）なら、その手順の Python の規則です。`segments.py` の手順は `super(controller, self).手順`、`kernel_first` の規則は `__wrapped__` です。始まったあとの `Unsupported` は `RuntimeError` にします（Python の経路を重ねないため）。
- **Provider の hook：** `consider_support_request` などは kernel の中の同じ場所で動きます。offline の router では driver のままで、`test_the_hooks_of_an_offline_router...` が呼び出しの順と引数を Python の経路と比べます。Provider を呼べる router は Python の経路です。
- **借用：** Python を呼ぶときに持ちません。

## 移したもの

| もの | 元の Python |
|---|---|
| 段階の順序、時計の確認 | `TickEngine.step` のループ、`__call__`、`_run_phase`、`_run_*_phase` |
| `_update_movement` の呼び出し（予定を立て直す人がいれば `_replan_journeys`） | `update.py` |
| 提案の適用のループ | `_apply_due_proposals`、`_apply_proposal` |
| 独立した FIFO のない世界の `ToiletServiceManager.arbitrate`、`ResourceServiceManager.arbitrate` のループ。戻り値を読まないので `find` を呼びません | `domain/facilities.py`、`domain/resource_service.py` |
| `update_staff_dynamics`、`update_staff_operations`、`assign_pending`、`assign_staff_operations`、`schedule_staff_operation_relief` の呼び出し | `arbitrate.py` |
| `admit_pending_returns`、`admit_pending_arrivals` の kernel の直接の呼び出し（driver のときだけ） | `dispatch.py` |

**Python の変更：** `_update_movement` から `_replan_journeys` を、`_assign_staff_support` から `_record_assigned_support` を、`__call__` から `_refresh_config_scopes` と `_config_scopes` を切り出しました。中身は変えていません。`investigate_performance.py` は `__call__` と `_record_outcome` を包むのをやめ、段階の時間を `phase_observer` で測ります。包むと driver が動かないからです。**結果の `OfflineSimulationController._record_outcome` の行はなくなりました。**

## Python に残した部分（手順4で Rust が要るもの）

driver が1 tick に通る入口と、その費用です（`cProfile`、µs/tick）。

| 入口 | 回/tick | 累積 | 理由 |
|---|---:|---:|---|
| `validate_after_tick`（`TickEngine.step` から） | 1 | 571 | **最大。** `TickValidationMemo`、`validate` の前半、`validate_simulator_extensions`。M5 の文書の「Python に残した規則」。`commit` の `deepcopy` 約 77 |
| `artifacts.capture_metric` | 1 | 65 | sink は Python のクラス。`MetricSnapshot` を作る |
| `_consider_due_complaints` | 1 | 24 | 依頼の索引が `_tick_memo`。`consider_complaint_response` に届く |
| `assign_pending` の Python の規則（依頼を取る tick） | 0.07 | 45 | M4、M6 の文書のとおり |
| `_record_assigned_support` | 0.07 | 4 | ケアの記録の Python のループ |
| kernel の中の `_parsed_config`、`selected_models` | 32、8 | 29、6 | 設定の解析のメモ。M6 overlay の範囲 |
| `_person_entry` | 0.42 | 23 | 人の文脈の作り直し |
| `_finalize_actions_at_end` | Run に1回 | | kernel は F レーンにあります |
| `_sample_proximity_contacts` | 設定があるとき | | `proximity_policy.sample` は Python |
| `_replan_journeys` | 7日分0回 | | 道の変更。ケアとベッドの規則を含む |
| `manager.arbitrate` | 独立した FIFO のとき | | `_arbitrate_independent_facility` |

**Python の経路そのもの**（`__call__`、`_run_phase`、`_run_*_phase`、`segments.py`、`kernel_first`）は比較の基準として手順4まで残します。

## モデルの担当の方に見ていただくコード

**新しい係数はありません。Rust だけの条件もありません**（断りは上の条件で、Python が動きます）。判断の順序が Python と Rust の2か所になりました。`rules/tick.rs` の `update`、`propose`、`arbitrate`、`arbitrate_toilets`、`arbitrate_resources` の順序です。**`experiments/phases/*.py` の手順の順を変えるときは `rules/tick.rs` も変えます。** `test_the_driver_takes_a_phase_s_steps_in_the_order_of_its_python_method` が、ずれたら失敗します。

## 結果

2026-11-02、WSL2、16コア、ほかのレーンと同時です。コードは `725a3b6`。その後の変更は `shadow_tick.py`（部分の回数）と文書だけです。

| 確認 | 結果 |
|---|---|
| 全テスト | Rust あり 4,121件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,048件 skip |
| `gate_all_paths.py --expect`（driver の回数つき） | 55シナリオ、問題0件 |
| `--reversed-order`（`fe3633f`。`tick_probe` が入るので Python の経路） | 55シナリオ、問題0件 |
| 全部の影のゲート（`fe3633f`。Python の経路） | 55シナリオ、問題0件、違い0件 |
| 7日分（`--expect`、**driver が動く回**） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致 |
| **`first_difference.py --both`：55シナリオすべて、driver と `SHELTERSIM_NATIVE=0`** | 段階の前後の digest 220,892か所、違い0件。最終のハッシュも同じ |
| `tracked_mirror_gate.py` | **動かしていません。** `core/state_mirror.py` は変えていません |
| `ruff`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| 7日分に `rule_shadow`（`shadow_dispatch`、`shadow_queues`、`shadow_service`、`shadow_tick`。Python の経路） | 同じハッシュ、違い0件 |

**最初の版の `investigate_performance.py` は driver を動かしていませんでした。** `__call__` と `_record_outcome` を包んでいたので、すべての tick が断られ、Python の経路でハッシュが一致するだけでした。プロファイルの回数で分かったので直しました。下の回数がその確認です。

### driver が本当に動いた回数（`SHELTERSIM_DISPATCH_COUNT_ONLY=1`、`stats()`）

| | ゲート | 7日分 |
|---:|---:|---:|
| 動いた tick | 28,892 | **10,080** |
| 断った tick | 2,640（Provider 1,800、trace 840） | **0** |
| 断った kernel（Python の規則が動いた手順） | 6,162 | 720 |

kernel の断りの理由：

| 理由 | ゲート | 7日分 |
|---|---:|---:|
| `a free worker can take a request`（`assign_pending`。依頼を取る tick） | 2,951 | 712 |
| `the policy has no long-term state this kernel knows` | 2,320 | 0 |
| `a staff member has a task`（`relief`） | 265 | 0 |
| `someone gives up on the queue` | 167 | 4 |
| `an independent FIFO may be here` | 149 | 0 |
| `no floor plan: a request at once` | 92 | 0 |
| `a recent receipt was not checked here` | 81 | 0 |
| `a temporary room may have a bed` | 68 | 0 |
| `the namespace has no row` | 60 | 1 |
| `a free worker may repair`、`a free worker can patrol` | 7 | 2 |
| `a request closed at once` | 2 | 1 |

**7日分の断りはすべて、M4 と M6 で記録した kernel の断りと同じものです。** `the namespace has no row` の1回は最初の tick（0分の update の段階）で、別の 1440分の回で確かめました。どの名前空間かは確かめていません。

driver から Python の部分に届いた回数（ゲート）：`_consider_due_complaints` 28,890、`_record_assigned_support` 1,994、`_sample_proximity_contacts` 180、`_finalize_actions_at_end` 120、**`_replan_journeys` 1回**。ゲートの tick と `_consider_due_complaints` の差の2回は、`StateInvariantError` の回と見ていますが、追っていません。

### 感度（`rules/tick.rs` をコピーで変更）

係数はないので、順序とループを変えました。`first_difference` は `generated:480:30`、ゲートは `--only generated_project`、テストは `test_tick_driver.py` です。

| 変更 | `first_difference` | ゲート | テスト |
|---|---|---|---|
| `sample` を `arbitrate` の前に | 違いあり（5行目） | **一致** | 1件失敗 |
| 列の先頭の確認を飛ばす（トイレ） | 違いあり | 不一致 | 4件失敗 |
| 資源の列の位置の確認で止まらない | 違いあり | 不一致 | 4件失敗 |
| Run の最後の `_finalize_actions_at_end` を飛ばす | 違いあり | 不一致 | 2件失敗 |
| `_record_assigned_support` を飛ばす | 違いあり | 不一致 | 5件失敗 |
| `_consider_due_complaints` と `relief` を入れ替え | **0件** | **一致** | 順序のテストだけが失敗 |
| `apply_deltas` と `update_long_term` を入れ替え | **0件** | **一致** | 順序のテストだけが失敗 |
| `_view_before_proposals` を残さない | **0件** | **一致** | 足したテスト1件が失敗。結果は変わらず、遅くなるだけです |

**順序の入れ替えの2つは、結果の比較には見えません。** どのシナリオでも、同じ分に両方が書かないからです。このため `test_the_driver_takes_a_phase_s_steps_in_the_order_of_its_python_method` が `phases/*.py` の AST と `tick.rs` の文字の順序を比べます。2つの入れ替えで失敗することは確かめました。

### ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ / 行数）

`experiments/phases/arbitrate.py` 40 / 40 / 40、`update.py` 127 / 119 / 182、`propose.py` 293 / 204 / 359、`sample.py` 33 / 32 / 34、`metrics.py` 10 / 10 / 10、`driver.py` 27 / 26 / 27、`domain/facilities.py` 227 / 190 / 262、`domain/resource_service.py` 409 / 350 / 477。**「kernel あり」には trace などで Python の経路を通った tick が入ります。** Rust の行の網羅は測っていません。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。
- **7日分そのものの段階ごとの比較はしていません。** `first_difference` はゲートのシナリオ（`generated_project` は720分）です。7日分はハッシュ3つです。
- **driver の `_replan_journeys` はゲートで1回だけ、7日分で0回です。** 切り出しただけの Python のコードですが、driver から渡す `state` はこの1回とテストに頼ります。
- **`rule_shadow` は driver を通りません。** driver の比較はハッシュ、`first_difference`、`test_tick_driver.py` です。
- **`--reversed-order` も driver を通りません。** driver が順に読む dict はなく、施設は kernel が ID 順にするので順序に頼らないはずですが、driver を通した逆順の回はありません。
- **patch の表は手で持ちます。** `DRIVEN_STEPS`、`DRIVEN_RULES` にない関数を driver が飛ばすようになると、その patch は届きません。今の表は、driver が呼ばなくなった入口を読んで作りました。kernel の中で呼ぶ規則の patch は、Python の経路でも今までどおり届きません。
- **Provider を呼べる router がある Run は Python の経路です。** driver の得はありません。
- **`measure_performance` の Run（`PhaseProfiler`）も Python の経路です。** 結果は同じで、測る値は driver のないものになります。
- 境界の表は `cProfile` の1回ずつで、時間は目安です。
- 1回の Run の中で driver と Python の経路が tick ごとに入れ替わることがあります（patch を途中で入れたとき）。テストの外では起きません。


## レーンとの重なり

- `native/src/rules/*.rs` の13ファイル：kernel の `fn` に `pub(crate)` を足しただけです（`queues.rs`、`service.rs`、`toilet.rs`、`dispatch.rs` など）。M6 handlers rest、M6 overlay とは1語の衝突があり得ます。
- `native/src/rules/dispatch.rs`：`run_inner`、`admit_returns`、`admit_arrivals` を足しました。
- `experiments/runner.py`、`core/engine.py`、`benchmarks/tick_probe.py`、`benchmarks/first_difference.py`、`benchmarks/investigate_performance.py`。
- **他のレーンへ：** 段階の手順を足す、`domain/facilities.py` の `arbitrate` の手順を変えるときは、`rules/tick.rs` と `experiments/phases/driver.py` の表も変えます。driver の中の順序は `phase_handler` の経路と別に書いてあります。
