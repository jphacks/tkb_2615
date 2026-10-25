# LLM連携と安全性

## Providerとの境界

Claude連携は任意です。llm extraを導入し、ANTHROPIC_API_KEYをGit外に設定します。SHELTERSIM_LLM_MODEを省略するとauto modeになり、keyが設定されている場合、対応workflowがProviderを呼ぶことがあります。すべてローカルで行う場合はSHELTERSIM_LLM_MODE=offline、またはcommandの--offlineを指定します。同梱.env.exampleは`record`と`stop`で、API接続失敗時のoffline fallbackを防ぎます。

`/demo-v2`の利用者向けデモはAPI接続を必須とし、`SHELTERSIM_LLM_MODE=record`と`SHELTERSIM_LLM_ERROR_POLICY=stop`で起動します。key不足やAPIエラーをoffline動作へ黙って切り替えず、処理失敗として表示します。オフラインmodeは自動テストなど、コマンドで明示した場合だけに使用します。

Anthropicへ接続するのはsrc/sheltersim/llm/anthropic_adapter.pyだけです。simulation coreにProvider依存はありません。requestはrouter、型付きtask schema、呼出し・token・同時実行・費用上限、usage ledger、cache、response tapeを通ります。

## 対応する段階

- 準備段階では合成Scenarioやprofile案を作れます。結果はpreviewであり、利用者が確認して同じhashを明示承認します。
- event解釈では導入済みprimitiveからEffectProgramを作成できます。previewまたは適用前に、現在Branch、対象ID、導入Policy、範囲、時系列に照らして検証します。
- runtimeでは設定済みのtriggerで少数の型付き提案を依頼する場合があります。毎tick・全員についてProviderを呼ぶ設計ではありません。
  - 支援要請の優先度判定（support-priority-v2）は、stress閾値と1日あたりの上限の範囲でだけ問い合わせます（model-validation.md参照）。以前のv1は依頼ごとに問い合わせていたため、既定の呼出し上限を開始直後に使い切り、social提案まで届きませんでした。v1の記録を含む履歴はv1のまま再生します。
  - v2のpromptはrationaleを80文字以内の短い句にするよう明示します。schemaの上限（160文字）を超えた応答は不正形式としてfallbackになります。
- reviewは保存Runの証拠を要約します。結果質問は記録された事実を根拠にするread-only操作で、ScenarioやBranchを変更できません。

利用可能な介入operatorはeffect-program.mdを参照してください。未導入機構が必要な要求は拒否するか、制限を示した近似として扱います。model responseはoperatorを追加したり、KPIを直接変更したり、Python/JavaScriptを実行したり、承認を迂回したりできません。

## 再現性とエラー

構造化responseをparseし、検証します。Provider error、不正形式、rate limit、offline mode、budget超過では、定義済みfallbackを使うか、利用不可を返します。選択された動作と利用量を記録します。

検証器は、Pydanticが生成するschemaで使う構文（`$ref`/`$defs`、`oneOf`、`const`、schemaとして与えた`additionalProperties`、数値・文字列の上下限、`pattern`）を扱います。以前は`$ref`を解決できず、Webの自由文読み取り（region-scenario-extraction-v1）は応答の内容にかかわらず必ず不合格になっていました。

### 分析用の予備枠

runtimeの呼出しは、Run（CLIでは1回のrun）の利用枠の上限から3回を残したところで止まります（`ANALYSIS_RESERVED_CALLS`）。残した3回は、終了後のreportと結果質問のための予備です。Projectでは、runtime・report・結果質問・介入の変換がすべて同じRunの利用枠を共有しています。この予備がないと、長いRunではruntimeが上限を使い切り、reportが生成されませんでした。

- 予備を超えたruntimeの依頼は`runtime_call_share_exhausted`としてfallbackし、fallback集計と警告に表示されます。
- 上限が小さい利用枠では、予備は「上限−1」回までです。上限1回の利用枠には予備がありません。
- 予備は呼出し回数だけを確保し、予算額は確保しません。

