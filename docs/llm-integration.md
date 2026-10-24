# LLM連携と安全性

## Providerとの境界

Claude連携は任意です。llm extraを導入し、ANTHROPIC_API_KEYをGit外に設定します。SHELTERSIM_LLM_MODEを省略するとauto modeになり、keyが設定されている場合、対応workflowがProviderを呼ぶことがあります。すべてローカルで行う場合はSHELTERSIM_LLM_MODE=offline、またはcommandの--offlineを指定します。同梱.env.exampleはofflineを選びます。

Anthropicへ接続するのはsrc/sheltersim/llm/anthropic_adapter.pyだけです。simulation coreにProvider依存はありません。requestはrouter、型付きtask schema、呼出し・token・同時実行・費用上限、usage ledger、cache、response tapeを通ります。

## 対応する段階

- 準備段階では合成Scenarioやprofile案を作れます。結果はpreviewであり、利用者が確認して同じhashを明示承認します。
- event解釈では導入済みprimitiveからEffectProgramを作成できます。previewまたは適用前に、現在Branch、対象ID、導入Policy、範囲、時系列に照らして検証します。
- runtimeでは設定済みのtriggerで少数の型付き提案を依頼する場合があります。毎tick・全員についてProviderを呼ぶ設計ではありません。
- reviewは保存Runの証拠を要約します。結果質問は記録された事実を根拠にするread-only操作で、ScenarioやBranchを変更できません。

利用可能な介入operatorはeffect-program.mdを参照してください。未導入機構が必要な要求は拒否するか、制限を示した近似として扱います。model responseはoperatorを追加したり、KPIを直接変更したり、Python/JavaScriptを実行したり、承認を迂回したりできません。

## 再現性とエラー

構造化responseをparseし、検証します。Provider error、不正形式、rate limit、offline mode、budget超過では、定義済みfallbackを使うか、利用不可を返します。選択された動作と利用量を記録します。

### fallbackの可視化

fallbackしたtaskはruleの既定値で進むため、その分の結果はofflineと同じになります。API modeのRunが実際にはClaudeの応答を使っていない状態を見落とさないよう、呼出し記録からoutcome集計を作ります。集計はsrc/sheltersim/llm/outcomes.pyの1関数に統一し、model出力本文は読みません。

| status | 意味 |
|---|---|
| not_requested | LLM taskを発行していません |
| offline_rules | offline modeで、全taskが設計どおりruleを使いました |
| all_accepted | 依頼した全taskが検証済みのProvider応答またはtapeを使いました |
| degraded | 一部のtaskがrule fallbackになりました |
| all_fallback | Provider応答を使えたtaskがありません |

fallbackの理由はoutcome別（max_api_calls、shared_allowance_unavailable、invalid_outputなど）、error_kind別、prompt版別に数えます。集計の表示先は次のとおりです。

- CLI Run manifestのllm_usage.outcomes。llm_usage.failuresはoffline以外のfallback件数です。以前のmanifestは呼出し上限や利用枠拒否を数えていなかったため、outcomes項目がない古いRunのfailuresは過少な値の可能性があります。
- sheltersim runの出力JSONのllm_status。degradedまたはall_fallbackのときはstderrに1行の警告を出します。終了codeは0のままです。
- Project Branchは GET /api/v1/projects/{project}/runs/{run}/branches/{branch}/llm-status。head Checkpointに保存済みのrouter記録から読み取り時に集計します。read-onlyで、Checkpoint形式は変えません。forkしたchild Branchの値には、fork前に親から引き継いだ記録も含みます。

受理したresponse tapeはstate/input hash、model、prompt、schema版に結び付きます。対応runtime状態はCheckpointに保存できます。過去seekは記録結果を使い、暗黙にProviderを呼びません。Branch stateが変わった場合、異なるinputにresponseを再利用しません。

## プライバシーと安全性

- key、.env、実在避難者の記録、機微な生prompt、未確認の施設図面をcommitしないでください。
- Scenario文章、取込データ、Provider出力を信頼しない入力として扱います。
- 匿名IDを使い、対応taskに必要な項目だけを送信します。
- 該当validatorがある場合、連絡先のような情報をredactまたは拒否します。browser stateやProject recordへProvider keyを保存しません。
- 結果Q&Aはread-onlyとし、保存証拠に結び付けます。
- 標準pytest、lint、CIからlive Providerを呼びません。

API keyはprocess環境変数から読みます。.env.exampleは設定名の参考であり、dotenv loaderではありません。Provider requestはrouterごとの予算と、ローカルProject・process間で共有するコード定義の累積50ドルusage ledgerに対して予約します。この上限はソフトウェアの制限であり、利用承認ではありません。API callにはプロジェクトオーナーの都度の承認が必要です。別taskの予算や承認は引き継がれません。公開前に累積上限の実装を再検討してください。

## 利用記録とlive連携test

CLI usage commandと/api/v1/usage/runtimeは上限付きusage receiptを表示します。Project RunにもRun単位の会計情報と操作recordが保存されます。

live連携testは標準testとは別です。ANTHROPIC_API_KEYとSHELTERSIM_ALLOW_LIVE_API_TESTS=1の両方が必要です。既定値は2 call、test budgetは0.01ドルです。実行前に実効上限を確認し、その時点でのオーナー承認を得てください。今回の移管では有料APIを呼んでいません。offline test結果はtesting.mdを参照してください。
