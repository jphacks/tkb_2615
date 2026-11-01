# M3 レーン：社会関係（`social_network_v1`）

Rust が `social_network_v1` を型付きの行で持ち、書き手を一緒に移しました。**係数と式の意味は変えていません。** Python の規則は比較の基準として残ります。

## 移したもの

| もの | 場所 | 元の Python |
|---|---|---|
| 行と検証 | `native/src/state/social_network.rs` | `validate_social_network_state` |
| イベントの記録 | `native/src/rules/social_network.rs` の `observe_event` | `observe_event`、`record_contact`、`record_observed_contact`、`record_interaction`、`_relationship`、`_sync_body_trust` |
| 社会の伝播の接触の記録 | 同じ `record_observed_contacts`。`rules/social_propagation.rs` から Python を呼ばずに使います | `record_observed_contacts` |
| 状態の文脈 | `native/src/rules/social_dialogue.rs` | `capture_social_state_context`、`validate_social_state_context` |
| 社会的な行動のハンドラー | `native/src/rules/social_interaction.rs` | `SocialInteractionHandler._apply_typed_interaction` の `reassurance_exchange`、`family_care`、`voluntary_support` が成立する枝 |

**Rust に入った係数（モデルの担当者向け）：**

- `observe_event`：cooperation の +0.01、未設定の trust 0.5、relief の配分 0.5（相手）と 0.25（本人）、範囲 [0, 1] と signal の [-1, 1]、既定値（`positive_affect` 0.5、`negative_affect` 0.0、`social_action_affect_transfer` 0.02）、`shared_message_ids` の上限 64、`MAX_RELATIONSHIPS` 100,000
- ハンドラー：relief の配分 0.5（相手の trust）と 0.25（本人の trust）、本人の fatigue +0.002、`voluntary_support` の差 0.15、長さ 1〜1440分、同じ既定値
- 状態の文脈：範囲 [0, 1] だけです。係数はありません

## 残したもの

- `virtual_dialogue_example` と手がかりの閾値 0.65（`domain/social_dialogue.py`）。履歴を読むときだけ動き、tick の中にありません。
- ハンドラーの残りの枝。`complaint_response`、`cooperation`、`information_reaction`、種類のない形は、ゲートの社会イベントの 0.2% で、7日分には0件です。`cooperation` はイベントログを読み、`information_reaction` は `TypedMessage` の検証を通します。
- ハンドラーの抑止と失敗。文が event の `detail` に残るので、Python が言います。ゲートで約 12%、7日分で約 5% です。
- `events/handlers.py:438,643,772` の乱数のキーと、イベントの番号の割り当て。どれも情報のハンドラーで、ここでは動かしていません。
- dispatch のループ、`effect_event_guard_or_suppress`、`record_social_event`（`body_state_v1` のレーン）。
- `remove_agent`、`population_lifecycle.py` の退去と復元。1項目ずつの転送で動きます。
- trace つきの Run の `_social_propagation`。Python の手順が転送で書きます。

## 判断

- **検証は書かれた記録だけを読みます。** 行への入り口は転送と規則の2つだけで、どちらも記録のキーを控えます（`Relationships::note_write`）。断ったときは控えを残すので、直るまで断り続けます。控えが 4096 件を超えたら全件を読みます。読み込みのあとも全件です。
- **キーの確認：** `json.dumps` がエスケープする文字を含むキーは `false` にして、Python が判定します。ID を作る側も同じで、kernel はそういう ID を断ります。Python の `_text_pair_key` と Rust の `write_json_string` はエスケープが違う（`ensure_ascii`）からです。
- **体の状態とイベントは Python のオブジェクトのまま読みます。** `as_readable_dict` を使うので、`body_state_v1` が行になっても木は同じように読めます。
- **`observe_event` は読みを先に済ませ、そのあと書きます。** 型の違う値、自分自身との接触、上限の近くは、何も書かずに `Unsupported` です。

## やめた確認

| やめたもの | 代わりに証明するもの | テスト |
|---|---|---|
| `social_relationships_valid`（`validation.rs`、`native.py`） | `state/social_network.rs` の `validate` | `test_native_kernels.py` の全組み合わせの比較 |
| `RecordWatch` の社会関係の分（`TickValidationMemo.social_watch`） | `note_write`：行に入るすべての書き込みが控えを残す | `test_every_write_the_check_of_a_tick_must_see_after_it_has_passed`（12経路）、`test_more_writes_than_it_lists_still_reach_the_check` |
| `_GOOD_PAIR_KEYS`、`_key_is_good` | `pair_key_is_good`（Python が断るキーを取らない） | `test_the_kernel_takes_no_key_python_refuses`（乱数 10,000 個） |

`watch.rs` は `body_state_v1` が使うので残しました。`validate_social_network_state` の `tick_memo` 引数はなくしました。

**証明できないもの：** 追跡する木の節点への `dict.__setitem__` は行に届きません。[t-tick-validation.md](t-tick-validation.md) の穴と同じで、このレーンで広がっても狭まってもいません。

## 費用

