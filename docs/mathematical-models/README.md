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

研究モデルは `model_configs.policy_registry` から選択します。デフォルトの心理・社会・情報モデルは維持され、比較対象として使えます。係数は `fixtures/source-manifest.json` と `src/sheltersim/contracts/model-catalog.json` に単位、範囲、出典ID、未校正の説明を記録しています。

検証はコード上の性質、再現性、状態不変条件、合成シナリオへの適用に限ります。実測データによる校正と外部検証は行っていません。高ストレス指標は臨床診断や公衆衛生の閾値ではありません。
