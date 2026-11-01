# M3：本体の状態（`body_state_v1`）

Rust が `body_state_v1` を持ち、毎 tick と毎イベントの書き手が一緒に移りました。結果は変わりません。Python の規則は比較の基準として残ります。

## 形が合うか（「計画して書く」）

**合います。** 他の M3〜M6 のレーンの計画に使えるように、調べたことを書きます（`9600分` からの分岐、100人）。

| 書き手 | 回数 | 形 | 扱い |
|---|---:|---|---|
| `apply_state_deltas` | 1回/tick、最大700値 | 計画して書く。もともと kernel | 行に書く |
| `record_social_event` | 約6.5回/tick。履歴約13件 | **イベントごと**ですが、1回の中は計画して書く形です。コールバックはなく、途中で読む Python のオブジェクトは凍った payload だけです | kernel。呼ぶ側は Python のまま（`kernel_first`） |
| `events/handlers.py` のケアと衛生 | イベントごとに数項目 | 1項目ずつの転送 | **Python のまま。** 名前空間を丸ごと書きません |
| `domain/population*.py` | 入退所のとき | 記録1件ずつの転送 | **Python のまま** |
| `events/effects.py` | 0 | `body_state_v1` を書きません。`policies/registry.py` は経路名を返すだけです | なし |

- **M3〜M6 への教訓：** 「イベントごと」は形の問題ではありませんでした。規則の中で別の規則や Python のオブジェクトを呼ばなければ、1イベントを1つの kernel にできます。境界の費用は 1回約 20 µs の中に収まります。
- **モデルの選択だけが Python の呼び出しです。** `selected_models` を 1回呼びます（tick の中はキャッシュされます）。選択がない Scenario では `canonical_body_config` も呼びます。
- **+16% の正体：** レーン I の +16% は、`apply_deltas` が `Unsupported` になり、Python の規則が約 700 値を転送していた費用が中心と見ています（規則を移すと消えました。+16% の内訳を個別に測ったわけではありません）。
- **履歴は行で deque にしました。** 上限 20,000件に達した後は、追記のたびに先頭が落ちます。`Vec` だと全件を動かします。

## 移したもの

| もの | 場所 |
|---|---|
| 行（`BodyRecord`、`Relationship`、`Relationships`、`History`、`BodyRow`） | `native/src/state/body_state.rs` |
| `record_social_event` と影響・信頼の演算 | `native/src/rules/body_state.rs` |
| `apply_deltas` の本体の書き込み | `native/src/rules/psychology.rs` |
| 検証 `rows_valid` | `native/src/state/body_state.rs` |
| 比較 | `benchmarks/rule_shadow/shadow_body_state.py` |
| テスト | `tests/integration/test_body_state_row.py` |

- **履歴は項目ごとの正準 bytes です。** 規則は履歴を読みません。古い項目への書き込みは合わないので taint します。
- **`rows_valid` は、書かれた関係だけを読みます。** Rust の書き込みも転送もキーを控えます。人の ID が変わると全部を読み直します。`false` のあとの確認も全部を読みます。
- **キーの判定：** ID が `[A-Za-z0-9._:-]` だけの `["a","b"]` の形だけを受け入れます。それ以外は `false` で、Python の規則が読みます。

## モデルの担当の方に見ていただくコード

`rules/body_state.rs` に次が入りました。係数と式の意味は変えていません。

- `domain/body_state.py`：`record_social_event`、`_record_relationship`、`_relationship_trust`、`_apply_affect_contribution`、`_body_deltas`、`_recorded_contact_distance`、`_unit`
- `policies/affect.py`：`LinearAffectPropagationPolicy`、`TrustWeightedAffectPropagationPolicy`、`_bounded_difference`
- `policies/relationships.py`：`DistanceDurationRepeatTrustPolicy`、`DurationOnlyTrustPolicy` の `update_trust`

**Rust に書いた定数：** 苦情の応答と訂正の `negative_affect` の減少 0.01、`contact` の 0.25（2か所）、`social_action` の 0.5（2か所）、信頼の重みと基準線の 0.5、信頼の既定値 0.5、1日 1440分、反復の上限 10、距離の尺度の既定値 2.0 m、反復の係数の既定値 0.05、`social_interaction_minutes` の既定値 5、topic の上限 32。上限：履歴 20,000件、関係 100,000件、topic の長さ 64字、睡眠負債 1440分。ほかの係数は実行時に `config` から読みます。

## 任せるもの（`Unsupported`）

変更の前に返し、Python の規則が同じ文を出します。ゲートと7日分では、下のどれも起きませんでした。

- 型が違う値：int の設定値や信頼（taint）、str でない ID、float でない距離
- 記録のない人、信頼が `None` の関係、自分自身とのやりとり
- 知らないモデル ID、選択や設定が例外を出すとき
- ID に JSON の escape が要るとき

## 確認の結果