`tick_budget.py`（`fork-b1` の 9600分から480 tick、3回、同時実行、スレッドの CPU 時間）。**基準との比 0.964**（1.062、0.964、0.949）、A/A 1.020（1.025、1.008、1.020）、状態は3つとも一致。load average 約 27〜36。予算の +10% の中です。

部品（同じ条件で基準とレーンを同時に3回。計測の枠の分を含みます）：

| µs/tick | 基準 | レーン | 回/tick |
|---:|---:|---:|---:|
| 検証 `validate_social_network_state` | 144、148、144 | 24、28、26 | 1 |
| `SocialInteractionHandler.apply`（抑止で Python に行く分を含む） | 178、201、185 | 80、101、87 | 6.6 |
| `capture_social_state_context` | 122、131、126 | 56、63、61 | 6.6 |
| `observe_event` | 78、87、86 | 53、56、55 | 6.3 |
| tick 全体 | 9,005、9,765、9,613 | 8,771、9,254、9,250 | |

4つで約 310 µs/tick、tick の約 3% 減ります。tick 全体の差（約 310 µs）とは合いますが、部品の足し算で、A/A の揺れと同じ程度の差です。

## 結果

2026-11-01、WSL2、16コア、ほかのレーンと同時。コードは `3788de1` です。

| 確認 | 結果 |
|---|---|
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order`（段階の終わりと段階の途中）に `rule_shadow` | 55シナリオ、3変種すべて問題0件、違い0件 |
| 7日分に `rule_shadow` | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致、違い0件 |
| `tracked_mirror_gate.py` | **動かしていません。** `state_mirror.py` は登録の1行だけです |

レーン F をマージした `f10f749` で動かし直したもの：両モードの全テスト（Rust あり 3332件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2047件成功、1291件 skip）、ゲートに `rule_shadow`（55シナリオ問題0件、違い0件）、7日分（ハッシュ一致）。**逆順のゲート、7日分の `rule_shadow`、`tick_budget.py` は `3788de1` の値です。**

`rule_shadow`（上段は3変種の合計なので、ゲート1回分はおよそ3分の1。下段は7日分）：

| | ゲート×3 | 7日分 |
|---|---:|---:|
| `observe_event` kernel が動いた / `Unsupported` | 105,681 / 0 | 54,061 / 0 |
| 比べた値 | 1,622,622 | 864,976 |
| `apply` kernel が動いた | 87,738 | 54,061 |
| `apply` `Unsupported`：Python が抑止 | 12,354 | 2,736 |
| `apply` `Unsupported`：ほかの種類 | 246 | 0 |
| `apply` 比べた値 | 526,428 | 324,366 |
| `capture_context` 動いた / `Unsupported` | 100,221 / 0 | 56,797 / 0 |
| `validate_context` 動いた / `Unsupported` | 268,257 / 0 | 170,391 / 0 |
| 検証 受け入れ / 断り | 105,183 / 0 | 10,434 / 0 |
| 検証の壊した写しの確認（1% の範囲外と、表を通らない木） | 102回、すべて断り | 20回、すべて断り |
| 伝播の接触の記録 | 559,980 | 22,462 |

- **道具の確認：** コピーで本人の relief の配分を 0.25→0.2525、相手の trust の配分を 0.5→0.505 に変えると（`long_generated`）、`rule_shadow` が35分目から `apply` と `observe` の違いを報告し、ゲートも `world_hash`、`events_hash` で失敗します。
- **順序の比較：** `social_propagation_shadow._text` は、順序を逆にする回（`SHELTERSIM_GATE_REVERSE`）だけキーの順序を見ません。修正前は逆順のゲートで 1,920 件の誤報がありました（コーディネーターの計測）。修正後は0件です。順方向の回は順序を見たままです（テストで固定）。
- **`--coverage`（行。kernel ありとなしの合計 / kernel ありだけ）：** `domain/social_network.py` 158/180 行（88%）/ 70/180（39%）、`domain/social_dialogue.py` 102/119（86%）/ 73/119（61%）、`events/handlers.py` 1091/1406（78%）/ 1077/1406（77%）。`--coverage` は `3788de1` の1つ前のマージ（レーン H の前）の値です。
- 両モードの全テスト：Rust あり 3187件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2002件成功、1191件 skip。
- `ruff`、`cargo clippy`（`disallowed_methods`）：問題なし。

## 確かめていないこと

- **Windows と macOS では動かしていません。** `Tree::list_remove_first`（`PyList_SetSlice`）は stable ABI の中です。
- `cooperation` の +0.01 と `information_reaction` の枝は、ゲートでも少ない回数です（ハンドラーでは2回と22回）。`observe_event` の枝は `test_observe_event_computes_trust_when_the_body_has_no_record` が補います。
- `shared_message_ids` の上限 64 の枝がゲートで通った回数は数えていません。テストが確かめます。
- `apply` の行と objects の一致は 500 回に1回だけ確かめます。ゲート×3 で3回、7日分は500回ごとの回数を `apply` と `observe` の共通の数で数えるので、`observe` が先に当たると0回に見えます。全体の一致は保存の `verify` とテストに頼ります。
- `body_state_v1` が行になったあとの動作は、このブランチでは試していません（木の読みは同じ形です）。
