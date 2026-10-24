# 08. パラメータと科学的根拠

## 管理場所

- `src/sheltersim/contracts/model-catalog.json`: model ID/version、理論式、参照、係数ID、値、単位、上下限、source ID、適用注意
- `fixtures/source-manifest.json`: source type、理論根拠、モデル版、校正status
- `ScenarioSpec.model_configs.policy_registry`: 実際に選んだ演算子
- Run manifest / trace: 適用したモデル、設定、seed、入力・出力hash

文献に式の形があることと、係数が文献から推定されたことは別です。主要な追加係数は `source_type=assumption`, `calibration_status=uncalibrated` として扱います。

| ID / version | 設定例 | 単位 | 理論・公的根拠 | 校正 |
|---|---|---|---|---|
| `PSY-RECOVERY-KAPPA` / `mean-reverting-distress-v1` | 0.01 | 1/minute | emotional inertia/mean reversionの構造 | 未校正 |
| `ASM-MEAN-REVERTING-LIFE-STATE-V1` / `mean-reverting-life-state-v1` | recovery 0.01、body feedbackは既存config値 | 1/minute + 各body unit | 平均回帰と既存life-state式を組み合わせたShelterSim統合 | 未校正 |
| `natural_recovery_rate_by_agent` | 空mapなら共有率、個別指定時に優先 | 1/minute/Agent | 個人差を表す構造 | 未校正 |
| `PSY-DAILY-RETENTION` | 0.90 | 日ごとの保持率 | AR(1)型潜在状態構造 | 未校正 |
| `SOCIAL-AFFECT-GAMMA` / `expressiveness-channel-openness-v1` | existing spread rate | 1/minute | Bosse expressiveness/channel/openness構造 | 未校正 |
| `INFO-CORRECTION-ACCEPTANCE` / `evidence-aware-rumor-v1` | 0.70 | 確率/曝露 | 噂訂正過程の構造、source trust | 未校正 |
| `INFO-RUMOR-BELIEF` / `evidence-aware-rumor-v1` | 0.50 | 確率/曝露 | 噂伝播・信頼構造 | 未校正 |
| `INFO-FORGETTING-RATE` | 0 | 1/minute | exponential hazard形式 | 未校正 |
| false-belief uncertainty input / integrated policy | `min(1, Σ 2(b-0.5))`; `uncertainty_drift_per_minute`を適用 | 正規化belief pressure、uncertainty/分 | belief updateと不確実性をつなぐ統合上の仮定 | 未校正 |
| action utility / temperature | existing model coefficients | dimensionless | Conditional Logit | 未校正 |
| toilet target ratios | 50 / 20 people per unit | people/unit | 内閣府計画ガイドライン | 個人行動モデルではない |
| toilet service duration | 4–8 | minute | 合成ソフトウェア設定 | 未校正 |

設定可能値と許容範囲はmodel catalogまたはtyped configを参照してください。上表の数値はソフトウェア既定/基準値であり、現地の校正結果ではありません。

## 根拠のグレード

Aは対象・単位・期間が一致する実測、Bは文献/行政資料の構造的文脈、Cは実装仮定です。今回の心理、社会、情報、選択、サービス係数はCです。内閣府のトイレ数目安は計画指標として参照し、個人係数と混合しません。外部データ検証は未実施です。
