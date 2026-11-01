# M4 レーン：職員と共有運用（`domain/staff.py`、`domain/staff_operations.py`、`domain/common_operations.py`）

**Rust が `staff_operations_v1` と `shared_operations_v1` を持ちます**（`native/src/state/staff_operations.rs`、`shared_operations.rs`）。職員が何も始めない tick の規則と、ケアの受領記録の追記、tick の検証を `native/src/rules/staff.rs`、`common_operations.rs` に移しました。結果は変わりません。Python の規則は比較の基準として残ります。呼び出し1回につき、計画してから書く kernel が1つで、コールバックはありません。

## 調査で決めた範囲

回数は7日分の project の経路（`cProfile`、`TickEngine.step` の中）です。µs は `fork-b1` の 9600分から480 tick、スレッドの CPU 時間です。基準2本（`2b01225`）とレーンを同時に3組動かした幅で、関数の出入りごとに約 2 µs の計測の枠を含みます（load average 12〜15）。基準の幅は2本の両方を入れています。tick 全体は基準 3,463〜4,042、レーン 3,415〜3,536 µs です。

| 規則 | 7日分の回数 | 基準 µs/tick | レーン µs/tick | 扱い |
|---|---:|---:|---:|---|
| `StaffService.assign_pending` | 10,080 | 123〜148 | 56〜59 | **kernel**（依頼を取る tick は断る） |
| `support_wait_reason_codes`、`support_waited_minutes`、`support_required_skills`（`MetricSnapshot.from_world` から） | 各 約 30万 | `from_world` 全体 145〜174 | `from_world` 全体 104〜111 | **1 tick 1回の kernel `wait_snapshot`** |
| `record_shared_operation` | 8,040 | 91〜109 | 30〜31 | **kernel**（ケアの受領記録） |
| `validate_shared_operations_state`（tick） | 10,080 | 77〜90 | 11 | **kernel** `validate_tick` |
| `update_staff_operations` | 10,080 | 31〜37 | 10 | **kernel** |
| `assign_staff_operations` | 10,080 | 35〜40 | 5 | **kernel**（作業を取る tick は断る） |
| `validate_staff_operations_state` | 10,080 | 24〜27 | 測っていない | **kernel**（作業中の職員がいれば Python） |
| `schedule_staff_operation_relief` | 10,080 | 9〜11 | 2 | **kernel**（作業中の職員がいれば Python） |
| `StaffService.request` | 3,309 | 11〜12 | 同じコード | Python |
| `timeout_support_request` | 3,308 | `cProfile` で 3.5 | 同じコード | Python |
| `complete_support_task` | 711 | `cProfile` で 2.2 | 同じコード | Python |
| `arrive_`、`complete_`、`interrupt_`、`handoff_`、`order_staff_operation` | **0** | | | Python（1項目ずつ転送） |

**kernel にして、やめたもの：** `support_wait_reason_codes` と `support_waited_minutes` を1回ずつの kernel にすると、`from_world` は基準と同じでした（134〜141 → 135〜140 µs/tick）。1回あたりの Python が約 1 µs しかなく、境界の費用で消えます。依頼ごとの呼び出しをやめ、1 tick 1回の `wait_snapshot` にまとめて、`from_world` が約 30% 短くなりました。このため `metrics.py` の内包表記を `_support_wait_snapshot` に切り出しました。

**`assign_staff_operations` の kernel は `shared_passage_edge_loads`（M4 列のレーンが Python に残した規則）も呼びません。** 7日分のこの呼び出しは、すべてここからでした。

## 持ち方

- `staff_operations_v1`：区域と職員の記録、終わった作業は型付きの行です。`config` は `V` です。Python の作業のイベントの規則は、1項目ずつ転送します。
- `shared_operations_v1`：`support_requests` は型付き（`Heads`。転送で書かれたら次に読むときに確かめ直します）、`history` は受領記録ごとの正準 bytes と `minute`、`event_id`、`verified`（`Receipts`）です。ハッシュの木が `history` を別に読むので、`_HASHED_BY_PARTS` に入れました。
- **`verified`：** kernel が追記のときにイベントと照らした記録か、tick の Python の検証が通した記録です。**直近の記録がすべて `verified` のときだけ** kernel が動きます。復元した世界や「写して差し替える」世界の記録は `verified` でないので、最初の tick の検証は Python が動き（kernel の `validate_tick` は false）、通ると `recent_passed` が印を付けて、次の tick から kernel に戻ります。`StaffOperationReceipt` はこの印を付けません。規則が `staff_operations_v1.history` も読むからで、直近にある間は Python が読みます。
- **最初の版の間違い：** Python が通しても印を付けなかったので、分岐の後は「直近の最後の古い記録」がずっと `verified` でなく、kernel が永久に断っていました。`long_generated`（履歴が空から始まる）の影では全部 kernel が動いて見え、`fork-b1` の部品の計測で初めて分かりました（`record_shared_operation` が速くならず遅くなった）。`test_a_restored_receipt_is_read_by_the_python_rule_once_and_then_trusted` が固定します。

