# M3 レーン：dispatch のループと移動の完了

M3 の最後のレーンです。`_run_dispatch_phase` を Rust（`native/src/rules/dispatch.rs`）が動かし、`AgentTravelCompletionHandler` も移しました。**結果は変わりません。係数は1つも増えていません。**

## Rust の dispatch は得か（結論）

**今の時点では、ほとんど得になりません。損もしません。** ループそのものはほぼ0になりましたが、もともと段階の時間の小さな部分でした。tick で見えるほどの差はありません。

| 計測（スレッドの CPU 時間、9600分から 480 tick、同時実行） | 結果 |
|---|---|
| `tick_budget.py`（基準 `bcdc42a`、レーン `599eba6`） | **1.060**（1.060、1.095、0.962）。A/A 0.982（0.982、1.110、0.954）。状態は3つとも一致。load average 7〜15 |
| 同じ（基準 `97ebe46`、レーン `c739195`） | 1.010（1.079、1.010、0.929）。A/A 0.972（1.034、0.956、0.972）。状態は一致。load average 約 5 |
| 同じ（統合前、基準 `408ad51`、`263d1d1`） | 0.972（0.972、0.914、1.000）。A/A 1.017（1.060、0.991、1.017）。load average 約 25〜30 |
| `_run_dispatch_phase`、同時に5回（`599eba6`。基準、A/A、レーン。µs/tick） | 757、753、663 / 859、869、874 / 795、766、776 / 783、790、701 / 817、797、717 |
| 上の比（レーン /（基準と A/A の平均）） | 0.878、1.012、0.994、0.891、0.888。**中央値 0.891**。A/A は 0.964〜1.012 |
| 同じ5回の tick 全体の比 | 0.953、1.032、1.035、0.959、0.950。中央値 0.959。A/A と同じ程度の揺れです |
| `c739195` の5回 | 比 0.757（基準が外れ値）、0.925、0.977、0.961、0.930。中央値 0.93 |
| 統合前の3回 | レーン / 平均 = 1158 / 1148、1283 / 1329、1185 / 1237 で中央値 0.965 |

段階で約 60〜90 µs/tick（段階の約 7〜11%、tick の約 2%）減る計算で、`tick_budget.py` の揺れより小さい差です。予算の中で、速くなったとは `tick_budget.py` では言えません。境界を越える費用が得を消しているわけでもありません。ループの中の境界は、Python のハンドラーに渡すイベントの分（約 1.1回/tick）だけです。

**中身**（統合前のレーンに一時的なタイマーを入れた1回の実時間、負荷約 32。タイマーはコミットしていません。µs/tick）：

| 部品 | µs/tick | 回/tick | 持ち主 |
|---|---:|---:|---|
| Python のハンドラー（`dispatch_one`） | 263 | 1.08 | M4 の資源とトイレ、M5、M6 |
| 移動の完了（`_after_arrival`、列の確認の呼び出しを含む） | 218 | 2.04 | 大半はサービスの要求。M4 の残り |
| `record_applied` のうち完了の記録 | 100 | 7.35 | すでに kernel |
| `record_applied` の残り | 116 | | 大半はケアの記録（`append_applied_care_transition`、Python） |
| `record_social_event` | 202 | 6.34 | すでに kernel |
| 記録を書く（`settle`） | 146 | 8.68 | レーンの費用。下を参照 |
| `_sync_movement_after_events` | 76 | 1 | ほぼ `finish_movement` と `_finish_stopped_journey` の呼び出し |
| `social_interaction` | 68 | 6.63 | すでに kernel |
| `observe_event` | 45 | 6.34 | すでに kernel |
| ループ自身（上の合計との差） | 約 30 | | |

- **最初の版は得がありませんでした（基準 1279、レーン 1251）。** 毎回 `EventRow` を読んでいたからです（8.7回/tick、タイマーで 165 µs/tick）。`ScheduledEvent` がすでにあるイベントでは行を作らず、オブジェクトから読むようにしました（`EventRef`）。単独で測ると行の読みは 1.5 µs ほどなので、tick の中で高いのはキャッシュの冷えとアロケーションだと見ていますが、確かめていません。
- **記録を書くのは Python の半分ほど：** 単独で測って、`EventDispatcher.settle` 12.1 µs、`settle_event` 6.3 µs（1件、社会イベントの大きさ）。8.7回/tick で約 50 µs で、これが今回減った分のほとんどです。

