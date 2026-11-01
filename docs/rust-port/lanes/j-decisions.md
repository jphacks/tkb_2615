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

`tick_budget.py`（9600分から480 tick、3回、スレッドの CPU 時間）：

| 回 | 基準との比 | A/A | load average |
|---|---:|---:|---:|
| レーン M3 社会の統合後（`e66d0cf`、基準 `ac275df`） | **0.923**（1.028、0.902、0.923） | 0.970（0.970、1.000、0.958） | 23〜30 |
| その前（`33904ef`、基準 `3aefc03`） | **0.992**（0.999、0.953、0.992） | 1.024（1.025、0.965、1.024） | 12〜15 |

状態はどの回も一致しました。**tick 全体の差は A/A の揺れと同じ大きさなので、「速くなった」とは言えません。** 確かな差は次の部品の表です。部品の表、1回あたりの表、レーン F の項目、道具の確認は統合前の `a94d9a4` で測りました（`rules/decisions.rs` は統合後も同じです）。

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

7日分（基準とレーンを同時に1組ずつ）：

| 組 | `TickEngine.step` の CPU 秒 | 計算のスレッド全体の CPU 秒 | ピーク RSS（KB） |
|---|---:|---:|---:|
| 統合後（基準 `ac275df`） | 65.4 → 63.5 | 132.0 → 129.0 | 4,505,216 → 4,530,244（+0.6%） |
| 統合前（基準 `3aefc03`） | 54.9 → 53.6 | 114.7 → 112.5 | 4,537,392 → 4,545,964（+0.2%） |

レーン F の前（`de98462`）の project のピーク RSS は 4,512,016 KB（別の回）で、レーン F の増分は +0.6% です。

**レーン F が測っていなかったもの（7日分、library、trace なし。3本同時）：**

| | レーン F の前 | 基準 | レーン |
|---|---:|---:|---:|
| `event_log.records` を初めて読む（行動の周期のオブジェクトを作る）CPU 秒 | 0.02 | 6.47 | 6.54 |
| `TickEngine.step` の CPU 秒 | 69.9 | 64.8 | 64.2 |
| ログを読む前のピーク RSS（MB） | 1,168 | 856 | 855 |
| Run の終わりのピーク RSS（MB） | 2,640 | 3,117 | 3,117 |

オブジェクトを作る費用は Run の終わりに6.5秒で、tick の差（5秒）と合わせて、library の経路ではほぼ差し引きゼロです。ピーク RSS は計算の間は 27% 下がり、Run の終わりは 18% 上がります（byte 列とオブジェクトを両方持つため）。終わりに書くだけなら、byte 列から直接 `events_realized.jsonl` を書けば避けられるはずですが、試していません。

## 結果

結果はレーン M3 社会の統合後の `e66d0cf`（拡張の sha256 `76fdf6d6…`）です。その後の変更はこの文書だけです。

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 3,356件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,047件成功、1,315件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| 7日分 `--expect` | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。すべて一致（`rule_shadow` の回も） |
| 索引（7日分、基準とレーン） | `records` 852,674行、`entities` 4,923,026行、`tokens` 3,242,150行の全列と、`records` の入った順が同じ |
| `ruff check`、`ruff format --check`、`clippy` の disallowed_methods | 通過 |
| `tracked_mirror_gate.py` | **動かしていません。** `core/state_mirror.py` を変えていないので、統合した版でまとめて動かします |

| `rule_shadow` | ゲート | 7日分 |
|---|---:|---:|
| `record_outcome` が動いた / 呼び出し | 62,777 / 63,473 | 87,464 / 87,464 |
| 断り | 696（すべて trace のある Run） | 0 |
| 追記の後に Python に任せた規則 | 0 | 0 |
| `propose_rows` が動いた / 手順の数 | 31,530 / 31,530 | 10,080 / 10,080 |
| `observed_row` が動いた / 呼び出し | 63,473 / 63,473 | 87,464 / 87,464 |
| sink（`Indexed` / 何もしない） | 37,522 / 25,255 | 87,464 / 0 |
| 比べた行の値（ビット比較） | 6,732,030 | 15,701,764 |
| 比べた行の byte | 125 MB | 287 MB |
| 比べた receipt | 77,131 | 101,968 |
| 比べた開いている行動の値 | 4,175 万 | 1 億 3,161 万 |
| 比べた判断の記憶の値 | 836,362 | 1,486,888 |
| 提案の比べた値（`behavior_shadow`） | 12,420,152 | 17,195,364 |
| 違い | 0 | 0 |

ゲートに残る他の断りは、前のレーンのものです（trace、`social_network` の Python がエラーにする値など）。7日分の断りは、社会の kernel の1回と `social_network` の 2,736回で、このレーンのものではありません。

**枝に届いた数：** 行動は、ゲートで11種類すべて、7日分で10種類（`seek_information` は0回）。区域の環境のある行はゲート 399、7日分 0 です。`committed` が偽の判断は、kernel が動いた中でゲート 1,761回、7日分 48回です。

**道具の確認（コピーで変更、`generated_project`、1回の Run で判断 11,714件）：**

| 変更 | 影の違い | ゲート |
|---|---:|---|
| 行の `stress` を 1.01 倍 | 11,590 | **通る** |
| 行の候補の `probability` を 1.01 倍 | 11,714 | **通る** |
| 判断の記憶の分 +1 | 11,714 | 失敗 |
| 行動の周期の `trigger` を変更 | 22,242 | 失敗 |
| 行の `next_decision_at` +1 | 行動の影が報告 | 失敗 |

**ゲートのハッシュは索引の行を見ていません。** 行の中の値を壊してもゲートは通ります。見つけるのは `rule_shadow` と、上の索引の比較です。

**ゲートの行の網羅（kernel ありだけ / kernel ありとなしの合計）：** `experiments/phases/propose.py` 79% / 81%、`policies/behavior.py` 44% / 89%。Rust の行の網羅は測っていません。上の枝の件数で見ています。

## 残っているもの

- `_apply_proposal` の本体（レーン M3、M4）。`committed` の判定も本体が読む値に頼るので、Python のままです。
- trace の文書は Python が作ります。`TraceSink` は入れていません。
- エラーの文は Python が出します（手順4）。
- `_IndexedMetricSink.record_decision` は、断ったときと比較のために残ります。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 新しい C API はありません（`PyList_Append` は既存の `raw_list_append` です）。
- 索引のプロセスは判断の行を `json.loads` します。pickle を解く費用との差は測っていません。7日分の索引の時間は別のプロセスなので、tick の予算には入りません。
- `partial` な view を断る経路はテストがありません（上記）。
- 観測状態が 8,192 byte を超える場合と `demand_modifiers` のある行は、ゲートの行の数が少なく（環境のある行 399）、7日分では通りません。長い場合はテストだけが比べます。
