# ShelterSimシミュレーション基盤

このrepositoryは、ShelterSimのsimulation coreとRun pipelineの基礎を、チームが独立して読んで変更できるようにした小規模な開発用repositoryです。

ここにあるコードはsimulationの基盤を検討するための骨格です。人口、施設、資源、心理、社会、region、LLM、Web、永続Projectを含む製品全体のsimulationではありません。製品固有の実装を加える場合は、製品全体の仕様・テストとの整合性を確認してください。

## このrepositoryに含むもの

- 整数分のSimulationClock。
- seedと安定keyから乱数を再現するCounterRNG。
- 型付きphaseを固定順に実行するTickEngine。
- domain状態を後から追加するための最小WorldState。
- state hashを検証する最小Checkpoint。
- 入力検証、WorldState初期化、tick実行、定期保存を接続する依存注入型SimulationPipeline。
- coreの順序、再現性、Checkpoint、pipeline接続を確認する小さなtest。

ドキュメントでは製品全体の契約も参照用に含みます。各仕様の実装がこのrepositoryに存在するとは限りません。対象範囲はdocs/pipeline-foundation.mdで確認してください。

## 開発環境

Python 3.12以降を用意し、repository rootで実行します。

~~~shell
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest
~~~

Windowsでは仮想環境の有効化commandが異なります。このrepositoryのpackageは外部APIを使いません。

## 基本の流れ

~~~python
from sheltersim_foundation.core import SimulationPipeline, WorldState
~~~

validator、WorldState factory、任意のphase handler、Checkpoint保存先をSimulationPipelineへ渡します。各層の契約と責務はdocs/architecture.mdおよびdocs/pipeline-foundation.mdを参照してください。

## ドキュメント

- docs/pipeline-foundation.md: このrepositoryの対象範囲とpipeline接続点。
- docs/development.md、docs/testing.md: 開発環境と確認方法。
- docs/architecture.md、docs/requirements.md: 製品側の全体像と要件。
- docs/simulation-models.md、docs/model-validation.md: モデル、出典、科学的な制約。
- docs/api-reference.md、docs/effect-program.md、docs/adaptive-event-compiler.md: 製品側のデータ/API契約。
- docs/project-persistence.md、docs/llm-integration.md: 製品側の永続化とLLM安全境界。
- AGENTS.md、CONTRIBUTING.md: チーム開発規則。

このrepositoryには既存のLICENSEファイルがあります。出典台帳に記載した第三者資料は、それぞれの利用条件を確認してください。
