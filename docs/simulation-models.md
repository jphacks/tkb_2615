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
- 避難者は知っている施設だけを行き先に選びます。施設は到着時に覚えます。開始時に何を知っているかは下記の初期Scenario生成を参照してください。
- 移動モデルはScenarioで宣言したZone接続と所要時間を使います。表示geometryやfallback地図座標を実測移動として扱いません。
- 任意の社会・情報モデルは合成接触、belief、interaction、上限付き効果を記録します。実際の会話記録を表すものではありません。
- EffectProgramは対応済み介入を既存の型付き操作・予定eventに変換します。表示KPIへの直接代入や生成コードの実行はできません。

有効なモデル一覧、式、source ID、係数単位、Scenario固有値はCLIと保存Runのprovenanceに出ます。仮定の詳細はmodel-validation.md、出典台帳はfixtures/source-manifest.jsonを参照してください。

## 初期Scenario生成

WebのScenario previewが使う初期Scenario生成器は、利用者が入力しなかった項目を次の仮定で補います。どれもprovenanceと出典台帳に仮定として記録し、実測値や地域統計ではありません。

- **世帯構成（ASM-SYNTHETIC-HOUSEHOLD-TEMPLATES-V1）:** 世帯構成テンプレートが入力されない場合、単身・大人のみ・大人と子どもの合成テンプレートを使い、生成した未成年者を同じ世帯の大人1人と親子関係で結びます。年齢や同居だけから関係を推測するのではなく、テンプレートで明示した関係だけを作ります。利用者が世帯構成を入力した場合は既定テンプレートを使いません。子どもを選べない年齢条件では、子どもを含むテンプレートを外します。
- **標準施設の出入口（ASM-STANDARD-SHELTER-GEOMETRY-V1）:** 標準施設は部屋を通路沿いに横一列に並べるため、部屋の間口が出入口幅の上限になります。各部屋に1.8 mの間口を確保できるよう小さい部屋の面積配分を広げ、その分を避難スペースから差し引きます。面積を入力しない場合は、避難スペースが1人3.5 m²を満たすまで延床を広げます。利用者が入力した延床では面積を変えず、確保できない場合は従来どおりapproval blockerを示します。
- **施設の認知（ASM-INTAKE-FACILITY-ORIENTATION-V1）:** 生成したScenarioはagent_dynamics v2（need-led-autonomy-memory-v2）を使い、受付での案内を仮定して、トイレ・給水・配食・救護・受付・静養の共用施設を開始時から知っています。agent_dynamics設定のない既存Scenarioはv1のままで、開始ゾーンの施設だけを知っています。
- **心理モデル（ASM-MEAN-REVERTING-LIFE-STATE-V1）:** 生成したScenarioは研究版の心理モデル`mean-reverting-life-state-v1`（平均回帰と身体・感情状態の統合版、[01-psychology.md](mathematical-models/01-psychology.md)）を選び、平均回帰の係数（`natural_recovery_rate_per_minute`など）を既定値のまま明示的に書き込みます。係数は未校正の仮定です。保存済みのScenarioは選択したモデルのまま再生します。`policy_registry`で心理モデルを指定しないScenarioは、従来どおり`causal-stress-v1`を使います。

## 旧来の心理モデル

`causal-stress-v1`と`life-state-feedback-v1`は研究モデル導入前のモデルです。保存済みRun・Checkpointの再生と、研究モデルとの比較対照のために残しています。新しい分析で意図せず使わないよう、次の場所に使用中の心理モデルを表示します。

- `sheltersim prepare`と`sheltersim run`は、旧来モデルを選んだScenarioでstderrに1行の警告を出します。終了codeは0のままです。
- `sheltersim run`の出力JSONに`psychology_model`を含めます。保存Runのmanifestでは`policy_implementations.psychology`に記録されます。
- `sheltersim compare`のレポートに`psychology_models`（baselineとcandidate）を含め、Markdownの先頭に表示します。旧来モデルなら警告行を加えます。

旧来モデルの印はmodel-catalog.jsonではなく`src/sheltersim/contracts/policies.py`の`LEGACY_PSYCHOLOGY_MODELS`に置きます。catalogの項目はPolicy署名に含まれるため、変更すると保存済みCheckpointの照合が変わるからです。

## 再現性と評価

対応するruntimeでは、ScenarioSpec、seed、Policy/モデル版、記録済みProvider応答テープが同じなら、正準stateとevent履歴の再現を目指します。Checkpoint復元、no-op branch replay、Branch分離、trace非干渉は回帰テストで確認します。

対応seedを使った評価は、同一の合成入力下でモデル案の差を記述できます。統計的有意性、外部妥当性、現実世界の因果効果は証明しません。新しい評価にはseed集合、Scenario hash、設定、source ID、出力hashを保存してください。

## 制約

現地人口、避難所、天候、臨床、結果データを組み合わせた校正用資料は登録されていません。サービス時間、容量、移動時間、stress反応、社会伝播などの数値は、出典記録が実測値と明示しない限りソフトウェア上の仮定です。

出力を予測、臨床結果、実在住民の会話、検証済み人員配置の推奨、物理的アクセシビリティの保証として解釈しないでください。施設計画や地域データは人が確認してください。任意PDFの意味解釈や正確な群集衝突物理は保証しません。
