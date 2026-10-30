# EffectProgram runtime

EffectProgramの版付き契約とoffline interpreterはsheltersim.events.effectsが管理します。LLM/compiler層は同じPydantic契約を作れますが、runtimeはProvider、HTTP client、UI、生成コードをimportしません。opはDEFAULT_PRIMITIVE_REGISTRYの検索keyです。未知のoperatorや未宣言parameter fieldは意味検証で拒否します。

## 導入済みprimitive

| Operator | 対象と単位 | 前提と動作 |
|---|---|---|
| set_facility_availability | 施設1件、facility_unit。working_unitsは0–1000 | 有効期間中の稼働単位数に上限を設けます。現在の値より稼働可能数を増やさず、使用中の単位数より下げません。 |
| facility_outage | 施設1件、facility_unit。unitsは1–1000 | 互換用形式で、上限付き稼働単位数を減らします。同じ可逆modifier stackを使います。 |
| deliver_resource | 資源ID1件、そのIDで定義された単位 | resource_ledger_v1が必要です。正の数量を既存の台帳handlerで一度適用します。 |
| modify_supply_schedule | 既知のshipment 1件、minute | operational_events_v1が必要です（生成Scenarioは持ちます。#65）。遅らせる納入がない4日未満の生成Scenarioでは、対象がないので適用できません。既存handlerで未到着shipmentを遅らせる一回操作です。 |
| set_staff_availability | staff 1人、staff_member | operational_events_v1が必要です。生成Scenarioは期間によらず持ちます（#65。それ以前は4日以上の場合だけ）。既知staffをend_minuteまで勤務不可にし、期限時に復帰します。不在の資格者を待つ支援依頼は、KPI「勤務していない資格者がいる支援待ち依頼分数（時間外・不在）」（`support_wait_staff_off_shift_request_minutes`）に数えます。このKPIは、#74で勤務時間外だけから不在にも広げました。 |
| water_outage | water station 1台、または設備があるZone。water_supply | operational event stateとWorldState v6のresource_service_v1が必要です。生成Scenarioは期間によらず両方を持ちます（#65）。既存の停止・復旧handlerを使います。 |
| set_ambient_noise | zone 1件、normalized_noise_0_1。levelは0–1 | 任意導入のzone_environment_v1 policyが必要です。既存心理Policyによる休息・睡眠中の疲労回復を変えます。stressやKPIへ直接書きません。 |
| modify_demand_rate | zone 1件、relative_action_rate。確率とrest/sleep決定間隔の倍率は0.5–2 | 任意導入のzone_environment_v1 policyが必要です。Zone内の1 actionに対するsoftmax選択重みを変えます。rest/sleepの決定間隔は設定範囲に制限します。water/meal actionにはresource_service_v1も必要です。 |
| add_staff | zone 1件、staff_member。countは1–100 | 決定的IDで通常支援staffを追加します。総staff数は100人以下です。一回限りの構造変更です。 |
| add_toilet | zone 1件、toilet_stall。countは1–20 | 1ユニット設備を追加します。設備総数は200以下です。一回限りの構造変更です。agent_dynamics v3のScenarioでは、追加したトイレを在所者全員へ知らせます（v2以前では既存の在所者は知りません）。図面のあるScenarioでは`geometry_overlay` schema_version 4が必要で、生成器と同じ格子規則で空き位置に置きます。置けない場合や版4未満の場合は適用不可です。 |
| add_temporary_space | zone 1件、person_capacity。people_capacityは1–1000、sleeping_capacityは0–1000でpeople_capacity以下 | `simulator_extensions.enabled`で作られるtemporary_spaces_v1が必要です。新しく生成するScenarioは有効にします（#80。それ以前に保存したScenarioは対象外）。end_minuteまでZoneの定員を増やし、到着者の受け入れ判定に使います。図面上の部屋は作りません（部屋は`temporary_room_geometry`）。Zoneごとの一時定員は予定中の分を含めて1000人までです。 |
| staff_task_order | 待機中のstaff 1人、staff_task_order | 清掃・保守・情報提供・受付・巡回のどれかを、いまの時刻に1件指示します。職員は経路を歩いて作業し、完了を記録します。技能のない職員への指示は承認前に却下し、理由を日本語で返します。誰も技能を持たなければ`no_qualified_staff`（担当変更`set_staff_role`、保守なら修理の手配`facility_repair`を案内）、ほかの職員が持っていれば`staff_lacks_task_skill`（その職員を挙げる）です。生成Scenarioには清掃・保守の技能を持つ職員がいません（#75）。 |
| transfer_equipment | 図面上の位置を持つ待機中の設備1台、metre_2d | geometry_overlay_v1が必要です。つながった別のZoneへ設備とその提供地点を恒久的に移します。給水所と配食所は、同じZoneにいる担当技能（`resource_service`の`water_staff_skill`・`meal_staff_skill`）の職員だけが受け持つので、移設先にその職員がいなければ承認前に却下します（#83）。職員は設備と一緒には移りません。 |

