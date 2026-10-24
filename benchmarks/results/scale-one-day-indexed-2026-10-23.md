# オフライン性能計測結果

結果は、この実装と合成workloadを測ったものです。実際の避難所運営や心理結果の証拠ではありません。

- 環境: Linux-6.18.55-x86_64-with-glibc2.41
- Python: CPython 3.12.14
- Git SHA: 19e5cde0a4d59de140582e45b6c9dce3e2b04b7b
- Git worktree: clean
- source tree hash: aedc603ac5704d09e3fa703c2226aec812bdd180c86be592c8a9bb2575b8b604
- Scenario source: fixtures/scenarios/baseline_100_7d.json
- 各Run期間: 1440分
- trace level: off
- 実行mode: offline。有料API呼び出しなし。
- model version: behavior=utility-softmax-v1、psychology=causal-stress-v1、space=shortest-travel-time-v1、toilet_service=toilet-service-uniform-v1

## Runごとの結果

| 人数 | seed | 壁時計 (秒) | CPU (秒) | ピークRSS (MiB) | event/tick | trace (MiB) | 成果物 (MiB) | Scenario hash |
|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 30 | 2026 | 6.29 | 6.28 | 49.8 | 1.3903 | 1.81 | 4.27 | 384bb5616406 |
| 100 | 2026 | 18.09 | 18.08 | 76.7 | 3.9146 | 5.26 | 12.15 | 9323a79a5190 |
| 500 | 2026 | 123.71 | 123.56 | 202.1 | 11.4701 | 15.57 | 48.44 | 0c3fe666e220 |
| 1000 | 2026 | 275.31 | 274.81 | 316.0 | 16.0542 | 21.84 | 78.06 | b598bd89bfcc |

## seed別要約

| 人数 | seed数 | 平均壁時計 (秒) | 壁時計SD (秒) | 平均ピークRSS (MiB) | 平均event/tick | 平均trace (MiB) |
|---:|---:|---:|---:|---:|---:|---:|
| 30 | 1 | 6.29 | なし | 49.8 | 1.3903 | 1.81 |
| 100 | 1 | 18.09 | なし | 76.7 | 3.9146 | 5.26 |
| 500 | 1 | 123.71 | なし | 202.1 | 11.4701 | 15.57 |
| 1000 | 1 | 275.31 | なし | 316.0 | 16.0542 | 21.84 |

ピークRSSは個別processのhigh-water RSSでPython起動分も含みます。成果物容量はRunが書き込んだ全fileの合計です。壁時計とCPU時間はsimulationと成果物書込みの両方を含みます。trace量は予定/実行済みevent、decision、social decision、contact traceを別に測っています。

人数ごとにseedは1件だけです。seed間のばらつき、因果効果、統計的有意性を主張できません。
