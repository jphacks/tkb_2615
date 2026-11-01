# M2 レーン F：行動の周期と `agent_dynamics_v1`

Rust が `agent_dynamics_v1` を型付きの行で持ち、`domain/agent_dynamics.py` と `domain/action_lifecycle.py` の規則を動かします。行動の周期の記録（`agent_action_lifecycle`）は Rust が `EventRow` として追記し、Python の記録のオブジェクトは Python が読むときに作ります。イベントの ID、連番、結果は変わりません。Python の規則は比較の基準とエラーの文のために残ります。

## 移したもの

| Python の規則 | kernel | 呼ばれる場所 |
|---|---|---|
| `record_action_proposal` | `rules/action_lifecycle.rs` の同名の関数（本体は `commit_action`） | `_apply_proposal`（7.8回/tick） |
| `record_applied_action_completion`、`record_applied_population_exit` | 同名 | 適用された結果ごと（各9回/tick） |
| `finalize_active_actions` | 同名 | Run の終わり |
| `record_proposal` | `rules/agent_dynamics.rs` の同名の関数（本体は `remember_decision`） | `_apply_proposal` |
| `agent_knows_facility`、`learn_facility`、`announce_installed_facility` | 同名 | 提案、移動の完了、設備の設置 |
| `add_agent`、`create_agent_dynamics_state`、`canonical_agent_dynamics_config` | `add_agent`、`create_state`、`canonical_config` | 到着、世界を作るとき、署名 |
| `validate_agent_dynamics_state` | `state/agent_dynamics.rs` の `validate` | tick の検証 |

- **開いている行動：** コントローラーの `active_actions` は `ActiveActions`（`rules/action_lifecycle.rs`）で、行として持ちます。保存はされず、復元のときにイベントログから作り直します（今までどおり）。Python の規則が動くときは `as_plain()` で素の dict を渡し、その dict が持ち主になります。次に kernel が動くときに行へ読み直し、合わなければ dict のままで、Python の規則が動き続けます。
- **`events/queue.py` の `event_ids_at_minute`：** `MetricSnapshot` は毎 tick `at_minute` で ID だけを読んでいたので、Rust が書いた記録のオブジェクトを毎回作るところでした。ID だけを返す読み取りを足しました。
- **やめた確認：** `agent_dynamics_valid`（`validation.rs`、`native.py`）。行の kernel が代わります。レーン T の規則に従い、`test_tick_validation_routes.py` の6つの経路に `agent_dynamics_v1` の壊れ方を4つ足しました。Python の `validate_agent_dynamics_state` は残ります。

## 「行を読み、計算し、結果を書く」に合うか

合います。kernel は最初に入力を読み、記録をすべて `EventRow` として組み立て（連番も決めます）、断るなら何も変えずに `Unsupported` を返します。そのあと書きます。**コールバックはなく、規則の途中で Python のコードは動きません。**

まだ Python のオブジェクトを入力として読むものと、なくなる時期：

| 入力 | いつまで |
|---|---|
| 提案（`ActionProposal`） | レーン J。`commit_action` は `Proposal` を、`remember_decision` は `Decision` を受け取るので、kernel の本体はオブジェクトを読みません |
| 結果のイベント（`ScheduledEvent`） | M3 の dispatch |
| `body_state_v1.agents[id]`（`body_needs` の1か所） | M3 |
| `population_lifecycle_v1.history`、`population_dynamics_v1.departures` | M6 |

`id_counters` は Python の dict のままで、C API で書きます。

## レーン J が呼ぶもの

~~~rust
// _apply_proposal と同じ順で。committed が真のときだけ。
action_lifecycle::commit_action(world, active, &Proposal { agent_id, action, target_id, minute, trigger })?;
// 名前空間があるときだけ。
agent_dynamics::remember_decision(world, &Decision { agent_id, action, target_id, minute, next_decision_at, trigger })?;
~~~

