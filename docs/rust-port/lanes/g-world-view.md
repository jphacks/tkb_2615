# M2 レーン G：`WorldView`

段階が作る `WorldView` を `native/src/rules/world_view.rs` が行から作ります。新しい係数はありません。移したモデルの式は「移したモデルのコード」に挙げます。

## 移したもの

| もの | 中身 |
|---|---|
| `view_after_movement`、`view_for_proposals` | `_view_after_movement`、`_view_for_proposals` を置き換える kernel（`segments.py`）。`from_world`、`after_state_update` と監査のハッシュに当たります |
| `ViewData`（`RustView.data()`） | `AgentView`、`FacilityView` のすべての項目を持つ、Python を参照しない写し |
| `WorldView.__getattr__` | `agents`、`facilities`、`zone_members`、`group_members` は、Python の読み手が最初に求めたときに作ります |
| 心理と社会の kernel | 区域の環境、体調の記録、`group_id`、情報の圧力、必要な睡眠を `view_of` から読みます。`AgentView.__dict__` を読むのは、Python が作った view を渡されたときだけです（`partial`） |
| 情報の kernel | view を作らず、`group_of` を共有します。`household_id` が `profile.group_id` を上書きする規則は1か所です |
| `@pytest.mark.python_rules` と lint | `tests/support/python_rules.py`、`tests/unit/repo/test_python_rules_lint.py` |
| 影 | `benchmarks/rule_shadow/world_view_shadow.py`（結果は `DIR/world_view`） |

## 移したモデルのコード

モデルの担当者向けの一覧です。式と値は変えていません。Python の規則は比較の基準として残しています。

| Python | Rust | 中身 |
|---|---|---|
| `domain/infrastructure.py` の `active_environmental_stress` | `active_stressors`、`read_zones` | 有効なストレス要因の `stress_per_minute` の `fsum`、開始イベント ID の並べ替え、ID 順で最初の `source_id` |
| `from_world` の区域の項目 | `read_zones` | `environmental_stress_per_minute` = 上の合計 + `temperature_stress_per_minute`。気温のストレスがあるときの仮定 ID の選び方 |
| `from_world` の誤情報の圧力 | `misinformation_of` | 偽の主張で `unknown` でなく、`belief_strength > 0.5` のとき `2.0 * (belief_strength - 0.5)` を足し、`min(1.0, 合計)`。**係数 2.0、閾値 0.5、上限 1.0 が Rust にもあります** |
| `from_world` の既定値 | `NO_BODY`、`read_zones`、`read_body_and_distress` | 記録がないときの `positive_affect` 0.5、ほかの体調 0.0、`sanitation_level` 1.0、`chronic_distress` 0.0 |
| `from_world` の待ち時間 | `read_rows` | `queue_wait_minutes`、`support_wait_minutes`、`support_cooldown_remaining_minutes` の `max(0, ...)` |
| `from_world` の `group_id` | `group_of` | `household_id` が `profile.group_id` を上書きする規則 |
| `infrastructure_event_context_ids`、断水の判定 | `read_facility_contexts` | 設備に効く停止のイベント ID（数値はありません） |

`zone_environment_view` の式（気温、騒音、換気など）は Python のままです。

## レーン H が使うもの

- `rules::world_view::view_of(view) -> PyResult<Bound<RustView>>`、`RustView::data() -> &ViewData`、`RustView::key(py, index)`。`ViewData` は `agents`（`ViewAgent`、ID 順）、`zones`、`facilities`、`zone_of(agent)`、`index_of(id)`、`zone_members()`、`group_members()` を持ちます。
- **`partial` を断るのは H の `rules/behavior.rs` だけです。** Python が作った view では `ViewData::partial` が true で、更新の kernel が読む項目だけが入り、ほかは 0 や空です（`facilities` も空、`minute_index` は 0）。`needs` や `activity` も実際の値ではないので、それらを読む H は `partial` を見て自分で `Unsupported` を返す必要があります。

## Python に残したもの

人の文脈の解析（`PersonContext`）と区域の環境（`zone_environment_view` と式）は Python のままで、入力が変わったときだけ呼びます。`applied_facility_event_ids` は設備が欠けているときに呼びます。`selected_models` は view ごとに1回呼びます。

