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
.venv/bin/pytest -q tests/test_research_models.py tests/test_information_spread.py tests/test_provenance.py
36 passed

.venv/bin/pytest -q
632 passed, 3 skipped in 662.77s
```

skip 3件は環境ゲートされたlive API試験で、offlineの標準テストから有料APIを呼んでいません。

新規 `distress_monitor.py` と新規research test fileはRuff検査に通過しました。変更した既存Pythonファイルでは115件のRuff既存指摘があり、同じファイルの作業開始時点は116件でした。rule/message比較で新規指摘は0件です。`git diff --check` と `model-catalog.json` / `source-manifest.json` のJSON構文検査も通過しました。

全体テストは最新 `origin/main` のmerge後に完走しました。全体実行後に追加した2つのテスト条件を含む36件は別途再実行して通過しました。

## A01: 初回比較実験と感度分析（急性心理Policy）

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

## A01: 初回100人・7日間run（統合Policy追加前）

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

## A02: 身体・感情統合版の追試

前節のA01は `mean-reverting-distress-v1` を使った初回比較です。統合Policy追加後の追試は [`research-validation-a02.json`](research-validation-a02.json) に保存し、以下のコマンドで再実行できます。

```text
.venv/bin/python scripts/run_research_model_validation.py --mode sweep --output docs/mathematical-models/research-validation-a02.json --seeds 3 4 5
```

合計34 runです。12人・180分の7条件ablationをseed 3, 4, 5で実行し、seed 3で13条件の一要因感度分析を行いました。`research-full` は `mean-reverting-life-state-v1`、`research-acute-only` は `mean-reverting-distress-v1`、`legacy-comparator` は旧心理・社会・情報Policyです。サービス時間比較は急性のみの同seed対照で実施し、実際に待ち行列が発生する条件を使いました。

| 条件 | 終了時急性状態平均 | p95 | (A≥0.7)最終割合 | 高負荷episode | 最大連続分 |
|---|---:|---:|---:|---:|---:|
| 旧モデル | 0.1679 | 0.3016 | 0% | 0 | 0 |
| 急性心理のみ | 0.0747 | 0.1541 | 0% | 0 | 0 |
| 身体・感情統合版 | 0.3198 | 0.4867 | 0% | 0 | 0 |
| 統合版・自然回復なし | 0.6552 | 0.9728 | 36.1% | 4.67/seed | 48分 |

統合版の平均・p95が急性のみより高いのは、この合成scenarioで身体・感情feedbackが働き、統合版が休息負債・不確実性などを状態更新へ含めるためです。これはモデル間の差であり、現実のアウトカムに対する優劣を示しません。回復率を0.005から0.02/minへ変えるseed 3の条件では平均が0.4194から0.1826へ動き、同seedの基準統合版は0.3019でした。基準の3 seed条件では0.7を超えるepisodeがなかったため、回復時間の分布は得られていません。自然回復なしでは継続・再上昇episodeが現れ、最長48分でした。

Bosse型伝播の正負の影響総量は、基準統合版でそれぞれ0.061、0.157（正規化状態単位の累積）でした。このscenarioでは鎮静方向の影響量が上回りました。伝播なしは平均0.3244で、基準0.3198との差は小さく、実地因果効果とは解釈しません。情報感度では噂の信念形成確率0/1の条件で終了時誤信念者が0/8人、訂正受容確率0.05/0.95で訂正受容agent-messageが0/4件でしたが、急性平均の差は小さいままでした。

設備感度の同seed比較では、急性のみモデルの既定4–8分サービス時間でqueue person-minutesが23、終了時平均待ちが1.92分、最大8分でした。サービス時間を8–12分へ変更すると、それぞれ58、4.83分、12分になりました。統合版の180分runではトイレサービスが発生しなかったため、この設備感度値には急性のみ対照を使っています。

追加した統合テストは高状態0.8からの自然回復とオンライン回復記録、社会同調と自然回復の同時作用、誤情報信念からの心理Trace、訂正受容時のfalse-claim圧力解消、訂正拒否時には安心効果を加えないことを確認します。これらは実装経路の確認であり、係数校正ではありません。

## A02: 100人・7日間の統合版追試

100人・10,080分・seed 2026の `mean-reverting-life-state-v1` を同一条件で2回実行しました。両方とも全tickの `WorldState.validate` と最終化を通過し、最終WorldStateは完全一致しました。結果は [`research-validation-7day-a02.json`](research-validation-7day-a02.json) に保存しています。Scenario hashは `0bd348c6f7810d95f9e3dc4c3c7f4ae5beb31d2a3521b795e4a8f020988c340a`、比較した最終状態hashは `52a720d432444c2813c23651447619148040b81edd8b111697c37d101aba7e7` です。

| 指標 | 結果 |
|---|---:|
| 終了時の急性状態平均 / p95 | 0.8588 / 1.0000 |
| 終了時 (A≥0.7) の割合 | 80% |
| 日単位潜在状態平均 | 0.4140 |
| 誤情報曝露 / 現在の誤信念者 / 過去に誤信念 | 92 / 0 / 1 Agent |
| 訂正受容 | 88 Agent-message |
| オンライン高負荷episode / 再上昇 | 147 / 65 |
| 最長高負荷連続 / 完了した回復episode | 9,906分 / 67 |
| 完了episodeの平均回復時間 | 66.55分 |
| 4基のトイレ利用 / 完了サービス | 利用なし / 0 |
| 長期計画目安に対する不足 | 10,080分 |

急性状態の高値は数値発散ではなく、状態値が `[0,1]` に収まったまま生じた継続負荷です。このScenarioには物資配布ledgerがなく、初期空腹等も7日間続きます。また、life-state policyを有効にすると生成されるAgent facility-awareness状態は初期位置の施設のみを既知とするため、この構成では別zoneにあるトイレが未利用のままでした。したがってこのrunは心理状態・状態不変条件・再現性の長期確認として解釈し、トイレ待ち行列/利用の検証とはしません。後者は短時間の急性のみ対照シナリオで別途確認しました。

Trace artifactは出力せず、Agentごとの高負荷連続時間・回復episodeをonline集計します。1回目は843.27秒（14分03秒）、同seed再実行は787.86秒（13分08秒）で、2回の合計は27分11秒でした。両方の状態hashは一致しました。これはこの実行環境での絶対時間です。A01に対応する実行時間記録がないため、旧版との性能差や「劣化なし」は定量結論できません。外部データによる係数校正、K6得点との対応、現地妥当性は未検証です。

## A03: 施設認知修正後の短時間統合比較

初期状態を「全員sleeping zone、心理Policyはmean-reverting-life-state-v1」とした1人・180分の同seed最小Scenarioで、明示案内なしではトイレ要求候補が出ても完了は0件でした。`initial_known_facility_ids=["toilet_1"]` の案内を与えると、施設への移動、到着時の同zone施設発見、queue/サービスを経て1件が完了し、排泄欲求が無案内条件より低くなりました。テストは案内対象以外の初期知識、設備capacity、心理Policy間の知識一致、Checkpoint復元も確認します。これは原因を切り分けた最小合成ケースです。

通常運営と不足の比較では、両fixtureのseed 2026・同じ100 profileを使って180分だけ実行し、実行窓外の配送eventを外しました。どちらも5 staff、2つのresource service station、施設案内、移動経路と心理係数を共有します。通常は4トイレと初期在庫5,000,000 ml/120食、不足は2トイレと100,000 ml/20食です。

| 180分の指標 | 通常運営 | 不足 |
|---|---:|---:|
| トイレ完了 | 58 | 42 |
| queue person-minutes | 540 | 2,253 |
| 終了時の平均待ち | 9.31分 | 11.26分 |
| 平均トイレ利用率 | 0.464 | 0.703 |
| water残量 | 5,000,000 → 4,994,250 ml | 100,000 → 94,250 ml |
| 食事残量 | 120 → 92単位 | 20 → 0単位 |
| 水または食事が0だった分数 | 0 | 80 |
| 終了時の平均急性stress | 0.3320 | 0.3667 |
| 終了時のp95 / 0.7以上のAgent割合 | 0.5926 / 0% | 0.6422 / 3% |

この3時間では初期在庫差と容量差がqueue、在庫、stressの差に現れました。終端不足Scenarioでは高負荷episodeが3件継続中（合計64 Agent-minute、最長38分）、完了episodeは0件でした。短い実行の値であり、7日間の長期結果と置き換えません。

## C: 一時ショックと自然回復の分離

`scenario_100_agents.json` を1 Agent・180分に切り出し、初期need、need pressure、queue/support入力、社会伝播、情報伝播を0/無効にしました。平均回帰率は0.01/min、環境熱stress入力は0.002/min、熱eventはminute 20から60までです。stressはminute 59で0.0490251、除去から120分後のminute 179で0.0147661でした。`0.0490251 × exp(-0.01 × 120) = 0.0147661` と一致し、最終状態は有限値でした。これは自然回復式とevent lifecycleの機構検査で、現実の熱曝露や人の反応係数を校正しません。

## A03: 施設認知を揃えた34 runの感度・ablation

既存スクリプトを施設認知明示設定付きで再実行しました。

```text
SHELTERSIM_LLM_MODE=offline python scripts/run_research_model_validation.py --mode sweep --output /private/tmp/research-validation-a03.json --seeds 3 4 5
```

12合成Agent・180分の7条件ablationを3 seedで、13条件one-factor感度をseed 3で行いました。全モデル比較で初期案内、設備、乱数条件が揃い、基準設定では各12-Agent runでトイレサービスが12件完了しました。

| 3 seed条件 | 最終平均急性stress | p95 |
|---|---:|---:|
| 旧モデル | 0.1679 | 0.3016 |
| 平均回帰・急性のみ | 0.0747 | 0.1541 |
| 平均回帰・身体/感情統合 | 0.1417 | 0.2805 |
| 統合版・自然回復を0にした介入 | 0.3179 | 0.6069 |

統合版で回復率を0.005/minから0.02/minへ動かした1 seed条件では、平均急性stressが0.1994から0.0814へ変化しました。社会同調なし条件は0.1451（基準0.1417）、3倍伝播率はseed 3で正の寄与0.1110・負の寄与0.2643でした。このScenarioでは正負両方向の差分があり、単純なストレス低下効果とは見なしません。

誤情報感度では、seed 3の低/高伝播設定で終了時の現在誤信念が0/6 Agent、低/高信念形成確率では0/8 Agentでした。訂正受容確率0.05/0.95では訂正受容が0/4 Agent-messageでした。訂正なしと統合基準の平均急性stressは0.1444/0.1417で差が小さく、心理効果の実地因果推定ではありません。トイレ所要時間8–12分の急性のみ対照ではqueue person-minutesが58、平均終了待ち4.83分となり、4–8分対照の11、0.92分から増えました。

この感度分析は施設認知の交絡を避けた内部応答試験です。1 seedの一要因条件は効果量推定でなく、全係数は未校正です。

## A03: 100人・7日間の通常運営/不足Scenario

`research_operational_100_7d.json` と `research_shortage_100_7d.json` をseed 2026・10,080分で `TraceLevel.SUMMARY` 実行しました。いずれも100人、最終tickの `WorldState.validate`、有限stress値を確認しました。Scenario SHA-256は通常 `2a15a9743a5d40d96486f3357e76c2b8e0bf7a10700cab502cbcf5f22b701034`、不足 `97acd2a4b18352c7ed034c4ad82c1f401ff0d503797c198a2ca329b0e264b633` です。これらの一回実行では最終WorldState SHA-256がそれぞれ `9bf8ab1b0830dbb2999453ec36bc51631d67aabae7a1c1fb849ebb6d3b26789a` と `f5ab104362bb0de100a9f4da40e7a79af2c1594a30c6b808791dea076f5faee9` でした。

| 7日指標 | 通常運営 | 不足 |
|---|---:|---:|
| 終了時平均stress / p95 / 0.7以上の割合 | 0.9255 / 1.000 / 94% | 0.9316 / 1.000 / 97% |
| 日単位潜在状態平均 | 0.4535 | 0.4618 |
| 高負荷episode総数 / 完了 / 継続中 | 434 / 340 / 94 | 383 / 285 / 97 |
| 完了episodeだけの平均継続 | 462.41分 | 482.19分 |
| 継続中episode経過分の合計 / 最長 | 675,386 / 9,900分 | 727,711 / 9,938分 |
| 再上昇回数 | 334 | 283 |
| 完了トイレサービス / queue person-minutes | 913 / 548 | 839 / 101,375 |
| 最終平均待ち / 観測最大待ち | 0.60 / 26分 | 0.71 / 26分 |
| 資源サービス完了 / 資源不足分数 | 2,421 / 0 | 2,405 / 1,772 |
| 食事在庫の初期→終了 | 120 → 5,522単位 | 20 → 38単位 |
| 水在庫の初期→終了 | 5,000,000 → 16,544,250 ml | 100,000 → 844,250 ml |
| イベント記録 | 26,742 | 26,505 |

通常運営でも高負荷agent割合は94%まで上がり、stressは多くのAgentで上限1.0付近に達しました。この結果を「正常な避難所では高ストレスにならない」と解釈しません。資源台帳の不足は0分で、トイレ913件・資源サービス2,421件が完了した一方、行動頻度と身体/感情feedbackを含むこのScenarioではneeds負荷が残りました。不足条件は通常よりトイレ処理が74件少ないのにqueue person-minutesが約185倍、資源不足が1,772分となり、運用面の負荷差は明確でした。急性stressの平均差が小さいのは、両条件でstressが上限に近づくためです。係数を下げて見かけ上の差を作らず、この高負荷を未校正モデルの限界として保持します。

Agent×168時間の16,800行の `psychology_contributions_hourly.jsonl` を一時出力からストリーム集計しました。通常運営での7日分の心理成分合計（正規化状態deltaの累積）は、自然回復 -8,624.44、need pressure +3,584.73、life-state feedback +4,396.14、施設queue +1,572.09、staff support wait +19.64でした。positive componentが継続し、自然回復を上回る時間帯があります。終了時stressが1.0に飽和したAgentでは追加の正入力が終端値に現れず、上限clipが蓄積負荷を圧縮して表示することも確認できます。

例として、終了時stress 1.0のA001では7日累積がneed +57.27、life-state +42.82、queue +15.55、staff wait +0.18、自然回復 -97.24でした。A009ではneed +70.20、life-state +45.28、queue +14.35、自然回復 -98.56でした。末尾24時間にも正味の心理入力が残り、自然回復は毎tick動いています。情報不確実性入力と環境stress入力の合計は0で、通常Scenarioの高止まりはそれらの外生入力で説明されません。Summaryでは分単位の既存traceを維持し、Agent別時間別にはraw/applied/social deltaとcomponent totalを別出力します。総clip損失は社会event差分も別に加味するためここでは推定値として報告しません。

通常/不足Summary runのWall timeはそれぞれ1,179.56/1,188.30秒、process CPUは1,168.00/1,171.88秒でした。全JSONL等の一時成果物は506.78/509.14 MB（うち時間別寄与10.63/10.62 MB）で、Gitには登録していません。この時間は統合life-state ScenarioのSummary出力であり、`baseline_100_7d.json` による性能比較とは別です。

通常運営ScenarioをTrace OFFでも再実行し、Summary runと同一のScenario hashと最終WorldState hash（`9bf8ab1b0830dbb2999453ec36bc51631d67aabae7a1c1fb849ebb6d3b26789a`）を確認しました。Trace OFF runは1,123.91秒でした。これはSummary成果物を出さない再現性・Trace中立性確認で、baseline性能計測とは分けます。

## A03: baseline性能測定とTrace sanitizer最適化

同一M1/macOS環境で、`fixtures/scenarios/baseline_100_7d.json`、seed 2026、100 Agent、offline、`TraceLevel.SUMMARY` を使って測りました。1日条件は同fixtureのdurationだけを1,440分に短縮しています。Wall/CPUは秒、RSSはprocess high-water MiB、Traceと成果物容量はMiB（1 MiB = 1,048,576 bytes）です。各条件一回の記述統計で、実行変動を推定する複数seed/反復ではありません。

| 時点・条件 | Wall | CPU | Peak RSS | events | Trace | 全成果物 |
|---|---:|---:|---:|---:|---:|---:|
| 研究branch編集前・1日 | 18.03 | 未記録 | 未記録 | 5,637 | 未記録 | 48.44 |
| 研究branch編集前・7日 | 151.72 | 147.10 | 461.12 | 37,444 | 未記録 | 328.83 |
| 今回変更後・1日 | 14.75 | 14.70 | 128.27 | 5,637 | 7.66 | 49.63 |
| 今回変更後・7日 | 127.79 | 127.47 | 492.91 | 37,444 | 50.60 | 337.05 |

編集前の1日Wall timeは前回作業時の測定記録で、CPU/RSS/trace容量は保存されていません。7日編集前値は同じ研究branchの既存ローカル計測です。Wall timeは1日で約18%、7日で約16%短縮しました。7日CPUは約13%短縮しました。一方、7日Peak RSSは約32 MiB、全成果物は約8.6 MiB増加しました。新設した `psychology_contributions_hourly.jsonl` が今回runで1日約1.25 MiB、7日約10.13 MiBを占めます。trace契約を残したまま、結果追跡用の情報を追加したための容量/RSS trade-offです。

ユーザー提供のmain `906a7d5` 測定は同じfixture・seed・trace levelの別スナップショットとして参照します。mainでは1日15.67秒/15.38 CPU秒/108.03 MiB/45.35 MiB、7日132.12秒/130.28 CPU秒/435.77 MiB/306.90 MiBでした。今回変更後はWall/CPUでそれぞれmain測定より短く、RSSと成果物は大きくなっています。mainと研究branchの結果は同一commitの前後比較として扱いません。

1日Summary runのcProfileでは、最適化前の `_sanitize` が累積13.76秒を占めました。キー名の機密フィールド判定を256項目LRU cacheで再利用し、出力内容・redaction条件・Trace項目を変えない修正を行いました。同じプロファイル条件で `_sanitize` は累積4.58秒（約67%減）、`record_psychology_trace` は12.88秒から5.10秒になりました。非profileの同条件1日runでも同一の最終state hash、イベント数、Trace byte数を保ったまま、Wall timeは変更前17.04秒から14.75秒へ短縮しました。Profile付きWall timeはPython profilerの計測負荷があるため、非profile値との速度比較に使いません。

profile後も `WorldView.from_world`（累積約7.3秒）、`support_wait_reason_codes`（約3.7秒）、`WorldState.validate`（約2.8秒）が観測上の処理時間を占めます。これらは複数の走査や全tick状態検証を含むため、本変更では意味や検証頻度を変える危険を避けて触れていません。Agent人数方向の計算量や大規模避難所の性能はこの100-Agent・単一seed・時間方向のbenchmarkから結論しません。
