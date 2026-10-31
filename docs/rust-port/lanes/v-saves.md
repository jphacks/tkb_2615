# V：保存を Rust の行から作る

Checkpoint の保存（`Checkpoint.capture` と `TimelineStore.record`）のうち、Rust の行が持つものを、Python のオブジェクトを読まずに作ります。**Checkpoint の byte 列、hash、archive の chunk と pack は変わりません。** fsync の回数、保存の間隔は変えていません。保存のスレッドも足していません。

## 移したもの

| もの | 場所 | 中身 |
|---|---|---|
| 実体の行の正準 JSON | `state/entities.rs` の `EntityStore.rows_json` | 人、設備、職員、支援の依頼、活動中のタスク。項目ごとの `write_json` を `row!` が作ります。`hydration_need[v6]` のように、文書に現れる条件も宣言します。 |
| 名前空間の行の正準 JSON | `NamespaceMirror.row_json`（P5 の `state_row!`） | `OWNED_NAMESPACES`・`OWNED_FIELDS` のすべて |
| 取り出し | `core/state_mirror.py` の `owned_row_bytes` | 行を持つものを全部集めます。**他のレーンは `state_row!` と登録の1行だけで、保存の経路に入ります。** |
| 履歴の古い項目の確かめ | `history.rs` の `HistoryPins` | `same_json_items` の前に通します |
| 判断の観測状態の大きさ | `checkpoint.rs` の `dumps_length` | `record_decision` の `json.dumps` の長さ。**`canonical_json` の長さそのものは使えません。** 区切りの空白のぶん短く、16,384 の判定が変わるからです。区切りの数を足します。 |
| 索引の行の入力 | `checkpoint.rs` の `index_row` | 鍵の組と、トークンを読む文字列を返します。Python はそれぞれ1回だけ読みます（`lru_cache`）。 |

## 仕組み

- **行に書ける道すべてに印：** `Table::changed` を、`slot_mut`（`get_mut` と転送の両方が通ります）、`reserve_slot`、`release_slot` が立てます。印のない行は同じ bytes オブジェクトを返すので、**同じオブジェクトなら同じ行**です。`Table` の `rows` は private で、他の道はありません。
- **実体のリストは `SavedItems`：** `Checkpoint.world_state["agents"]` などはリストではなく `SavedItems` です（読むたびに解析、`==` はリストとも比べられます）。`event_log` と同じ扱いです。
- **chunk の鍵：** 実体のリストは追記専用ではありません（途中の依頼が変わります）。先頭と末尾の項目で chunk を見分ける `chunk_key` は使いません（`splice_rows`、`append_only`）。
- **復元の確認：** 復元の結果は入力の byte 列だけで決まるので、前の確認で等しいと示した行と同じ bytes オブジェクトなら、復元も比較もしません。変わった行だけ、今までどおり `from_dict` の規則を通して比べます。確認の世界は `_bare_world` で、行を持ちません（前は確認のたびに `start_core` が全部の行を作っていました）。失敗した保存はこの記憶を捨てます。
- **Rust が持つ名前空間の比較：** 行の byte 列から読んだ文書を、木と今までどおり比べます。小さく（1つ数十 µs）、省くと読み戻しの違いが見えなくなるので省きません。最初の版は省いて、`test_a_restored_world_that_differs_...` が失敗しました。切り離した名前空間はここで木を作り直すので、切り離すレーンは比べ方を決める必要があります。
- **変わらないフィールド：** Python の経路のフィールドも、前の保存と同じ byte 列なら文書を解析し直しません。
- **`HistoryPins`：** 項目の中のすべてのオブジェクトへの参照を持ち、同じオブジェクトを同じ順で持っていれば変わっていないとします。文字列や数を読まないので、散らばったメモリを読みません。参照を持つので、同じアドレスに別の値は来ません。項目を最初に入れるときは文書と型まで比べ（`1` と `True`、タプルとリストを区別）、false は何も言わないので、呼ぶ側が今までどおり文書と比べます。順序だけ変わった項目（`--reversed-order`）はこの比較で通り、持ち直します。
- **索引：** `entities` と `tokens` は主キーだけの表なので、rowid なしで作ります。前に作った索引はそのままで、同じ答えです。