## 遅延の判断

作るのは Python の読み手が求めたときだけにしました。更新の段階では、Run の最初の tick を除いて作りません（60 tick で、最初の tick の社会の手順が1回）。提案の段階でも、レーン H が `propose_due` を移したので作りません（[h-behavior.md](h-behavior.md)）。監査のハッシュは今までと同じく、view を作るときに計算します。

## 計測

2026-10-31、WSL2、16コア。ほかのレーンと同時に動かしました（load average 51〜107）。

`tick_budget.py`（9600分から480 tick、3回）：基準との比 **0.925**（0.988、0.920、0.925）、A/A は 0.979（0.952、0.979、1.020）。世界の状態は3回とも一致しました。

部品（µs/tick、基準とレーンを同時に3回、スレッドの CPU 時間）。**どの回も30 tick しか数えていません。** スクリプトが480や240まで数えなかった理由は調べていません。

| 部品 | 基準 | レーン |
|---|---:|---:|
| `_view_after_movement` | 761〜919 | 251〜371 |
| `_view_for_proposals` | 559〜613 | 218〜248 |
| Python のオブジェクトを作る（`_apply_due_proposals` の中） | なし | 212〜266 |
| `_psychology_update` | 195〜271 | 121〜177 |

## 結果

2026-10-31〜11-01、`ae88a44` の拡張（sha256 `d5586511…`）で実行しました。その後の変更は文書とコメントだけで、再ビルドした `.so` は同じ byte 列です。

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 2465件成功、6件 skip。`SHELTERSIM_NATIVE=0` 1778件成功、693件 skip |
| `gate_all_paths.py --expect`（影なし） | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| 7日分 `--expect` | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。すべて一致。474秒、スレッドの CPU 時間 195秒（load average 27〜47） |
| `tracked_mirror_gate.py`（55シナリオ × 4回） | 220行、問題0件。段階の比較は合計 662,130 回 |
| `ruff check`、`ruff format --check`、`clippy` の disallowed_methods | 通過 |
| `rule_shadow`（ゲート全体、55個。hash も `--expect` と一致） | view の作成 31,530回 × 2、断り0回、比べた値 228,583,660個、違い0件。ほかの影も違い0件 |
| `rule_shadow`（7日分。hash も一致） | view の作成 10,080回 × 2、断り0回、比べた値 253,295,400個、違い0件。ほかの影も違い0件 |
| 道具の確認（コピーで変更） | 環境のストレスを 1.01 倍にすると影が報告。`household_id` の上書きを外すと、30分の生成 Run では影に出ず、ゲートでは `population/reunite` の30分目から影が報告し、ハッシュも失敗。テストも失敗 |

**kernel の断り：** 影の回で、この2つの view の kernel の断りはゲートでも7日分でも0回です。影のない回は数えていません。7日分のほかの断りは社会の手順の1回だけで（`the namespace has no row`）、Run の最初の tick です。この tick では Python の社会の手順が view を求めます。

**どの値を通ったか：** 7日分では、問い合わせの圧力、誤情報、区域の需要の変更、ストレス要因、状態の変更、騒音、設備の文脈を持つ人や設備は0件でした。これらはゲートだけが通ります（誤情報 113,566人回、ストレス要因 15,828人回、状態の変更 11,512人回、騒音 4,092人回、需要の変更 2,930人回、設備の文脈 6,848設備回、問い合わせの圧力 141,308人回）。

## 確かめていないこと

- Windows と macOS では動かしていません。使った C API（`PyDict_Copy` など）は stable ABI です。
- `first_tasks` は行の順序で最初のタスクを選びます。行の順序は挿入順で、ふだんは Python の dict と同じです。同じ人に列のタスクが2つある場合、順序を逆転した回では Python と違う可能性があります。逆転の回は問題0件でしたが、影はこの回で動かしておらず、タスクが2つある人が通ったかも数えていません。
- `gate_all_paths.py --coverage` は実行していません。行の網羅は上の影の件数で見ています。`read_facility_contexts` などの分岐ごとの網羅は測っていません。
- `from_python_view` の値（`partial`）は影で比べていません。ふつうの Run では通らず、テストの Python 製の view だけが通ります。
