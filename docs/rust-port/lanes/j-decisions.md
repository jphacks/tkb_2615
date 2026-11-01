# M2 レーン J：判断の記録

`_apply_proposal` の末尾（`record_decision`、行動の周期、判断の記憶）と、そこまでの提案の受け渡しを `native/src/rules/decisions.rs` に移しました。結果は変わりません。**提案の Python オブジェクト（`ActionProposal`）は、M4 を待たずに今なくせました。** `_apply_proposal` の Python の本体は提案の5項目しか読まないからです。Python の規則は比較の基準として残ります。

## 移したもの

| 手順（`segments.py`） | kernel | 中身 |
|---|---|---|
| `_propose_due` | `propose_rows` | 計算は `rules/behavior.rs` のままです。`ActionProposal` の代わりに `ProposalRow` を返します |
| `_observed_state` | `observed_row` | 記録用の dict の代わりに `ObservedRow`（見る人の場所だけ）を返します |
| `_record_outcome`（新しい手順。`_apply_proposal` の末尾を切り出しました） | `record_outcome` | `record_decision`、`commit_action`、`remember_decision` の順 |

- **`ProposalRow`：** `agent_id`、`action`、`next_decision_at`、`target_id`、`minute_index`、`observed_state_hash`、`policy_version` に行から答えます。ほかの項目（`candidates` など）か `with_outcome` を読むと、レーン H の `proposal_of` で `ActionProposal` を1回だけ作ります。`ProposalRow` は Python に渡す入れ物なので `Py` のハンドルを持ちますが、計算の結果の `Outcome` は持ちません（手順4で人ごとに計算できます）。
- **`_IndexedMetricSink.observations`：** 判断の行は dict の代わりに JSON の bytes で入ります。キーの順は dict と同じです。`HistoryIndex.ingest` が読むときに `json.loads` するので、索引の処理と中身は変わりません。Rust が書いたものと Python が書いたもの（`record_social_decision`、断ったときの判断）は同じリストに順に入ります。
- **`decision_sink`：** sink が自分のクラスに宣言します（継承は見ません）。`"indexed"`（`_IndexedMetricSink`）、`"silent"`（`_MetricSink` 2つ）、`"artifacts"`（`RunArtifactWriter`。trace が off で、開始後で、終了前なら何もしない）。宣言がなければ Python の sink を呼びます。

## 断るもの（`Unsupported`）

**記録を追記する前：** Python の手順がそのまま動き、何も二重になりません。

- **trace のある Run（`RunArtifactWriter` の summary、full）。** 文書は Rust で作らず、Python の手順が `ActionProposal` から作るので同じです。`_observed_state` もこのときは dict を返します。
- `decision_sink` のない sink（テストの偽物、継承したクラス）
- `ProposalRow` でない提案、`ObservedRow` でない観測状態（`Indexed` のとき）
- `next_decision_at` が分を超えない（Python が `proposal has invalid decision timing` を出します）、`committed` が bool でない、`target_id` が `str` か None でない
- 汚染された store、NaN や無限大、`policy_sources` や `household_id` の値が `str` か None でない、`people` が dict でない
- 観測状態が 8,192 byte を超える（`json.dumps` の 16,384 の判定は Python がします）

**追記の後：** `commit_action` か `remember_decision` が断ったら、その規則だけ Python で動きます（`record_action_proposal`、`record_agent_proposal`。行動の周期は `active.as_plain()`）。記録は1件のままです。

`partial` な view は `snapshot_of`（レーン H）が断ります。今の経路で `_rust_view` に `partial` な view が入ることはないので、テストはありません。

## `_apply_proposal` の本体を移さなかった理由

本体が単純な行動（`wait`、`rest`）だけを Rust で処理する案は採りませんでした。7日分の判断 87,464回のうち 6,246回（7.1%）だけで、残りは `help_others` 46,777回（53%、`capture_social_state_context` を呼びます）、`sleep` 14,585回（17%、ベッドの予約）などです。本体は M3 と M4 で移します。