`active` は `controller.active_actions` です。どちらも `Unsupported` のときは何も変えていないので、Python の規則を動かします。行動の周期には `active.as_plain()` を渡します。

## 途中まで書いた状態が残るか

`commit` の中でエラーが起きると、その呼び出しの前の記録は追記されたまま、開いている行動は元のままになります。何が起きうるかを調べました。

| エラー | データから起きるか | 扱い |
|---|---|---|
| 重複した ID・連番 | 起きない | 計画の段階で `receipt` が `Unsupported` にします |
| 負の分 | 起きない | 同上（`0 <= start <= minute`） |
| 有限でない小数 | 起きない | 同上。`Encoder` が断るのはこれだけです |
| カウンタのあふれ | **起きました（修正）** | `2**63 - 1` の近くで Rust の加算が折り返し、Python の int と違う連番になるところでした。`EventWriter::of` が `2**62` を超えるカウンタを `Unsupported` にし、Python の規則が動きます |
| `EventLog` や `QueueStore` の借用 | 起きない | 計画の借用は commit の前に手放します。commit の間に Python のコードは動きません |
| メモリ不足 | データとは別 | 例外で Run が止まります（tick の中の他の例外と同じ） |
| 不具合 | データとは別 | 同上 |

`id_counters` に、`"event"` とハッシュが衝突して `__eq__` が Python で動くキーがあれば、`set_item` が Python のコードを動かします。`src/` にそのようなキーはなく、対処していません。

同じ調査で、`dict.pop(tree_node, id)` のように木だけから記録を消した世界（レーン T の「証明できないもの」と同じ経路）で、次の2つも直しました。

- `record_proposal`：木の読みが `RuntimeError` になっていました。書く前に `Unsupported` を返します。
- `learn_facility`、`announce_installed_facility`、`add_agent`：行を変えたあとで木の経路が見つからず、行と木がずれていました。木を先に書くので、失敗しても行は元のままです。`announce_installed_facility` は人ごとに書くので、途中の人で断ると前の人の分は書かれたままですが、そのあと動く Python の規則は同じ値を飛ばすので結果は同じです（この人数をまたぐ場合はテストしていません）。

## `EventWriter` を実際に使って分かったこと

- `record` は負の `realized_minute` を受け取っていました（`EventLogRecord` は断ります。復元もできません）。同じ文で断ります。
- `schedule` は、カウンタが i64 に入らない連番を `Unsupported` にします。
- **`settle` はまだ本番で使っていません。** このレーンには期限の来たイベントがないからです。コードは読みましたが、不具合は見つかっていません。最初の実際の利用者は M3 です。

## 計測

2026-11-01、WSL2、16コア。ほかのレーンと同時に動かしました。

`tick_budget.py`（9600分から480 tick、3回、スレッドの CPU 時間）：基準との比 **0.960**（0.96、0.863、0.975）、A/A 0.978（0.978、0.982、0.962）、状態は一致。統合後の `461edb2` と `de98462`。A/A の揺れの中なので、速くなったとは言えません。統合前の `0425d3d` では 0.974（0.93、0.974、1.007）、A/A 0.960（0.96、0.908、1.005）でした

部品（100人の生成 Run の480分、同時に3回の中央値、計測の枠を含む、µs/tick。統合前の `0425d3d` と `41bde2f`）：

| 手順 | 基準 | Python の規則を行の上で | kernel |
|---|---:|---:|---:|
| `record_action_proposal` | 671 | 724 | 226 |
| `record_proposal` | 26 | 56 | 44 |
| `agent_knows_facility` | 10 | 10 | 7 |
| `record_applied_action_completion` | 213 | 212 | 95 |
| `record_applied_population_exit` | 13.5 | 13.5 | 13 |
| `learn_facility` | 16 | 38 | 14 |
| 合計 | 約 950 | 約 1,050 | 約 400 |

