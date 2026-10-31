# レーン B：社会の伝播（`social_propagation_v1`）

Rust が `social_propagation_v1` を持ち、更新の段階の手順 `_social_propagation` を3つの方策ごと Rust で動かします。手順は [p5-template-namespace.md](p5-template-namespace.md) のとおりです。

## 移したもの

| もの | 場所 |
|---|---|
| 行（`schema_version`、`contacts`、`agent_cooldown_until`） | `native/src/state/social_propagation.rs` |
| 手順（接触の抽選、続いた分数、クールダウン、3つの方策の式） | `native/src/rules/social_propagation.rs` の `update_social_propagation` |
| 接触の記録を丸ごと入れ替えずに書く | `NamespaceMirror::refill_record_map`、`Tree::order_keys`、`Tree::clear`（`core/world_core.rs`） |
| Rust の中の抽選の記録（比較の道具） | `start_draw_log`、`take_draw_log`（`core/rng.rs`） |
| 比較 | `benchmarks/rule_shadow/social_propagation_shadow.py` |

モデルの式（モデルの担当者向け）：`StressPropagationPolicy.update`、`ExpressivenessChannelOpennessPolicy.update`（`policies/social.py`）、`DeGrootContactPolicy.update`（`policies/alternatives.py`）、`_contact_buckets`、`_select_contacts`。**係数と式の意味は変えていません。** Python の規則は比較の基準として残ります。

やめたもの：`ExpressivenessChannelOpennessPolicy.native_update`、`native.social_channel_update`、`_social_propagation` の中の分岐。

## 判断

- **丸ごとの書き込みをやめました。** 接触は毎分ほぼすべてのキーが変わる（1 tick に約300件）ので、差分を書いても得がありません。`contacts` の dict を空にして Python の新しい dict と同じ順に入れ直し、記録の節点（`TrackedDict`）は使い回します。節点の表のキーだけを新しいキーに変えます。記録の意味は値だけで、tick をまたいで記録を持ち続けるコードはありません。tree から `pop` された記録は dict にないので、使い回されません。
- **キーの順序：** 設計では順序は結果に関わりませんが、ここでは Python の新しい dict と順序まで同じにしました。比較を JSON の文字列のまま厳密にできるからです。復元のあとは順序が JSON の順になるので、最初のキーの比較で見分けます（`first_key_is`、`order_keys`）。
- **人の値は行から読みます。ただし group だけは `WorldView` から読みます。** `population_presence_v1` の `household_id` が `profile.group_id` を上書きするためです（`policies/world_view.py`）。行の `profile.group_id` を読むと、家族の接触が静かに変わります。
- **方策と乱数は `policy_version` ではなくクラスの同一性で確かめます。** 派生クラス、`CounterRNG` でない乱数は Python に任せます（`policies/social.py` の旧 `type(rng) is CounterRNG` と同じ）。
- **`social_network_v1` への記録** は `record_observed_contacts`（`domain/social_network.py`）を、すべて書いたあとに呼びます。Python の手順と同じ順番です。この名前空間を移す作業（M3）が中に取り込みます。

## Python に任せる場合（`Unsupported`）

trace、`CounterRNG` でない乱数、方策の派生クラス、int の係数（`stress_spread_per_minute=0` など）、taint、schema 違い、負の値、重みのない接触（Python はここで例外）、delta と view の不一致、範囲外の stress。何かを変える前に判断します。

**Run の最初の tick：** 名前空間がまだないので Python の手順が動き、丸ごとの書き込みが1回起きます。

## 残っているもの

- trace つきの Run は Python の規則で、毎 tick 丸ごと書きます（設計どおり）。
- `population.py`、`population_lifecycle.py` の接触の書き込みは1項目ずつの転送です（まれです）。
- 木はまだ毎 tick 書いています。tick の中で Python が読まないので切り離せそうですが、切り離すと `model_state` が `_DetachingTable` になり、ほかの kernel が断ります。

## 費用

`tick_budget.py`（9600分から480 tick、5回、同時実行、スレッドの CPU 時間）。実行中の load average は約 70〜140 です。