## 断るもの（`Unsupported`、何も書く前）

- `assign_pending`：資格のある空いた職員がいる依頼（経路とイベントが要ります）、`support_priorities` が正の依頼（割り込みが要ります）、`validate_state=True`、世界と違う分、知らせの宛先を持つ依頼（一度終わって開き直した依頼）、世界のものでない依頼オブジェクト。
- `wait_snapshot`：待っている依頼に、資格のある空いた職員がいる（経路を読みます）。
- `update_operations`：int の率（`0` など）、`effects_by_zone` のない設定（Python が `KeyError` を出します）、区域の記録がない。
- `assign_operations`：別の方策の式、仕事のある空いた職員（清掃、修理できる設備、情報提供、巡回）、記録のない職員、fatigue が有限でない。
- `relief`：作業中の職員がいる。fatigue が有限でない。
- `record_shared_operation`：`CareTransitionReceipt` でない受領記録、Python の規則が断るすべて（分、順序、同一性、重複、イベントとの不一致、状態の変化の不一致）、`verified` でない直近の記録、値の型が違う（イベントの int と受領記録の float など）。
- 型が違う値（行が taint）、世界が行と別の文書を持っている（`dict.__setitem__` で表を飛ばした場合）、`SHELTERSIM_NATIVE=0`、追跡の session。

## Python に残した規則（手順4で Rust が要るもの）

- **`domain/staff.py`：** `assign_pending` の割り当ての本体、`_preempt_background_staff`、`_support_assignment_order`、`request`、`complete_support_task`、`timeout_support_request`、経路を読む `_support_route_wait_reasons`、`support_wait_reason_codes` と `support_waited_minutes`（`inspection/frames.py` と `wait_snapshot` が断ったとき）、`support_required_skills`。
- **`domain/staff_operations.py`：** `canonical_staff_operations_config`、`create_staff_operations_state`、`add_staff_operations_agent`、`assign_staff_operations` の割り当ての本体、`validate_staff_operation_order`、`order_staff_operation`、`schedule_staff_operation_relief` の本体、`handoff_`、`arrive_`、`complete_`、`interrupt_staff_operation`、`interrupt_staff_trips_on_closed_routes`、`_maintenance_blocked`、作業中の記録の検証。
- **`domain/common_operations.py`：** `validate_shared_operations_state` の完全検証（保存、Run の開始と終了）、ケアでない受領記録の `record_shared_operation`、`operational_demand_snapshot`、`_validate_staff_operation_receipt`。
- **割り当てを断る理由の経路：** `SpaceRouter.route` と `_route` が Python なので、`assign_pending` は依頼を取る tick を断ります（7日分の約 7%）。`_route` と `_schedule_travel` が Rust になれば、ここも移せます。
- **保存の経路の完全検証は重いままです：** `fork-b1` で1回約 35 ms（tick の外。480 tick で 21回、合計 0.73 秒）。受領記録の再生を `verified` で短くする余地はありますが、試していません。

## モデルの担当の方に見ていただくコード

**式と値は変えていません。**

| Python | Rust |
|---|---|
| `update_staff_operations` | `rules/staff.rs` の `update_operations` |
| `assign_staff_operations` の候補の判定（閾値、平均、`reserved_operations`） | `assign_operations` |
| `_CARE_SKILL_ALTERNATIVES` | `required_skills` |
| `assign_pending` の待ちの記録と曝露の数え方 | `assign_pending`、`kind_exposure` |
| `support_wait_reason_codes`（職員が空いていない場合）、`support_waited_minutes` | `kind_wait`、`wait_snapshot` |
| `_CARE_TRANSITIONS`、`_validate_observed_state_changes`、`_event_payload_matches`、`CareRequestState` の確認 | `rules/common_operations.rs` |
| `validate_staff_operations_state`（職員がすべて空いている場合）、`canonical_staff_operations_config` の確認、`validate_shared_operations_state`（tick） | `state/staff_operations.rs`、`state/shared_operations.rs` |