### 出典候補・report・結果質問の扱い

- 地域情報の公式ドメイン検索は、応答本文の引用に加えて、web searchが返した検索結果そのもの（URLとタイトル）を出典候補にします。JSONだけを返す応答には引用がほとんど付かず、以前は検索が成功しても候補が常に0件でした。候補は許可ドメイン（go.jp、lg.jp）に限り、重複を除いて最大25件です。ページ本文は取得・照合しません。
- Project Runの完了時は、reportを書き込んでから（生成できなかった場合はその旨を書き込んでから）Runを`completed`にします。画面は`running`から状態が変わった時点で一度だけreportを取得するためです。
- 利用記録は、同じ依頼（同じ入力・状態・prompt版）に二度課金しません。Webの下書きプレビューとProject Runの分析（report・結果質問）は、採用された応答を`usage-tapes/<draft>.json`と`runs/<run>/analysis-tape.json`に保存し、同じ依頼にはAPIを呼ばずに保存済みの応答を使います。以前は応答を保存していなかったため、同じ依頼を繰り返すと重複として拒否され、ruleの結果になっていました。保存するのは応答だけで、promptは保存しません。連絡先や秘密情報に見える文字列を含む応答は、保存せずにその場限りで使います。介入の自然言語指示（compiler）は、この保存の対象外です。
- 結果質問では、各根拠の値が記録値の完全な写しである必要があります。数値はJSONの値として、文字列はJSON形式（引用符付き）でもそのままでも受け付けます。丸めた値、型を変えた値、言い換えた文字列は拒否します。

### thinkingと出力上限

runtime以外のstage（準備、介入、review）が使うOpusは、thinkingを無効にできません。thinkingのtokenはAPIの`max_tokens`に含まれます。そのためrouterは、各taskの回答の大きさ（`max_output_tokens`、設定の`SHELTERSIM_LLM_MAX_OUTPUT_TOKENS`で上限）に、thinking用の余裕として4,096 tokenを加えて要求し、その分の費用も予約します。準備stageでは`effort: low`を指定し、thinkingを短くします。runtimeのHaikuは従来どおりthinkingを無効にします。

Opus呼出し1回の予約額は、既定設定でtaskの回答上限に応じておよそ$0.2〜0.4になりました（review約$0.26、シナリオ下書き最大約$0.38）。介入compilerは入力上限を64 KiBで予約するため、約$0.69です（以前は約$0.30）。実際の費用は送信量に応じて精算され、予約額よりずっと小さくなります。runの予算（`SHELTERSIM_LLM_BUDGET_USD`）がこれより小さいと、Opusの呼出しは`budget_exceeded`としてfallbackします。100人規模のシナリオ下書きは、回答が既定の出力上限（8,192）を超える見込みです。その場合は`SHELTERSIM_LLM_MAX_OUTPUT_TOKENS`を上げてください。

web searchのようなserver toolを使う呼出しでは、APIが検索の前後でモデルを複数回実行し、`usage.output_tokens`はその合計になります。予約は「検索回数＋1」回分の`max_tokens`で計算します。実際の使用量が予約を超えると、利用記録は安全のため凍結（frozen）され、以後その記録を共有するすべての有料呼出しが拒否されます。

凍結の原因を確認したら、次のコマンドで解除できます。対象は、PC全体で共有する累積上限の記録（task-global-v1）、CLIの利用枠（cli-…）、legacy demoの利用枠（legacy_demo）です。

~~~shell
sheltersim usage --unfreeze task-global-v1 --confirm task-global-v1 --reason "確認した理由"
~~~

- 解除で変わるのは凍結フラグだけで、記録済みの利用量は残ります。未決着（pending）の予約が残っている間は解除できません。
- 解除した日時・理由・その時点のreceipt数は、利用記録とは別の監査ファイル（usage-audit/）に残ります。usage summaryの`unfreeze_events`に表示されます。
- 利用記録の形式はv1のままです。PC全体で共有する記録なので、古い版のShelterSimからも引き続き読めます。
- Project Runの利用枠は、このコマンドの対象外です。