2026-11-01、WSL2、16コア、`eef13e7`（レーン F を取り込んだあと）。ほかのレーンが同時に動いていました。**7日分の `rule_shadow` だけは1つ前の `08d922a` の結果です**（レーン F の取り込み前）。

| 確認 | 結果 |
|---|---|
| `e657934`（社会関係のレーンを取り込み、`watch.rs` をやめたあと）：`tick_budget.py`（基準 `ac275df`） | **0.971**（0.971、0.958、1.031）。A/A 1.013（1.013、1.082、0.936）。状態は3つとも一致。load average 11〜13 |
| `e657934`：ゲート、`--reversed-order`、`rule_shadow`（ゲート）、7日分のハッシュ | ゲート55個とも問題0件、`rule_shadow` の違い0件、7日分は期待値と一致 |
| `e657934`：テスト | Rust あり 3422件通過・6件 skip。`SHELTERSIM_NATIVE=0` 2047件通過・1381件 skip |
| `tick_budget.py`（9600分から480 tick、3回、基準 `3aefc03`） | 基準との比 **1.007**（0.977、1.007、1.042）。A/A 1.009（0.998、1.010、1.009）。状態は3つとも一致。load average 約 3〜4 |
| 同じ（`08d922a`、基準 `de98462`、load average 10〜17） | 1.038（1.038、0.977、1.087）。A/A 1.043 |
| 同じ（`ec455ac`、基準 `41bde2f`、load average 約 10） | 0.951（0.920、0.951、1.025）。A/A 0.979 |
| `gate_all_paths.py --expect`（`rule_shadow` の回） | 55個すべて一致 |
| `--reversed-order` | 55個すべて一致 |
| 7日分のハッシュ（素の回は `eef13e7`、`rule_shadow` の回は `08d922a`） | どちらも world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致 |
| テスト | Rust あり 3350件通過・6件 skip。`SHELTERSIM_NATIVE=0` 2047件通過・1309件 skip |
| `ruff`、`cargo clippy` | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（コーディネーターがまとめて動かします） |

| `rule_shadow` | ゲート | 7日分 |
|---|---:|---:|
| `record_social_event` の呼び出し | 35,380 | 54,061 |
| kernel が動いた | 35,227 | 54,061 |
| 名前空間なし（何もしない） | 153 | 0 |
| 断り | 0 | 0 |
| 比べた値（木） | 11,842,238 | 39,604,258 |
| 比べた値（行） | 10,880,420 | 38,219,301 |
| 比べた履歴の項目 | 71,320 | 108,122 |
| 行全体と木の比較 | 118 | 136 |
| `rows_valid`：受け入れ / 断り | 32,622 / 0 | 10,530 / 0 |
| 壊した写しの確認（すべて kernel が断りました） | 57 | 30 |
| `apply_deltas` が動いた / 手順 | 31,530 / 31,530 | 10,080 / 10,080 |
| 違い | 0 | 0 |

イベントの内訳（ゲート）：`social_interaction` 29,328、`information_contact_update` 4,885、`proximity_contact_update` 916、`information_correction` 98。7日分は `social_interaction` だけです。

**影の最初の版は違いを 2,398件報告しました。** どれも「関係」で、影の写しが持つキーの数が少ないために新しいキーの順序が木と違うという、影の側の誤りでした。キーごとの比較に直して0件になりました。木のキーの順序そのものは、`test_each_event_is_python_s_bit_for_bit` が全体の JSON の文字列で比べています。

### 影の感度（コピーで変更）

| 変更 | 影 | ハッシュ |
|---|---|---|
| 信頼の利得を 1.01 倍 | 2,446件 | `long_generated` で不一致 |
| `social_action` の効果を 1.01 倍 | 1,774件 | 不一致 |
| `contact` の正の移転と訂正の減少を 1.01 倍（`model__information__evidence-aware-rumor-v1`） | 102件 | 不一致 |
| 行の履歴の bytes だけ変える | 2,450件 | **一致（ハッシュに見えません）** |
| `rows_valid` の記録の上限を 1.02 に | 2件 | 一致 |
| `rows_valid` の信頼の上限を 1.02 に | 2件 | 一致 |

`contact` の変更は `long_generated` では0件でした。この Scenario に情報の接触がないからです。

## 費用

同じ分岐を基準とレーンで同時に3回、480 tick、スレッドの CPU 時間（µs/tick）。`ec455ac`、基準 `41bde2f`。部品の計測で、全体の差として確かめたものではありません。

| 部品 | 基準 | レーン |
|---|---:|---:|
| tick 全体 | 6052、6062、5742 | 5706、5966、5620 |
| `record_social_event`（6.3回/tick） | 207、201、194 | 130、137、127 |
| `validate_body_state`（毎 tick） | 111、116、113 | 46、49、45 |
| `validate_body_state`（保存の完全検証、1 tick あたりに換算） | 151、208、199 | 265、256、179 |
| `_apply_state_deltas` | 66、62、59 | 64、67、61 |
| `observe_event`（社会関係。別のレーン） | 55、57、54 | 64、67、62 |
| GC の世代0 | 432、430、441 | 449、487、474 |

