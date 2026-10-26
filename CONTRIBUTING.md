# ShelterSimへの貢献

## ブランチと統合

ハッカソン期間中は短期開発のため、レビューを必須としません。

1. 最新の`develop`から、目的を絞ったfeatureブランチを作ります。
2. `develop`を宛先にPull Requestを作成します。GitHubの既定ブランチは`main`のままなので、宛先を明示してください（例: `gh pr create --base develop`）。
3. 下記のローカル確認を終えたら、作成者自身がPull Requestをマージします。

`main`は審査中の提出版として凍結しています。`main`へのcommit・merge・Pull Requestは行わないでください。`develop`へも実装commitを直接pushしないでください。このリポジトリではGitHub branch protectionとPull Request時の自動CIは設定されていません。Actions workflowは手動実行です。トリガーやリポジトリ設定を変える場合は、プロジェクトオーナーと相談してください。

## ローカル確認

開発依存を導入し、offlineで実行します。

```shell
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest -n auto
ruff check .
ruff format --check .
```

`-n auto`はpytest-xdistで複数CPUに分けて実行します。直列で実行する場合は省略できます。詳細は[テストと検証](docs/testing.md)を参照してください。

テストから有料APIを呼ばないでください。ライブ連携テストは別枠で、[LLM連携文書](docs/llm-integration.md)にある明示ゲートが必要です。過去タスクの予算やAPI利用承認を、新しい実行の許可として扱ってはいけません。

リポジトリ全体には既存のRuff指摘があります。指摘の現状は[テストと検証](docs/testing.md)を確認してください。機能変更に関係のないファイルを一括整形しないでください。

## 変更時の基本

- 動作や互換性を変えたら回帰テストを追加します。
- テストは[テストの置き場所と名前](docs/testing.md#テストの置き場所と名前)に従い、`tests/unit/<担当>/`、`tests/integration/`、`tests/e2e/`に置きます。`tests/`直下にファイルを足さず、直下の既存ファイルは変更するときに移します。
- 該当する仕様文書も同じPull Requestで更新します。
- モデルやデータ契約を変えるときは、出典、単位、上限、仮定、版を記録します。
- シミュレーションコアから外部接続を分離します。Providerの出力はスキーマと意味検証を通します。
- APIキー、ローカル`.env`、個人記録、Run成果物、未確認の施設図面をcommitしません。
- 第三者ライブラリ等を同梱する場合は、LICENSEとNOTICEを保持します。

## Pull Requestの説明

変更した動作、関連する契約、必要な移行を記載します。実行したコマンドと結果を明記し、未実施のcheck、結果の解釈に関わるモデルやデータ上の制限も示してください。
