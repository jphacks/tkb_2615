# 日本語PDF分析レポート

レポート画面の既存の「↓ レポート保存」からA4縦のPDFをダウンロードします。
ボタンの名称・位置・デザインは変更しません。生成中は既存の処理中表示と操作ガードを使い、
失敗時はエラーを表示して同じボタンから再試行できます。JSONファイルのダウンロードには切り替えません。

## 利用条件と対象

`pip install -e '.[llm]'` で導入し、既存のAnthropic設定・利用枠を使用します。
PDF執筆は `SHELTERSIM_OPUS_MODEL_ID` のOpusを使用し、他のreview用途のモデル設定は変えません。
`SHELTERSIM_LLM_MODE=record` とAPI接続が必要です。`replay` では一致する記録応答を再利用できます。
offlineモードでAI分析を代用したPDFを作ることはありません。
保存済みPDFはofflineでもダウンロードできます。

- 比較を取得している場合は、比較画面で現在選択されている分岐とその親、比較の表示時刻を使用します。
- 比較を取得していない場合は、レポート画面の現在Branch・表示時刻を使用します。
- 単一Branch、A/Bの2分岐に対応します。複数候補がある場合は選択中のペアだけを出力します。
- 未計算範囲・共通範囲外・欠けたBranchは生成前に拒否します。途中の計算済み時刻は途中結果と明記します。
- 実計算中は保存を無効にします。生成中に別操作でBranchが更新された場合も再試行を求めます。

## データと責務

`web/pdf_report.py` は保存済みScenarioと `TimelineStore.seek` から読み取り専用の集計projectionを作ります。
既存の比較APIと同じ共通範囲・起点情報を利用します。engine、Policy、数理モデルは変更しません。
KPIは評価境界の `MetricSnapshot`、時系列は保存済みsnapshotsです。
比較の差はプログラムでB−Aを計算し、ストレスを100点尺度、稼働率を百分率に換算します。
稼働率の増減自体には良否を付けません。

問題の継続時間は、トイレ待ち人数・未解決支援要請数が正で、両端が記録された1分区間の合計です。
初回観測・欠測時間を明記し、欠測を補間しません。これらは表示上の整理であり、医療的な判定基準ではありません。
重要イベントはexogenous/interventionの実行記録です。介入表は型付き作用の実行・予定時刻と結果を区別します。

`llm/pdf_analysis.py` が既存LLMRouter・公式Anthropic SDK・共有UsageLedger・StoredResponseTapeを通して
Opusへ執筆を依頼します。APIへ渡すのは固定の集計項目と匿名の根拠参照のみです。
個人profile、任意の施設名・所在地、介入の生の入力文、過去の会話、認証情報は渡しません。
数値・観測事実はプログラムで印字し、AIは解釈・原因候補・優先案・代替案・追加検証を執筆します。
JSONスキーマ、段落数・長さ、根拠ID、数値混入、秘密情報・連絡先、代表的な因果断定表現を検証します。
解釈と原因候補は仮説として表示します。自由記述の意味の完全な証明はできないため、担当者による照合が必要です。

`web/pdf_render.py` がReportLabで検索・コピー可能なPDFを組版します。
既存のnavy/blue/teal、見出し、表、4種類の時系列図、ページ番号を使う6章構成です。
通常は6ページ程度ですが、根拠や表の量で増減します。長い表は見出し付きで改ページします。
施設表は先頭12件、介入表は先頭6件、重要イベント表は先頭10件を掲載し、省略を明記します。
全件の集計根拠は内部JSONに残します。実建物の形状を推測しないため施設マップは掲載しません。
日本語フォントはBIZ UDゴシックを同梱・埋め込みし、外部フォントサービスやOSのフォントに依存しません。
同梱ライセンスは `src/sheltersim/web/fonts/OFL.txt`（SIL Open Font License 1.1）です。
未対応字形は「表示対象外」と明示します。

## API・保存・再現性

同一オリジンのJSON POST:

