# 01. 心理状態

## 目的と限界

心理モデルは未充足ニーズ、行列待ち、支援待ち、環境負荷、休息による急性の心理的負荷を表します。病名、PTSD、K6の個人得点や診断を出すモデルではありません。

既存の `AgentState.stress` を急性状態 (A_i(t)in[0,1]) として維持します。`acute_distress` は同じ値を読む・設定する互換propertyであり、独立した状態ではありません。政策により、日単位以上の潜在状態 (D_i(d)in[0,1]) を `psychological_distress_v1` に保存します。

## 根拠と数式

Kuppens et al. (2010) の感情変化・個人差・感情慣性の議論は平均回帰を使う構造上の根拠です。実装式は複数の影響を避難所シミュレーションへ組み合わせた提案モデルで、論文にそのまま載る式ではありません。

急性状態は1分刻みで一定のtick内負荷 (F_i(t)) を仮定した厳密な平均回帰解です。

\[
A_i(t+\Delta t)=\operatorname{clip}_{[0,1]}\left[A_{0i}+(A_i(t)-A_{0i})e^{-\kappa_i\Delta t}+\frac{F_i(t)}{\kappa_i}(1-e^{-\kappa_i\Delta t})\right].
\]

\(\kappa_i\) が0なら最後の項は極限 \(F_i\Delta t\) です。分単位の \(\kappa_i\) と \(F_i\) は未校正仮定です。設定既定の自然回復率は `0.01 / minute`、基準状態は既定 `0` です。負荷は既存のNeed、待ち時間、支援待ち、環境、休息・身体状態feedbackの寄与を合計します。ランダム雑音 \(\sigma\epsilon\) は標準モデルには実装せず、seedだけで再現できる決定的演算です。

日単位の状態は、日内の最終急性状態の平均と高負荷分数で更新します。

\[
D_i(d+1)=\operatorname{clip}_{[0,1]}\{\mu_i+\phi_i(D_i(d)-\mu_i)+\theta_A(\bar A_i(d)-\mu_i)+\theta_H H_i(d)\}.
\]

\(\bar A\) は観測された分の平均、\(H\) は高負荷閾値を超えた観測分の割合です。コード既定は \(\phi=0.90,\theta_A=0.08,\theta_H=0.02\)、閾値0.70で、すべて未校正仮定です。1日は1440分として日境界で一度更新します。部分的な最終日は更新されません。

## 計算例

仮の値 \(A=0.60,A_0=0.10,\kappa=0.01/分,F=0,\Delta t=1分\) なら、
\(A'=0.10+0.50e^{-0.01}\approx0.5950\) です。これは説明用の数値で実測値ではありません。

## 実装・連携

- `src/sheltersim/policies/psychology.py`: `MeanRevertingDistressPolicy`, `mean_reverting_step`, `PsychologyParameters`
- `src/sheltersim/domain/psychological_state.py`: 日内積分、日境界更新、checkpoint検証
- `src/sheltersim/domain/models.py`: `AgentState.stress` と `acute_distress` の単一状態alias
- `src/sheltersim/policies/behavior.py`: 行動効用で急性・長期状態の最大値を負荷入力に使う
- version: `mean-reverting-distress-v1`; 状態schema: 1

1分tickでは心理policyが基礎更新を計算し、社会policyがあれば同じ更新前スナップショットから差分を適用します。その最終値を一度だけ日積分へ加算します。支持の声掛けは身体状態/社会イベントを通じて影響し、自然回復率と同じ項には加えません。睡眠や休息による既存recovery寄与は別の明示的入力です。

## 内部検証と限界

`tests/test_research_models.py` で無刺激の減衰、刻み幅整合性、持続負荷の平衡、個人回復係数、非有限値拒否、日単位蓄積とcheckpoint復元を検査します。時間刻み整合性は定係数の解析解に対する数値検査です。

中長期状態は短い避難期間の臨床指標ではありません。日本語版K6は過去30日を参照する尺度であり、この7日以内の (D_i(d)) をK6得点や推定値と呼びません。基準、回復、日積分係数の外部校正は未実施です。
