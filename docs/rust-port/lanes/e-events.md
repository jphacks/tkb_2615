# M2 レーン E：EventQueue と EventLog の追記

未来のイベントの列（`EventQueue`）を Rust が持ち、`EventLog` の記録の byte 列を追記のときに Rust が書きます。保存の復元の確認は、自分で byte 列を書いたログなら、そのログをそのまま読みます。**イベントの順序、同順位の解決、`allocate_event_sequence`、保存の byte 列は変わりません。** モデルの係数や式は動かしていません。

レーン V（`rust/m1-saves`）の上に作りました。V を先にマージする必要があります。

## 移したもの

| もの | 場所 | 中身 |
|---|---|---|
| 列 | `state/events.rs` の `QueueStore`、`events/queue.py` の `_NativeEventQueue` | Python の列と同じ構造を1対1で持ちます（コンストラクタの重複も `validate` が同じ文で断ります）。`pending()` は変わるまで同じタプルを返します |
| イベントの行 | `state/events.rs` の `EventRow` | Py のハンドルなし。`to_py`（`ScheduledEvent` を作る）、`write_json`、`from_event`、`from_saved_record` |
| 規則の窓口 | `state/events.rs` の `EventWriter` | 下の節 |
| ログの追記 | `eventlog.rs`、`checkpoint.rs` の `encode_record` | 追記のときに凍結された値から byte 列を書きます。索引のための payload の読みは、索引が使う種類のイベントだけにしました |
| 復元の確認 | `core/checkpoint_cache.py` の `_live_log` | 下の節 |
| 最初の Rust の予定 | `rules/information.rs` | `_schedule_information_contact` を呼ばず、行で予定します。行が断るもの（安定 ID でない出典など）だけ Python のメソッドを呼び、同じ文で失敗します |

## 規則がイベントを予定し、記録する方法（レーン F へ）

~~~rust
// 計画の段階で。Rust のものでなければ Unsupported で、何も変えません。
let writer = EventWriter::of(&world)?;

// 書き込みの段階で。
let sequence = writer.next_sequence()?;               // 変えずに読む
let Some(row) = EventRow::new(id, kind, due, priority, sequence, event_type,
                              payload, source, causation, provenance) else {
    // ScheduledEvent が断る値。Python の手順を呼び、その文で失敗させます。
};
writer.allocate_sequence()?;      // WorldState.allocate_event_sequence
writer.schedule(row)?;            // WorldState.schedule_event（同じエラー）
writer.record(&row, minute, EventStatus::Applied, None)?;  // EventLog.append（同じエラー）
writer.settle(event_id, minute, status, detail)?;          // 列から外して記録する
~~~

- **Python のオブジェクトは作りません。** ハンドラーや Python の読み手が読むときに、列なら `row.to_py`、ログなら `record_from_bytes` が作ります。作った列のオブジェクトは持ち続けるので、同じオブジェクトが返ります。
- **読み：** `queue.row(py, id)`、`queue.pending_ids(minute)`、`log.positions_of_type(type)`、`log.saved_document(py, position)`。Python が予定したイベントの行は、最初に読むときに作ります。行にならない値なら `None` で、規則は `Unsupported` にします。
- **キーの順序：** `EventRow::new` は渡した順を保ちます。ログの記録は byte 列から作るのでキーがソート順です。復元した Run と同じ形で、順序に頼るハンドラーがあれば復元でも壊れます。
- **Python のコールバックのあと**は `EventWriter::of` を呼び直します。
- `id_counters` は Python の dict のままです（素の dict のときだけ）。追跡の session では `Unsupported` です。

## 復元の確認を省く根拠と、失うもの

byte 列は、厳密な型（素の JSON のスカラー、タプル、mapping proxy）だけを受ける書き手が、凍結された値から書きます。読み戻せば同じ値になるので、そのようなログは復元してもそのログ自身です（`EventLog.all_encoded`）。`CheckpointHistoryCache.restored_log` は live のログを返し、解析も `same_saved_records` も動きません。書き手が断る値（`str` の派生クラスなど）が1件でもあれば、そのログは今までの確認に戻ります。読み込み・分岐は今までどおり全部を解析して確かめます。

**失うもの：** 書き手に不具合があると、保存の時点では見つかりません。1.01 倍の小数を書くコピーでは、保存は通り、後の読み込みが `checkpoint_incompatible_or_corrupt` で止まりました。代わりに `events_shadow.py` と `test_random_values_written_on_appending_restore_to_the_record` で確かめます。

## 確かめていないこと

- Windows と macOS では動かしていません。
- 保存をしない経路（library）では、前は作らなかった byte 列を追記ごとに持つので、メモリが増えます。測っていません。
- ピーク RSS は測っていません。
- Rust の窓口で予定した実例は情報の接触だけです（7日分の Run にメッセージはありません）。`settle` と `record` は本番の経路では使っていません。テストと影の比較だけです。

## 残っているもの

- 列の保存（`event.to_dict()`、100件ほど）と、復元の確認の列の比較は今までどおりです。
- Python の記録のオブジェクトは持ち続けます。byte 列があるので捨てられますが、キーの順序を確かめてからです。
- `id_counters` の持ち主を Rust に移すこと。

## 計測

途中の記録です。末尾の「作業の再開」を参照。2026-10-31、WSL2、16コア、load average 約 90〜150。

| 7日分（スレッドの CPU 秒、同時の1回） | 基準 | 今 | 比 |
|---|---:|---:|---:|
| `Checkpoint.capture` | 53.1 | 45.3 | 0.85 |
| `TimelineStore.record` | 94.5 | 87.2 | 0.92 |
| `TickEngine.step`（byte 列を書く分が移りました） | 112.5 | 117.7 | 1.05 |
| `phase.dispatch_due_events` | 17.6 | 20.1 | 1.14 |
| 計算のスレッド全体 | 210.4 | 208.1 | 0.99 |

| 確認 | 結果（`08668ae` の Rust） |
|---|---|
| `tick_budget.py`（9600分から480 tick、3回） | 1.038（1.038、1.024、1.038）、A/A 0.963（0.963、1.018、0.946）、状態は一致 |
| 7日分のハッシュ | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041` |
| 保存の byte 列（基準と比べた7日分） | `history.sqlite` 以外の 1,021 ファイルの SHA-256 がすべて同じ |
| `gate_all_paths.py --expect` | 55個すべて一致、問題0件（素の回、`rule_shadow` の回） |
| `rule_shadow`（ゲート） | 追記 516,718 件（580 MB）すべて Rust が書き、差0件、断り0件。列の比較 154,960 回（1,480 万値）。保存 217 回すべて live のログ、古い確認で再確認した記録 167,562 件。Rust が予定したイベント 5,125 件 |
| 道具の確認（コピー） | 書き手の小数 1.01 倍、列の優先度の逆転、行の小数 1.01 倍のどれも差として報告 |

## 作業の再開

マシンを空けるため中断しました。残りの確認と小さな修正は、作業ブランチの報告に書いてあります。
