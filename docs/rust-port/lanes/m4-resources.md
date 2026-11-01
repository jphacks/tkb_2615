# M4 レーン：資源台帳（`resource_ledger_v1`）

Rust が `resource_ledger_v1` を型付きの行で持ち、操作の規則を一緒に移しました。**操作のたびに名前空間を丸ごと置き換える書き込みは、tick からなくなりました**（P1 で転送量の 92.9%、P3 で 1回 60 ms と計った書き込みです）。結果は変わりません。Python の規則は比較の基準として残ります。

## 7日分の調査と、何を移したか

基準の木に回数と時間の計測を掛けて、7日分（10,080 tick）を動かしました。下の「基準」と「レーン」は、基準とレーンを同時に動かした1回の値で、計測の枠の分を含みます（load average 約 30〜60）。`tick` の合計は基準 85.2秒、レーン 77.5秒です。

| もの | 回数 | 基準（秒） | レーン（秒） | 扱い |
|---|---:|---:|---:|---|
| `apply_resource_operation`（`reserve` 4,008、`consume` 4,008、`delivery` 8） | 8,024 | 2.71 | 0.24 | **kernel** |
| `validate_resource_ledger_state`（操作の中の2回、指標、毎 tick の検証） | 36,545 | 2.89 | 0 | **kernel**（Python の規則は通らなくなりました） |
| `resource_metric_values` | 10,417 | 1.25 | 0.13 | **kernel** |
| `_reservation_id_is_in_use` | 16,201 | 1.72 | 0.21 | ID の確認だけ **kernel**。予定イベントの走査は Python |
| `ResourceServiceManager.arbitrate`（上の内数を含む） | 10,080 | 6.55 | 3.69 | Python（下） |
| `ResourceServiceManager.complete`（同） | 4,008 | 1.65 | 0.53 | Python（下） |
| `ResourceServiceManager.request`（同） | 4,016 | 0.53 | 0.20 | Python（下） |
| `care_receipt_for_request` | 1,208,148 | 1.63 | 1.61 | Python（下） |
| `append_applied_care_transition` | 7,329 | 1.87 | 1.85 | Python（下） |
| `ingest_applied_shared_operation_records` | 91,262 | 0.45 | 0.45 | Python（下） |
| `validate_resource_ledger`（保存の前の再生） | 351 | 0.75 | 0.87 | Python（下） |

| もの | 場所 |
|---|---|
| 行（`LedgerRow`、`Account`、`Reservation`、`AuditRecord`、`Audit`、`UsedIds`）と毎 tick の検証 `validate` | `native/src/state/resource_ledger.rs` |
| `apply_operation`、`metric_values`、`reservation_id_was_used` | `native/src/rules/resources.rs` |
| 比較 | `benchmarks/rule_shadow/shadow_resources.py` |
| テスト | `tests/integration/test_resource_ledger_row.py` |
| 木から dict のキーを消す `NamespaceMirror::delete_through` | `native/src/core/world_core.rs` |

- **監査の記録は項目ごとの正準 bytes です。** 規則が読むのは各資源の最新の1件だけで、それは型付きで持ちます。
- **行に入る監査の記録は、`_validate_audit_record` の規則のうち記録だけを読むものを通ったものだけです。** 通らない記録、すでにある記録への書き込みは taint し、Python の規則が木を読みます。なので毎 tick の確認は、記録を読み直さずに済みます。
- **計画して書く形です。** 1回の操作が1つの kernel で、コールバックはありません。読むのは `world.resources`、`agents`、`zones` の dict と event です。
- **Python の規則は操作の後の状態も確かめますが、kernel は確かめません。** 操作の前の確認から従うからです。新しい記録の ID はここで確かめ、出典は口座の確認を通っています。どの操作も `remaining - reserved >= 0` と予約の合計を保ちます。分が戻る操作は Python に任せます。`test_random_sequences_with_refusals_match_the_python_rule` とすべての `rule_shadow` の回が、これを確かめます。
- **`world.resources` は Python の規則と同じく新しい dict です。** 古い dict を持つ人には古い値が見えます。
- **毎 tick の検証だけを任せます。** `world.validate()` の保存の前の完全検証は、Python で監査を再生します（`core/world.py` の条件に `validate_event_history` を足しました）。
- **係数はありません。** 台帳は整数の足し引きなので、`mathx` も使いません。モデルの担当の方に見ていただくのは、在庫の規則そのものです（`apply_resource_operation`、`resource_metric_values`、`validate_resource_ledger_state`）。給水量、配食量、`hydration_relief`、`hunger_relief` などは Python に残ります。