## 落ちる先（taint、NaN など）

`rows_json` が `Unsupported`、`row_json` が `None` のときは、そのフィールドだけ Python のオブジェクトから作ります。エラーの文は変わりません。

- taint した store、名前空間
- `world.agents` などが store の `IdMap` でないとき（`__setattr__` を通らずに入れた map）。**この確かめは `tracked_mirror_gate.py` で見つかった不具合の修正です。** 確かめがなかった版は、session が `__setattr__` を差し替える回で、古い行を保存していました（`population`、`arrivals` の6行）。本番の `__setattr__` を通る経路ではゲートもハッシュも通っていて、ハッシュの比較だけでは見えませんでした。
- NaN・無限大（`Encoder.float` が断ります）
- 丸ごと代入された `metric_state`（`mirror.holds` が false）
- hash の木が中を分けて持つ名前空間（`_WORLD_TREE` の `body_state_v1` など）。**これらを持つレーンは、履歴を行の外に持つ形を決めるまで、Python の経路のままです。**

## 計測

2026-10-31、WSL2、16コア。ほかのレーンが同時に動いていました（load average 約 30〜150）。7日分の生成 Run（project の経路）を、基準（レーンの開始点 `dd6a294`）と `daca212` で同時に動かした1回の組です。そのあとの修正は `IdMap` の確かめだけで、その commit でも7日分のハッシュ、ゲート、両モードのテストを確かめました。`rule_shadow`、`--reversed-order`、`tick_budget.py` は `daca212` の結果です。別の組（修正前の版）でも比はほぼ同じでした。

| 7日分（スレッドの CPU 秒） | 基準 | 今 | 比 |
|---|---:|---:|---:|
| `Checkpoint.capture` | 72.0 | 49.9 | 0.69 |
| `save_captured_branch` | 15.4 | 14.0 | 0.91 |
| `TimelineStore.record`（上の2つと frame） | 113.5 | 89.9 | 0.79 |
| `_cache_frame` | 23.8 | 23.6 | 0.99 |
| 索引のプロセスの ingest | 141.7 | 121.7 | 0.86 |
| `record_decision` | 2.8 | 1.7 | 0.61 |
| `TickEngine.step` | 109.1 | 109.1 | 1.00 |
| 計算のスレッド全体 | 226.3 | 202.1 | 0.89 |

負荷の高い回の値なので、秒そのものは静かな回より大きくなります。比を見てください。fsync はどちらも 3,034回でした（壁時計は 43.8 秒と 45.3 秒で、揺れの範囲です）。Run の保存先の大きさは 4.31 GB から 3.52 GB になりました（`history.sqlite` の rowid なし）。ピーク RSS は同時に動かした1回で 4,492,892 KB と 4,530,604 KB（+0.8%）でした。

| 確認 | 結果 |
|---|---|
| 7日分のハッシュ | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。基準と同じ |
| 保存の byte 列（基準と比べた7日分） | 338個の Checkpoint の hash がすべて同じ。`history.sqlite` 以外の 1,020 個のファイルは名前と大きさが同じで、pack と frame の全 byte の SHA-256 も同じ |
| 索引の内容 | `records` 852,674行、`entities` 4,923,026行、`tokens` 3,242,150行、ほかの表も、全列で並べて比べて同じ。ファイルは rowid のぶん違います |
| `rule_shadow`（7日分） | 保存 336回、実体 554,258項目（183 MB）と名前空間 672回（15 MB）の byte 列が Python の経路と差0件。落ちた回数0。復元の確認 336回で、等しいと分かっていた行 499,568、復元した行 54,690。省かない比較と結果の違い0件 |
| `rule_shadow`（ゲート） | 履歴キャッシュのある保存 217回、実体 18,380項目（8.5 MB）と名前空間 434回（3.4 MB）で差0件。落ちた回数0。ゲートでは保存の回数が少ないので、taint や NaN の経路は `test_capture_rows.py` が通します |
| 道具の確認（`rule_shadow`） | 小数の書き出しを 1.01 倍したコピー：差を報告し、Run も途中で止まりました。行の印を立てないコピー（古い byte 列を返す）：実体4種の差と「復元の確認の結果が省かない比較と違う」を毎回報告 |
| `tick_budget.py`（9600分から480 tick、3回、同時） | 基準との比 0.984（0.956、0.999、0.984）。A/A 1.009（1.056、0.967、1.009）。状態は3つとも一致 |
| `gate_all_paths.py --expect` | 55個すべて一致、問題0件（素の回、`rule_shadow` の回とも） |
| `gate_all_paths.py --expect --reversed-order` | 55個すべて一致、問題0件 |
| 両モードの全テスト | Rust あり 2,318件通過・6件 skip。`SHELTERSIM_NATIVE=0` 1,781件通過・543件 skip |
| `tracked_mirror_gate.py` | 220行のうち、最初の回は6行で問題（`population`、`arrivals`。上の「落ちる先」の不具合）。修正後にその2つのシナリオの8行を動かして問題0件。残りの53シナリオは修正前の commit の回で問題0件で、全体を修正後にもう一度は動かしていません |
| 索引の行の kernel（`index_row`、1行あたり） | 18.3 → 13.1 µs（行動の行）、46.6 → 35.1 µs（会話の行） |

