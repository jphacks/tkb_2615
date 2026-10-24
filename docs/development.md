# 開発環境

## 必要なもの

- Python 3.12以降
- pytestとRuff（開発用optional dependencies）

この基盤packageは外部APIやLLM Providerを使用しません。API keyの設定は不要です。

## セットアップ

repository rootで実行します。

~~~shell
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
~~~

Windowsでは仮想環境のactivate commandを環境に合わせてください。

## 最小pipelineの接続例

~~~python
from sheltersim_foundation import SimulationPipeline, WorldState

pipeline = SimulationPipeline(
    validate_scenario=lambda raw: raw,
    create_world=lambda scenario: WorldState(
        scenario_id=scenario["id"],
        seed=scenario["seed"],
        duration_minutes=scenario["duration_minutes"],
    ),
)
result = pipeline.run({"id": "example", "seed": 7, "duration_minutes": 60})
~~~

製品版Scenario schema、永続化adapter、CLI、Web applicationはこのpackageに含みません。拡張時の境界は`docs/pipeline-foundation.md`を参照してください。

## 変更時の手順

1. 対象の契約と責務を確認します。
2. 小さな変更に対応する回帰testと日本語文書を更新します。
3. pytest、Ruff lint、format checkを実行します。
4. 実際に確認した結果と、未確認の範囲をPull Requestに書きます。

時計、RNG keyの表現、tick phase順、Checkpoint schemaは再現性と互換性に関わります。変更には意図、schema/versionの扱い、既存stateをどう読むかを明記してください。