```text
POST /api/v1/projects/{project}/runs/{run}/pdf-report
{"branch_ids":["main","candidate"],"minute":120}
```

成功時は `application/pdf` と `Content-Disposition: attachment` を返します。
失敗時は既存のJSONエラー応答を返します。GET参照によるProvider呼び出しはありません。

`runs/{run}/pdf-reports/{hash}.json` に独立したschema_version=1の成果物として、
入力identity、集計projection、AI分析・利用記録、PDFのbase64と検証hashを原子的に保存します。
既存のProject容量制限を適用します。identityにはBranch順序・head Checkpoint・評価時刻・Scenario hash・
Opusモデル・プロンプト版・組版版を含めます。同じidentityは再起動後も保存済みPDFをそのまま返します。
組版だけの再試行では成功済みのOpus応答テープを再利用します。プロセス内の重複生成は直列化します。
API・意味検証の失敗後に利用者が再試行した場合は、独立した試行IDを付けます。
失敗済みの利用記録・不確定な費用予約は消さず、同じ共有枠の残量内でだけ再依頼します。
その試行番号はschema_version=1の `runs/{run}/pdf-attempts/{hash}.json` に保存します。
複数サーバープロセスによる同時要求は既存の共有台帳で二重課金を防止しますが、一方が一時的に失敗する場合があります。

既存のScenario・Run・Checkpointスキーマを変えず、移行は不要です。
従来のreport.json、比較API、内部JSON保存、結果Q&A、Checkpoint・応答テープの再現契約は維持します。
新しい成果物だけに `pdf-report-ja-v1` と `pdf-analysis-ja-v1` の版を付けます。
書式・根拠計算・検証契約を変更するときは対応する版を更新してください。

## 検証

```bash
pip install -e '.[dev,llm]'
SHELTERSIM_LLM_MODE=offline pytest tests/integration/test_pdf_report.py \
  tests/unit/web/test_pdf_report.py tests/e2e/test_pdf_download.py
```

テストは有料APIを使わず、実際に計算した合成Scenarioとテスト用Providerを使用します。
単一Branch、複数候補の選択、無介入・介入あり、差分ゼロ、途中結果、欠測、未計算範囲、
不正なAI出力、APIエラー、予算・回数上限、PDF失敗と再試行、二重実行、再起動後の再利用、
HTTP応答、A4・日本語文字抽出・フォント埋め込み・ページ番号・PDFium描画を検証します。
既存の比較・再生・LLM・結果Q&Aテストも実行してください。
ライブAPI検証は通常のpytestから実施せず、既存の明示的な環境ゲートと承認済み予算を必要とします。

### 実装時の確認（2026-10-26）

- 関連offlineテスト106件成功。live用の追加テスト1件は通常実行でskipを確認。
- 明示承認後、`test_live_pdf_reports_use_opus_and_reuse_saved_pdf` を実Opusで2回実行して成功。
  `claude-opus-5-5` を計4回呼び出し、台帳上の推定費用は合計US$0.292944。
- 100人・180分の合成Scenarioを実際に計算し、60分時点のトイレ増設Branchと比較。
  最終組版は保存済みの実Opus応答をreplayして再利用し、単一・比較ともA4縦6ページ。
  全ページをPDFiumで描画・目視確認し、日本語検索・文字抽出、フォント埋め込み、ページ番号、
  未対応字形ゼロ、ページ余白外の字形ゼロを確認。
- ヘッドレスChromeで単一・選択したA/BのPDF取得、ボタン名称、処理中のdisabled表示、
  二重クリック抑止、APIエラー表示と再試行、JavaScript例外ゼロを確認。
- 既存の `scripts/test_demo_v2.cjs` も成功。Scenario承認、計算・再生、分岐、比較、
  親Branchの分離、履歴、offline Q&A、復元、各画面幅に加え、offline保存時に
  エラーが表示され、JSONや代替PDFをダウンロードしないことを確認。
- 新規PythonファイルのRuff、JavaScript構文、差分の空白チェックが成功。
  wheelビルドで日本語フォントとOFLライセンスの同梱を確認。
