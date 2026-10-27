# 01. 心理状態モデル

## 目的と限界

急性状態 `A_i(t)` は、ニーズ・待ち時間・環境・身体/感情状態・情報不確実性に応じて分単位で変わる心理的負荷です。日単位潜在状態 `D_i(d)` は、日中の急性負荷と高負荷時間から更新する内部状態です。病名、PTSD、K6の個人得点、診断結果は出しません。

`AgentState.stress` が急性状態の唯一の保存先です。`acute_distress` は同じ値の互換propertyで、独立状態ではありません。日単位状態は該当Policyが選ばれた場合だけ `psychological_distress_v1` に保存されます。

## 理論と状態方程式

Kuppens et al. (2010) の感情の時間的慣性・個人差は平均回帰構造を考える理論的動機です。以下は避難所シミュレーション向けに複数の項を統合した提案式で、論文にそのまま掲載された式ではありません。

\[
\frac{dA_i}{dt}=-\kappa_i(A_i-A_{0i})+F_i(t)
\]

1分tick内で外力 `F_i` を一定と置き、解析解を計算します。

\[
A_i(t+\Delta t)=\operatorname{clip}_{[0,1]}\left[A_{0i}+(A_i(t)-A_{0i})e^{-\kappa_i\Delta t}+F_i(t)\frac{1-e^{-\kappa_i\Delta t}}{\kappa_i}\right].
\]

`κ_i` の単位は `1/minute`、`F_i` は正規化状態/分、`A_i` と `A_0i` は `[0,1]` です。`κ_i=0` では外力項は極限 `F_i Δt` です。初期条件はシナリオの `stress`、基準値は個人別設定、未設定時0です。上下境界では最終状態を `[0,1]` にclipします。

## 外力とモデル構成

| 入力 | 主な元 | 重複を避ける責務 |
|---|---|---|
| ニーズ・行列・支援待ち・環境 | `SimplePsychologyPolicy` | 各tickの率として一度入力 |
| 自然回復 | `MeanRevertingDistressPolicy` または統合版 | 心理policyだけが平均回帰を計算 |
| 睡眠負債・不確実性・安心・不満・感情・衛生 | `LifeStatePsychologyPolicy` | 身体/感情の既存状態からfeedbackを作る |
| 誤情報を信じること | 統合版の不確実性更新 | 有効な信念状態を不確実性の率に加える。真偽イベント自体は再適用しない |
| 他者との感情同調 | `ExpressivenessChannelOpennessPolicy` | 同じtick開始snapshotから社会差分を計算。自然回復は含めない |
| 社会的支援・訂正 | 型付き交流/訂正event | 一度のeventで安心・不確実性などの状態を変える |

### 選択可能なPolicy

- `mean-reverting-distress-v1`: 基本ニーズ等と厳密な平均回帰、日単位潜在状態を扱います。既存の身体状態Policyとは合成しません。
- `life-state-feedback-v1`: 既存の睡眠負債、安心感、不確実性、感情等を扱います。従来モデルの互換性を維持します。
- `mean-reverting-life-state-v1`: 新しい明示的な統合版です。平均回帰と身体/感情状態を一つの心理更新に合成し、日単位潜在状態も更新します。旧Policyの意味は書き換えません。

