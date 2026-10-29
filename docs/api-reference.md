# CLI・データ契約・HTTP API

## 版管理されるデータ

正準input/output schemaはパッケージ内で実装し、テストで確認しています。主なrecordは次のとおりです。

| Record | 用途 |
|---|---|
| ScenarioSpec | 固定された人口、避難所、schedule、モデル設定 |
| ProfileSpec / 生成profile集合 | 合成人物の特性、ニーズ、provenance |
| WorldState | 可変simulation state、将来event queue、実行済みevent履歴、モデル状態 |
| Checkpoint | 復元、seek、replay、Branch作成のための版管理snapshot |
| EffectProgram | 対象、時刻、仮定を持つ型付き・上限付き介入計画 |
| Project / Run / Branch | ローカルworkspace、Scenario版、Run状態、独立した履歴 |

主要schemaは個別に版管理しています。現在の形式にはScenarioSpec v3、ProfileSpec v2、拡張状態に応じてWorldState v5またはv6、Checkpoint v1があります。契約変更時は現行validatorと移行テストを確認してください。版の異なるデータは互換とは限りません。

Scenario JSON、Run manifest、CheckpointはJSONを使います。eventとdecisionはJSONL、metricsはCSVを使う場合があります。Run manifestにはseed、モデル版、仮定、source ID、実行mode、hash、LLM利用量とfallback集計（llm_usage.outcomes、llm-integration.md参照）を記録します。有効なsubsystemにより、追加の版管理済みtrace成果物が作られます。

## CLI

完全なcommand一覧は sheltersim --help で確認できます。

- prepare: Scenarioを検証し、previewを作成します。--freezeはpreview hashと結び付いた承認記録を作ります。
- run: 凍結済みScenarioを実行し、Run成果物を書きます。--offlineはProvider呼び出しを無効にします。
- inspect: profile、予定・実行済みevent、Agent履歴、証拠に結び付いた原因を検索します。
- compare: 保存済みRun directoryを比較します。
- project: Project、Run、Checkpoint、Branch、履歴の永続操作を行います。
- model-catalog: 選択モデル、係数、単位、範囲、source分類を表示します。
- evaluate-models: 明示的に依頼されたoffline対応比較実験を実行します。
- web: ローカルブラウザーworkspaceとInspectorを起動します。
- usage: CLI/Demoの利用記録と、PC全体の累積上限の記録を表示します。--unfreeze ACCOUNT_ID --confirm ACCOUNT_ID --reason TEXT で、凍結された利用記録を解除します（llm-integration.md参照）。

実行例はREADME.mdとdevelopment.mdにあります。

## HTTP route群

serverは型付きrequest契約と、許可されたfield/queryだけを受け付ける検証を使います。実装はsrc/sheltersim/web/server.py、projects.py、project_services.py、workspace.pyにあります。

| Route | 操作 |
|---|---|
| GET /api/v1/projects | ローカルProject一覧と保存容量を返します |
| /api/v1/projects/{project} | Project、Scenario、Run、asset、証拠を参照します |
| /api/v1/projects/{project}/scenario-preview | Scenario案を作成・検証します |
| /api/v1/projects/{project}/scenario-approve | preview hashを承認し、保存ScenarioとRunを作成します |
| /api/v1/projects/{project}/runs/{run} | Runを再開・取消し、telemetryや比較を参照します |
| /api/v1/projects/{project}/runs/{run}/branches/{branch} | timeline、履歴、report、会話、LLM fallback集計（llm-status）を参照し、型付きBranch操作を送ります |
| /api/v1/runs | read-only Inspectorから保存Run成果物を検索します |
| /api/v1/demo | process内DemoとBranchを参照します |
| /api/v1/usage/runtime | CLI/Demoの上限付き利用記録を参照します |

Project APIはasset、確認済みfacility geometry、draft、evaluation、dataset、履歴index、Checkpoint、証拠検索も扱います。OpenAPI documentは別途生成していません。field・responseの正確な契約は現在のPydantic modelとtestを参照してください。

### Projectと介入の流れ

