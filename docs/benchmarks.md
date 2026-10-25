# オフライン性能計測

この計測器はProviderやネットワークを使わず、合成Scenarioでシミュレーションと成果物の書き込み時間、プロセスのピークRSS、記録量を測ります。結果は実装・ホスト・合成条件の記述であり、実際の避難所運用やモデルの妥当性を示しません。

## 再計測

開発依存を入れ、出力先を指定して実行します。

```shell
SHELTERSIM_LLM_MODE=offline python benchmarks/run_benchmarks.py \
  --output-dir /tmp/sheltersim-benchmarks
```

既定値は30、100、500、1000人、seed 2026と2027です。期間を指定しない場合はScenarioの期間を使用します。比較するときは人数・期間・seed・trace level・モデル版・環境をそろえてください。出力先にRun成果物が作られるため、Git管理外の一時ディレクトリを使います。

## 保存付き計算の内訳

上の計測器は `run_scenario()` の単体実行です。UIからの計算はProjectAPIのworkerを通り、30分ごとにCheckpointの検証・保存・索引登録を行います。両者の差と処理別の内訳は、1ケース1プロセスで次のように測ります。

```shell
PYTHONPATH=src SHELTERSIM_LLM_MODE=offline python benchmarks/investigate_performance.py \
  --scenario generated --duration 1440 --route project --output /tmp/sheltersim-perf-project
```

`--route library` は単体実行、`--route project` は `ProjectAPI.submit()` からworker完了までを測ります。`--scenario-file` で承認済みScenarioのローカルコピーを指定できます。生Scenarioには人口情報が含まれる場合があるため、出力先はGit管理外にします。`result.json` の処理別時間は内側の処理を含む累積値なので合計しません。修正前後の比較では `world_hash`、`events_hash`、`metrics_hash` が一致することも確認します。ネットワーク接続は計測中に拒否します。

## 保持した1日計測

次の結果は、コードSHA `19e5cde0a4d59de140582e45b6c9dce3e2b04b7b` の時点で取得した、Linux x86_64・CPython 3.12.14・報告CPU数3の単一seed（2026）結果です。1ケースあたり1440分、trace level `off`、offline modeを使用し、有料API呼び出しはありません。元の作業環境に含まれていたScenarioの絶対パスだけを移管時にリポジトリ相対パスへ置き換えています。値と計測対象は変更していません。

| 人数 | 壁時計 (秒) | CPU (秒) | ピークRSS (MiB) | 成果物 (MiB) |
|---:|---:|---:|---:|---:|
| 30 | 6.29 | 6.28 | 49.8 | 4.27 |
| 100 | 18.09 | 18.08 | 76.7 | 12.15 |
| 500 | 123.71 | 123.56 | 202.1 | 48.44 |
| 1000 | 275.31 | 274.81 | 316.0 | 78.06 |

この結果は各人数につきseedが1つだけなので、seed間のばらつきは分かりません。30人・30日間の別試行は10分以内に完了成果物が得られず停止しました。30日間の完了時間や性能目標は確立していません。

詳細なScenario/state hash、イベント数、trace量、実行環境は[JSON記録](../benchmarks/results/scale-one-day-indexed-2026-10-23.json)と[表形式の記録](../benchmarks/results/scale-one-day-indexed-2026-10-23.md)にあります。過去の性能値とはコードSHAや設定が異なるため、単純な改善率として比較しません。

## 解釈上の制約

- 合成人口、設備数、需要、容量、seedは計測用条件であり、現実の施設構成を表しません。
- ピークRSSは個別プロセスの高水位でPython起動時の分も含みます。壁時計・CPU時間には成果物書き込みを含みます。
- 1000人・30日間、実機上のブラウザー応答性、複数利用者の同時運用は確認されていません。
- CIは小さな計測器のschema/smoke確認を行い、大規模matrixは通常実行しません。
