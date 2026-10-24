# ローカルデモと管理画面

## アプリの起動

開発依存を入れ、repository rootで次を実行します。

~~~shell
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline sheltersim web \
  --runs-dir fixtures/demo_replay/runs \
  --demo-scenario fixtures/scenarios/baseline_100_7d.json \
  --host 127.0.0.1 --port 8765
~~~

次のURLを開きます。

- http://127.0.0.1:8765/demo: Project workspaceとoffline demo。
- http://127.0.0.1:8765/admin: 保存Runの参照画面。

このserverはローカル用途で、loginを持ちません。別途確認済みのaccess controlがない限りloopbackにbindしてください。

## デモデータ

repositoryに含むfixtures/demo_replayには、合成baselineと介入比較の事前計算結果があります。browserと回帰testが直接参照するため保持しています。このreplayはlive simulationでも、実際の避難所記録や予測でもありません。

同梱Scenarioも合成データです。Providerを設定していない場合、free-text previewはoffline parserを使います。previewだけでBranchは変更されません。介入は確認と明示承認の後に子Branchを作ります。

## Runの確認

admin画面は保存済みRun directoryを読みます。小さなRunは次のように作成できます。

~~~shell
SHELTERSIM_LLM_MODE=offline sheltersim prepare \
  --scenario fixtures/scenarios/h00_small_replay.json \
  --freeze --output /tmp/sheltersim-approved.json
SHELTERSIM_LLM_MODE=offline sheltersim run --offline \
  --scenario /tmp/sheltersim-approved.json \
  --output /tmp/sheltersim-run
~~~

CLIでRunを確認するか、sheltersim web --runs-dir /tmpでローカルdirectoryを表示できます。保存Runの参照はread-onlyです。Project workspaceは永続ローカルstoreと、状態変更用の別APIを使います。

## 事前計算replayの再作成

同梱replayを再作成すると長時間のoffline simulationを実行し、生成ファイルを上書きします。デモ利用には不要です。

~~~shell
SHELTERSIM_LLM_MODE=offline python scripts/build_demo_replay.py --force
~~~

固定結果は決定性回帰確認に使います。合成データを保ち、個人情報や非公開運用データで置き換えないでください。

## 困ったとき

- 比較が表示されない場合、--runs-dirがfixtures/demo_replay/runsを指し、fixtures/demo_replay/comparison.jsonが存在するか確認します。
- 8765番portが使用中なら別portを指定し、そのURLを開きます。
- server再起動でprocess内Demo Branchは初期化されます。永続Projectと保存Runは設定されたProjectStore directoryを使います。
- 静的appは手動のページ描画用にPDF.jsを同梱します。OCRや施設geometryの正確な抽出を保証しません。
