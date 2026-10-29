# 自然言語によるevent解釈

## 目的と正直な限界

利用者が「Day NにXが起きたら」といった未知の日本語質問を入力し、Opusが固定event名へ分類するだけでなく、導入済みの因果機構を組み合わせます。既定の名前がないeventでも、実際に支えられる機構があれば新しいRunを作れることを目指します。

自然言語を広く解釈できることは、物理・医療・心理simulationが普遍的に正確であることを意味しません。proposalはnative、composed、approximate、clarification_required、not_simulatableのいずれかで扱います。計算していない効果を計算済みと表示しません。approximateでは仮定と編集可能な規模を明示し、結果はモデル依存の仮説として表示します。

## 処理の流れ

従来の単純な流れは「文章 -> facility_outage等のenum -> handlerまたは拒否」でした。現在の設計は次のとおりです。

文章 -> Opus Event Compiler -> 意味機構の組合せ／型付きEffectProgram -> schema検証 -> 意味・実行可能性・因果経路の検証 -> 仮定とpreviewの説明 -> 明示承認 -> Checkpointから独立Branchを作成し決定的interpreterで適用 -> Agent/施設/空間/社会Policyを実行 -> A/B比較

再現可能な実行には有限のatomic primitive集合が必要ですが、eventの物語や組合せは拡張できます。言い回しごとの専用handlerを増やすのではなく、小さなPolicyで機構を追加します。

## primitiveとEffectProgram

各primitiveはKPIを直接書き換えず、実装済み機構を操作します。対象例は施設、資源、環境、Agent/group、移動、運営、社会・情報です。現在実行可能なoperator、必要Policy、範囲、期限、未対応事項は[EffectProgram runtime](effect-program.md)が正本です。下記はoperator追加時に確認する契約です。

- JSON/Pydantic型、対象selector/ID、数値範囲、単位、開始・終了、可逆性、active submodel、競合順、source/provenance、不変条件testを定義する。
- 未導入subsystem用primitiveは未対応です。自由文labelだけでは機構は実装されません。
- Event Compilerはcode-owned registryを参照します。ここに挙げた将来候補がすべて導入済みという意味ではありません。

EffectProgramは任意Python/JavaScript、shell、dynamic import、LLM提供関数を実行しません。主なfieldはschema_version、program_id、narrative、source_text、simulation_start_minute、effects、assumptions、support_mode、compiler_provenanceです。effectにはeffect_id、op、target_selector、start_minute、任意のend_minute、parameters、guard、source_id、causation_id、reversibleを含めます。

guardはfield allowlistとeq/ne/gt/gte/lt/lte/and/orだけを使う上限付き型付きASTです。effect数、guard深さ、期間、数値範囲、scope、target数、予定event数、tickあたり作業量に上限を設け、無制限chainを拒否します。モデル状態への効果は既存event schedulerと承認済みdomain handlerを通します。Agentの反応は行動・心理・職員規則と追跡可能な入力から生じる必要があります。

時間変化modifierは安定ID、開始・終了、stacking規則、決定的な期限復元を持ち、Checkpointにはmodifier stackと未実行期限eventを保存します。stress=90のような表示KPIへの直接代入は禁止です。noise等はsource IDのある仮定として導入済みPolicyへ入力できます。複数effectを含むplanは全体を一括事前検証します。無効planの一部だけを元Run/候補Branchへ適用してはいけません。

## Event Compilerの段階

1. **状況把握:** scenario manifest、Zone/施設/資源/staff ID、timeline、利用可能primitive schema、導入Policy、固定済み過去stateの要約、Scenario上限、選択minuteを受け取る。
2. **解釈:** 時刻、範囲、対象、開始・終了、主な支障、想定経路、不明点を特定する。schema通過だけでは意味検証にならない。
3. **構成:** EffectProgram、確度/support状態、日本語の機構説明、仮定、欠けるPolicy、予想計算時間とAPI費用を返す。未計算の結果を事実のように示さない。
4. **検証:** target、導入Policy、KPI直接変更の有無、state/capacity/resource不変条件、単位、event schedule、guard複雑度を検証する。不正なら制約付き修正を一度試すか、限界と調整案を示す。
5. **preview/承認:** 対応action、仮定、対応範囲、限界、approximate用parameterを表示し、利用者の確認を求める。
6. **Branch作成:** event前のCheckpointから分岐し、compiler/model、prompt hash、承認plan、利用者修正、event記録、modifier stateを保存する。
7. **simulation:** 決定的tick engineが組合せeffectと後段Policyを処理する。previewを観測済みmodel出力と偽らない。
8. **管理画面:** compiled AST、検証・修正記録、対象ID、係数仮定、schedule、decision、Run hash、provenance、Provider利用を参照可能にする。

## 現在のcompiler動作

sheltersim.llm.natural_language.compile_natural_language()がPREPARE段階のentry pointです。ParseRequest、注入されたLLMRouter（または環境設定から作るrouter）、code-owned PrimitiveRegistryを受け取ります。scenario_summary_from_world()は選択Branchの匿名snapshotを作り、現在minute/horizon、Zone・施設ID/容量/占有数、Agent数、勤務可能staff、未着shipment、資源ledger、WorldStateに実際に存在するPolicyを返します。