**Rust に書いた係数：**

- 清掃の閾値の既定値 0.85、情報提供 0.45、巡回 0.6。`thresholds_by_zone` があれば実行時にそちらを読みます。
- 依頼の種類ごとの資格の表（9種類、それ以外は `support`）。
- 検証の範囲：作業時間 1〜1440 分、率 0〜1、`effects_by_zone` の `sanitation_use_per_person_minute` 0〜0.01、履歴の上限 20,000、依頼の上限 100,000、種類の表（5種類）。

**Rust だけの条件（超えたら Python が動きます）：** 2^53 以上の分と経過時間、int の率、int の閾値で絶対値が 2^53 以上、大きな int の状態の変化。

## 計測

2026-11-01、WSL2、16コア、ほかのレーンと同時。基準は `2b01225`（M4 列のレーンの統合後）。

| `tick_budget.py`（9600分から480 tick、3回） | 基準との比 | A/A | load average |
|---|---:|---:|---:|
| 統合した版（`7826fb7`） | **0.887**（0.887、0.854、0.902） | 1.020（1.024、0.984、1.020） | 10〜15 |
| 統合前（`22a1d09`、基準 `97ebe46`） | 0.882（0.843、0.882、0.941） | 1.035（1.000、1.062、1.035） | 4〜9 |

状態はどの回も3つとも一致しました。A/A の揺れの外で、約 11% 短くなります。

7日分の project の素の回は実時間 121.6 秒、スレッドの CPU 時間 81.6 秒です（ほかの確認と同時の1回で、基準とは同時に測っていません）。

## 結果

`7826fb7`（`feature/rust-port` の `2b01225` を統合）の結果です。その後の変更は、`test_staff_rules.py` のテスト1件とこの文書だけです。

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 3,640件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,073件成功、1,573件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| ゲートの `rule_shadow`（全部の影） | 55シナリオ、問題0件、違い0件 |
| 7日分 | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致（素の回、影の回） |
| 7日分の `rule_shadow` | **このレーンの影（`shadow_staff.py`）だけ。** 全部の影は約 2.5 時間かかるため |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（指示どおり） |

| `rule_shadow`（kernel が動いた / 呼び出し） | ゲート | 7日分 |
|---|---:|---:|
| `assign_pending` | 28,391 / 31,530 | 9,368 / 10,080 |
| `wait_snapshot` | 30,762 / 31,757 | 10,415 / 10,417 |
| `record_shared_operation` | 8,857 / 9,171 | 8,040 / 8,040 |
| `update_operations`（ほかに名前空間のない世界 2,680回） | 28,850 / 28,850 | 10,080 / 10,080 |
| `assign_operations` | 31,523 / 31,530 | 10,078 / 10,080 |
| `relief` | 31,265 / 31,530 | 10,080 / 10,080 |
| `validate_tick`（受け入れ / 呼び出し） | 28,008 / 28,410 | 10,080 / 10,080 |
| `staff_operations` の検証（受け入れ / 呼び出し） | 35,333 / 35,685 | 10,434 / 10,434 |
| 比べた値（`assign_pending`、`wait_snapshot`、受領記録、`update`） | 4,668,532、546,431、10,632,630、2,026,785 | 5,465,020、936,771、13,584,534、977,760 |
| 違い | 0 | 0 |

呼び出しの数は影の回のものです。受け入れはすべて Python の規則も通しました。

**断り（7日分）：** `assign_pending` の 712回はすべて「資格のある空いた職員」で、完了した支援 711回とほぼ同じです（依頼を取る tick。7.1%）。`assign_operations` の2回は巡回の候補（`cProfile` の `route` 2回と同じ）、`wait_snapshot` の2回は資格のある空いた職員です。

**断り（ゲート）：** `assign_pending` 3,139回（同じ理由）、`wait_snapshot` 995回（同じ理由）、`relief` 265回（作業中の職員）、`assign_operations` 7回（修理）、受領記録 245回（直近に検証していない記録がある：復元と「写して差し替える」世界）と 69回（ケアでない受領記録）。Python の規則が通した検証を断ったのは `validate_tick` 402回、`staff_operations` 352回で、どちらも kernel の範囲の外（直近に検証していない記録、作業中の職員）です。

