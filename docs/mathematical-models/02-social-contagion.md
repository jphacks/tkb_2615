# 02. 感情伝播・社会的相互作用

## 目的と理論

Bosse et al. の expressiveness–channel–openness 構造を参考に、家族・同一区画・近接区画で接触した人同士の急性心理状態の同調を表します。これは実際の表情、会話内容、集団心理やスタッフ介入の効果測定ではありません。文献のモデル式をそのまま移植したものではなく、避難所向けに有界・疎接触へ改変した提案演算子です。

## 数式

接触者 `j` から対象 `i` への重みを

\[
w_{ji}=\varepsilon_j\alpha_{ji}\delta_i,\qquad W_i=\sum_{j\in C_i}w_{ji},\qquad G_i=\min(1,\lambda W_i)
\]

と置きます。実装では `ε_j` は `source_expressiveness_by_agent`（未指定時1）、`α_ji` は関係別接触強度、`δ_i` は既存の `stress_sensitivity` を代用します。接触可能な相手は最大接触数以下にkeyed RNGで抽出し、家族・同一区画・隣接Zoneを別の接触チャネルとして扱います。

\[
r_{ji}^{raw}=G_i\frac{w_{ji}}{W_i}(A_j^{snapshot}-A_i^{snapshot}),\qquad
r_{ji}^{pair}=\operatorname{clip}_{[-c_p,c_p]}(r_{ji}^{raw}),
\]
\[
r_i^{agg}=\operatorname{clip}_{[-c_i,c_i]}\left(\sum_{j\in C_i}r_{ji}^{pair}\right),\qquad
\Delta A_i^{social}=\operatorname{clip}_{[-A_i^{psych},1-A_i^{psych}]}(r_i^{agg}).
\]

`λ` は1分あたりの同調率、`c_p` はpair上限、`c_i` は受信者1分あたりの合計上限です。実装tickは1分です。`W_i=0`、または適格な接触がなければ社会差分は0です。近隣平均との差を重み総和で正規化し、強度が大きい場合は `G_i` が1で飽和します。個々の接触時間は伝播量に重ねて掛けません。`minimum_contact_minutes` は接触を有効にする条件であり、cooldownは反復更新を抑えます。

`A_j^snapshot` と `A_i^snapshot` は同一tick開始時の状態です。差分を計算した後、心理policyで更新した値 `A_i^psych` に一度加えます。pair cap、受信者cap、最終状態の境界capを順に適用するため、Agentの処理順に結果が左右されません。

## 計算例

仮に適格な相手が1人、重み `w=1`、相手 `A_j=0.2`、対象 `A_i=0.8`、`λ=0.1/分` で上限に達しないなら `G=0.1`、pair差分は `0.1(0.2-0.8)=-0.06` です。数値は説明用の仮定です。

## 実装・責務

`src/sheltersim/policies/social.py` の `ExpressivenessChannelOpennessPolicy`（version `expressiveness-channel-openness-v1`）が接触の重みと有界同調を計算します。Traceには各接触の `w_ji`、`G_i`、pair cap前後の差分、aggregate scale、実際に適用された差分を記録します。自然回復はこのpolicyにありません。平均回帰policyと組み合わせた場合、旧社会policyの自然回復係数は0にして二重回復を防ぎます。社会的支援は積極的接触・身体状態への作用であり、単純な感情同調とは別経路です。

全員の状態が同じなら同調項は0です。その場合も心理policyの自然回復は独立して働きます。継続中のニーズや環境負荷は別の心理入力として残るため、高い状態が維持される場合があります。

## 内部検証と限界

`tests/unit/policies/test_social.py` は高い状態から低い状態への上昇、逆方向の鎮静、無接触時の差分0、Agent mapping順の不変、上限、Traceと正規化式の一致を検査します。表出性・接触・感受性係数や接触抽出確率は未校正です。増幅bias項は実装していません。