| | 基準との比 | 各回 |
|---|---:|---|
| レーン | **0.959** | 0.963、0.953、0.939、1.014、0.959 |
| A/A | 1.013 | 0.998、0.997、1.024、1.060、1.013 |

世界の `state_digest` は5回とも3つが一致しました。各回の比の幅（0.94〜1.01）は A/A の幅（1.00〜1.06）と同じくらいで、A/A の中央値は 1.013 なので、**差は小さく、予算の +10% には届きません**。約4% 速いという値は、下の部品の差とほぼ合いますが、A/A の揺れと近いので、これだけで言い切りません。

| 部品（同じ条件、基準とレーンを同時に3回） | 基準 | レーン |
|---|---:|---:|
| 手順 `_social_propagation`、µs/tick（`social_network_v1` への記録を含む） | 522、492、514 | 336、320、319 |
| tick 全体、µs/tick（同じ回） | 10,055、9,753、10,368 | 9,833、9,801、9,598 |

手順は約 180 µs 減り、tick の約 2% です。

## 結果

2026-10-31、WSL2、16コア。コードは `bf81452` です。

| 確認 | 結果 |
|---|---|
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| 7日分のハッシュ | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致 |
| `rule_shadow`（ゲート全体） | 手順 31,530回。kernel 27,865回（channel 27,507、sparse 179、DeGroot 179）、無効 3,120回、`Unsupported` 545回（trace 480、Run の最初の tick 65）。比べた値：抽選 3,123,326（鍵とビット）、接触 2,449,921、クールダウン 291,931、delta 829,958、`social_network_v1` への記録 186,660。違い0件 |
| `rule_shadow`（7日分） | 手順 10,080回。kernel 10,079回、`Unsupported` 1回（最初の tick）。抽選 3,185,930、接触 3,019,364、クールダウン 96,506、delta 1,007,900、記録 22,462。違い0件 |
| 道具の確認：1% 変えたコピー（`model__social__*` の3シナリオ） | 変えない回は違い0件。channel の gamma、DeGroot の alpha、sparse の寄与、抽選の値のそれぞれで、`rule_shadow` が違いを報告し、ゲートのハッシュも失敗 |
| `first_difference.py --both`（ゲート全体） | 55個すべて、220,892 段階で digest が同じ。最終のハッシュも同じ |
| `first_difference.py --both --draws`（`generated:1500:100`、5分ごと） | 301段階で digest が同じ。両側にある抽選 14,652件で違いなし。Python 側だけの抽選 783,934件は Rust の中なので、ここでは比べられません（`rule_shadow` が比べます） |
| `gate_all_paths.py --coverage`（kernel ありとなしの合計） | `policies/social.py` 288/315行、`policies/alternatives.py` 56/57行。通らない行は入力の検証のエラーと無効のときの戻りが主です |
| `gate_all_paths.py --expect --reversed-order`（段階の終わりと段階の途中） | 55個すべて、どの回も一致 |
| `tracked_mirror_gate.py`（55シナリオ × 4回） | 220行、問題0件。段階の比較は合計 662,130 回。追跡する回は Python の規則、比べる相手の素の回は Rust の規則です |
| 両モードの全テスト | Rust あり 2332件成功、6件 skip。`SHELTERSIM_NATIVE=0` 1771件成功、567件 skip |

途中で見つけた違い：最初の版は復元のあとの名前空間の dict のキーの順序が Python と違い、`rule_shadow` が報告しました（ハッシュは同じ）。`order_keys` で直しました。

## 確かめていないこと

- **Windows と macOS では動かしていません。**
- 記録の節点の使い回しは「tick をまたいで記録を持ち続けるコードがない」ことに頼ります。`src/` を読んだこととテストで確かめましたが、証明ではありません。
- 人の値を行から読むのは、この手順の時点で view と行が同じだという前提です。確かめたのはゲートと7日分の比較の範囲です。
- `order_keys` は最初のキーだけを見ます。順序は結果に関わらないので、ほかの順序の組み合わせは直しません。
- sparse と DeGroot はゲートで各179 tick だけです。`tests/integration/test_social_propagation_row.py` の乱数のテストがこれを補います。