Webと初期Scenario生成器で作る新しいScenarioは`mean-reverting-life-state-v1`を選びます。`causal-stress-v1`（registry未指定時の既定）と`life-state-feedback-v1`は旧来モデルとして、保存済み入力の再生と比較対照のために残します。旧来モデルを選んだRunにはCLIと比較レポートが警告を表示します（[simulation-models.md](../simulation-models.md#旧来の心理モデル)）。

統合版では睡眠・休息の直接stress回復項を0にします。睡眠による睡眠負債の減少、睡眠で維持/回復する感情や安心状態は残します。これにより同じ睡眠を直接stress回復と睡眠負債feedbackの両方で加算しません。統合版のdiscontent・negative affect計算はtick開始の心理状態を読み、計算途中の値を再参照しない同期更新です。

誤情報に関する有効な圧力は、誤った主張がfalseと定義され、現在の信念強度が0.5を超えるとき `min(1, Σ 2(b_i^m-0.5))` で集約します。これは不確実性を押し上げる持続入力であり、rumor eventの一回効果を毎tick再生するものではありません。訂正を受容すると信念強度が訂正側へ移り、この圧力が消えます。係数は未校正で、直接の臨床効果を意味しません。

## 日単位潜在状態

\[
D_i(d+1)=\operatorname{clip}_{[0,1]}\{\mu_i+\phi_i(D_i(d)-\mu_i)+\theta_A(\bar A_i(d)-\mu_i)+\theta_H H_i(d)\}.
\]

`D` は日単位の潜在状態、`μ` は基準、`φ` は日次保持率、`Ā` は観測した分の急性状態平均、`H` は便宜的閾値を超えた分の割合です。1日を1440分とし、各日の終端で一度更新します。部分的な最終日は更新しません。既定値は `φ=0.90, θ_A=0.08, θ_H=0.02, threshold=0.70` で未校正です。

この `D` はK6推定値ではありません。日本語版K6は過去30日間の症状を参照する尺度であり、7日シミュレーションの日次状態と同一視しません。

## 計算例

仮に `A=0.60, A0=0.10, κ=0.01/min, F=0, Δt=1 min` なら `A'=0.10+0.50e^-0.01≈0.5950` です。説明用であり、観測データではありません。

統合版ではclip前の寄与を `自然回復 + Σ(各外力率 × (1-exp(-κΔt))/κ)` と分けます。たとえば外力率が `0.002/min`、`κ=0.02/min`、`Δt=1min` なら、その寄与は `0.002×(1-exp(-0.02))/0.02≈0.00198` です。

## Traceと実装対応

- `src/sheltersim/policies/psychology.py`: `MeanRevertingDistressPolicy`, `LifeStatePsychologyPolicy`, `IntegratedDistressLifeStatePolicy`, `mean_reverting_step`
- `src/sheltersim/domain/psychological_state.py`: 日内積分、日境界更新、checkpoint検証
- `src/sheltersim/domain/body_state.py`: 身体・不確実性・安心・感情状態と訂正eventの一回適用
- `src/sheltersim/policies/behavior.py`: 情報beliefをsnapshotへ投影し、Trace用の情報event出典を保持
- `src/sheltersim/experiments/runner.py`: 心理・社会更新、Trace、日内積分を同じtickで順序制御
- model version: `mean-reverting-distress-v1`, `life-state-feedback-v1`, `mean-reverting-life-state-v1`; 潜在状態schema: 1

心理Traceは各入力の寄与、解析的な平均回帰分、clip前候補、clip後の心理差分、社会差分、実際の適用差分を別々に記録します。平均回帰Policyでは各forcing寄与へ解析解の積分係数を掛けます。したがって寄与合計はclip前差分と一致し、clip差分と社会差分を合わせると実際の適用差分になります。Summary Traceは分単位の `psychology_contributions.jsonl` を維持し、追加の `psychology_contributions_hourly.jsonl` へAgent×時間帯の平均stress、閾値以上の分数、寄与成分別合計を出します。これはRun後の要約で、状態更新には使いません。

`distress_monitor_summary` は高負荷episode総数、完了episode数、終了時点で継続しているepisode数とAgent別経過時間、完了episodeだけの平均継続時間、最長継続時間、再上昇回数、終了時の高負荷Agent割合を分けて返します。完了episode平均は右打ち切りの継続中episodeを含みません。「全Agentの平均回復時間」とは呼びません。終了時点で閾値未満なら未完了episode数は増えず、終了時点で高負荷のAgentはongoing一覧に載ります。monitorはpost-social値を毎tick観測しますが、心理状態や乱数を変更しません。

## 内部検証

`tests/unit/policies/test_psychology.py` と `tests/integration/test_research_psychology.py` は無刺激回帰、回復率差、刻み幅整合性、持続負荷平衡、範囲と有限値、統合版の睡眠負債と平均回帰の共存、睡眠stress回復の二重計上防止、情報圧力から不確実性/急性状態への経路、Traceの寄与再構成を確認します。`distress_monitor_v1` 相当のオンライン集計は急性状態を変更せず、閾値以上の連続時間・回復回数を少数の数値指標としてcheckpoint可能なmetric stateに記録します。

内部テストは数式実装やプログラム上の一貫性を確認するもので、日本の避難所での外部妥当性は示しません。全係数は未校正です。0.70はソフトウェア指標で、臨床閾値ではありません。
