# M2 レーン H：行動の scorer

提案の段階の `propose_due` を `native/src/rules/behavior.rs` に移しました。結果は変わりません。`policies/behavior.py` は比較の基準として残ります。分割はしていません。`propose` は組み込みの2つの Policy の全部を移しました。

## 移したもの

| 手順（`segments.py`） | kernel | 中身 |
|---|---|---|
| `_propose_due`（新しい手順） | `propose_due` | `BehaviorDecisionRunner.propose_due`、`UtilityBehaviorPolicy.propose`、`PriorityRuleBehaviorPolicy.propose`、`_decision_interval_minutes`、`_sleep_debt_utility`、`_target_for_action`、`_demand_trigger`、`ActionCandidate`・`ActionProposal` の確認 |
| `_observed_state`（新しい手順） | `observed_state` | `_agent_view_snapshot`（`experiments/phases/propose.py`） |

- **読むもの：** レーン G の `ViewData` だけです。ほかに Policy の項目を呼ぶたびに読みます。`compute` は Python を読みません（手順4で人ごとに動かせます）。
- **返すもの：** Python の `ActionProposal` と記録用の dict です。後ろの `_apply_proposal` がまだ受け取ります。レーン J が移せば、作らずに済みます。
- **view：** ふつうの tick では、提案の段階でも Python の `agents` を作りません。
- **trace：** この規則は trace の文書を出しません。記録されるのは提案そのもので、同じオブジェクトの中身なので、trace のある Run でも kernel が動きます。

## 任せるもの（`Unsupported`）

kernel は何も書かず、抽選は状態を持たないので、どこで断っても Python の手順が同じ結果か同じエラーを出します。

- Python が作った view（`partial` を含む）、`state_hash` のない view
- クラスが完全一致でない runner、Policy、scorer、RNG
- Python がエラーにする値（`exp` の範囲外、重みの合計が 0、`ActionCandidate`・`ActionProposal` の確認に通らない値）
- NaN の圧力（タプルの `max` の結果が同一性に依存するため）、float か int でない Policy の項目

`propose`、`propose_due` などへの patch は kernel に届かないので、`tests/support/python_rules.py` に名前を足しました。

## 移したモデルのコード

モデルの担当者向けの一覧です。式と値は変えていません。**次の係数がすべて Rust にもあります。**

| Python | Rust | 係数 |
|---|---|---|
| `propose` の `distress` | `candidates_of` | `discontent * 0.75`、`negative_affect * 0.6` |
| `propose` の条件 | `candidates_of` | `support_need >= 0.25`、`distress >= 0.50`、`uncertainty >= max(0.65, 閾値)`、`pressure >= 0.25` |
| 待つ | 同上 | `0.10 + 0.15 * (1 - distress)` |
| トイレ | 同上 | 適格 `toilet >= 0.25` か `hygiene >= 0.5`。`0.20 + 0.90 * toilet + 0.35 * hygiene`。緊急 `toilet >= 0.80` か `hygiene >= 0.8` |
| 休む | 同上 | 適格 `fatigue >= 0.25`、`hunger >= 0.70`、`stress >= 0.50`。`0.10 + 0.45 * fatigue + 0.20 * hunger + 0.30 * stress` |
| 眠る | 同上 | 適格 `fatigue >= 0.65`、`sleep_debt >= 240.0`、睡眠の時間帯。`0.25 + 0.90 * fatigue + 負債 + (0.20) - 0.20 * max(0, lighting - 0.3)` |
| 睡眠の負債（`_sleep_debt_utility`） | `sleep_debt_utility` | `debt / 1440.0 * 0.45`。v2 は `weight * debt / required`、既定の必要睡眠 480 |
| 助けを求める | `candidates_of` | `0.15 + 0.65 * distress + 0.25 * support_need + 0.10 * trust + 0.20 * uncertainty + 0.15 * discontent - 0.10 * reassurance + query_weight * pressure`。緊急 `distress >= 0.80` |
| 情報を求める | 同上 | `0.20 + 0.55 * uncertainty + query_weight * pressure`。緊急 `uncertainty >= 0.9` |
| 話す | 同上 | `0.10 + 0.25 * uncertainty + 0.15 * negative_affect + 0.10 * (1 - reassurance)` |
| 家族の世話 | 同上 | `0.12 + 0.45 * pressure`。緊急 相手の `stress >= 0.8` |
| 手助け | 同上、`target_for_action` | `0.15 + 0.4 * pressure`。相手の条件 `pressure > distress + 0.15`（相手を選ぶときは `自分の pressure + 0.15`）。緊急 相手の `stress >= 0.8` |
| 水 | `candidates_of` | Policy の `water_*`、閾値。緊急 `hydration >= 0.8` |
| 食事 | 同上 | Policy の `meal_*`、閾値、時間帯の `+ 0.12`。緊急 `hunger >= 0.85` |
| 食事の時間帯 | 同上 | 起床から 120、420、660 分の前後 45 分 |
| 確率（`propose`） | `score` | `exp((u + p * 0.5 - max) / temperature) * 需要の倍率`。最後の適格な候補が残りを取る |
| 同点の処理（`PriorityRuleBehaviorPolicy`） | `compute` | `(priority, utility)` の最大、`action` の値の順、`1 / 同点の数` |
| 間隔（`_decision_interval_minutes`） | `decision_interval` | `round(base * duration_multiplier)`、上限と下限 |
| `immediate_needs` | `Frozen::new` | `stress` と4つのニーズの `max` |

