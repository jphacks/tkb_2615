# ShelterSim 数理モデル

本書は、現行コードで動作する式、未校正の設定値、既存の運営モデルを説明します。文献はモデル構造を考える根拠として扱い、避難所固有の係数を推定した根拠とは区別します。添付の「ShelterSim 科学的妥当性強化のための深度調査報告書」はこの作業環境で確認できなかったため、リポジトリ資料と参照先が確認可能な文献に基づいています。

## 文書

1. [心理状態](01-psychology.md)
2. [感情伝播・社会的相互作用](02-social-contagion.md)
3. [情報・噂・訂正](03-information-rumor.md)
4. [行動選択](04-behavior-choice.md)
5. [設備・待ち行列・資源](05-facilities-resources.md)
6. [移動](06-movement.md)
7. [モデル統合・ODD](07-model-integration.md)
8. [パラメータと根拠](08-parameters-and-evidence.md)
9. [内部検証](09-internal-validation.md)
10. [参考文献](references.md)

## 適用範囲

研究モデルは `model_configs.policy_registry` から選択します。心理状態には `mean-reverting-distress-v1`、従来の`life-state-feedback-v1`、両方を合成する明示的な `mean-reverting-life-state-v1` があります。社会・情報モデルも従来Policyを残して研究版と比較できます。係数は `fixtures/source-manifest.json` と `src/sheltersim/contracts/model-catalog.json` に単位、範囲、出典ID、未校正の説明を記録しています。

統合版では既存body/affect状態を再利用し、false情報への現在の信念を不確実性・心理更新へ接続します。Traceはclip前の心理寄与、clip後の値、社会差分、実適用差分を分け、情報効果はrumor originとbelief更新eventへ遡れます。Traceを保存しない7日間run用に、高負荷の連続時間と回復回数を定数個の指標でオンライン集計します。

検証はコード上の性質、再現性、状態不変条件、合成シナリオへの適用に限ります。実測データによる校正と外部検証は行っていません。高ストレス指標は臨床診断や公衆衛生の閾値ではありません。

先行する統合版比較・感度分析は [`research-validation-a02.json`](research-validation-a02.json)、施設認知修正前の100人・7日間再現性runは [`research-validation-7day-a02.json`](research-validation-7day-a02.json) に保存しています。施設認知修正後の短時間比較、感度分析、最終100人・7日間run、および性能測定の更新結果は [内部検証](09-internal-validation.md) に記録します。A02の7日runは修正前の歴史的結果として保持し、修正後の値と混ぜていません。