## モデルの担当の方に見ていただくコード

**数値の係数は増えていません。** レーン H の係数はすべてそのまま `rules/behavior.rs` にあり、値も式も変えていません。文書の形だけが2か所になりました。

| Python | Rust | 中身 |
|---|---|---|
| `_IndexedMetricSink.record_decision` | `decision_bytes` | 記録の項目、`candidates[:32]`（行動は11種類なので切り詰めは起きません）、`status`、`execution_evidence` の文 |
| `_agent_view_snapshot` | `write_observed` | レーン H の `observed_dict` と同じ文書の bytes 版 |
| `current_household_id` | `write_household` | `population_presence_v1` の読み方。`group_id` に戻る条件 |
| `record_decision` の上限 16,384 | `MAX_OBSERVED_BYTES` 8,192 | 係数ではなく、超えたら Python に任せる線です |

## 計測

2026-11-01、WSL2、16コア。基準は `3aefc03`（レーン F の統合後）。

`tick_budget.py`（9600分から480 tick、3回、スレッドの CPU 時間、load average 12〜21）：基準との比 **0.992**（0.999、0.953、0.992）、A/A 1.024（1.025、0.965、1.024）、状態は一致。A/A の揺れの中です。

部品（同じ分岐から480 tick、基準2本とレーンを同時に4回、µs/tick、load average 約 17）：

| | 基準 | レーン | 比（A/A） |
|---|---:|---:|---:|
| `_run_propose_phase` | 1,236〜1,366 | 1,149〜1,221（外れ値 1,650） | 0.933（1.010） |
| `_apply_due_proposals` | 1,034〜1,159 | 928〜990（外れ値 1,389） | 0.908（1.012） |
| `BackgroundIndexer.submit`（計算のスレッドの保存の経路で、tick の外） | 107〜142 | 64〜70 | 0.50（1.26） |

1回あたり（同じ状態で繰り返し。load average 約 15）：

| | Python のオブジェクト | 行 |
|---|---:|---:|
| 提案1件を作る | 6,100 ns | 1,500 ns |
| 観測状態 | 1,311 ns | 333 ns |
| `with_outcome` と `record_decision` | 676 + 7,636 ns | 3,333 ns（`record_outcome` の中の差） |

7日分（基準とレーンを同時に1組）：`TickEngine.step` のスレッドの CPU 秒 54.9 → 53.6、計算のスレッド全体 114.7 → 112.5 秒。実時間は 164.0 → 162.4 秒です。1組なので、部品の表の裏づけとしてだけ読みます。

**ピーク RSS（7日分、project）：** レーン F の前 `de98462` 4,512,016 KB（別の回）、基準 4,537,392 KB、レーン 4,545,964 KB（基準の +0.2%）。行は次の保存までしか持ちません。

**レーン F が測っていなかったもの（7日分、library、trace なし。3本同時）：**

| | レーン F の前 | 基準 | レーン |
|---|---:|---:|---:|
| `event_log.records` を初めて読む（行動の周期のオブジェクトを作る）CPU 秒 | 0.02 | 6.47 | 6.54 |
| `TickEngine.step` の CPU 秒 | 69.9 | 64.8 | 64.2 |
| ログを読む前のピーク RSS（MB） | 1,168 | 856 | 855 |
| Run の終わりのピーク RSS（MB） | 2,640 | 3,117 | 3,117 |

オブジェクトを作る費用は Run の終わりに6.5秒で、tick の差（5秒）と合わせて、library の経路ではほぼ差し引きゼロです。ピーク RSS は計算の間は 27% 下がり、Run の終わりは 18% 上がります（byte 列とオブジェクトを両方持つため）。終わりに書くだけなら、byte 列から直接 `events_realized.jsonl` を書けば避けられるはずですが、試していません。

## 結果

