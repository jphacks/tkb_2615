# 小規模な合成条件による対応モデル比較

- code SHA: 41d89f3cbb20c459b6c85cc45f1c9e94f8406ac4
- Scenario: 合成人物8人、1日（1,440分）。
- 実行: offline、6 Run cell、96.253秒。有料API呼び出しなし。
- seed: 2026、2027、2028。両設定で対応付けて比較。
- 比較モデル: causal-stress / utility-softmax / sparse-family-zone / sparse-information と bounded-stress / priority-rule / DeGroot-contact / hazard-information。
- 結果: 全cell完了。モデル以外の初期input hashはvariant間で一致。
- 登録済みsynthetic stress上限testにpass。これはソフトウェア不変条件で、外部検証ではありません。照合可能な実避難所結果データはありません。

## 対応seedによる指標差

| 指標 | seedごとの候補 - baseline | 平均 | 近似95%区間 | 解釈 |
|---|---|---:|---:|---|
| stress_mean | -0.0420, 0.0648, 0.0257 | 0.0162 | [-0.1181, 0.1504] | 不確実 |
| toilet_queue_length | 0.0000, 0.0000, 0.0000 | 0.0000 | [0.0000, 0.0000] | 不確実 |
| staff_load_ratio | 0.0000, 0.0000, 0.0000 | 0.0000 | [0.0000, 0.0000] | 不確実 |

この区間は選択した合成seed3件での記述値です。モデル構造の不確実性を含まず、実世界の因果効果に対する信頼区間でもありません。小規模Scenarioでqueue/load差が0だったことは、需要が発生する条件で各subsystemを検証したことを意味しません。実測校正は主張しません。

再現可能なraw reportはpaired-models-small-synthetic-2026-10-23.jsonにあります。
