# 04. 行動選択

## 目的と理論

McFaddenのConditional Logit / Random Utility構造を参考に、既存の適格Action候補から行動を選びます。候補を生成する施設可用性、アクティビティ、支援request、閾値条件は確率選択の外側にあり、利用できないActionは候補集合に含めません。

## 数式

コードでは各候補の効用 \(u_{ia}\)、priority \(q_{ia}\)、Zone demand multiplier \(m_{ia}\) を計算し、temperature \(T>0\) に対して
\[
P_i(a)=\frac{m_{ia}\exp[(u_{ia}+0.5q_{ia}-M_i)/T]}{\sum_{b\in C_i}m_{ib}\exp[(u_{ib}+0.5q_{ib}-M_i)/T]},\quad M_i=\max_b(u_{ib}+0.5q_{ib}).
\]
`M_i` subtraction is a stable log-sum-exp shift and cancels from the ratio. `m` is a positive probability multiplier; zero excludes the option. Need, acute/chronic distress, expected wait/utility, information query, family state, staff and facility availability enter the candidate utility or eligibility where implemented. Utility is a dimensionless relative score; priority correction is explicit.

## 計算例

仮に2候補のshifted utilityが0と1、priorityは同じ、multiplier=1、T=1なら確率は約0.269と0.731です。未校正の効用差を使った例で、行動率の実測値ではありません。

## 実装・連携

`src/sheltersim/policies/behavior.py` の `UtilityBehaviorPolicy` (`utility-softmax-v1`) は候補集合生成、効用、安定softmax、keyed draw、ActionProposal traceを持ちます。既存 `priority-rule-v1` は比較用に残ります。研究比較では `temperature=1` とし、温度と効用係数の共通倍率による識別問題を避けられますが、既存互換のdefault 0.2は変更していません。

## 内部検証と限界

既存回帰は確率総和、候補適格性、同seed再現、極端値の数値安定性を検査します。値は未校正です。実行開始タイミングは既存decision schedulerが担い、今回は独立した行動開始hazardを導入していません。
