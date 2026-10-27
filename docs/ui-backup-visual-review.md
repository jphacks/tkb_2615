# `/demo-v2` 参照画像との再比較・修正記録

2026-10-25。対象は `scenario.png`、`simulation.png`、`comparison.png`、`report.png`。
バックアップ専用ブランチ `feature/ui-backup-20261025` で実施。
モデル、係数、WorldState、APIの入出力契約は変更していない。

## レビューと修正

| 観点 | 修正前の差異 | 今回の修正と再評価 |
|---|---|---|
| レイアウト | 大きな見出しと常設通知が上部を占有し、比較・レポートの下部が画面外に出ていた | 共通ヘッダー、左ナビ、KPI列をコンパクトに統一。設定は3列、比較は左右対称、レポートはグラフ・マップ・対話の3列。両デスクトップ解像度で主要カードと操作が収まる |
| サイズ | シミュレーションのマップが小さく、操作欄より下に追いやられていた | 重複見出しを減らし、分岐・レイヤー選択をマップ上端へ統合。マップ上端を約127〜137px上げ、表示面積を約18〜30%拡大 |
| 余白 | 大きな区切りやカード内余白で情報が分断されていた | パネル間隔を約10〜12pxに統一。人物・イベント・AI応答を小カードに整理し、長い根拠は開閉式にした |
| 色 | 地図の面が平坦で、KPI・操作・警告の視覚的役割が弱かった | 濃紺の本文、淡青の背景、青の主操作、緑の承認・差分、赤の問題を統一。床・用途別の部屋・設備に色を追加 |
| フォント | 数値が重く、英語の状態値や長いIDが読み取りを妨げていた | 数値はArial系、日本語はローカルのシステムフォント。状態・イベント名を日本語化し、分岐名を短縮。元IDや根拠は詳細参照を保持 |
| 情報密度 | 生JSONが対話欄を占有し、比較の差分が下へ伸びていた | 比較の正式KPI4項目を1列に整理。Q&Aは記録事実・AI解釈・出典チップ・開閉式詳細に分離。レポートは正式な履歴のストレスとスタッフ負荷を同時表示 |
| マップ描画 | 小さな点、単色の床、読みにくいラベルでゲームらしさが弱かった | 独立Canvasレンダラーへ分離。床板、壁の厚みと影、用途別の床、設備アイコン、人物スプライト、集合人数、物理スケールを追加。記録された仮設室・bed point・障害物も描画対象にした |
| 操作性 | 長いズームボタン、空の対話欄、URL復元時の画面選択の不整合があった | レイヤータブ、ドラッグ、Ctrl/⌘＋ホイール、矢印キー、Homeリセット、Escape選択解除を追加。比較は時刻・ズーム・パンを同期。人物集合から各人を選択でき、URLの画面復元と対話の案内を修正 |

## 修正ループと実測

実ChromeをPlaywrightで開き、device scale factor 1、1440×900と1920×1080のビューポートそのものを撮影した。
APIモックや画像用の固定KPIは使っていない。

1. 修正前8枚を取得し、マップ面積と画面外へのはみ出しを計測。
2. 1回目の修正後8枚を取得。設備アイコン、比較カード、レポートのグラフ・AI欄を再調整。
3. 2回目の修正後8枚を取得。1440px比較画面の残り10pxのはみ出し、軸ラベル、人口履歴の欠測表現を修正。
4. 最終8枚を取得し、4画面×2解像度でページ全体の縦横はみ出しがないことを自動確認。凡例・状態名・選択解除の最終修正後にも再撮影。

長い警告・承認詳細・レポート本文・人物一覧は、必要時に開閉またはカード内をスクロールして参照する。

| 画面 | 修正前のページ高さ 1440 / 1920 | 最終のページ高さ 1440 / 1920 |
|---|---:|---:|
| 設定 | 1018 / 1080px | 900 / 1080px |
| シミュレーション | 1014 / 1130px | 900 / 1080px |
| 分岐比較 | 1246 / 1328px | 900 / 1080px |
| レポート | 1082 / 1141px | 900 / 1080px |

| シミュレーションマップ | 修正前 | 最終 |
|---|---:|---:|
| 1440×900 | 約789×390px、上端350px | 約817×445px、上端223px |
| 1920×1080 | 約1084×490px、上端373px | 約1133×610px、上端236px |

成果物ディレクトリに `before/`、`iteration-1/`、`iteration-2/`、`final/` を作成。
各ディレクトリのファイル名は `{scenario,simulation,comparison,report}-{1440,1920}.png`。
`final/metrics.json` に寸法、JavaScript例外、操作検証結果を保存する。
生成Run・スクリーンショットはGitへ登録しない。

## データと参照画像の残る違い

