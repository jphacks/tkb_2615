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

