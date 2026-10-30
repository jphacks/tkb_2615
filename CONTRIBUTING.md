# ShelterSimへの貢献

## ブランチと統合

ハッカソン期間中は短期開発のため、レビューを必須としません。

1. 最新の`main`から、目的を絞ったfeatureブランチを作ります。
2. `main`を宛先にPull Requestを作成します。
3. 下記のローカル確認を終えたら、作成者自身がPull Requestをマージします。

`main`へ実装commitを直接pushしないでください。審査提出版はタグ`submission`で参照できます。Rustへの移植は`feature/rust-port`で進め、完成するまで`main`へ統合しません。このリポジトリではGitHub branch protectionは設定されていません。`オフラインCI`は`main`へのPull Requestごとに自動で実行され、手動でも実行できます。トリガーやリポジトリ設定を変える場合は、プロジェクトオーナーと相談してください。

## ローカル確認

開発依存を導入し、offlineで実行します。

```shell
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest -n auto
ruff check .
ruff format --check .
```

`-n auto`はpytest-xdistで複数CPUに分けて実行します。直列で実行する場合は省略できます。詳細は[テストと検証](docs/testing.md)を参照してください。

各実行の結果は`.test-results/`に残り、出力の最後の行に失敗したテストの名前と`summary.txt`の場所が出ます。失敗の原因は再実行せずに`summary.txt`で確かめてください（[結果の残り方と見方](docs/testing.md#結果の残り方と見方)）。

テストから有料APIを呼ばないでください。ライブ連携テストは別枠で、[LLM連携文書](docs/llm-integration.md)にある明示ゲートが必要です。過去タスクの予算やAPI利用承認を、新しい実行の許可として扱ってはいけません。

`ruff check .`と`ruff format --check .`は指摘なしで通ります。この状態を保ってください。長い文字列の書き方と、一括整形のcommitを`git blame`で飛ばす設定は[テストと検証](docs/testing.md#通常のoffline確認)を参照してください。

## 変更時の基本

- 動作や互換性を変えたら回帰テストを追加します。
- テストは[テストの置き場所と名前](docs/testing.md#テストの置き場所と名前)に従い、`tests/unit/<担当>/`、`tests/integration/`、`tests/e2e/`に置きます。有料APIを呼ぶテストは`tests/live/`にだけ置きます。`tests/`直下にはテストファイルを置きません（`tests/unit/repo/test_test_layout.py`が確かめます）。
- 該当する仕様文書も同じPull Requestで更新します。
- モデルやデータ契約を変えるときは、出典、単位、上限、仮定、版を記録します。
- シミュレーションコアから外部接続を分離します。Providerの出力はスキーマと意味検証を通します。
- APIキー、ローカル`.env`、個人記録、Run成果物、未確認の施設図面をcommitしません。
- 第三者ライブラリ等を同梱する場合は、LICENSEとNOTICEを保持します。

## Pull Requestの説明

変更した動作、関連する契約、必要な移行を記載します。実行したコマンドと結果を明記し、未実施のcheck、結果の解釈に関わるモデルやデータ上の制限も示してください。
