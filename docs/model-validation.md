# モデル値の出典と検証範囲

このシミュレーターの数値は、避難所運営の選択肢を検討するための仮説です。実在する避難者の心理、健康、安全性や個別行動を予測するモデルとして検証済みではありません。根拠資料が運営上の課題を示していても、その記述から係数や処理時間を直接推定したとは扱いません。

## 出典台帳

出典・版・URL・用途・限界は [`fixtures/source-manifest.json`](../fixtures/source-manifest.json) に記録しています。資料は一次発行元の内閣府防災ページから確認し、2026-10-11 に参照しました。

| ID | 分類 | 資料 | この実装で使う範囲 |
|---|---|---|---|
| `SRC-CAO-SHELTER-GUIDELINE-2024-12` | literature | [避難所運営等避難生活支援のためのガイドライン（令和6年12月改定）](https://www.bousai.go.jp/taisaku/hinanjo/pdf/2412hinanjo_guideline.pdf) | 物資管理、トイレ、健康管理、要配慮者対応を運営上の検討項目として扱う根拠。個人係数の測定根拠ではありません。 |
| `SRC-CAO-TOILET-GUIDELINE-2024-12` | literature | [避難所におけるトイレの確保・管理ガイドライン（令和6年12月改定）](https://www.bousai.go.jp/taisaku/hinanjo/pdf/2412hinanjo_toilet_guideline.pdf) | 断水、停電、下水・浄化槽の破損、既設便器の被害でトイレが使えなくなる事象があり得ることを確認。今回の設備停止は一般化した合成介入です。 |
| `SRC-CAO-NOTO-SHELTER-INTERVIEWS-2026-03` | interview | [令和6年能登半島地震における避難所運営実態調査報告（令和8年3月）](https://www.bousai.go.jp/taisaku/hinanjo/pdf/r6notojishin_hinanjo_chousa.pdf) | 奥能登4市町の5事例を、自治体職員・住民・NPO等へのオンラインまたは対面ヒアリングで記録。運営主体や支援連携の事例理解に使います。 |

今回参照した資料の中に、Agentのstress/need時系列やトイレ1回ごとの処理時間を測定したデータセットはありません。したがって、`measured` に分類する資料や、実測済み係数はありません。ヒアリング報告は限られた事例の質的資料であり、全避難所への頻度・因果効果の推定には使いません。

## 実装値の分類

主要な仮定は `fixtures/source-manifest.json` の `assumptions` にモデル版・対象・値または実装参照・校正状況と一緒に記録します。CI の `tests/test_provenance.py` は心理係数を `PsychologyParameters` と、行動・トイレ値を実装および ScenarioSpec と照合します。

| 対象 | 現在の扱い |
|---|---|
| 人物プロフィール | `synthetic-balanced-v1` が生成する合成人物。初期のトイレ urgency・空腹・疲労、stress 感受性、支援ニーズや移動特性の分布は、実在個人や避難所名簿から推定していません。 |
| need / stress 遷移 | `causal-stress-v1`。need pressure・施設queue待ち・staff支援待ち・休息回復を別成分で計算し、`psychology_contributions.jsonl` に原因と係数を記録します。係数はScenarioSpec v3の `model_configs.psychology` で上書きでき、既定値と各scenarioの値は `ASM-H04-CAUSAL-STRESS-V1` に対応します。resource-service opt-in時のhydration growthはH02の `model_configs.resource_service` と `ASM-H02-RESOURCE-SERVICE-V1` が引き続き定義します。いずれも未校正の仮定です。 |
| 家族・Zone接触によるstress伝播 | `sparse-family-zone-contact-v1`。明示的な `model_configs.social_propagation.enabled` がある場合だけ動作し、`ASM-SOCIAL-PROPAGATION-V1` の接触上限・伝播率・自然回復・cooldownは実験用仮定です。ON/OFF比較はコード・係数の挙動確認であり、心理効果の実証ではありません。 |
| 行動選択 | `utility-softmax-v1` は適格候補をutilityとpriorityに基づき選択します (`ASM-UTILITY-BEHAVIOR-V1`)。`priority-rule-v1` は同じ適格条件・utility係数を使い、urgency priority、utility、同点時だけkeyed抽選の順で選びます (`ASM-PRIORITY-RULE-BEHAVIOR-V1`)。両方とも未校正のルール仮定で、行動頻度から推定した値ではありません。 |
| トイレサービス | `toilet-service-uniform-v1` の4〜8分の離散一様分布は `ASM-TOILET-SERVICE-DURATION-V1` に記録した仮定で、実測時間ではありません。 |
| スタッフ支援 | `staff-support-fifo-aging-v2` は高優先度依頼を先に扱い、通常依頼は設定した待機時間を超えるとFIFOで優先します。既定の昇格待ち30分、タスク10分、未解決期限90分、再要請cooldown30分は `ASM-STAFF-SUPPORT-LIFECYCLE-V1` の未校正仮定です。技能を満たすスタッフがいない待機依頼数と依頼分数を記録します。資料は業務・役割の文脈だけを支え、時間値を測定していません。 |
| Zone routing | `shortest-travel-time-v1` はZone edgeに宣言された分数の合計を最小化し、同値はedge IDで決定します。edge分数はScenario入力で、歩行速度の実測値ではありません (`ASM-ZONE-ROUTING-V1`)。 |
| 任意stress伝播 | `sparse-family-zone-contact-v1` の全係数と既定OFFは `ASM-SOCIAL-PROPAGATION-V1` に記録します。係数は合成テスト用で、心理的伝播の根拠資料はありません。 |
| 任意information spread | `sparse-information-contact-v1` は `ASM-INFORMATION-SPREAD-V1` に記録した接触数、trust/家族/Zone倍率、伝達率、問い合わせ圧力で動作します。すべて未校正の実験仮定で、真偽・belief・到達率は合成scenario内だけの状態です。既定OFFの回帰確認と、短いON scenarioで伝達・訂正・support request・Checkpointを検証します。 |
| Haiku social候補 | stress閾値0.65、agent cooldown240分、1日当たりAgent別3/global4候補、イベント遅延1分は `ASM-EVENT-TRIGGERED-SOCIAL-LIMITS-V1` に記録する選択・費用上限です。閾値を臨床的尺度として扱いません。 |
| Haiku social interaction | 30分待機後の苦情応答、同一groupの相互支援、false claimの確認依頼が固定効果として記録されます (`ASM-EVENT-TRIGGERED-SOCIAL-INTERACTIONS-V1`)。reason/actionは列挙値で、架空会話や事実の追加を生成しません。効果係数は未校正の仮定です。 |
| 給水・食料配給サービス (H02) | `resource-service-v1` のサービス時間、1回量、hydration成長/閾値/relief、hunger閾値/relief、utility係数、staff skill、FIFO再試行とstaff/slot規則は `ASM-H02-RESOURCE-SERVICE-V1` に記録した合成仮定です。H02テストの初期stockは `ASM-H02-TEST-INITIAL-STOCK-V1`、stall理由は `ASM-H02-QUEUE-STALLS-V1` に分けて記録します。これらはソフトウェアの因果経路/不変条件の検証用で、摂取量・提供時間・在庫量や現実の配給手順を推定する根拠ではありません。 |
| H05 固定デモscenario | 100人×7日のS1物資遅延、S2断水、介入の施設容量・staff配置・stock量・時刻は `ASM-H05-OPERATIONAL-CAPACITY-V1` / `ASM-H05-OPERATIONAL-STOCK-V1` / `ASM-H05-OPERATIONAL-SCHEDULE-V1` / `ASM-H05-INTERVENTION-V1` に記録した合成条件です。ガイドラインは課題の検討文脈のみを与え、これらの値やKPI変化を校正しません。 |
| Day 3 の設備停止 | minute 2880 にトイレ2基を止める合成ストレステストです。停止時刻と規模は実災害事例の再現値ではありません。 |
| Zone、スタッフ、設備 | 人数・定員・辺ごとの移動時間は動作検証用 fixture 値です。 staffing ratio や標準定員の提案として解釈しません。 |
| Inspector fallback地図 | 座標未設定時の4列gridは `ASM-SYNTHETIC-ZONE-DISPLAY-V1` に記録する表示用座標です。シミュレーションや経路計算には使いません。 |

根拠資料とモデル仮定の関係は「この種類の運営課題をシナリオで調べる理由」に限られます。例えばトイレガイドラインは、断水や下水設備の破損がトイレ利用を妨げる状況を説明していますが、fixture の停止時刻、何基が停止するか、Queue wait やAgent stress の変化量を予測するものではありません。

各 Scenario fixture は適用する `model_assumption_ids` を宣言し、各 `model_configs` と予定イベントにも対応する `source_id` を持ちます。Run manifest は実際のpolicy version・有効値・source IDを記録し、実行イベントとDecision traceにも同じIDを残します。Inspector の合成座標には「表示のみ」のsource IDを付け、移動・隣接・距離として解釈されないよう区別します。

## 検証方法と限界

- 同一の固定 ScenarioSpec・seed・policy version は同じ canonical state、event trace、KPI を生成することをテストします。
- 施設故障、待ち列、サービス、復旧は状態不変条件・イベント provenance・メトリクスを検証します。
- `tests/test_provenance.py` は全主要係数、fixture/model/event source ID、policy trace、Run manifestの有効値を台帳に照合します。
- H02 resource-service係数とテスト初期stock/stall reasonは対応するmanifest entryと照合し、enabled runのmanifestと`resource_service.json`にもversion/sourceを記録します。これは設定・実装整合性の検査であり、校正検証ではありません。
- 変更前後の仮説比較は複数 seed を用い、平均とばらつきを記録します。単一 seed の結果は記述的な例であり、因果効果や統計的優位性とは呼びません。
- A08 policy comparison は同じseed/Scenario inputsでpolicy variantだけを替え、action count、KPI、記録済み事前decision needsを比較します。支援ニーズ上位quartileと移動支援フラグのcohortは、Agentごとの記録need平均を等重みで集計します。これは分単位の未充足時間でも、妥当性確認済みの公平性指標でもありません。結果はシミュレーション内部の記述的比較です。
- 実測データを将来導入する場合は、集計単位、観測期間、選択方法、欠測、データ利用承認、秘匿化方法を記録し、実測・文献・ヒアリング・仮定を分けます。個人データや原ヒアリング記録は fixture や Git に登録しません。

現時点では、結果は仮説の内部整合性と実装経路の検証に限定します。現実の避難所の安全性、住民の心理、被災者支援の効果が実証されたと結論づけません。

## A07 小規模感度分析

`scripts/run_model_sensitivity.py` は、固定済みの100人プロフィールと7日間（10,080分）の Scenario を使い、seed 3・4・5を同じ条件のまま対応付けて比較します。プロフィール生成seedは2026です。各ケースで変えるモデル係数は一つだけで、条件を選んだ後に結果を見て値を調整しません。18 run の実行結果とSHA-256は [`model-sensitivity-a07.json`](model-sensitivity-a07.json) に記録します。

| ケース | Scenario | 変更する値 | assumption / model version |
|---|---|---|---|
| H04 stress pressure | `failure_day3` | `model_configs.psychology.stress_pressure`: 0.0 / 0.012 | `ASM-H04-CAUSAL-STRESS-V1` / `causal-stress-v1` |
| 家族・Zone stress伝播 | `water_outage` | `model_configs.social_propagation.enabled`: false / true | `ASM-SOCIAL-PROPAGATION-V1` / `sparse-family-zone-contact-v1` |
| false claimの伝達 | `water_outage` | `model_configs.information_spread.base_transmission_probability`: 0.0 / 0.2 | `ASM-INFORMATION-SPREAD-V1` / `sparse-information-contact-v1` |

誤情報ケースは両条件で同じ情報伝播機構を有効にし、minute 2880に合成のfalse claimを一件投入します。比較するのはその主張の伝達確率だけです。このイベントとプロトコルは `ASM-A07-SENSITIVITY-CASES-V1` に記録した仮定で、実際の噂や被災者の発言ではありません。

レポートは最終時点の対応seedごとのmetric差を示し、baseline・variant・paired deltaごとに平均、標本標準偏差、経験分位点（p05/median/p95）と差の方向別seed数を出します。3 seedは小さな記述的サンプルです。信頼区間、有意差、実世界の効果量、係数の校正結果として読み替えません。`source_type` は係数・分析設計とも `assumption` です。ガイドラインは運営上の背景だけを与え、係数や変化量の実測根拠には使いません。ヒアリング資料は運営事例を理解する質的情報として台帳に分けており、数値化した観測とは扱いません。

再実行したScenarioのcanonical hash、model-config hash、最終WorldState hashと、seed・対象係数を固定文字列に置き換えた共通input hashをレポートに含めます。短い同seed反復テストはcanonical WorldState、metric CSV、実行event trace、final state hashが一致することを検証します。

### 実行結果

3ケース×2条件×3 seedの18 runはofflineで94分12秒（worker 2つ）でした。この時間は、100人×10,080分の受け入れ条件に必要な計測のみを実行した所要時間です。全paired deltaは [`model-sensitivity-a07.json`](model-sensitivity-a07.json) にあり、下表の値はseed 3、4、5の順です。

| ケース | metric | seed 3 | seed 4 | seed 5 | 読み方 |
|---|---|---:|---:|---:|---|
| H04 stress pressure | `stress_mean` | +0.7096 | +0.7244 | +0.7439 | 3 seedすべて増加。モデル係数の高い感度を示すが、現実の校正根拠ではない。 |
| H04 stress pressure | `stress_high_count` | +73 | +74 | +77 | 3 seedすべて増加。 |
| social propagation | `stress_mean` | -0.0005 | -0.0005 | -0.0005 | 3 seedで同じ小さな減少。 |
| social propagation | 残食数（meal units） | -9 | -16 | -6 | 3 seedすべて減少。 |
| false-claim transmission | 到達Agent数 | +99 | +99 | +99 | 各seedで発信Agent以外の99人に到達。これは合成接触networkと設定した伝達確率の結果。 |
| false-claim transmission | support requests | +1 | 0 | +6 | seed間で異なる。 |
| false-claim transmission | correction count | 0 | 0 | 0 | このScenarioには訂正イベントを設定していない。 |

これらはこの実装、合成Scenario、固定seedでの条件付き出力です。特にstress-pressureの大きな差は係数感度を示し、係数が妥当であることや現実の避難者の反応を示しません。社会伝播時の一部KPIは小さいかseed依存で、false claim到達数は仮定したnetworkとイベントから機械的に生じます。出力と入力hashを今後変更せず保存し、別の係数範囲や実測データを導入した場合は別分析として記録します。

## 交換モデルと評価の読み方

`contracts/model-catalog.json` は登録済み演算器の式・参照文献を定義し、
`model_configs.policy_registry` で心理・社会・情報の実装を交換できます。
論文から着想した演算器と、避難所向けの未校正係数は別物です。
参照文献は係数の実測根拠として扱いません。

`sheltersim model-catalog --scenario <ScenarioSpec>` は有効モデル、係数、
単位・範囲、出典、適用条件と以下の凡例を返します。
A は対象・単位・期間の一致する実測、B は文献/行政資料による文脈、
C は合成条件・モデル仮定です。現時点の心理/行動/社会係数は C です。
`GET /api/v1/projects/{p}/runs/{r}/evidence` も同じ情報を返します。

`sheltersim evaluate-models --scenario <ScenarioSpec> --plan <plan.json>
--output <new-directory>` は明示的に依頼されたオフライン行列を実行します。
plan は schema_version:1、seeds、variants（id/policies/parameters）、metrics。
parameters は登録済み `psychology.stress_pressure` 等のみを受け付けます。
全条件で初期プロフィールを固定し、各seedの乱数キーを対応させます。
各Runの設定・モデル版・scenario/state hash・出典を残し、先頭variantを
基準に最終tickのKPI差を比較します。既存結果を上書きしません。

複数seedの平均差・標本標準偏差・近似95% Student-t区間を記録します。
単一seedでは区間を出さず不確実とします。この区間は選んだseedによる
試算の変動だけを扱い、構造モデルの不確実性や現実の因果効果を含みません。
`contracts/validation-datasets.json` は内部範囲検査の合成データと、
将来の外部観測を区別します。現時点で対応する実測結果データは未登録です。
適用条件の違う資料は not_applicable、指標欠損は missing_metric とし、
範囲内であることだけで外部妥当性の成立とは表示しません。

3つの対応seedを使った小規模なモデル比較結果は
[`benchmarks/results/paired-models-small-synthetic-2026-10-23.md`](../benchmarks/results/paired-models-small-synthetic-2026-10-23.md)
に保存しています。8人・1日・合成条件の記述的な比較で、実測データによる
検証でも、統計的・実世界の因果効果の証明でもありません。再現用JSONも同じ
ディレクトリにあります。
