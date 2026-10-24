# ShelterSimへの貢献

## ブランチとレビュー

1. 最新の`main`から、目的を絞ったfeatureブランチを作ります。
2. チームメイトがレビューできる大きさにPull Requestを保ちます。
3. 少なくとも1名のチームメイトにレビューを依頼します。永続スキーマ、Checkpoint互換性、モデル動作、Provider安全性、公開HTTP契約の変更は、その境界に詳しい人に確認を依頼します。
4. 必須checkとレビューを終えてからPull Request経由で統合します。

`main`へ実装commitを直接pushしないでください。このリポジトリではGitHub branch protectionとPull Request時の自動CIは設定されていません。Actions workflowは手動実行です。トリガーやリポジトリ設定を変える場合は、プロジェクトオーナーと相談してください。

## ローカル確認

開発依存を導入し、offlineで実行します。

```shell
python -m pip install -e "[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest
ruff check .
ruff format --check .
```

テストから有料APIを呼ばないでください。ライブ連携テストは別枠で、[LLM連携文書](docs/llm-integration.md)にある明示ゲートが必要です。過去タスクの予算やAPI利用承認を、新しい実行の許可として扱ってはいけません。

リポジトリ全体には既存のRuff指摘があります。指摘の現状は[テストと検証](docs/testing.md)を確認してください。機能変更に関係のないファイルを一括整形しないでください。

## 変更時の基本

- 動作や互換性を変えたら回帰テストを追加します。
- 該当する仕様文書も同じPull Requestで更新します。
- モデルやデータ契約を変えるときは、出典、単位、上限、仮定、版を記録します。
- シミュレーションコアから外部接続を分離します。Providerの出力はスキーマと意味検証を通します。
- APIキー、ローカル`.env`、個人記録、Run成果物、未確認の施設図面をcommitしません。
- 第三者ライブラリ等を同梱する場合は、LICENSEとNOTICEを保持します。

## Pull Requestの説明

変更した動作、関連する契約、必要な移行を記載します。実行したコマンドと結果を明記し、未実施のcheck、結果の解釈に関わるモデルやデータ上の制限も示してください。
