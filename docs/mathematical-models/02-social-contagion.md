# 02. 感情伝播・社会的相互作用

## 目的と理論

Bosse et al. の expressiveness–channel–openness 構造を参考に、家族・同一区画・近接区画で接触した人同士の急性心理状態の同調を表します。これは実際の表情、会話内容、集団心理やスタッフ介入の効果測定ではありません。文献のモデル式をそのまま移植したものではなく、避難所向けに有界・疎接触へ改変した提案演算子です。

## 数式

接触者 \(j\) から対象 \(i\) への重みを
\[
\gamma_{ji}=\varepsilon_j\alpha_{ji}\delta_i
\]
と置きます。実装では \(\varepsilon_j\) は `source_expressiveness_by_agent` (未指定時1)、\(\alpha\) は関係別接触強度、\(\delta_i\) は既存の `stress_sensitivity` を代用します。接触可能な相手は最大接触数以下にkeyed RNGで抽出し、家族・同一区画・隣接Zoneを別重みとします。

\[
\Delta A_i^{social}=\operatorname{clip}_{[-c_i,c_i]}\left[\operatorname{clip}_{[-c_p,c_p]}\left(\lambda\frac{\sum_j\gamma_{ji}(A_j-A_i)}{\max(1,\sum_j\gamma_{ji})}\right)\right].
\]

ここでは \(\lambda\) が1分あたりの同調率、\(c_p\) がpair上限、\(c_i\) が対象者1分あたり上限です。出力状態は[0,1]に制限されます。分母ゼロ、接触なしの場合は社会差分0です。\(A_j,A_i\) はtick開始snapshotから読みます。

## 計算例

例示として、重み1の落ち着いた相手 \(A_j=0.2\) に対し対象が \(A_i=0.8\)、\(\lambda=0.1/分\) で上限に当たらなければ、同調差分は \(-0.06\) です。これは仮の数値です。

## 実装・責務

`src/sheltersim/policies/social.py` の `ExpressivenessChannelOpennessPolicy` (version `expressiveness-channel-openness-v1`) が接触の重みと有界同調を計算します。自然回復はこのpolicyにありません。`mean-reverting-distress-v1` が選択された場合、旧社会policyの自然回復係数を0にして二重回復を防ぎます。社会的支援は積極的接触/身体状態への効果であり、同調項とは別経路です。

全員が高い状態でも、無刺激時の自然回復を心理policyが適用します。高ストレス同士の単純同調差は0です。更新順による非対称性を避けるため、結果をAgent順で逐次上書きしません。

## 内部検証と限界

研究モデルテストで高状態から低状態への上昇、低状態への下降、無接触時の差分0、Agent mapping順の不変、pairあたり上限を検査します。表出性・接触・感受性係数、接触抽出確率は未校正です。増幅bias項は実装していません。
