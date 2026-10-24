# 避難所IF！ バックアップUI (`/demo-v2`)

`/demo`、`/admin`とは独立した、HTML/CSS/JavaScriptの4画面です。WorldState、Run、Checkpoint、EffectProgram、モデル係数の契約を変更しません。

## 起動

バックアップworktree内に専用venvを用意します。既存ディレクトリのvenvへeditable installして、既存作業の参照先を変更しないでください。デモはClaude APIへ接続する`record` modeで起動し、APIエラー時にoffline fallbackせず理由を表示します。API keyはGit管理外の環境変数またはローカルの`.env`から読み込ませてください（アプリは`.env`を自動読込しません）。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,llm]'
# keyを保管したローカル.envを使う場合。パスは手元の保存先に合わせます。
set -a
. /path/to/local/.env
set +a
test -n "${ANTHROPIC_API_KEY:-}" || { echo 'ANTHROPIC_API_KEY is required' >&2; exit 1; }
SHELTERSIM_LLM_MODE=record SHELTERSIM_LLM_ERROR_POLICY=stop \
  .venv/bin/python -m sheltersim web \
  --runs-dir /tmp/hinanjo-if-backup-runs --host 127.0.0.1 --port 8876
```

[http://127.0.0.1:8876/demo-v2](http://127.0.0.1:8876/demo-v2) を開きます。生成Runは専用ローカル保存先へ保存されます。既存デモの事前計算リプレイは使用せず、実際に計算したProject Runを再生します。再生速度は1回の表示更新で進むシミュレーション分数で、計算速度の指定ではありません。未計算時刻へはシークできません。

## 画面とAPIの対応

`P=/api/v1/projects/{project}`、`R=P/runs/{run}`、`B=R/branches/{branch}`。

| 画面・機能 | 既存API | 調査時の状態 / 新UIでの対応 |
|---|---|---|
| Project作成・保存Run復元 | POST /api/v1/projects、GET P、GET P/runs | 実装済み / 専用ProjectとURL・ブラウザー設定から接続 |
| 自然言語・基本条件・任意資源 | POST P/scenario-preview | 実装済み / 入力値を優先し、出典・補完仮定・警告・ブロッカーを表示 |
| 施設種別 | request.facility_conditions | 条件の記録に対応 / 実施設の図面・適合性や別形状を推測しない |
| 人口構成・施設概要 | preview.scenario、facility_design | 実装済み / 記録年齢の構成図と生成Geometryを描画 |
| 明示承認・Run作成 | POST P/scenario-approve | 実装済み / preview_id、approved_hash、approved:trueを送信 |
| 計算・停止・進捗 | POST R/resume、R/cancel、GET R | 実装済み / 計算と再生を独立、保存済み時刻を進捗表示 |
| マップ・人物・スタッフ・正式KPI | GET B/timeline?minute=M | 実装済み / 実Geometry・物理座標のみ描画、欠測位置の行動を補わない |
| レイヤー・拡大縮小・人物選択 | timelineデータによるブラウザー描画 | 接続が必要 / 新規Canvas描画。健康レイヤーは記録された疲労・空腹・水分ニーズで、医学診断ではない |
| 再生・停止・速度・巻戻し | 同上 | 接続が必要 / 計算済み範囲のみ。1/5/15/60分刻み、表示更新間隔700ms＋通信時間 |
| 自然言語介入プレビュー | POST B/intervention-preview | 実装済み / 変換作用・検証結果・support_mode・制限を表示 |
| 介入の明示承認・独立分岐 | POST B/intervention-apply | 実装済み / hashと入力・時刻・分岐を照合。変更で再承認が必要 |
| 同期比較 | GET R/compare?branch_ids=A,B&minute=M | 実装済み / parentとchildを共通時刻・レイヤー・ズームで比較 |
| 両分岐を先へ計算 | POST R/resumeを順番に実行 | 実装済み / 共通の目標まで計算後compareを取得 |
| KPI差分 | compare.differences | 実装済み / ストレス、累積最大待機時間、稼働比率、未解決支援要請の符号付き差分 |
| 折れ線グラフ | GET B/history?kind=kpis | 実装済み / tick終了時記録を標本化・ページ取得。マップのtick直前境界と区別 |
| Branch report・出典・モデル | GET B/report、GET R/evidence、GET B/llm-status | 実装済み / 途中・未生成・fallback・利用不可を区別 |
| 結果Q&A | POST B/questions | 実装済み / factsとexplanations・出典・制限を区別。offlineは回答生成不可を表示 |
| レポート保存 | 上記データからJSON書出し | ブラウザー実装 / 記録とモデル根拠を保存 |
| 感染症リスク、情報到達率 | 共通の正式KPIなし | 未提供 / 数値や「高・中・低」を捏造しない |
| PDF出力・チーム共有 | 専用APIなし | 本UIでは提供しない。操作不能な飾りボタンを配置しない |

## 利用の流れ

1. 「例文を使う」か、基本設定の自治体・災害・人数を入力し、期間などを任意指定します。項目指定を優先します。未入力の基本条件は既存parserで文章から抽出します。基本項目が一部だけの場合は抽出プレビューと統合後の最終プレビューを順番に取得します。
2. 「次へ」で入力値・補完値・出典・警告を確認します。公的基準への適合は主張しません。承認ブロッカーがある場合は条件を修正し、再プレビューします。
3. 確認チェックを選択して承認するとRunを作成します。計算目標を経過分で指定して計算します。短い動作確認には100人・0.025日（36分）を利用できます。長期・大人数は計算と保存量が増えます。
4. 再生・シークで計算済み時刻を確認し、介入文を入力します。追加先が曖昧な場合は、APIが返した候補から明示的に選択します。選択結果を `zone_id: ...` として指示へ追記し、作用を再プレビューします。作用を確認・承認すると独立分岐が生成されます。
5. 分岐比較の「両分岐を指定時刻まで計算」で両方を共通目標へ進めます。比較画面の計算目標（経過分）を指定します。シミュレーション画面の目標欄とも同期します。未指定時は両者の先へ60分（期間上限まで）進めます。
6. レポート画面で記録の時系列、人物状態、イベント、出典を確認します。最後まで計算するとBranch reportが生成されます。結果Q&Aと自然言語機能はClaude APIを呼び出します。API接続、利用枠、応答検証で失敗した場合はfallbackせず、エラー理由を表示します。

## 設計・表示上の境界

- 主ブランドは「避難所IF！」。青 `#0878ff`、濃紺 `#102766`、背景 `#edf7fd`、緑 `#00a995`、注意色 `#ff365e`。日本語システムフォントと数値用Avenir/Arialを利用し、CDNは不要です。
- 添付参照のヘッダー・左ナビ・カード密度・青い操作・左右分岐比較を踏襲します。マップの部屋配置は実APIのGeometryを優先するため、参照画像の架空の体育館形状とは異なります。床の木目と人物は装飾的描画であり、建物や人物行動を追加しません。
- 写実的な建物写真、未記録の人物の声、根拠のない改善予測・感染リスク、実行時間・費用の推定値は生成しません。
- スタッフ位置がなければマップに推測配置しません。人物情報の一覧には記録状態を表示します。
- 履歴は保存範囲の標本です。グラフの線は記録点同士を結ぶ表示であり、未計算時刻を生成しません。
- 介入文の補助表示は当該ブラウザーのsessionStorageを利用します。復元できない場合は欠測を明示し、APIのCheckpoint分岐根拠を表示します。
- UIのサマリーは記録値と一般的な検討案です。AIによる因果説明や人物の実発言として表示しません。
- 新しい永続スキーマ・係数・モデルは導入しません。承認・分岐・再現性の検証は既存APIへ委ね、UI側でも条件変更時の承認無効化を行います。