**残りが得になる時期：** 段階の残りは kernel の本体と Python の規則です。ハンドラーの 263 µs と `_after_arrival` の大半は、`ToiletServiceManager`、`ResourceServiceManager`、`care_receipts` が移る M4 の残りで減り、イベントを行で予定する `_apply_proposal` のあとは Python のオブジェクトも作らなくて済みます。このレーンの価値は速さではなく、段階の順序と表が Rust に移って、それらのレーンが表に1行足すだけで済むことです。**tick の差が見えるのはそこからです。**

## 移したもの

| もの | 場所 | 元の Python |
|---|---|---|
| ループ、表、段階の手順の順 | `rules/dispatch.rs` の `run_phase` | `_run_dispatch_phase`、`EventDispatcher.dispatch_due` |
| 移動の完了 | `rules/travel_completion.rs` | `AgentTravelCompletionHandler.apply` の `_after_arrival` の前まで |
| 社会的な行動の抑止の3つの文 | `rules/social_interaction.rs` の `apply_event` | `_apply_typed_interaction` |
| 記録の書き込み（Python の記録を作らない） | `EventWriter::settle`、`settle_object`、`EventStore::append_outcome` | `EventDispatcher.settle` |
| `_sync_movement_after_events` の判定 | `rules/dispatch.rs` | 同名。止まった人だけ `_finish_stopped_journey` を呼びます |
| 完了の記録を行かオブジェクトから | `complete_for_event`（`rules/action_lifecycle.rs`） | `record_applied_action_completion` の kernel |

## 表

`events/handlers.py` が読み込みのときに `install_table` で渡します。`social_interaction` と `agent_travel_complete` が Rust の関数です。イベントごとに `dispatcher.handlers` を読み、**そのクラスで、`apply` が定義のときのものなら** Rust の関数を呼びます。ほかはすべて `controller._dispatch_one_event`（Python の dispatcher のイベント1件分）です。`ScheduledEvent` はそのとき初めて作ります。

- テストの差し替え（`handlers[...] = 偽物`、`monkeypatch.setattr(Handler, "apply", ...)`）は、イベントごとに見るのでそのまま届きます。
- `Unsupported` は最初の変更より前だけです。関数が断ったイベントは Python が初めから処理し、その文で失敗します。
- `provenance` に `effect_program_v1` があるイベントは、ガードを Python が読みます。
- `dispatcher` のクラスが違う、キューやログが Rust のものでない、`id_counters` が素の dict でないときは、段階全体を Python が動かします。
- コールバックをまたいで借用を持ちません。Python を呼んだあとは `EventWriter::of` を呼び直し、断られたら記録は `EventDispatcher.settle` に任せます。
- `@pytest.mark.python_rules` は段階の kernel を外します。`tests/support/python_rules.py` に名前を足しました（`record_social_event`、`observe_event`、`learn_facility`、段階の手順など）。

## 抑止の文を Rust に移した判断

移しました。ゲートで社会イベントの約 12%、7日分で約 5% です。Python に任せると kernel が断ってから Python の規則が例外を投げるので二度手間で、3つの文は定数です。Python の手順でその文より前にある確認がすべて通ったあとにだけ返すので、文が変わることはありません。`rule_shadow` がゲートで 12,354 件、7日分で 2,736 件の文を Python の規則と比べ、違いは0件です。`failed` の文と、ほかの種類の `interaction` は Python のままです。

## `EventWriter::settle` を実際に使って分かったこと

E レーンの版のままでは使えませんでした。

- **`Ok(false)` が「行がない」と「列にない」を区別できません。** ハンドラーが自分でイベントを外して記録を書くと、ループは次に何をするか分かりません。
- **今の列の行を記録していました。** Python の dispatcher は `due` の時点のイベントを記録します。同じ値で置き換えられたイベントでは同じですが、API としては違います。
- **ハンドラーが書いた記録があるときの比較がありませんでした。** 重複のエラーになるところでした。