## Python に残したものと理由

- **`ResourceServiceManager.arbitrate`：** 残りの大半は `progress_physical_queue_positions` と `progress_physical_queue_head_to_service`（調査で 1.46秒と 0.69秒）、`prepare_facility_queue`（0.45秒）です。**レーン `rust/m4-movement` と、キューのレーンが移すものです。** 途中で呼ぶと kernel の中で Python を呼ぶことになるので、それらが移ってから1つの kernel にできます。空のキューだけ kernel にしても、Python の空の場合は数 µs で得がありません。
- **`complete`、`request`：** 残りは1回あたり 50〜130 µs で、0.4回/tick です。tick の約 0.5% ずつです。`_audit_event`、`_record_internal_event` の `ScheduledEvent` と、人・設備・職員・タスクの書き込みが中心で、形は計画して書けますが、今は効果より誤りの余地が大きいと判断しました。`arbitrate` と一緒に移します。
- **`resource_service_v1`：** 持ち主にしていません。読み書きする kernel がなく、持つだけでは転送の費用が増えるだけだからです（約3回/tick）。名前空間の丸ごとの書き込みもありません。`request_history`、`queue_stall_history` の bytes は、`arbitrate` を移すときに一緒に行にします。
- **`care_receipt_for_request`（120回/tick）：** dict を3回読むだけで、1回約 1 µs です。kernel の呼び出しのほうが高くつきます。読むのは `shared_operations_v1` で、まだ行ではありません。回数は `domain/staff.py` のループから来るので、職員のレーンが移すと一緒に減ります。
- **`append_applied_care_transition`、`make_*_record`、`ingest_applied_shared_operation_records`：** 費用の大半は pydantic の受領書と `record_shared_operation` です（`domain/common_operations.py`、`shared_operations_v1`）。名前空間と一緒に移すものです。
- **`care_history_by_agent`：** 表示の経路で、tick の外です。
- **`validate_resource_ledger`（保存の前の再生）：** 木の上の Python のままです。tick の外で、保存の検証の省略はレーン V の作りをそのまま使います。
- **エラーの文：** 変更の前に `Unsupported` を返し、Python の規則が同じ文を出します。

## 影の結果と kernel の回数

| `rule_shadow` | ゲート | 7日分 |
|---|---:|---:|
| `apply_operation` 呼び出し / kernel が動いた / `Unsupported` | 4,075 / 4,075 / 0 | 8,024 / 8,024 / 0 |
| 内訳 | `reserve` 1,994、`consume` 2,026、`delivery` 53、`loss` 1、`release` 1 | `reserve` 4,008、`consume` 4,008、`delivery` 8 |
| 比べた値（台帳の全体と `resources`、JSON の文字列） | 6,948,639 | 596,137,098 |
| 行と木の比較（7日分は 2,000回を過ぎると32回に1回） | 4,075 | 2,188 |
| `metric_values` 動いた / `Unsupported` | 31,757 / 0 | 10,417 / 0 |
| `reservation_id_was_used` 動いた / `Unsupported`（どれも `False`） | 14,501 / 0 | 16,201 / 0 |
| `validate` 受け入れ / 断り（受け入れるたびに Python の規則も通過） | 29,210 / 0 | 10,080 / 0 |
| 違い | 0 | 0 |

ゲートの `--reversed-order`（3変種の合計）：`apply` 12,225回、`metric` 95,271回、`used` 43,503回、`validate` 87,630回。`Unsupported` 0回、違い0件です。

**7日分はこのレーンの影だけで動かした結果です。** 全部の影を掛けた7日分は、こちらが与えた 7,000秒の制限で 9,570分目に止まり、結果がありません（負荷の下で遅くなりました。止まるまでに違いが出たかは、結果を書く前に止まったので分かりません）。ほかのレーンの影は、このコードの7日分では動いていません。ゲートでは全部の影が動いて違い0件です。

`True` の枝（使った ID）は、ゲートにも7日分にもなく、テストだけが通ります。

### 影の感度（コピーで変更。`--only long_generated`）

