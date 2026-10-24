# シミュレーションモデル

## 時刻と状態

engineは整数分の時計を進めます。Scenario stateには合成人物、世帯、Zone、施設、職員、資源、schedule、モデル設定を含みます。engineは期限到来eventと完了処理を実行し、モデル状態を更新し、適格actionを選び、資源・施設制約を検証して、結果のstateとeventを記録します。

WorldStateはRun中の可変stateです。Checkpointは乱数状態、未処理作業、ID採番、metrics、対応runtime記録を含む版管理済みsnapshotです。BranchRuntimeは独立したCheckpointを復元し、親を変更せずに別案を比較します。

## モデル群

- profileと初期ニーズは版管理されたtemplateから合成生成します。実在避難所の名簿から抽出しません。
- 行動Policyは適格な型付きactionからutilityまたはpriority規則で選択します。
- ニーズと心理Policyは、設定された増加率、未充足ニーズ、queue、支援、休息、環境に応じて、範囲内でニーズ・疲労・stressを更新します。
- 施設・資源モデルはサービス能力、queue、stock、予約、消費、復旧eventを表します。
- 職員モデルは技能、勤務可否、task割当、移動、サービス、shift、上限付き支援requestを表します。
- 移動モデルはScenarioで宣言したZone接続と所要時間を使います。表示geometryやfallback地図座標を実測移動として扱いません。
- 任意の社会・情報モデルは合成接触、belief、interaction、上限付き効果を記録します。実際の会話記録を表すものではありません。
- EffectProgramは対応済み介入を既存の型付き操作・予定eventに変換します。表示KPIへの直接代入や生成コードの実行はできません。

有効なモデル一覧、式、source ID、係数単位、Scenario固有値はCLIと保存Runのprovenanceに出ます。仮定の詳細はmodel-validation.md、出典台帳はfixtures/source-manifest.jsonを参照してください。

## 再現性と評価

対応するruntimeでは、ScenarioSpec、seed、Policy/モデル版、記録済みProvider応答テープが同じなら、正準stateとevent履歴の再現を目指します。Checkpoint復元、no-op branch replay、Branch分離、trace非干渉は回帰テストで確認します。

対応seedを使った評価は、同一の合成入力下でモデル案の差を記述できます。統計的有意性、外部妥当性、現実世界の因果効果は証明しません。新しい評価にはseed集合、Scenario hash、設定、source ID、出力hashを保存してください。

## 制約

現地人口、避難所、天候、臨床、結果データを組み合わせた校正用資料は登録されていません。サービス時間、容量、移動時間、stress反応、社会伝播などの数値は、出典記録が実測値と明示しない限りソフトウェア上の仮定です。

出力を予測、臨床結果、実在住民の会話、検証済み人員配置の推奨、物理的アクセシビリティの保証として解釈しないでください。施設計画や地域データは人が確認してください。任意PDFの意味解釈や正確な群集衝突物理は保証しません。
