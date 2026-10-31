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
- NaN・無限大（`Encoder.float` が断ります）
- 丸ごと代入された `metric_state`（`mirror.holds` が false）
- hash の木が中を分けて持つ名前空間（`_WORLD_TREE` の `body_state_v1` など）。**これらを持つレーンは、履歴を行の外に持つ形を決めるまで、Python の経路のままです。**

## 計測

RESULTS_PLACEHOLDER

## 残っているもの

- **EventLog の確認：** `same_saved_records`、`extend_saved`、`record_json` が保存の時間の上位に残ります。行に入っていないので省きませんでした。レーン E が持てば、同じ考え方で省けます。
- **本体の履歴の比較：** `HistoryPins` は読む量を減らしただけで、履歴の長さに比例します。履歴を Rust が持てば要りません。
- **Rust が持っていない大きな名前空間：** `social_network_v1.relationships`、`body_state_v1.relationships` は、保存のたびに符号化と解析をします。
- **`world_frame`：** 2 MB の frame を毎回作ります。今回は手を付けていません（`geometry.model_dump` は小さく、まとめて省ける場所がありませんでした）。
- **索引の SQLite：** 索引の CPU の大半は SQLite の挿入と commit です。`records` の6つの索引は検索が使っています。
- **切り離し（`detach`）：** `_with_empty` は `dict(model_state)` で木を読むので、切り離した名前空間を毎回作り直します。切り離すレーンが直す必要があります。