## 索引の rowid なしの表

`entities` と `tokens` を読み書きする SQL はすべて `storage/history_index.py` にあります（`src/` のほかの `sqlite3.connect` は `storage/streams.py` の1か所で、別のファイルです）。rowid の順序に頼るものはありませんでした。

| 場所 | SQL | 順序に頼らない理由 |
|---|---|---|
| `search`（人、世帯、職員、区域、会話の ID） | `EXISTS(SELECT 1 FROM entities e WHERE ...)` | 有無だけ。外の `ORDER BY minute,kind,key` が順序を決めます |
| `search`（`event_id`、`text`、`causal`） | `EXISTS(SELECT 1 FROM tokens t WHERE ...)` | 同じ |
| `conversation_tail` | `entities e JOIN records r ... WHERE e.entity=? ORDER BY e.key DESC LIMIT ?` | `ORDER BY` があり、1つの会話の中で key は重なりません（主キーが branch, kind, key, entity） |
| `flush` | `INSERT`、`INSERT OR IGNORE` | 読みません |
| `ingest` の作り直し | `DELETE ... WHERE branch=?` | 読みません |

`tests/integration/test_history_index_formats.py` は、同じ Run を新しい形、古い形（同じコードで書いたもの）、古い形で rowid を key と逆順にしたものの3つにして、全部の問い合わせの答えを、ページと cursor ごと JSON の文字列で比べます。古いファイルへの書き込みと作り直しも確かめます。

## 確かめていないこと

- **Windows と macOS では動かしていません。** `HistoryPins` と `rows_json` が使う C API（`PyDict_Next`、`PyList_GetItem`、`Py_NewRef` 相当）はどれも stable ABI です。
- ゲートの行の網羅率（`--coverage`）は測っていません。保存の経路に届いた回数は上の表のとおりです。
- `rows_json` と `Table::changed` は、今の Rust の規則（`add_agent_stress` と心理）では実体を書く規則がほぼありません。実体を書く規則が増えたあとの7日分の比較は、そのレーンの `rule_shadow` で確かめます。

## 残っているもの

- **EventLog の確認：** `same_saved_records`、`extend_saved`、`record_json` が保存の時間の上位に残ります。行に入っていないので省きませんでした。レーン E が持てば、同じ考え方で省けます。
- **本体の履歴の比較：** `HistoryPins` は読む量を減らしただけで、履歴の長さに比例します。履歴を Rust が持てば要りません。
- **Rust が持っていない大きな名前空間：** `social_network_v1.relationships`、`body_state_v1.relationships` は、保存のたびに符号化と解析をします。
- **`world_frame`：** 2 MB の frame を毎回作ります。今回は手を付けていません（`geometry.model_dump` は小さく、まとめて省ける場所がありませんでした）。
- **索引の SQLite：** 索引の CPU の大半は SQLite の挿入と commit です。`records` の6つの索引は検索が使っています。
- **切り離し（`detach`）：** `_with_empty` は `dict(model_state)` で木を読むので、切り離した名前空間を毎回作り直します。切り離すレーンが直す必要があります。