- 参照画像は写実的な学校と、体育館を囲む複数の部屋を描いている。現行の合成Geometryは避難スペースと細長いサービス区画から成るため、部屋配置は一致しない。実建物・通路・ステージを架空に追加していない。背景・床材・外周の植栽は表示上の演出と明記する。
- 同じ記録座標の人物は集合人数として表示する。比較用の保存Runでは30人が少数地点に集中しており、多数の人物を架空に散らしていない。位置未記録の設備やスタッフも推測配置しない。
- 寝床はGeometry overlayの `bed_points_m` がある場合だけ表示する。未記録のマットや家具で密度を増やしていない。
- 感染症リスク・情報到達率は共通の正式KPIがなく、避難者数の時系列も現在のMetricSnapshotにない。現在の人数は実フレームから表示し、人口履歴の選択肢は理由付きで無効化する。
- 介入は同時刻の実比較で評価する。確認用の共通時刻では主要KPIの差が0だったため、0を表示する。参照画像の改善率・推奨判定は流用しない。
- 人物の記録状態を表示し、発言は生成していない。PDF出力・チーム共有の専用APIもないため、JSONレポート保存を提供する。

## 検証方法

配信・回帰テストはLLMを使わない隔離サーバーで実行する。これは製品の起動設定ではない。
製品は `record` / `stop` で起動し、APIエラー時にofflineへ切り替えない。

```sh
PYTHONPATH=src SHELTERSIM_LLM_MODE=offline python -m pytest -q \
  -p no:cacheprovider tests/integration/test_demo_v2_routes.py tests/test_web_routes.py

# 別途起動したoffline専用テストサーバーに対する実HTTP E2E
UI_BASE_URL=http://127.0.0.1:8877 \
UI_SCREENSHOT_DIR=/tmp/hinanjo-ui-regression \
PLAYWRIGHT_MODULE=/path/to/playwright node scripts/test_demo_v2.cjs

# 既存の計算済みRunを読み取る視覚・操作検証。Python PlaywrightとChromeが必要。
python scripts/review_demo_v2.py \
  --base-url http://127.0.0.1:8876 \
  --project PROJECT --run RUN --branch CHILD_BRANCH \
  --minute COMMON_COMPUTED_MINUTE --output /tmp/hinanjo-ui-visual
```

`review_demo_v2.py` は既存Runを計算・分岐しない。記録座標と描画位置の一致、
全人物IDの集合表示への包含、描画入力の不変、人物クリック、レイヤー、
ズームリセット、比較の同期パン、元Branchのstate hash不変、URL画面復元を検証する。
800px / 390pxで4画面の横方向はみ出しも確認する。

ライブプレビューには `--exercise-preview --preview-project QA専用PROJECT`、
ライブ質問には `--ask-live-question` を明示指定する。
この2フラグはProvider呼出しがあり、明示的な予算承認と接続設定が必要。
今回の承認済み枠で、ライブプレビュー、保存済みの検証済みBranch report、
質問の `mode=record` と非空のfacts・sourcesを確認した。
通常のpytestおよびCIからは有料APIを呼ばない。

最終結果：配信・回帰pytest **12件通過**、新規PythonのRuffとJavaScript構文チェック通過。
実Chromeの視覚・操作検証は **8画面、例外0件、全寸法検査通過**。
ライブ質問は最初の検証で `recorded`、最終再実行で `cache_hit`（facts 1件、sources 1件）。

新規作成からの回帰E2Eでは、シナリオ作成・承認無効化・再承認、計算、
再生・シーク、介入の曖昧性解消・明示承認、独立分岐・元Branch不変、
共通時刻比較、レポート、未接続時の質問表示、JSON保存、画面復元を確認する。
ライブ検証とoffline回帰の結果は分けて扱う。
最終の新規Projectによるoffline E2Eも **PASS**。
offlineのBranch reportは `rule_fallback` であり、ライブAI生成の成功とは混同しない。

## 主な変更ファイル

- `src/sheltersim/web/static/demo-v2.html`、`demo-v2.css`：4画面構成、共通トークン、操作配置。
- `src/sheltersim/web/static/demo-v2.js`：表示整形、選択・パン・履歴描画・対話・画面復元。
- `src/sheltersim/web/static/demo-v2-map.js`：記録に基づく独立マップレンダラー。
- `src/sheltersim/web/server.py`：新しいJavaScriptの静的配信ルート1件のみ。
- `tests/integration/test_demo_v2_routes.py`：静的ルート検証。
- `scripts/test_demo_v2.cjs`、`scripts/review_demo_v2.py`：実ブラウザー検証。

`/demo`、`/admin`、シミュレーションエンジン、承認hash、Checkpointの保存契約は維持する。
`main`、`feature/ui-design`は変更せず、PRは作成しない。
