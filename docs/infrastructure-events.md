# インフラ障害event model

infrastructure modelはopt-inです。Scenarioのmodel_configsに設定を追加します。省略したScenarioは従来の挙動と正準形式を保ちます。

~~~json
{
  "schema_version": 1,
  "policy_version": "infrastructure-events-v1",
  "source_type": "assumption",
  "source_id": "ASM-A03-INFRASTRUCTURE-V1",
  "dependency_edges": [
    {"dependent_id": "toilet_1", "dependency_id": "power-panel-a"},
    {"dependent_id": "water_station_a", "dependency_id": "power-panel-a"}
  ],
  "heat_stress_per_minute": 0.001,
  "cold_stress_per_minute": 0.001
}
~~~

edgeはdependent componentがdependencyを必要とすることを表します。edgeは安定IDを持ち、facilityへ向かう非循環graphでなければなりません。型付きpower_outageはcomponentとrun minuteを指定し、simulationはgraphをたどって依存facilityだけを使用不可にします。equipment_failureはfacilityとunit数を指定します。equipment_repairedとpower_restoredは原因eventに結び付いた復旧eventです。並行障害を個別管理します。facilityを覆うすべての障害が終了した場合に、発生前のworking capacityへ戻します。active facility占有やavailability modifierと競合する障害は、stateを一部変更せず理由を記録して失敗します。

staff_shift_changeは既存staff勤務不可lifecycleを使います。active workはH03取消規則に従い中断し、shift終了時にstaff_restoredを予定します。supply_route_interruptionは対象shipment IDを列挙します。指定shipmentに既存delivery_delayed mechanismを適用し、因果route eventとsupply_route_restored完了eventを記録します。どのshipmentがrouteを使うか自動推定しません。Scenario内でdelayできないshipmentを含む要求は、state変更前に拒否します。

extreme_heatとextreme_coldは実験用のZone単位stress入力です。rateはminuteあたりの正規化stress単位で、各Agentの既存stress感受性を掛け、stressor event IDと仮定source IDとともに心理traceへ記録します。既定rateは暫定的なソフトウェア仮定で実測値ではありません。期限後の新規stress入力は停止しますが、過去のstress履歴は残ります。温度、疾病、thermal comfort、建物設備、緊急時安全性をモデル化しません。

開始eventのEventKind、source_id、生成された完了eventのcausation_idをevent queue/logとWorldState Checkpointに保存します。安定したINFRASTRUCTURE_START_EVENT_TYPESとINFRASTRUCTURE_COMPLETION_EVENT_TYPESはScenarioや将来の自然言語候補に使えるevent名を示します。自然言語previewがeventを使う場合は、対象と期間を独自に検証する必要があります。event名があるだけで効果を保証しません。

このmodelは特定避難所、電気設備、気象暴露、勤務契約、配送網に対して校正されていません。結果は設定したsimulationの出力であり、実世界の安全予測として表示してはいけません。