## 検証

通常のpytestはofflineで実行します。実APIの有効性確認には、ローカルに設定したAPI keyと、既存の上限付きlive integration testを使用します。デモ起動とは別工程です。

```sh
SHELTERSIM_LLM_MODE=offline PYTHONPATH=src python -m pytest \
  tests/test_web_routes.py tests/test_demo_v2.py tests/test_web_demo.py \
  tests/test_project_store.py tests/test_results_chat.py
```

ブラウザーE2Eは `scripts/test_demo_v2.cjs` を参照してください。Playwrightを含むNode環境とChromeを使用し、上記offlineサーバーを起動してから実行します。実API・一時Projectでシナリオ→承認→計算→シーク→介入→分岐→比較→レポート→質問を確認し、1440×900 / 1920×1080の各画面スクリーンショットを出力します。生成データ・画像はGitに登録しません。

明示Zone IDは自然言語の一般名詞との一致より優先します。`zone_id: toilet`、`zone_id: staff` など既存のIDを誤って不明扱いしないように対象解決だけを修正しました。ID未指定の曖昧さと、未知IDの拒否は維持します。

## このブランチで実施した検証

- Python: UI配信、parser、既存Web、永続化、Q&A、Checkpoint、EffectProgram、厳密timelineのofflineテスト95件が通過。
- Chrome headless: 実HTTP APIによるE2E、承認無効化、巻戻し、自然言語の曖昧性解消、元Branchのhash不変、両Branchの計算・同期再生、report取得、質問UI、JSON保存、復元が通過（offline modeによる既存検証）。
- 表示: 1440×900 / 1920×1080の4画面を撮影し目視確認。800px / 390pxでも横方向にはみ出さないことを確認。
- JS構文チェックとgit diff --checkが通過。新規Pythonテストはruffを通過。既存のserver.py（13件）とnatural_language.py（14件）のlint指摘は起点mainと同じで、新規指摘はありません。
- 上記pytestはoffline専用です。live APIの確認は利用者が明示した予算上限と`SHELTERSIM_ALLOW_LIVE_API_TESTS=1`を使う別コマンドで実施します。長期7日・大人数での性能は未検証です。

今回のローカル環境では、既存venvの依存だけを読み取り、`PYTHONPATH=src` でバックアップworktreeを優先する次の起動も確認済みです（既存venvへのinstallや設定変更は行いません）。

```sh
cd /Users/mizuki/.codex/worktrees/ui-backup-20261025/tkb_2615
PYTHONPATH=src SHELTERSIM_LLM_MODE=offline \
  /Users/mizuki/Dev/tkb_2615/.venv/bin/python -m sheltersim web \
  --runs-dir /private/tmp/shelter-ui-backup-data --host 127.0.0.1 --port 8876
```

作業ブランチ: `feature/ui-backup-20261025`。起点: `origin/main` の `0d3a641d6a8470085b49e09cfda4a8c2cbe467a5`。既存ディレクトリの未コミット変更は引き継がず、独立worktreeで作業しました。