Policy の既定値（`temperature` など）は Python のままで、呼ぶたびに読みます。`mathx` に `py_is_close`（`math.isclose` の既定の許容差）を足しました。

## 計測

2026-11-01、WSL2、16コア。ほかのレーンと同時に動かしました。

`tick_budget.py`（9600分から480 tick、3回。基準はこのレーンの直前の feature/rust-port）：

| 回 | 基準との比 | A/A | load average |
|---|---:|---:|---:|
| マージ前（レーン G の上） | **0.872**（0.872、0.866、0.929） | 1.004（0.996、1.004、1.004） | 37〜68 |
| 最後のマージの後（`924a610`） | **0.894**（0.931、0.864、0.894） | 0.937（0.935、0.937、1.060） | 7〜19 |

世界の状態はどの回も一致しました。2回目は A/A の揺れが大きいので、差は「1割前後の短縮」とだけ読みます。

部品（マージ前。µs/tick、生成 Run の最初の480分、基準とレーンを同時に3回、スレッドの CPU 時間、load average 約 30）。

| 部品 | 基準 | レーン |
|---|---:|---:|
| `_propose_due`（Python の `agents` を作る時間を含む） | 954〜990 | 86〜88 |
| `_observed_state` | 27〜28 | 31〜32 |
| `_apply_proposal` | 916〜939 | 989〜1,008 |
| `_run_propose_phase` 全体 | 2,215〜2,270 | 1,379〜1,392 |

`_apply_proposal` は約7%増えました。原因は調べていません。

## 結果

最後のマージの後（`924a610`、拡張の sha256 `2eb5c847…`）の結果です。その後の変更はこの文書だけです。

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 2882件成功、6件 skip。`SHELTERSIM_NATIVE=0` 1795件成功、1093件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| 7日分 `--expect` | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。すべて一致。スレッドの CPU 時間 124秒（load average 約 5〜20） |
| `tracked_mirror_gate.py`（55シナリオ × 4回） | 220行、問題0件 |
| `ruff check`、`ruff format --check`、`clippy` の disallowed_methods | 通過 |
| 道具の確認（コピーで変更、480分。マージ前の `c72ce8f`） | トイレの 0.90 → 0.909、手助けの 0.15 → 0.1515、照明の 0.3 → 0.303 と `discontent` の 0.75 → 0.7575。どれも影が報告 |

| `rule_shadow` | ゲート | 7日分 | 逆順のゲート（マージ前の `6132710`） |
|---|---:|---:|---:|
| `propose_due` が動いた / 手順の数 | 31,530 / 31,530 | 10,080 / 10,080 | 94,590 / 94,590 |
| `observed_state` が動いた / 手順の数 | 63,473 / 63,473 | 87,464 / 87,464 | 190,419 / 190,419 |
| 断り | 0 | 0 | 0 |
| 比べた値 | 11,717,550 | 16,207,050 | 35,152,650 |
| 違い | 0 | 0 | 0 |

**枝に届いた数（ゲート）：** 選ばれた行動は11種類すべて（情報を求める 290、水 1,640、食事 1,235、世話 851、手助け 21,256）。優先順位の Policy の同点処理 224、需要の倍率の文 158、区域の環境の記録 399、相手あり 33,936。Policy の版は6種類です。7日分は1種類で、需要の倍率と同点処理は通りません。

**ゲートが届かないもの：** 決定のときに依頼（`support_request_id`）か列のタスクを持つ人は、ゲートでも7日分でも0回でした（`observed_with_queue`、`observed_with_request`、`people_with_request`）。この枝は `tests/integration/test_behavior_row.py` が Python と比べます。

ゲートの行の網羅（マージ前の `6132710`。kernel ありだけ / kernel ありとなしの合計）：`policies/behavior.py` 44% / 89%、`experiments/phases/propose.py` 78% / 81%。Rust の行の網羅は測っていません。上の枝の件数で見ています。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 新しい C API はありません。
- **`first_tasks`（レーン G の注）：** この規則が読むのは `_observed_state` の記録だけです。列のタスクを持つ人が決定する場面は、順方向でも逆順でもゲートの影で0回で、タスクが2つある人の提案も0回です。挿入順で同じことはテストで見ていますが、逆順の回でタスクが2つある人の比較はできていません。
- **逆順のゲートでの社会の影：** 影の `social_propagation_v1` の比較が違いを報告します（96 プロセス。報告は1プロセス20件まで）。文字列の比較なので、逆順で変わるキーの順序も違いになると見ています（中身は確かめていません）。ハッシュは一致しています。このレーンの変更の前の基準でも、`--only interv` の逆順の回で同じ名前の違いが出ます。この影はもともと順方向用で、行動の影の違いは0件です。
