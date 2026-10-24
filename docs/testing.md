# テストと確認

## 基本コマンド

~~~shell
python -m pytest
ruff check .
ruff format --check .
~~~

testはofflineで実行し、外部APIを呼びません。

## 確認する契約

- 同じseedと安定keyから同じ乱数値が得られ、独立したkeyの呼出順で変化しないこと。
- TickEngineが決められた7 phaseを実行し、1 stepで時計を1分だけ進めること。
- Checkpointが独立stateを復元し、state改変をhash不一致として検出すること。
- pipelineが入力検証、初期化、実行、周期Checkpoint出力を順につなぐこと。

このtest suiteは小さな基盤契約だけを対象にします。製品本体のtest、CLI、Web、`/demo`、`/admin`、LLM fallback、Branch/Run比較の動作を代替・再検証するものではありません。それらは製品本体の開発環境で確認してください。
