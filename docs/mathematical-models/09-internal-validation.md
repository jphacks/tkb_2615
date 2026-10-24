# 09. 内部検証

## テストで確認する性質

追加検証は `tests/test_research_models.py`, `tests/test_information_spread.py`, `tests/test_sanitation_diagnostics.py` にあります。既存の挙動検査は行動、待ち行列、資源、移動、checkpoint、provenanceのテスト群で再利用します。

| 領域 | 期待性質 | 確認方法 |
|---|---|---|
| 急性心理 | 無刺激で基準へ平均回帰、回復速度差、定刻みで同じ解析解、持続負荷平衡、値域/有限値 | `mean_reverting_step` と `MeanRevertingDistressPolicy` 単体テスト |
| 中長期心理 | post-social値を1回だけ日内集計、1440分で更新、状態復元 | 日次状態単体・checkpointテスト |
| 感情伝播 | 双方向、無接触ならゼロ、同値ならゼロ、上限、入力順序不変 | Bosse構造policyテスト |
| 情報 | 曝露/信念/共有/訂正を分離、信頼度付き非決定的受容、忘却・共有停止、同seed再現 | evidence-aware scenarioテスト |
| 行動 | 適格候補のみsoftmax、確率和、極端値安定、同seed再現 | 既存behaviorテスト |
| 施設/資源 | FIFO、capacity、完了イベント、在庫reserve/consume/release保存則 | 既存facility/resourceテスト |
| 設備診断 | 稼働unitと初期/長期計画目安、利用回数の算術 | `tests/test_sanitation_diagnostics.py` |

本検証はソフトウェア内部の性質確認です。閾値0.7などは便宜的なシミュレーション指標で、臨床診断閾値ではありません。

## 実行コマンドと結果

### 追加・関連テスト

最新 `main` を起点とする作業ブランチ上で実行しました。

```text
.venv/bin/pytest -q tests/test_research_models.py tests/test_information_spread.py tests/test_sanitation_diagnostics.py tests/test_provenance.py tests/test_policy_registry_full.py tests/test_policy_parameter_overrides.py::test_selected_formula_overrides_reach_runtime_config_and_checkpoint_signature
30 passed

.venv/bin/pytest -q
603 passed, 3 skipped in 610.68s
```

skip 3件は環境ゲートされたlive API試験で、offlineの標準テストから有料APIを呼んでいません。

新規ファイルおよび変更したsocial policyへのRuff検査は `All checks passed!` でした。変更した17個の既存PythonファイルをまとめたRuffでは既存長行・import整形指摘が132件残ります。同じファイルの `origin/main` 版は141件で、file/rule/source-line/messageによる比較で新規指摘は0件、9件を解消しています。`git diff --check` と `model-catalog.json` / `source-manifest.json` のJSON構文検査も通過しました。

全体テストは、社会モデルのprovenance ID互換修正後に最新 `main` 起点の作業ブランチで完走しました。

## 比較実験と感度分析

再実行可能なスクリプトは `scripts/run_research_model_validation.py`、保存結果は [`research-validation-a01.json`](research-validation-a01.json) です。31 runで、12人の合成Agent、180分、同一Scenario/seedの6条件ablation（seed 3, 4, 5）と、seed 3の13条件one-factor感度分析を実施しました。

| 条件 | 急性状態平均（3 seed平均） | p95（3 seed平均） |
|---|---:|---:|
| legacy comparator | 0.1679 | 0.3016 |
| research full | 0.0747 | 0.1541 |
| 自然回復なし | 0.1483 | 0.2852 |
| 感情伝播なし | 0.0765 | 0.1596 |
| スタッフなし | 0.1157 | 0.2105 |
| 訂正なし | 0.0729 | 0.1483 |

one-factor感度では、回復率を0.005から0.02/minへ上げた条件で急性状態平均が0.1005から0.0496へ変わりました。噂の信念受容確率を0から1へ変えた条件では、誤情報を信じる最終Agent数が0から8へ変わりました。各one-factor値は1 seedのため効果量の推定ではなく、実装上のパラメータ応答確認です。

サービス時間分布を8–12分に変更すると、比較条件の4–8分に比べてqueue person-minutesは23から58へ、最終の累積平均待ち時間は1.92分から4.83分へ増えました。この短い合成runでは全ての条件で0.7以上の急性値は発生せず、高負荷episode・再上昇は0でした。したがって本実験はその閾値での心理回復時間を観察できていません。

この規模の合成比較では自然回復を無効にすると状態が高くなりました。社会伝播や訂正の出力差は限定的、またはScenario内の他機構と絡むため、現実の避難所での因果効果として解釈できません。`no-staff-support` はスタッフを空にする介入、`no-correction` は訂正イベントを外す介入です。全係数は未校正です。

## 指標と未実施事項

保存結果は終了時の急性平均/p95、高負荷者割合、日次潜在状態、意思決定数、曝露・誤信念・訂正受容、トイレ基数・待ち人数・完了数に加え、高負荷episode数・最長連続時間・回復時間・再上昇回数、社会的な正負の累積影響、queue待ち分、平均/最大待ち、利用率、長期計画基準に対する設備不足分数を含みます。回復時間は (A\ge0.7) の連続episodeが (A<0.7) に下がるまでの観測分数です。終了時に閾値以上のepisodeは回復時間の平均から除外します。

短いA01 runには物資ledgerがないため、物資不足時間は `null` です。資源保存則は既存の資源serviceテストで別途確認します。7日runはTraceをOFFにしており、高負荷episode・社会影響のTrace指標は `null` とし、仮に0だったかのように扱いません。

## 100人・7日間のrun

再実行コマンド:

```text
.venv/bin/python scripts/run_research_model_validation.py --mode seven-day --output docs/mathematical-models/research-validation-7day-a01.json
```

結果は [`research-validation-7day-a01.json`](research-validation-7day-a01.json) に保存しました。合成100 Agent、10,080分、seed 2026、policyは `mean-reverting-distress-v1` / `expressiveness-channel-openness-v1` / `evidence-aware-rumor-v1`。Scenario hashは `e918499a32ce29fd9f82aa74dc923a851f8a303a0951de0391ea93b9ac92eb13` です。すべてのtickで `WorldState.validate` が通り、最終minuteは10080でした。

| 指標 | 結果 |
|---|---:|
| 終了時の平均急性状態 / p95 | 0.2954 / 0.6671 |
| 終了時 (A\ge0.7) の割合 | 4% |
| 終了時の平均日単位潜在状態 (D) | 0.1229 |
| 噂曝露 / 訂正曝露 / 訂正受容 | 99 / 97 / 96 Agent-message |
| 誤情報を信じるAgent数 | 2 |
| 稼働トイレ / 完了サービス | 4基 / 3,192件 |
| 平均queue長 / queue person-minutes | 0.264人 / 2,658分 |
| 最終平均待ち / 最大待ち | 0.83分 / 29分 |
| 平均設備利用率 | 0.477 |
| 長期計画目安に不足した分数 | 10,080分 |

100人では長期目安が5基、Scenarioでは4基なので全期間不足となります。これは計画目安との比較であり、利用可能性、アクセス可能性、個々の排泄行動を予測しません。高負荷episodeの連続時間と社会伝播Traceはこの長時間runで記録していないため、JSONの値は `null` です。物資ledgerはScenarioに含まれず、物資不足時間も `null` です。

外部データによる係数校正・検証、K6値との対応、現地妥当性は実施していません。