**枝に届いた数：**

| | ゲート | 7日分 |
|---|---:|---:|
| 待っている依頼（`assign_pending` の延べ） | 140,935 | 273,174 |
| 受領記録：requested / assigned / started / completed / interrupted | 2,427 / 1,822 / 1,859 / 1,926 / 823 | 3,309 / 711 / 711 / 711 / 2,598 |
| 状態の変化のある受領記録 | 1,926 | 711 |
| 待ちの理由：`staff_busy` / `staff_resting` / `no_qualified_staff` | 145,455 / 4,298 / 2,206 | 271,471 / 31,427 / 0 |
| `staff_on_other_operation` / `staff_unavailable` | 176 / 1,426 | 0 / 0 |
| 衛生度が 1 未満の区域（`update` の延べ） | 116,563 | 40,312 |

**7日分は職員の作業が1回も始まりません**（`arrive_`、`complete_` が0回、`history` は空）。作業のある経路の比較はゲートとテストだけです。

**ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）：** `domain/staff.py` 353行のうち 303行（86%）/ 300行（85%）、`domain/staff_operations.py` 557行のうち 391行（70%）/ 378行（68%）、`domain/common_operations.py` 282行のうち 213行（76%）/ 213行（76%）。kernel ありだけでも高いのは、断った tick と保存の検証で Python の規則が動くからです。Rust の行の網羅は測っていません。上の枝の件数と `test_staff_rules.py` で見ています。

### 影の感度（コピーで変更。`long_generated`、統合前の `ae691e8`）

| 変更 | 影 | ハッシュ | `test_staff_rules.py` |
|---|---:|---|---:|
| 区域の使用量を 1.01 倍 | 20件以上 | world、events が不一致 | 1件失敗 |
| 清掃の閾値 0.85 → 0.8585 | 0件 | 一致 | 1件失敗 |
| 情報提供の閾値 0.45 → 0.4545 | 0件 | 一致 | 1件失敗 |
| 巡回の閾値 0.6 → 0.606 | 0件 | 一致 | 1件失敗 |
| 資格の表から `nursing` を1つ外す | 0件 | 一致 | 3件失敗 |
| 休憩の境界 `>` → `>=`（1% ではない） | 1件 | KPI が不一致 | 11件失敗 |
| 勤務外の曝露を数えない（1% ではない） | 20件以上 | world、KPI が不一致 | 最初は0件。`test_the_wait_counts_staff_off_shift_away_and_busy_as_the_python_rule_does` を足しました |
| 依頼の頭の `updated_minute` +1（1% ではない） | 20件以上 | **一致** | 1件失敗 |
| 検証が依頼の状態を見ない | 0件 | 一致 | 1件失敗 |
| 検証の衛生度の上限を 2.0 に | 0件 | 一致 | 1件失敗 |

**閾値の 1% は影とハッシュに届きません。** 平均が閾値の 1% の範囲にある tick が `long_generated` にないからです。テストが閾値のすぐ両側の値で見ます。**ハッシュは最後の状態を比べるので、終わった依頼の頭の違いは見えません**（`updated_minute` の行）。影とテストが見ます。検証の2行は、影ではよい状態しか通らないので、テストが壊した状態で見ます。

## 確かめていないこと

- **Windows と macOS では動かしていません。** 新しい C API はありません。`fsum`、`max` は `mathx` です。libm は使いません。
- `tracked_mirror_gate.py` は動かしていません（指示どおり）。`core/state_mirror.py` の変更は `OWNED_NAMESPACES` の2行と `_HASHED_BY_PARTS` の1語だけです。
- `assign_operations` は `shared_passage_edge_loads` と `_maintenance_blocked` を読まないので、それらが壊れた状態で例外を出す世界でも kernel は通します（候補がなければ Python の規則も結果を使いません）。例外だけが違います。`round(1000 * fatigue)` の例外は同じになるよう断ります。
- `verified` の受領記録は、イベントの記録が追記専用であることに頼って読み直しません。`event_log` を別のものに差し替える世界は、全体を作り直すので `verified` が消えるとしか確かめていません（`has_event_id` は毎回見ます）。
- `validate_staff_operations_state` の kernel の費用は測っていません（部品の計測の枠が kernel の中に入らないため）。tick 全体には入っています。