`settle(&row, ...)` は渡された行を記録し、列にあれば外し、記録がすでにあれば `Ok(false)` を返します。呼ぶ側が `EventDispatcher.settle` で比べます。`remove_row` は使い手がなくなったので消しました。テストは `test_event_rows.py` の3件です。

## 順序

- 列の `due` の時点のイベントを順に処理します（分、優先度、連番）。処理の前に、なくなっていれば飛ばし、別のものになっていれば Python の `dispatch_one` が比べます（ゲートで 1,164件。`DueState::Changed`）。
- `id_counters["event"]` と `events/handlers.py:438,643,772` の乱数のキーは変えていません。割り当ては同じ順序で動きます。
- **後の手順はすべての結果のあと、同じ順で動きます：** 完了と退出と移動の記録 → 空間の更新 → 道の閉鎖 → 帰還 → 移動の同期 → 到着 → 情報の接触。`_consider_social_contact_event` が LLM の hook に届くのは今までと同じ場所で、借用は持っていません。
- 取り消されたイベントの記録が、取り消したイベントの記録より先に来ることがあります（ゲートの1シナリオ、3変種で各1回）。Python の手順も同じです。`rule_shadow` の最初の版はこれを違いと報告しました。`events_skipped` が増えた段階だけ許すように直しました。

## 残したもの

- `_after_arrival`（部屋の中の接近、トイレと資源の要求）。M4 の残りが移します。
- 列の確認が呼ぶ `agent_is_at_queue_slot`、`agent_is_at_queue_trip_destination`、`agent_is_at_facility_service_point`、`facility_has_independent_queues`（`domain/movement.py`、`domain/facility_queues.py`）。読むだけです。
- `sleep_bed` と `presence_relocation`：`movement_v1` と `population_lifecycle_v1` を書くので `Unsupported` です。ゲートで計39回、7日分で0回。
- 社会の kernel（`apply_event`、`record_social_event`、`observe_event`）は `ScheduledEvent` を読みます。**イベントが行だけのときはオブジェクトを作ります。** 今は Python が予定するのでオブジェクトがあり、無駄はありません。M4 の `_apply_proposal` が行で予定するようになったら `EventRef` に変えます。
- ほかのハンドラーは M5、M6 です。Python の dispatcher は `dispatch_one` として残ります。

## 結果

2026-11-01、WSL2、16コア。**`599eba6`（`bcdc42a` をマージしたあと）。** 逆順のゲートだけは次の `5e2cf7a`（`tick_probe.py` の修正）です。**7日分の `rule_shadow`、`tracked_mirror_gate.py`、ゲートの網羅は1つ前の `c739195`（`97ebe46` のマージのあと）の値です。**