- POST /api/v1/projects でローカルProjectを作成し、GETで一覧と容量を確認します。Project、Run、Branch IDは表示名ではなく不透明な識別子です。
- POST /api/v1/projects/{project}/scenario-preview はdraftを検証し、期限付きpreview IDと正準approval hashを返します。previewだけではRunを作らず、simulationも開始しません。
- POST /api/v1/projects/{project}/scenario-household-corrections は、previewで保護者を確認できない未成年者へ同一世帯内の親・介護者を明示し、新しいpreviewを作ります。解消されるのは保護者のapproval blockerだけで、アクセシブル経路・容量などの他のblockerはpreviewに保存された記録から引き継ぎます。blocker記録のない古いdraftは修正せず、previewの再生成を求めます。
- POST /api/v1/projects/{project}/scenario-approve はpreview ID、一致するhash、approved: true、新しいRun IDを要求します。不変のScenario版と初期Run状態を保存します。
- Branchのintervention-previewは、指定時刻に自然言語または型付きprogramを受け取り、分離したstateで検証します。元Branchは変えません。自然言語の変換がruleの結果になった場合（`fallback_used: true`）、Opusを使わなかった理由を`limitations`に含めます。`SHELTERSIM_LLM_ERROR_POLICY=stop`では、ruleの結果の代わりに409（`code: intervention_compiler_unavailable`）で理由を返します（llm-integration.md参照）。intervention-applyはpreview ID、program hash、明示承認、child IDを要求し、元state hashを再検査して独立Branchを作ります。
- Branchの分岐（`POST …/branches/{branch}/fork`）とintervention-applyは、Webサーバーでは別プロセスで子Branchを作り、すぐに `{"status": "running", "branch_id": 子のID, "minute": 分岐時刻}` を返します（#48）。時刻の範囲、子IDの重複、program hashの不一致は、その場で拒否します。作っている間、Runの状態は `running` です。終わると元の状態に戻り、Runの参照（`GET …/runs/{run}`）の `last_fork` に結果を載せます。成功なら `{"status": "created", "branch_id", "minute", "state_hash"}`、失敗なら `{"status": "failed", "error", "code"}` です。`last_fork` は、そのサーバーのプロセスで最後に始めた分岐の結果だけで、保存しません。CLIとテスト（`ProjectAPI(worker="thread")`）では、従来どおり子Branchを作り終えてから `{"branch_id", "minute", "state_hash"}` を返します。
- POST /api/v1/projects/{project}/assets/{asset}/plan-preview は、アップロードした図面から候補の抽出（`extract`）・候補の確認（`review`）・トレース（`trace`、`reviewed_trace`）・GeoJSONの取り込み（`geojson`）・経路付きRevision（`geometry_revision`）のpreviewを作ります。POST /api/v1/projects/{project}/geometries/{geometry} は、previewのhashと`approved: true`で確定Geometryを保存します。scenario-previewの`geometry_id`で、確定Geometryを施設の図面に指定できます。図面の検証で拒否した入力は、409で理由を返します（`code`は`plan_<操作>_invalid`、指定した図面からScenarioを作れないときは`scenario_plan_invalid`。#204）。契約の検証で拒否した項目は、場所と内容だけを返し、入力値は返しません。JSONとして読めない図面のアップロードは`invalid_plan_json`です。
- timeline frameは指定tickの直前境界を表します。未計算・利用不可の履歴を推定値で返さず、要求を失敗させます。Branch比較は共通の計算済み時刻を使います。出力はsimulation上の記述で、外部データによる因果証拠ではありません。
- `GET /api/v1/projects/{project}/runs/{run}/replay-frames?branch_ids=A,B&start=M&end=N` は、1〜2個の異なるBranchについて、両端を含む最大8個の計算済み境界を連続読取りします。各queryは1回だけ指定します。`samples` はBranchが1個なら既存timeline frame、2個なら既存compare responseと同じ契約です。未計算範囲・fork前・重複Branch・読み取り中のhead変更は拒否します。記録済み応答だけで復元し、保存Branchを進めず、live Providerも呼びません。
- Branchへの質問は保存Run/Branchの証拠を読むだけで、simulation stateを変更しません。詳細はproject-persistence.md、adaptive-event-compiler.md、effect-program.mdを参照してください。

ブラウザーrouteは /demo、/admin、および独立したバックアップUIの /demo-v2 です。/demo-v2 は同じProject APIと承認契約を利用します（[対応表](ui-backup.md)）。root routeは /demo へredirectします。serverは既定で127.0.0.1にbindし、認証機能を持ちません。信頼できないnetworkへ公開しないでください。

POSTはsame-origin確認、JSON content type、request size上限、厳密なfield allowlistを使います。正確な上限とvalidatorはsource契約および回帰testで管理します。

## 状態変更とreplayの規則

Scenario作成とBranch状態変更には、確認済みhashに結び付く明示承認が必要です。serverは適用前にhash、元Branch、現在state、対象、上限、導入済みPolicyを再確認します。無効なplanは一部だけ適用されません。

過去timeline参照は記録済みstateとProvider応答を使います。不足履歴をlive modelで補いません。時刻Mはtick M直前の境界です。未計算時刻は厳密な状態として返しません。

## 正確な契約の参照先

- src/sheltersim/contracts: APIとstate model。
- src/sheltersim/experiments/scenario.py: ScenarioSpec。
- src/sheltersim/core/world.py、checkpoint.py: 永続simulation state。
- src/sheltersim/events/effects.py: EffectProgram schemaとinterpreter。
- src/sheltersim/web/projects.py、project_services.py: Project HTTP API。
- tests/: request、validation、replay、互換性を確認する実行可能契約。