### 入力の大きさの上限

routerは送信文のUTF-8バイト数を`max_input_tokens`（`SHELTERSIM_LLM_MAX_INPUT_TOKENS`、既定16,384）と比べ、超える依頼はAPIを呼ばずに`input_token_limit`としてfallbackします。バイト数はtoken数より多めの見積もりです。

文脈が大きくなるtaskは、task側で入力上限（`input_byte_limit`）を宣言できます。routerは設定値とtaskの宣言値の大きい方を使い、呼出し前の予算確認もその上限で予約します。宣言値は応答を変えないので、cache keyとresponse tapeには影響しません。

- 介入の自然言語指示（compiler）は64 KiBを宣言します。生成Scenarioでは導入済みprimitiveの説明だけで約13 KBあり、以前はScenarioの人数によらず16,384バイトを超えていました。その結果、Opusを一度も呼ばずにruleの結果になっていました。64 KiBは約1,500人までの文脈に相当します。
- それ以上の人数でも上限を超えると、これまでどおりAPIを呼ばずにfallbackし、その理由を結果に示します。
- usage ledgerの予約は、従来どおり実際の送信バイト数（入力とschema）から計算します。

### 介入compilerのfallback理由

介入の変換がruleの結果になったときは、Opusを使わなかった理由を日本語の固定文で`limitations`に入れます。対象はofflineで変換できた案、聞き返し、変換できなかった結果のすべてです。末尾の理由コードは`usage.error_kind`、またはrouterのoutcomeと同じです（例：`offline`、`input_token_limit`、`max_api_calls`、`budget_exceeded`、`rate_limited`、`invalid_output`）。model出力本文やprovider errorの本文は含めません。

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

API keyはprocess環境変数から読みます。.env.exampleは設定名の参考であり、dotenv loaderではありません。Provider requestはrouterごとの予算と、ローカルProject・process間で共有する既定50ドルのusage ledgerに対して予約します。端末の共有上限を明示的に増やす場合は`usage --increase-budget task-global-v1 --new-budget-usd <新しい合計額> --confirm task-global-v1 --reason "承認理由"`を使います。変更理由と旧・新上限は端末内の監査記録に残り、Git管理の既定上限には反映されません。この上限はソフトウェアの制限であり、利用承認ではありません。API callにはプロジェクトオーナーの都度の承認が必要です。別taskの予算や承認は引き継がれません。公開前に累積上限の実装を再検討してください。

## 利用記録とlive連携test

CLI usage commandと/api/v1/usage/runtimeは上限付きusage receiptを表示します。Project RunにもRun単位の会計情報と操作recordが保存されます。

CLIはprepareとrunのたびに新しい利用枠（cli-prepare-…、cli-run-…）を作り、その時点の設定を上限として固定します。reviewは対象Runの利用枠を引き継ぎます。以前は全CLI実行が固定の cli-default を共有していました。一度使い切ると、それ以降はAPIを呼ばずに全taskがfallbackしていました。既存の cli-default は使われなくなるだけで、記録は残ります。Run間の合計は、既定50ドルまたは端末台帳の監査済み増額により制限されます。

live連携testは標準testとは別です。ANTHROPIC_API_KEYとSHELTERSIM_ALLOW_LIVE_API_TESTS=1の両方が必要です。設定の既定値は2 call、test budget 0.01ドルですが、OpusとSonnetの呼出しはthinking用の余裕を予約するため、この既定値ではOpusを1回も呼べません。そのため各testは、上限を自分で設定します。

- モデル別確認：3 call、0.15ドル
- 公式ドメイン検索：2 call分の枠
- 機能の通し確認：12 call、2ドル

実際の費用は、予約額よりずっと小さくなります。実行前に実効上限を確認し、その時点でのオーナー承認を得てください。offline test結果はtesting.mdを参照してください。