| 確認 | 結果 |
|---|---|
| 全テスト | Rust あり 3727件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2073件成功、1660件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` に `rule_shadow` | 55シナリオ、問題0件、違い0件 |
| 7日分（`--expect`） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致 |
| 7日分に `rule_shadow`（`c739195`） | 同じハッシュ、違い0件 |
| `tracked_mirror_gate.py`（`c739195`） | 220行、問題0件 |
| `ruff`、`cargo clippy` | 問題なし |

kernel が本当に動いた回数（`stats()`。直接呼ぶ普通の回。`SHELTERSIM_DISPATCH_COUNT_ONLY=1`）：

| | ゲート | 7日分 |
|---|---:|---:|
| 段階 動いた / 断り | 31,532 / 0 | 10,080 / 0 |
| イベント Rust / Python | 61,328 / 19,183 | 80,767 / 10,495 |
| 飛ばした / 置き換え | 2 / 1,164 | 0 / 0 |
| 社会 動いた（うち抑止）/ 断り | 32,781（4,068）/ 82 | 56,797（2,736）/ 0 |
| 移動 動いた / 断り | 28,547 / 13 | 23,970 / 0 |
| `record_social_event`、`observe_event` 動いた / 断り | 28,713 / 0 | 54,061 / 0 |
| 完了の記録 動いた / 断り | 38,030 / 0 | 63,837 / 0 |
| `learn_facility` 動いた / 断り | 23,604 / 0 | 20,872 / 0 |
| 移動の同期 動いた / 断り | 28,410 / 0 | 10,080 / 0 |
| 記録 Rust / Python | 61,328 / 0 | 80,767 / 0 |

断りの理由：社会は `an event without a typed interaction` 26、`another type of interaction` 56。移動は `sleep_bed` と `presence_relocation` の13。**7日分の断りは0件です。**

`rule_shadow`（`shadow_dispatch.py`。各規則を Python の入口から呼びます。ゲートは3変種の合計）：

| | ゲート×3 | 7日分 |
|---|---:|---:|
| 段階 呼び出し / 動いた | 94,596 / 94,590 | 10,080 / 10,080 |
| 記録の bytes を Python の `json` と比較 | 241,497 | 91,262 |
| 移動の値（人の3項目とタスク） | 345,648 | 95,880 |
| `_after_arrival` の引数の比較 | 29,415 | 11,537 |
| 抑止の文の比較 | 12,354 | 2,736 |
| 違い | 0 | 0 |

- **呼び出しと動いた数の差の6回**は、段階が `StateInvariantError` を出した回です（ゲート1回あたり2回）。ゲートのハッシュは Python の回と一致するので、同じ例外と見ていますが、シナリオは追っていません。
- **道具の確認（コピー）：** 抑止の差 0.15 を 0.1515 に：`rule_shadow` が違いを報告し、480分の `world_hash` も変わりました。到着後の活動を取り違える：違いを報告。`encode_outcome` の分を 1 足す：記録の bytes の違いを報告し、読み込みも `checkpoint_incompatible_or_corrupt` で止まりました。
- **ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）：** `experiments/phases/dispatch.py` 131/136 行（96%）/ 91/136（67%）、`events/handlers.py` 1101/1417（78%）/ 1028/1417（73%）。

## 逆順のゲートと影の組み合わせの修正

`bcdc42a` を取り込むと、`--reversed-order` に `rule_shadow` を付けた回の `reversed_within_phase` が55シナリオすべてで止まりました。`shadow_staff.py` が `assign_pending.__wrapped__` を読み、`tick_probe.py` の包みがそれを隠していたからです（このレーンの規則ではありません）。包みが元の `__wrapped__` を渡すようにしました。

## モデルの担当の方に見ていただくコード

**新しい係数はありません。** 判断の規則が Python と Rust の2か所になりました。

- `rules/travel_completion.rs`：移動の完了の確認の全部（目的ごとのタスクの種類、列の確認）。到着後の活動（`staff_support_travel` は `seeking_help`、ほかは `idle`）。`resource_for_facility` の種類（`water_station`、`meal_distribution`）。
- `rules/dispatch.rs`：`_sync_movement_after_events` の列の目的の4つ。
- `rules/social_interaction.rs`：抑止の文。`0.15` は M3 の社会関係のレーンで移したものです。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません（既存の `PyDictProxy_Type` などだけ）。
- 行だけのイベントが Rust の関数に来る経路は、本番では動きません。`test_a_walk_that_has_only_a_row...` だけが通します。
- `rule_shadow` の回は Python の入口を通るので、直接呼ぶ回の比較ではありません。同じ kernel を呼ぶことと、ハッシュ、`test_a_run_is_the_same_in_rust_through_python_entries_and_in_python`（240分）で確かめます。
- `sleep_bed` の kernel はありません。影はこの目的で Python の規則を動かさず、Python が1回だけ動きます。
- 移動の影は、人の3項目、タスクの有無、`_after_arrival` の引数を比べます。`world` の全体は比べません。全体はハッシュです。
- Python の記録のオブジェクトを持たなくなった分のメモリは測っていません。`events_realized.jsonl` を書くときに作る費用も測っていません。
- タイマーの内訳は統合前のコードで、実時間です。