## 網羅

ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）：`domain/body_state.py` 88% / 38%、`policies/affect.py` 98% / 67%、`policies/relationships.py` 100% / 51%。

## 残っているもの

- **ハンドラーのケアと衛生の書き込み**（`events/handlers.py`）、**`add_body_agent`・`remove_body_agent`、`population_lifecycle` の書き込み**：転送で動きます。ハンドラーを移す M3 の dispatch、M6 のレーンが移します。
- **`create_body_state`、`canonical_body_config`**：tick の外です。
- **検証の設定と履歴の規則**：`validate_body_state` に残ります。人口の状態と `model_configs` を読むからです。
- **保存：** Python の経路のままです（`_WORLD_TREE` の名前空間）。全体の bytes は作らないようにしました（履歴だけで 8 MB）。`relationships` と `agents` の bytes、履歴の項目の bytes を保存が使えば、`HistoryPins` と符号化を省けるはずです。測っていません。
- **`AgentView`、`WorldView`、`rules/action_lifecycle.rs` の `body_needs`：** 本体の値を木から読みます。行から読めば木を切り離す道が開きます。
- **Python の読み手：** `action_lifecycle`、`staff_operations`、`social_dialogue`、`social_network`、`inspection/frames` は木を読みます。切り離しはできません。

## やめた確認

| やめたもの | 代わりに証明するもの | 経路のテスト |
|---|---|---|
| `body_agents_valid` | `rows_valid`（毎回すべての記録） | `test_each_write_route_is_stopped_with_python_s_text`、`test_a_record_s_bounds` |
| `body_relationships_valid`、`_pair_key_is_good`、`_GOOD_KEYS` | `rows_valid`（書かれたキー。人が変われば全部） | `test_a_relationship_is_never_taken_when_python_refuses_it`、`test_a_key_is_never_taken_when_python_refuses_it`、`test_a_key_of_someone_who_left_is_read_again` |
| `TickValidationMemo.body_watch` | 行の書かれたキーの控え | `test_a_relationship_written_in_each_route_is_read`、`test_a_tick_s_write_of_a_kernel_is_read_by_the_next_check` |
| `native/src/watch.rs` の全体（`RecordWatch`、`install_dict_watcher`）、`native.record_watch`、`TickValidationMemo.relationships_to_check`、`PairKeys`、`topics_valid`、`social_relationships_valid` | 本体と `social_network_v1` の行が、書かれたキーを自分で控えます。使う場所がなくなりました。dict watcher は stable ABI の外だったので、ctypes で探す処理もなくなりました | 上のテストと `test_social_network_row.py`。読み込みの `test_tick_validation.py` の2件はやめました |

taint した世界では Python の規則が毎 tick すべての関係を読むので、遅くなります。`SHELTERSIM_NATIVE=0` は前からこの形です。

## `social_network_v1` との読み合い

`handlers.py` は `record_social_event` のあとに `observe_event` を呼びます。`observe_event` は本体の信頼を**木**から読むので、kernel が書いた値がそこにある必要があります。kernel の書き込みは同じ呼び出しの中で木に届きます。逆向きの読みはありません（`record_social_event` は社会関係を読みません）。`test_observe_event_copies_the_trust_this_kernel_wrote` が、4種類のやりとりで両方の名前空間を Python の規則とビット単位で比べ、2つの信頼が同じビットであることを確かめます。本体が taint しても木は残るので、`observe_event` は同じ値を読みます。

本体の値を読む kernel（`social_network`、`social_interaction`、`social_dialogue`、`action_lifecycle`、`world_view`）は木を読むので、木を切り離すには全部を行に変える必要があります。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API は `PyList_SetSlice`、`PyDictProxy_Type` で、stable ABI です。
- **木の節点への `dict.__setitem__` は行に届きません（レーン T と同じ）。** `rows_valid` は true を返します。`test_a_write_past_a_tracked_dict_is_seen_only_by_the_python_rules` が固定します。`src/` にこの書き込みはなく、lint と `verify()` が見つけます。
- **別名：** 入れた履歴のリストを持ち続けても、木とは別のものです。`test_first_difference.py` の1件はこれに頼っていたので、入れた後に読み直す形に直しました。`src/` でこの形を走査で確かめたのは lint の範囲だけです。
- **`rule_shadow` の外の枝：** 関係の上限（100,000件）はゲートにも7日分にも届かず、テスト1件だけです。ケアの Python の書き込みは影の比較の対象ではなく、行と木の比較で見ています。
- **`kernel_first` なので、`policies/affect.py` などへの monkeypatch は kernel に届きません。**
- **メモリ：** 行に履歴の bytes（約 8 MB）と節点の表が増えます。RSS は測っていません。
- **分岐の attach：** 復元のたびに履歴 20,000件を符号化し、節点を作ります。`tick_budget.py` の `fork_seconds` は基準 7.8、7.4、8.7 秒に対して 9.3、8.6、7.9 秒でした（同時に動かした3回）。
