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

### 保存の時点の確認が見つけるもの（確認を残すかの判断の材料）

前の確認は「復元した世界が live と違えば保存を止める」ものではありません。`Checkpoint.capture` は `same_state` が偽なら `restored.validate()` を呼ぶだけで、通れば保存します（`test_a_restored_world_that_differs_is_validated_and_compared_in_full_next_time` が固定しています）。この検証は読み込みでも同じように動くので、**書き手の不具合のうち保存の確認が見つけるものは、読み込みでも見つかり、読み込みが見逃すものは保存の確認も見逃します。** 確認を省いて変わるのは見つかる時点だけです。

一時的なコピーで書き手を壊して、生成シナリオの2日分（76,117件）を動かしました。

| 壊し方 | 確認なし（このレーン） | 確認あり | 止めた規則 |
|---|---|---|---|
| 全記録の小数（1.01 倍、最後の1桁）、payload の文字列・整数 | 保存は通り、読み込みで `checkpoint_incompatible_or_corrupt` | 保存で停止 | 共有運用の記録と event の照合（`_event_payload_matches`） |
| 全記録のキーの順序 | 同上 | 保存で停止 | `record is not canonical JSON` |
| 全記録の `detail` | **保存も読み込みも通る**。events ハッシュだけ違う | 同じく通る | なし |
| 1種類の記録の payload（小数・文字列・整数、効果のあった15通り） | `care_support_transition` の3通りは読み込みで停止。**残り12通りは保存も読み込みも通る** | 同じ12通りが通る | 同上 |

ハッシュは間違った byte 列から作るので一致します。「読み込みで見つかる」と言えるのは、共有運用の記録と結びつく記録とキーの順序だけです。

壊れた書き手を止めるのは開発時の確認です。`social_interaction` の小数の最後の1桁だけを壊すと、`gate_all_paths.py --expect` は58件の問題（`generated_project: main and library differ` を含む）を出し、pytest は24件失敗しました（`test_all_paths_gate.py`、`test_save_validation.py`、`test_checkpoint_saves.py` など）。4件しかない `facility_queue_abandon` の文字列を壊しても、ゲートは30件の問題を出しました。ただし、どれも実行した経路の値だけです。**壊れた書き手が読み込みで止まることを示すテストはありません。** `test_a_checkpoint_read_again_still_detects_a_corrupt_chunk` はディスクの改竄を、`test_random_values_written_on_appending_restore_to_the_record` は書き手の正しさを確かめるもので、不具合の検出は確かめません。

等しくない復元を保存のエラーにするコピーでは、上で通っていた `social_interaction` と `agent_action_lifecycle` の小数の破損が保存で止まりました（1日分）。正しい書き手でゲートの問題は0件ですが、pytest は8件失敗します。少なくとも2件（上のテストと `test_a_save_reaches_the_verdict_of_the_plain_validation[operation_sources_tuple]`）は等しくなくても通すことを決めているので、設計の変更です。このレーンには入れていません。

## 確かめていないこと

- Windows と macOS では動かしていません。
- Rust の窓口で予定した実例は情報の接触だけです（7日分の Run にメッセージはありません）。`settle` と `record` は本番の経路では使っていません。テストと影の比較だけです。

## 残っているもの

- 列の保存（`event.to_dict()`、100件ほど）と、復元の確認の列の比較は今までどおりです。
- Python の記録のオブジェクトは持ち続けます。byte 列があるので捨てられますが、キーの順序を確かめてからです。
- `id_counters` の持ち主を Rust に移すこと。

## 計測

2026-11-01、WSL2、16コア。`b015945` を基準に、統合後の `3b6987a` で測りました。7日分のハッシュはすべて world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041` です。同時に動かした組の中で比べます。

| 7日分、project | 基準 | このレーン | 確認あり |
|---|---:|---:|---:|
| 1組目：計算のスレッドの CPU 秒 | 156.5 | 155.5 | 158.0 |
| 1組目：`Checkpoint.capture` の CPU 秒／実時間 | 41.5／61.9 | 34.8／44.0 | 38.1／56.7 |
| 1組目：Run の実時間 | 283.7 | 277.0 | 285.0 |
| 2組目：計算のスレッドの CPU 秒 | 182.7、183.8（A/A 1.006） | 180.2 | |
| 3組目（同じ木で確認だけを切替）：CPU 秒 | | 173.5 | 177.3 |
| 3組目：`Checkpoint.capture` の CPU 秒／実時間 | | 39.9／67.6 | 44.5／94.1 |
| 3組目：Run の実時間 | | 352.0 | 375.0 |
| ピーク RSS（MB） | 4,387–4,392 | 4,372–4,404 | 4,408–4,432 |

確認の解析は別のスレッドでも動くので（rayon）、計算のスレッドの CPU 秒には一部しか出ません。確認を省いて減るのは、スレッドの CPU 秒で 2.5〜3.8 秒、Run の実時間で 8〜23 秒です（負荷で変わります）。

| 7日分、library（保存なし） | 基準 | このレーン |
|---|---:|---:|
| ピーク RSS（MB） | 2,470（3回の差 220 KB） | 2,820（+350 MB、+14.2%。2回） |
| 計算のスレッドの CPU 秒 | 121.5 ／ 118.0、117.1（A/A 1.008） | 126.8（1.044）／ 124.1（1.056） |

**保存をしない経路は、byte 列を書く分と持つ分だけ損をします。** 7日分の byte 列は 329 MB で、RSS の増分とほぼ同じです。保存する経路は前から持っているので増えません。Python の記録の byte 列を最初の保存まで書かなければ避けられるはずですが、試していません。

| 確認 | 結果（`3b6987a`） |
|---|---|
| `tick_budget.py`（9600分から480 tick、3回） | 1.035（0.994、1.074、1.035）、A/A 0.972（0.920、0.972、1.035）、状態は一致 |
| `gate_all_paths.py --expect` | 55個すべて一致、問題0件（素の回、`--reversed-order`、`rule_shadow` の回） |
| `rule_shadow`（ゲート） | 追記 516,718 件（580 MB）すべて Rust が書き、差0件、断り0件。列の比較 154,960 回（1,480 万値）。保存 217 回すべて live のログ、古い確認で再確認した記録 167,562 件。Rust が予定したイベント 5,125 件 |
| `rule_shadow`（7日分） | 追記 273,176 件（329 MB）すべて Rust が書き、差0件、断り0件。列の比較 182,852 回（4,937 万値）。保存 336 回すべて live のログ、再確認 273,176 件。Rust が予定したイベント 0 件（メッセージなし） |
| 道具の確認（コピー、8時間分） | 書き手の小数の最後の1桁、列の優先度の逆転、行の小数 1.01 倍のどれも差として報告 |
| `tracked_mirror_gate.py` | 220行、問題0件 |