zone_environment_v1は任意設定で、既存Zoneに限定されます。編集係数は仮定のsource IDを持ち、範囲を制限します。設定を省けばoperatorはcompiler previewから外れ、追加runtime stateも作りません。実測音、臨床的睡眠障害、群集移動、照明、温度、情報伝播、施設固有需要、任意のmodel config変更は表現しません。対応Policyが追加されるまでは未対応です。

## 検証とschedule

validate_effect_program(world, program)は、対象、parameter model、単位、導入済みPolicy、時刻、効果数、queue上限、guard対象、使用中設備、effect間の競合を変更なしで検証します。

schedule_effect_program(world, program)は事前検証を再度行い、分離した候補WorldStateを作成します。開始・期限eventを追加し、水停止用stateを更新して候補全体を検証した後に一括commitします。不正effectがあれば元stateは変わりません。BranchRuntime.apply_effect_programも同じ操作を行い、program hashと予定event IDをBranch command historyに記録します。

## 承認済みCheckpointからのBranch

POST /api/v1/demo/branch-planは、正確なDraftPlan preview hash、source Branch ID、idempotency keyに結び付いたApprovedPlanを受け取ります。このreceiptは利用者がpreviewを明示確認した後にだけclientが作成する想定です。serverはpreviewとの対応を検証しますが、誰がクリックしたかは認証しません。

source Branchはpauseまたは完了状態である必要があります。要求した分岐minute以前の最新Checkpointを復元し、共有履歴をreplayしてからprogramをscheduleします。全programの検証とreplayが成功してからchildを作成します。却下時にsourceやBranch一覧は変わりません。

過去minuteからの分岐では、そのminuteにscheduleし、sourceの現在時刻までreplayします。将来の計画はsourceの現在時刻に留まり、eventをqueueへ追加します。応答にはsource、origin、scheduled、final Checkpoint、program receipt、approval hash、lineageを含みます。child Checkpointはidempotency keyのhashと結果を保存し、生keyや元文章は保存しません。同一承認を再送すると既存childを返し、異なるpreviewでkeyを再利用すると拒否します。guard state、期限event、安定event ID、causation IDはWorldState/Checkpoint契約で復元されます。

Program ID、effect ID、modifier IDは安定しています。eventにはeffect ID、source ID、可能な場合は親原因、compiler種別・版、source文章SHA-256、仮定、単位、依存Policyを記録します。元文章はWorldStateへ複製しません。資源配布、shipment遅延、staff不在、水停止は既存の型付きhandlerが処理します。

施設稼働modifierは、有効な稼働単位数のうち最も厳しい上限を適用します。各期限は自身のmodifier IDだけを除き、残りの上限または保存した基準値に戻します。modifierと競合する直接の故障・復旧やBranch availability変更は拒否します。staff不在と水停止には明示的な復旧eventがあります。一回限りの操作には期限を指定できません。

GuardExpressionは上限付きASTです。最大20 node、深さ4、固定fieldと比較・boolean operatorのみを許可します。dispatcherはASTを直接解釈し、Python eval、コード実行、動的import、event trigger chainを使いません。guardがfalseなら抑止eventだけ記録し、対象subsystemは変えません。`target.*`と`zone.occupancy`は、その効果の対象（施設・区画）で評価します。図面の変更の操作は対象をpayloadの`target_id`に持つので、それも対象として読みます（#194）。`eq`・`ne`は、数値（整数・小数）どうしなら数として比べ（`0`と`0.0`は等しい）、真偽値と数値は等しいとみなしません（#196）。開始がguardで抑止された効果の期限切れも、抑止として記録します（環境の変更を含む。#195）。

任意のeffect_program_v1とzone_environment_v1 stateはWorldState、したがってCheckpointに保存されます。EffectProgramを含むCheckpointはmodel_versionsにeffect_program_interpreter=1を記録します。任意stateがないWorldState v5/v6とCheckpoint v1・v2も互換性を保ちます。