部品の足し算で、全体の差として確かめたものではありません。`record_proposal` だけは基準より遅くなります（木にも書くため）。

## 結果

レーン H の統合後のコード（`461edb2`）で動かした結果です。

| 確認 | 結果 |
|---|---|
| `rule_shadow`（ゲート） | 55シナリオ、問題0件、違い0件。比べた receipt 146.8 MB、値 約 1.39 億個（`agent_dynamics` は約 162 万個） |
| kernel が動いた / 呼び出し（ゲート） | `record_action_proposal` 63,473 / 63,473、`record_applied_action_completion` 75,417 / 75,488、`record_applied_population_exit` 75,487 / 75,488、`finalize_active_actions` 130 / 130、`record_proposal` 62,786 / 62,786、`learn_facility` 23,457 / 23,604（残りは名前空間なし）、`add_agent` 153 / 153、`agent_knows_facility` 13,208 / 13,208、`canonical_config` 39,204 / 39,204、`create_state` 65 / 65、`announce_installed_facility` 2 / 3（残りは名前空間なし）。検証は 35,536 回受け入れ、断り0回 |
| ゲートの断り | 72回（完了 71、人の退出 1）。すべて「記録が同じ値の別のオブジェクトを持つ」で、`interventions`、`population`、`geometry` だけです。Python の規則が値で比べます。世界を写して差し替える Scenario だからと見ていますが、その段階は追っていません |
| `rule_shadow`（7日分） | **断り0回**、違い0件。`record_action_proposal` 87,464回、完了 88,526回、人の退出 88,526回、`record_proposal` 87,464回、`learn_facility` 20,872回、`agent_knows_facility` 22,945回、`canonical_config` 11,091回、`finalize` 1回、検証 10,530回受け入れ。比べた receipt 206 MB。7日分は人の退出の記録が0件です |
| 7日分のハッシュ（`rule_shadow` つき） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。期待値と一致 |
| `gate_all_paths.py --expect --reversed-order` | 55シナリオ、問題0件 |
| 道具の確認（コピー） | kernel の `current_needs.fatigue` を 1.01 倍：`long_generated` で違い 4,065件、ゲートは world と events で失敗。`last_decision_minute + 1`：違い 3,116件、ゲートは world で失敗 |
| ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ） | `domain/action_lifecycle.py` 94% / 61%、`domain/agent_dynamics.py` 90% / 33% |
| ゲートに届かない枝 | 記録の `abandoned`。ゲートでも7日分でも0件で、`test_action_lifecycle_rows.py` の小さな入力だけが比べます |

両モードの全テスト、ゲート、7日分、`ruff`、`clippy` の最終結果は統合した版の `lane_check.sh` で確かめます。

## 残っているもの

- `remove_agent` と `population_lifecycle.py` の書き込みは Python のままです。どれも記録1件の転送です（M6）。
- `inspection/frames.py` と `population_lifecycle.py` が木を読むので、ミラーは捨てられません。
- エラーの文は Python が出します（手順4）。

## モデルの担当の方に見ていただくコード

**数値の係数は移していません。** 判断の規則が Python と Rust の2か所になりました。`_completion_status`、`_need_snapshot`（既定値 0.0）、イベントと行動の対応表、`_is_plain_receipt` の上限（理由 120 文字、活動 80 文字）、`ORIENTATION_FACILITY_KINDS`、方策の版と出典の ID、`FacilityAwarenessConfig` の上限 1000 件です。意味は変えていません。

## 確かめていないこと

- Windows と macOS では動かしていません。
- Run の終わりに記録のオブジェクトをまとめて作る費用（`events_realized.jsonl`）は測っていません。7日分のピーク RSS も測っていません。
- `rule_shadow` の行と木の一致（`verify`）は25回の書き込みに1回です。毎回ではありません。
- `ActiveActions` は `deepcopy` できません。`src/` にコントローラーを写すコードは見つかりませんでした（検索だけで確かめました）。
