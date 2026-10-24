# 自然言語解釈のfixture基準値

2026-10-12に記録した、導入済みprimitiveに対する決定的offline基準値です。対象はfixtures/evaluation/unseen_prompts_ja.jsonにある日本語prompt 21件です。物理・施設、資源、環境、群集、行動、社会、未知の7群から各3件を使い、phrase専用parser規則は追加していません。

## 測定範囲

各caseにはfake Providerとresponse tapeから固定済みcompiler responseを与えます。testは厳密なresponse schemaと意味検証を確認します。受理したcaseでは、分離した小規模合成WorldStateにEffectProgramをscheduleし、開始eventを適用して、適用前後のruntime観測値・KPI・資源metrics、event/effect provenance、Checkpoint復元を確認します。元WorldStateはbyte単位で不変です。未対応・確認要求caseはplanもevent/KPI変更も作りません。

固定responseはtest fixtureであり、live Opus生成ではありません。この報告はOpusの精度、productionでの解釈品質・遅延・費用を測りません。network、有料API呼び出しはありません。fake responseはtoken 0、Provider時間0、概算費用0を返します。記載するtest所要時間はローカルfixture評価時間のみです。

## 基準値

| 結果 | 件数 | 割合 |
|---|---:|---:|
| native | 6 | 28.6% |
| composed | 1 | 4.8% |
| approximate | 5 | 23.8% |
| 許可機構でcompile成功 | 12 | 57.1% |
| clarification_required | 1 | 4.8% |
| not_simulatable | 8 | 38.1% |

compileされた12 planはすべてschema/domain検証とruntime validate_effect_program事前検証に合格しました（12/12、100%）。確認または機構不足となった9件はeffectを返しませんでした（誤って成功扱いしたcase 0件）。これはfixture上の機構対応率であり、LLM成功率ではありません。coverage改善は汎用の検証済み機構追加で行います。fixtureの回答だけを変更しても改善とは数えません。

## runtime観測とKPI記録

次の前後値はoffline runtime testでassertされています。表にないKPIはevent時刻でも変化せず、各適用eventはmetricのsource_idsと、生成effect_idおよびcompiler provenanceを保持します。

| case | 期待される結果 / operator | 適用後観測 | KPI/資源差 |
|---|---|---|---|
| physical-toilet-outage | native / facility_outage | 稼働便器4 -> 3基 | toilet_working_units -1、stress不変 |
| physical-add-toilet | native / add_toilet | 稼働便器4 -> 5基 | toilet_working_units +1、stress不変 |
| physical-composed-outage-response | composed / outage + add_staff | 稼働便器4 -> 3基、staff 1人追加 | toilet_working_units -1、stress不変 |
| resource-water-delivery | native / deliver_resource | 水残量1,000 -> 201,000 ml | water_ml_delivered +200,000、water_ml_remaining +200,000 |
| resource-delay-shipment | native / modify_supply_schedule | shipment予定minute 9,000 -> 10,080 | 即時KPI変化なし |
| resource-water-station-outage | native / water_outage | 水サービス停止false -> true | 即時stock、stress、queue変化なし |
| environment-ventilation-noise | approximate / set_ambient_noise | 休息回復倍率1.0 -> 0.65 | stress/KPIへ直接書込みなし |
| environment-sleep-demand | approximate / modify_demand_rate | sleep選択倍率未設定 -> 1.3、間隔倍率1.2 | KPI直接変更なし |
| crowd-water-queue-proxy | approximate / modify_demand_rate | fetch-water選択倍率未設定 -> 1.25 | 新規来訪者や群集物理の結果はなし |
| behavior-staff-absence | native / set_staff_availability | S01勤務可true -> false | 即時staff負荷、stress変化なし |
| behavior-rest-demand-proxy | approximate / modify_demand_rate | rest選択倍率未設定 -> 1.2、間隔倍率1.2 | KPI直接変更なし |
| social-complaint-support-proxy | approximate / modify_demand_rate | seek-help選択倍率未設定 -> 1.4 | 苦情eventやstaff負荷の直接変更なし |
| environment-temperature-unsupported | not_simulatable | plan/eventなし | 変更なし |
| crowd-crush-unsupported | not_simulatable | plan/eventなし | 変更なし |
| crowd-external-arrivals-unsupported | not_simulatable | plan/eventなし | 変更なし |
| behavior-line-cutting-unsupported | not_simulatable | plan/eventなし | 変更なし |
| social-rumor-unsupported | not_simulatable | plan/eventなし | 変更なし |
| social-meal-sharing-unsupported | not_simulatable | plan/eventなし | 変更なし |
| unknown-medical-outcome | not_simulatable | plan/eventなし | 変更なし |
| unknown-stock-ambiguity | clarification_required | plan/eventなし。確認質問を1つ返す | 変更なし |
| unknown-gravity-unsupported | not_simulatable | plan/eventなし | 変更なし |

proxyは限定的です。Zone内action選択や休息回復のmodifierは、来訪者増加、群集移動、医療結果、社会的交流、実測音、苦情を表しません。それぞれの固定responseに限界を記録しています。

## 検証と所要時間

- 評価moduleのtest: SHELTERSIM_LLM_MODE=offline PYTHONPATH=src python -m pytest tests/test_adaptive_event_eval.py -q — **22 passed**。
- compiler/runtime関連test: **49 passed**、0.74秒。
- 当時のshort suite: **343 passed、1 skipped、4 deselected**、14.78秒。skipはlive API gate付きtestです。
- ruff check .、ruff format --check .、git diff --checkは当時pass。
- 評価module自体は0.46秒。fake Providerはlatency/costを発生させません。
- 変更がevaluation fixture/compiler testのみだったため、その時点では7日long_simulation suiteを省略しました。simulation core、Scenario入力、event、Policy、Branch、Checkpoint、replay fixture、test設定は変更していません。

上記は2026-10-12時点の過去記録です。現行HEAD全体のtest結果はtesting.mdを参照してください。
