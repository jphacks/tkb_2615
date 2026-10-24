# 基盤repositoryへの貢献

## 進め方

1. 最新mainからfeature branchを作成します。
2. 目的を絞った変更にし、関連testとdocsを同じPull Requestで更新します。
3. 少なくとも1名の担当者にレビューを依頼します。Checkpoint、乱数、tick順、pipeline契約の変更はその境界に詳しい人が確認します。
4. CIとレビューが完了してからPull Requestで統合します。

mainへ直接実装commitをpushしないでください。branch protectionやCI triggerを変更する場合は、repository ownerと先に相談してください。

## ローカル確認

~~~shell
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest
ruff check .
ruff format --check .
~~~

このrepositoryのcore testは外部APIを呼びません。実際に実行したcommandと結果、未確認の範囲をPull Requestに記載してください。

## 変更の注意

- 時刻、seed、RNG key、phase順は再現性に関わります。変更には意図と回帰testを示します。
- 永続stateやCheckpoint形式を変える場合はschema版と復元互換性を確認します。
- coreはHTTP、disk storage、LLM Providerから独立させます。
- 製品本体と異なる実装を加える場合は、試作用であることと移植方法を明記します。
- secret、個人情報、生成Run成果物をcommitしません。