| 変更 | 影 | ゲートのハッシュ |
|---|---|---|
| `consume` の `served` を約 1% 増やす | `apply` の1件で止まり報告（`resource-consume-69`。そのあと kernel が別の値を見て `Unsupported`） | 問題2件 |
| `reserve` の `reserved` を約 1% 増やす | 同じ（`resource-reserve-66`） | 問題2件 |
| KPI の列 `*_served` だけを約 1% 増やす | 20件以上（`metric`） | 問題あり |
| 行の監査の bytes だけ変える | 20件以上（`the row and the tree`） | **問題0件（ハッシュに見えません）** |
| `validate` から最新の記録との比較を外す | ゲートでは正しい状態しか来ないので見えません。テスト2件が失敗します（`test_balances_that_add_up_...`、`test_the_check_takes_nothing_python_refuses_on_random_writes`） | — |

## 確認

2026-11-01、WSL2、16コア、ほかのレーンと同時。基準は `408ad51`。

| 確認 | 結果 |
|---|---|
| `tick_budget.py`（9600分から480 tick、3回） | **0.963**（0.963、1.023、0.877）。A/A 1.020（1.038、1.010、1.020）。状態は3つとも一致。load average 約 25〜46 |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件。`rule_shadow` の回も同じ |
| `--reversed-order`（`rule_shadow` つき） | 55シナリオ、問題0件、違い0件 |
| 7日分のハッシュ | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致 |
| 両モードの全テスト | Rust あり 3475件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2047件成功、1434件 skip |
| `--coverage`（行。kernel ありとなしの合計 / kernel ありだけ） | `domain/resources.py` 279/369（76%）/ 189/369（51%）、`domain/resource_service.py` 393/461（85%）/ 同じ、`domain/care_receipts.py` 140/164（85%）/ 同じ |
| `ruff`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません。** `state_mirror.py` は登録の2行だけで、意味は変えていません |

部品の費用は上の調査の表です。`tick` の合計の差（85.2→77.5秒）は同時に動かした1回の値で、`tick_budget.py` の揺れの中では言えません。


## 別名の変化

`world.model_state[KEY]` はもう置き換えられないので、持ち続けると次の操作の後の値が見えます。今までは操作の前の値のままでした。`src/` でこの形は調べた範囲でありません（`resource_service.py` の `account` は操作の前にだけ読みます）。`test_a_successful_operation_leaves_the_previous_ledger_untouched` は Python の規則の形なので、Python の規則を直接呼ぶように直しました。`SHELTERSIM_NATIVE=0` と Python の規則の経路の動作は変わりません。

## テストの変更

- `tests/support/python_rules.py` に `apply_resource_operation`、`resource_metric_values`、`_validate_audit_record` を足しました。`test_save_validation.py` の `reads` を使うテストは `@pytest.mark.python_rules` が付きます。**ただしこの印は `kernel_first` の kernel を切りません**（今までと同じです）。`reads` が数えるのは保存の前の再生で、これは Python なので結果は変わりません。
- `test_the_tick_memo_still_refuses_bad_reservation_ids` は Python の規則を直接呼びます。`TickValidationMemo.reservation_ids` を使うのは今は Python の規則だけです。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API は `PyDict_DelItem` で、stable ABI です。
- **`ResourceServiceManager.cancel`、`interrupt_for_staff` の `release`：** ゲートの `release` は1回、7日分は0回で、`test_each_operation_is_the_python_rule_to_the_bit` と乱数のテストが補います。
- **木の節点への `dict.__setitem__` は行に届きません**（レーン T と同じ）。`src/` にこの書き込みはありません。
- **行の監査の bytes と節点：** `audit` の木はそのままなので、Python のオブジェクトは減らず、bytes の分（7日分で 8,000件 × 約 400 バイト）が増えます。RSS は測っていません。
- **分岐の読み込み：** 行を作るときに監査の全件を読みます。`fork_seconds` は負荷で揺れていて、差は言えません（基準 37.0、18.5、29.7秒、レーン 41.6、18.6、20.9秒）。
- **`python_rules` の印で台帳の kernel は切れません。** `apply_resource_operation` を patch するテストは今はありません。足す場合はこの点に注意してください。
- **レーンとの重なり：** `events/handlers.py`、`experiments/phases/dispatch.py`、`domain/movement.py` は変えていません。`native/src/core/world_core.rs` に `delete_through` を足し、`list_at` のコメントを広げました。ほかのレーンが同じ関数を足すと衝突します。`state_mirror.py` は `OWNED_NAMESPACES` と `_HASHED_BY_PARTS` の2行です。