compilerはsnapshotとprimitiveのtarget種別・必要Policyを照合します。実行可能operatorとparameter schemaだけをOpus prompt/response schemaへ入れます。現在のregistryはeffect-program.mdに記載されています。set_ambient_noiseとmodify_demand_rateはzone_environment_v1がopt-inされる場合だけ候補です。既存の休息回復とaction選択確率に限った経路です。route cost、群集移動、照明、温度、情報拡散、施設固有需要は未対応です。必要機構がない場合はnot_simulatableとし、別の導入済みprimitiveで根拠ある限定近似ができる場合に限りapproximateにできます。

Provider responseは動的JSON schemaを通し、operator別parameter、target/Policy適合性、数量、施設占有、shipment時刻、期限、全体上限を検証します。意味検証に失敗した場合、同じrouterで制約付き修正を一度試すことがあります。結果はcompiled、clarification_required、not_simulatableです。実行可能DraftPlanには必ず明示承認が必要です。previewは提案した機構の説明であり、観測済み結果ではありません。

routerがoffline、budget超過、rate limit、timeout、response拒否となった場合、認識済みで文脈に合う形式に限り決定的offline parserへfallbackします。それ以外は理由付きnot_simulatableを返します。ただし`SHELTERSIM_LLM_ERROR_POLICY=stop`では、offline mode以外でOpusの案を使えなかった場合にparserへfallbackせず、理由を返して失敗します（Project APIは409 `intervention_compiler_unavailable`、legacy demoは409）。fallback状態、model、prompt/input hash、token、概算費用、cache状態、error種別を表示します。入力は信頼しないデータとし、Provider送信前に連絡先をredactします。compilerはWorldStateを受け取らず変更もしません。適用時はBranchRuntime.apply_effect_program()が意味検証をやり直してから一括scheduleします。

CIと標準pytestはfake Providerまたはoffline modeだけを使います。21件の日本語promptによるoffline評価はevent-coverage-report.mdに記載しています。導入operatorのcoverageとruntime validationを測るもので、live Providerの精度測定ではありません。

## 対応状態

- **native:** 必要な機構がすべて導入済みで、既存の因果経路を使う。
- **composed:** 2つ以上の導入primitiveを組み合わせ、専用source codeを増やさずにeventを表す。
- **approximate:** staff taskや環境proxyなど、明示的に限定した代替表現を使う。近似ラベル、係数の不確実性、表現しない内容を示す。
- **clarification_required:** 対象・時刻・範囲が曖昧な場合に選択を求める。重大な仮定を隠して選ばない。

追加先のZoneを書かない指示は、操作ごとにScenarioのデータで候補を絞ります（#35）。トイレ追加は既存トイレのあるZone、避難者の到着は受付（`intake`）のあるZone、スタッフ追加はスタッフの配置されているZone、避難者の帰宅は避難者のいるZoneです。該当するZoneがないScenarioでは、すべてのZoneを候補にします。候補が1つなら自動で選び、その理由を仮定（assumption）としてPreviewに示します。反映には従来どおり承認が要ります。Zoneを名前・ID・`zone_id:`で明示した指示は、そのZoneを使います。「トイレゾーン」「トイレエリア」のように、一般名の名前に「ゾーン」「エリア」を付けて書いたものも、その名前のZoneを指します。Opusが追加先のZoneを聞き返した場合でも、この規則で1つに決まり、規則が文全体を解釈できるなら、規則の案で補います（`fallback_used: true`、理由をlimitationsに記載）。Opusの聞き返しの選択肢は、1種類の対象（Zoneだけ、設備だけ、など）のIDに限ります。Project画面とDemo画面（/demo-v2）は、選択肢をボタンで示し、押すと指示文に`zone_id:`の行を足して再Previewします。Project画面では、Zone以外のIDは`対象ID:`の行として足します。compilerに渡すScenarioの要約には、Zoneごとのスタッフ数と避難者数が入ります。
- **not_simulatable:** 根拠ある経路がない、必須機構がない、または疫学等の高リスク主張を含む要求を拒否する。必要なsubsystemを説明し、利用者承認の下で比較用proxyを提案する場合がある。

将来、LLMにPolicy/plugin案を作らせる場合は、隔離sandbox、code review、test、明示登録、版管理、restartが必要です。live serverで未信頼コードを即時実行してはいけません。

## 評価契約

保持するoffline評価には、施設・資源・環境・群集・行動・社会・未知の種類から21件の日本語promptがあります。測定値はevent-coverage-report.mdを参照してください。test文字列に合わせてparser規則を個別に過学習させません。評価では以下を確認します。

1. 対応結果のcoverage、時刻・範囲の正確さ、限界表示。
2. 物理機構や心理予測の捏造がないこと。
3. 受理した全programがschemaとdomainの事前検証を通ること。
4. parse/previewで元WorldStateが変わらず、承認適用が一括かつidempotentであること。
5. no-op branch/replay決定性と、Checkpoint復元後もmodifier期限が一致すること。
6. 適用effectにeffect_idとevent/LLM compiler provenanceがあり、Inspectorから確認できること。
7. model利用不可時に、計算する内容・しない内容を説明し、架空の数値結果を出さないこと。

目標は限界を表示した上で汎用primitiveを組み合わせることです。万能な科学的妥当性や、phraseごとの固定handlerを増やすことではありません。
