# テストと検証

## 通常のoffline確認

開発toolを導入し、次を実行します。

~~~shell
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest
ruff check .
ruff format --check .
~~~

標準pytestには長時間simulation testも含まれます。live API testはSHELTERSIM_ALLOW_LIVE_API_TESTSとANTHROPIC_API_KEYの両方を明示した場合だけ対象です。通常のCIではlive gateを設定しないでください。

## 移管時の検証記録

採用したsource snapshot a162aed8284641c2a21e21bbcd04bad6e595ffb1で、CPython 3.12.14と固定dependencyを使ってoffline pytest全件を実行し、**579 passed、3 skipped**でした。所要時間は545.17秒です。ANTHROPIC_API_KEYとlive test gateは未設定で、有料Provider呼び出しはありません。

同じsource snapshotでRuffも確認しました。

- ruff check . は既存指摘1,906件: E501 1,758、E701 71、E702 40、I001 28、F401 6、F841 3。
- ruff format --check . は130個のPython fileを整形対象、87個は整形済みと報告しました。

これらのrepository全体のstyle指摘は文書整理以前から存在します。文書整理ではsource/testの動作を変更していません。GitHub Actionsはdispatchしていません。二つのworkflowは手動実行のままです。

## 主なテスト範囲

testは決定的state遷移、Checkpoint serializationと復元、独立Branch、event順、施設・資源・職員操作、履歴・timeline検索、model provenance、LLM fallbackとresponse tape、Project永続化、Web route、Demo成果物を確認します。詳細は各packageに隣接するtest fileを参照してください。

## 確認できていないこと

- 今回、live有料Provider testは実行していません。
- Windows/macOSの実機上で起動確認していません。
- 30日間の性能結果は確立していません。過去の30人・30日試行は10分以内に完了しませんでした。
- 現在のbrowser証跡だけでは、全map layer、実施設図面取込、介護・会話・職員の組合せ、大規模人口を含む一連の操作を確認したとは言えません。
- 地域人口、避難所、天候、医療、結果の照合データを用いた外部検証はできていません。
- 心理、健康、社会、容量、incidentの係数は実測校正されていません。合成Branchの差は、条件付き出力であり、予測や現実の因果効果ではありません。

再現可能な1日性能値と環境はbenchmarks.md、モデル仮定とsource recordはmodel-validation.mdとfixtures/source-manifest.jsonを参照してください。
