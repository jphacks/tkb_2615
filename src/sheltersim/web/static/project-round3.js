/* Round 3/4 views on the shared Project, history, comparison, usage and asset APIs. */
(() => {
  "use strict";
  const mode=document.body.dataset.mode;
  if(!["demo","admin"].includes(mode)||new URLSearchParams(location.search).get("legacy")==="1")return;
  const $=id=>document.getElementById(`ws-${id}`);
  const encode=encodeURIComponent;
  const projectId=()=>$("project").value.trim();
  const apiRoot=()=>`/api/v1/projects/${encode(projectId())}`;
  const state={project:null,runRecords:[],runDetails:new Map(),branchControls:[],comparison:null,tracks:new Map(),managementSerial:0,auditSerial:0,auditPage:null,comparisonSerial:0,branchBuildSerial:0,divergenceSerial:0,conditionDiffSerial:0,currentView:null,historyTrail:null,historyTrailScope:null,divergence:null,conditionDiff:null,experiment:null,experimentResults:[],experiments:[],householdPreview:null,facilityCandidate:null,facilityDesign:null,servicePoints:[],modelContracts:null};
  const colors=["#267baf","#c66d3d","#688d48","#8a65a7","#ba9d2c","#4e9b9a","#c25778","#6d7484"];
  const setStatus=(id,message)=>$(id).textContent=message;
  const text=(parent,tag,value,className)=>{const node=document.createElement(tag);node.textContent=value==null?"未記録":String(value);if(className)node.className=className;parent.append(node);return node;};
  async function api(url,body){
    const response=await fetch(url,{method:body===undefined?"GET":"POST",headers:{Accept:"application/json",...(body===undefined?{}:{"Content-Type":"application/json"})},...(body===undefined?{}:{body:JSON.stringify(body)})});
    let data={};try{data=await response.json();}catch{}
    if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);
    return data;
  }
  function query(values){return new URLSearchParams(Object.entries(values).filter(([,value])=>value!==undefined&&value!==null&&value!=="")).toString();}
  function options(select,rows,selected=[]){select.replaceChildren();for(const row of rows){const option=document.createElement("option");option.value=row.value;option.textContent=row.label;option.selected=selected.includes(row.value);select.append(option);}}
  function bind(id,event,fn){$(id).addEventListener(event,e=>Promise.resolve(fn(e)).catch(error=>{setStatus("r3-status",error.message);setStatus("r3-management-status",error.message);}));}
  function facilityCandidateFromEditor(){try{return JSON.parse($("facility-edit-json").value);}catch{return null;}}
  async function legacyServicePoint(facility,geometry){
    const encoded=new TextEncoder().encode(facility.id),hash=await crypto.subtle.digest("SHA-256",encoded),suffix=[...new Uint8Array(hash)].slice(0,12).map(value=>value.toString(16).padStart(2,"0")).join("");
    const point=geometry.facility_points?.[facility.id];if(!Number.isFinite(point?.x)||!Number.isFinite(point?.y))throw new Error(`施設 ${facility.id} のGeometry位置がないため互換サービス点を作れません。`);
    return {schema_version:1,id:`legacy-service:${suffix}`,facility_id:facility.id,point_m:{x:point.x,y:point.y},access:{accessibility:"unknown",required_access_tags:[],required_staff_skills:[]},service_capacity:facility.working_units??facility.capacity??0,queue_id:facility.id};
  }
  function renderServicePoints(){
    const box=$("service-points"),add=$("service-point-add"),candidate=state.facilityCandidate;
    box.replaceChildren();if(!candidate?.geometry){add.disabled=true;setStatus("service-point-status","標準施設のPreview後にサービス点を編集できます。");return;}
    if(!state.facilityDesign?.facilities?.length){add.disabled=true;setStatus("service-point-status","Previewに編集対象のFacility IDがないため、サービス点は追加できません。");return;}
    add.disabled=false;const facilities=state.facilityDesign?.facilities||[],geometry=candidate.geometry;
    for(const [index,point] of state.servicePoints.entries()){
      const row=document.createElement("article");row.className="equipment-row service-point-row";
      const input=(key,label,type="text")=>{const wrap=document.createElement("label");wrap.textContent=label;const control=document.createElement("input");control.type=type;control.dataset.index=String(index);control.dataset.field=key;if(type==="number"){control.step="any";control.min="0";}control.value=key==="x"?point.point_m?.x??"":key==="y"?point.point_m?.y??"":key.startsWith("access.")?(point.access?.[key.slice(7)]||[]).join(","):point[key]??"";wrap.append(control);row.append(wrap);return control;};
      input("id","ServicePoint ID");const facilityLabel=document.createElement("label");facilityLabel.textContent="施設ID";const facilitySelect=document.createElement("select");facilitySelect.dataset.index=String(index);facilitySelect.dataset.field="facility_id";for(const facility of facilities){const option=document.createElement("option");option.value=facility.id;option.textContent=facility.id;option.selected=point.facility_id===facility.id;facilitySelect.append(option);}facilityLabel.append(facilitySelect);row.append(facilityLabel);input("x","X位置 (m)","number");input("y","Y位置 (m)","number");
      const access=document.createElement("label");access.textContent="アクセス可否";const select=document.createElement("select");select.dataset.index=String(index);select.dataset.field="access.accessibility";for(const [value,label] of [["accessible","accessible · 確認済み"],["restricted","restricted · 制限あり"],["unknown","unknown · 未確認"]]){const option=document.createElement("option");option.value=value;option.textContent=label;option.selected=(point.access?.accessibility||"unknown")===value;select.append(option);}access.append(select);row.append(access);
      const capacity=input("service_capacity","同時サービス容量","number");capacity.step="1";capacity.max="1000000";input("queue_id","独立FIFO queue ID");input("access.required_access_tags","必要アクセスtag (comma separated)");input("access.required_staff_skills","必要スタッフskill (comma separated)");
      const remove=document.createElement("button");remove.type="button";remove.textContent="このサービス点を削除";remove.disabled=state.servicePoints.length<=1;remove.addEventListener("click",()=>{state.servicePoints.splice(index,1);writeServicePoints();});row.append(remove);box.append(row);
      for(const control of row.querySelectorAll("input,select"))control.addEventListener("change",updateServicePoint);
    }
    const queues=new Set(state.servicePoints.map(row=>row.queue_id)),connected=state.servicePoints.filter(row=>facilities.some(f=>f.id===row.facility_id)&&row.queue_id===row.facility_id).length;
    setStatus("service-point-status",`${state.servicePoints.length} service point · ${new Set(state.servicePoints.map(row=>row.id)).size} unique ID · ${queues.size} unique queue ID · ${connected} point(s) use a legacy Facility FIFO. Other queues can be geometry-valid while runtime binding remains not_connected; throughput is not inferred.`);
  }
  function writeServicePoints(){
    const field=$("facility-edit-json"),candidate=facilityCandidateFromEditor();if(!candidate?.geometry)throw new Error("Facility Geometryの候補がありません。");
    candidate.geometry.service_points=state.servicePoints.map(point=>JSON.parse(JSON.stringify(point)));state.facilityCandidate=candidate;field.value=JSON.stringify(candidate,null,2);field.dispatchEvent(new Event("input",{bubbles:true}));renderServicePoints();
    setStatus("service-point-status","サービス点の草案をScenario候補へ反映しました。既存Scenarioは変更されていません。「Geometryを再検証」して新しいScenario Previewを作ってください。");
  }
  function updateServicePoint(event){
    const index=Number(event.currentTarget.dataset.index),field=event.currentTarget.dataset.field,point=state.servicePoints[index];if(!point)return;
    if(field==="x"||field==="y"){point.point_m=point.point_m||{x:0,y:0};point.point_m[field]=Number(event.currentTarget.value);}
    else if(field==="access.accessibility"){point.access=point.access||{};point.access.accessibility=event.currentTarget.value;}
    else if(field.startsWith("access.")){point.access=point.access||{};point.access[field.slice(7)]=event.currentTarget.value.split(",").map(value=>value.trim()).filter(Boolean);}
    else if(field==="service_capacity")point[field]=Number(event.currentTarget.value);
    else point[field]=event.currentTarget.value.trim();
    writeServicePoints();
  }
  async function prepareServicePoints(detail){
    const candidate=state.facilityCandidate,scenario=detail?.scenario,geometry=scenario?.model_configs?.facility_geometry||candidate?.geometry;
    if(!candidate?.geometry){renderServicePoints();return;}
    const explicit=Array.isArray(candidate.geometry.service_points)&&candidate.geometry.service_points.length?candidate.geometry.service_points:geometry?.service_points;
    if(Array.isArray(explicit)&&explicit.length){state.servicePoints=explicit.map(point=>JSON.parse(JSON.stringify(point)));renderServicePoints();return;}
    const facilities=Array.isArray(scenario?.facilities)?scenario.facilities:state.facilityDesign?.facilities||[];
    state.servicePoints=await Promise.all(facilities.map(facility=>legacyServicePoint(facility,geometry||candidate.geometry)));
    renderServicePoints();
  }
  window.ShelterSimRound3ScenarioUI={
    renderHouseholdCorrection,
    clearHouseholdPreview(){state.householdPreview=null;$("household-correction").hidden=true;$("household-correction-preview").disabled=true;},
    prepareServicePoints(detail){state.facilityDesign=detail?.facility_design||null;return prepareServicePoints(detail);},
    setFacilityCandidate(detail){
      state.facilityCandidate=detail?.candidate||null;
      state.facilityDesign=detail?.design||state.facilityDesign;
      if(state.facilityCandidate?.geometry?.service_points?.length)
        state.servicePoints=state.facilityCandidate.geometry.service_points.map(point=>JSON.parse(JSON.stringify(point)));
      renderServicePoints();
    },
    addServicePoint(){
      const candidate=facilityCandidateFromEditor(),facility=(state.facilityDesign?.facilities||[])[0];
      if(!candidate?.geometry||!facility)throw new Error("サービス点を追加する施設IDがありません。");
      const point=candidate.geometry.facility_points?.[facility.id]||{x:0,y:0},id=`service-${crypto.randomUUID()}`;
      state.servicePoints.push({schema_version:1,id,facility_id:facility.id,point_m:{x:Number(point.x),y:Number(point.y)},access:{accessibility:"unknown",required_access_tags:[],required_staff_skills:[]},service_capacity:facility.working_units??facility.capacity??1,queue_id:`queue-${crypto.randomUUID()}`});
      writeServicePoints();
    },
  };

  async function refreshProjects(){
    const data=await api("/api/v1/projects"),items=data.projects||[],chosen=projectId();
    options($("r3-projects"),items.map(item=>({value:item.project_id,label:`${item.project_id} · ${item.run_count??item.runs?.length??"保存済み"}`})),items.some(item=>item.project_id===chosen)?[chosen]:[]);
    if(chosen&&!items.some(item=>item.project_id===chosen)){const option=document.createElement("option");option.value=chosen;option.textContent=`${chosen} · 作成中`;option.selected=true;$("r3-projects").prepend(option);}
    return data;
  }
  async function refreshManagement(){
    const project=projectId();if(!project)throw new Error("Project IDが空です。");
    const serial=++state.managementSerial;setStatus("r3-management-status",`Project ${project} の管理情報を読み込み中…`);
    const [record,drafts,assets,evaluations,usage,runtimeUsage]=await Promise.all([
      api(apiRoot()),api(`${apiRoot()}/drafts?limit=100`),api(`${apiRoot()}/assets?limit=100`),api(`${apiRoot()}/evaluations?limit=100`),api(`${apiRoot()}/usage?limit=100`),api("/api/v1/usage/runtime")
    ]);
    if(serial!==state.managementSerial||project!==projectId())return;
    state.project=record;state.runRecords=record.runs||[];state.runDetails.clear();
    renderAssets(assets.items||[]);renderDrafts(drafts.items||[]);renderEvaluations(evaluations.items||[]);renderUsage(usage,runtimeUsage);
    updateRunChoices();setStatus("r3-management-status",`Project ${project} · ${state.runRecords.length} Run · ${record.scenarios?.length||0} Scenario · Project Storeから取得`);
    await refreshProjects();
  }
  function renderAssets(items){
    const box=$("r3-assets");box.replaceChildren();
    if(!items.length){text(box,"p","保存Assetはありません。");return;}
    for(const item of items){
      const row=document.createElement("article");row.className="worldline-lane";text(row,"strong",`${item.file_name||item.asset_id||"Asset"} · ${item.media_type||"形式不明"}`);text(row,"p",`SHA-256 ${item.sha256||"不明"} · ${Number(item.byte_count||0).toLocaleString()} bytes · ${item.rights_status||"権限状態不明"}`);
      const open=document.createElement("button");open.type="button";open.textContent="図面/内容を表示";open.addEventListener("click",()=>showAsset(item).catch(error=>setStatus("r3-management-status",error.message)));row.append(open);
      const remove=document.createElement("button");remove.type="button";remove.textContent="Assetを削除";remove.addEventListener("click",()=>deleteAsset(item).catch(error=>setStatus("r3-management-status",error.message)));row.append(remove);box.append(row);
    }
  }
  function renderDrafts(items){
    const box=$("r3-drafts");box.replaceChildren();
    if(!items.length){text(box,"p","保存草案はありません。");return;}
    for(const item of items){const row=document.createElement("article");row.className="worldline-lane";text(row,"strong",`${item.draft_id} · ${item.kind} · revision ${item.revision}`);text(row,"p",`更新 ${item.updated_at||"時刻未記録"} · hash ${item.content_hash||"不明"}`);const load=document.createElement("button");load.type="button";load.textContent="Scenario入力へ復旧";load.addEventListener("click",()=>{$("draft-id").value=item.draft_id;$("draft-restore").click();$("create").open=true;});row.append(load);const remove=document.createElement("button");remove.type="button";remove.textContent="草案を削除";remove.addEventListener("click",()=>deleteArtifact("drafts",item.draft_id).catch(error=>setStatus("r3-management-status",error.message)));row.append(remove);box.append(row);}
  }
  function renderEvaluations(items){
    const box=$("r3-evaluations");box.replaceChildren();
    if(!items.length){text(box,"p","評価レポートはありません。評価結果は未確認のまま表示します。");return;}
    for(const item of items){const row=document.createElement("article");row.className="worldline-lane";text(row,"strong",`${item.evaluation_id||item.report_id||"評価"} · ${item.title||item.status||"imported report"}`);text(row,"p",`状態 ${item.status||"operator supplied"} · データ ${item.dataset_id||"未記録"} · 承認 ${item.approved_hash?"hashあり":"未記録"}`);const open=document.createElement("button");open.type="button";open.textContent="評価のRun/比較を確認";open.addEventListener("click",()=>showEvaluation(item).catch(error=>setStatus("r3-management-status",error.message)));row.append(open);box.append(row);}
  }
  function renderUsage(data,runtimeData={}){
    const box=$("r3-usage");box.replaceChildren();
    const totals=data.project_totals||{},summaries=data.items||[];
    text(box,"p",`プロジェクト集計 · ${totals.calls??0} calls · $${(Number(totals.cost_nusd||0)/1e9).toFixed(6)} · 入力 ${totals.input_tokens??0} tokens · 出力 ${totals.output_tokens??0} tokens`);
    if(!summaries.length)text(box,"p","利用量のアカウント記録はありません。未記録は未使用を意味しません。");
    for(const account of summaries){
      text(box,"h4",`Account ${account.account_id} · coverage ${account.coverage} · uncertain ${account.uncertain_calls||0} · ${account.frozen?"予算/Account凍結":"Account稼働"}`);
      const limits=account.limits||{};text(box,"p",`予算 $${limits.budget_usd??"不明"} · call上限 ${limits.max_calls??"不明"} · 呼出 ${account.totals?.calls??0} · 実費見積 $${Number(account.estimated_cost_usd||0).toFixed(6)}`);
      const stages=account.by_operation||{};for(const [operation,stage] of Object.entries(stages))text(box,"p",`stage/operation ${operation}: ${stage.calls} calls · $${(Number(stage.cost_nusd||0)/1e9).toFixed(6)} · ${stage.input_tokens} in / ${stage.output_tokens} out`);
      for(const [label,rows] of [["工程別",account.by_stage||{}],["モデル別",account.by_model||{}]])for(const [key,value] of Object.entries(rows))text(box,"p",`${label} ${key}: ${value.calls} calls · $${(Number(value.cost_nusd||0)/1e9).toFixed(6)} · ${value.input_tokens} in / ${value.output_tokens} out`);
      text(box,"p",`結果別 ${JSON.stringify(account.by_outcome||{})} · エラー別 ${JSON.stringify(account.by_error_code||{})} · 残予算 ${account.remaining_allowance?.estimated_usd??"不明"} USD`);
      if(account.explanation)detailSection(box,"予算・費用・再試行の扱い",account.explanation);
    }
    const campaign=runtimeData.campaign||{};text(box,"h4","全Project / Run / CLI / Web共通のtask予算");
    text(box,"p",`累積上限 $${campaign.budget_usd??50} · 呼出 ${campaign.calls??0} · 使用/予約 $${Number(campaign.reserved_or_spent_usd||0).toFixed(6)} · 残り $${Number(campaign.remaining_usd??campaign.budget_usd??50).toFixed(6)} · 不確定 ${campaign.uncertain_calls??0} · 状態 ${campaign.status||"不明"}`);
    for(const receipt of campaign.recent_receipts||[])text(box,"p",`${receipt.created_at||"時刻なし"} · ${receipt.operation||"operation不明"}/${receipt.stage||"stage不明"} · ${receipt.status||"状態不明"} · ${receipt.model_id||"model不明"} · $${Number(receipt.cost_nusd||0)/1e9}`);
    text(box,"h4","CLI / 従来DemoのUsageLedger");
    text(box,"p",`coverage ${runtimeData.coverage||"未取得"} · 過去の未記録呼出し ${runtimeData.unrecorded_history||"不明"}`);
    const runtimeAccounts=runtimeData.accounts||[];
    if(!runtimeAccounts.length)text(box,"p","CLI / 従来Demoの記録はありません。未記録は未使用を意味しません。");
    for(const account of runtimeAccounts){
      const summary=account.summary||{},totals=summary.totals||{};
      text(box,"h4",`${account.scope} · ${account.account_id} · coverage ${summary.coverage||"不明"} · uncertain ${summary.uncertain_calls||0}`);
      text(box,"p",`${totals.calls||0} calls · $${Number(summary.estimated_cost_usd||0).toFixed(6)} estimated · ${totals.input_tokens||0} input / ${totals.output_tokens||0} output tokens · receipts ${account.receipt_count||0}${account.receipts_truncated?" · 最新20件を表示":""}`);
      for(const receipt of account.recent_receipts||[])text(box,"p",`${receipt.created_at||"時刻なし"} · ${receipt.operation||"operation不明"}/${receipt.stage||"stage不明"} · ${receipt.status||"状態不明"} · ${receipt.calls||0} call · $${Number(receipt.cost_nusd||0)/1e9}`);
    }
    text(box,"p","APIキーの値はブラウザへ公開しません。認証・予算拒否・通信失敗・不明なUsageは各呼出のledger statusで確認してください。","muted");
  }
  function selectedValues(select){return [...select.selectedOptions].map(option=>option.value);}
  function renderHouseholdCorrection(detail){
    const panel=$("household-correction"),list=$("household-correction-list"),button=$("household-correction-preview");
    const scenario=detail?.scenario,profiles=Array.isArray(scenario?.profiles)?scenario.profiles:[];
    state.householdPreview=detail?.preview_id&&detail?.approved_hash?detail:null;
    list.replaceChildren();button.disabled=!state.householdPreview;
    if(!state.householdPreview||!profiles.length){panel.hidden=true;return;}
    const traits=scenario.model_configs?.population?.people||{},people=scenario.model_configs?.population_context?.people||{},groups=new Map();
    for(const profile of profiles){if(!profile?.id||!profile.group_id)continue;groups.set(profile.group_id,[...(groups.get(profile.group_id)||[]),profile]);}
    if(!groups.size){panel.hidden=true;return;}
    panel.hidden=false;
    const make=(tag,label)=>{const node=document.createElement(tag);node.textContent=label;return node;};
    const updateLimit=()=>{const checked=[...list.querySelectorAll('[data-correction-target]:checked')],atLimit=checked.length>=100;for(const check of list.querySelectorAll('[data-correction-target]:not(:checked)'))check.disabled=atLimit||check.dataset.eligible!=="true";button.disabled=!state.householdPreview||checked.length===0;button.textContent=`選択した関係 ${checked.length}件を検証し新しいPreviewを作る`;};
    let preselected=0;for(const [household,members] of groups){
      const group=document.createElement("details");group.append(make("summary",`${household} · ${members.length}人 · 初期世帯ID（固定）`));
      for(const profile of members){
        const id=profile.id,context=people[id]||{},age=traits[id]?.age_years,eligible=Number.isInteger(age),row=document.createElement("article");row.className="household-review-person";
        const reviewRequired=Boolean(context.requires_review||(context.review_reasons||[]).length||(eligible&&age<18));
        const checkLabel=document.createElement("label"),check=document.createElement("input");check.type="checkbox";check.dataset.correctionTarget="true";check.dataset.agentId=id;check.dataset.householdId=profile.group_id;check.dataset.eligible=String(eligible);check.checked=eligible&&reviewRequired&&preselected<100;if(check.checked)preselected++;check.disabled=!eligible;checkLabel.append(check,document.createTextNode(` 関係を修正 · ${id} · ${eligible?`年齢 ${age}`:"年齢不明・API対象外"} · 世帯 ${profile.group_id}`));row.append(checkLabel);
        const peers=members.filter(person=>person.id!==id),older=peers.filter(person=>Number.isInteger(traits[person.id]?.age_years)&&eligible&&traits[person.id].age_years>age);
        const field=(title,values,current)=>{const label=document.createElement("label");label.textContent=title;const select=document.createElement("select");select.multiple=true;select.size=Math.max(2,Math.min(5,values.length));for(const person of values){const option=document.createElement("option");option.value=person.id;option.textContent=`${person.id} · ${Number.isInteger(traits[person.id]?.age_years)?`${traits[person.id].age_years}歳`:"年齢不明"}`;option.selected=(current||[]).includes(person.id);select.append(option);}select.disabled=!check.checked;label.append(select);row.append(label);return select;};
        const parent=field("親（年上の同一世帯合成人物）",older,context.parent_ids),caregiver=field("保護者・介護者（同一世帯合成人物）",peers,context.caregiver_ids);
        const statusLabel=document.createElement("label");statusLabel.textContent="保護者状態";const guardian=document.createElement("select");guardian.dataset.guardian="true";guardian.disabled=!check.checked;const unchanged=document.createElement("option");unchanged.value="";unchanged.textContent="変更しない（関係を追加すると同伴へ更新）";guardian.append(unchanged);const choices=eligible&&age<18?["accompanied","unaccompanied"]:["not_applicable"];for(const value of choices){const option=document.createElement("option");option.value=value;option.textContent=value==="accompanied"?"保護者・支援者と同伴":value==="unaccompanied"?"同伴者なし":"該当なし";guardian.append(option);}statusLabel.append(guardian);row.append(statusLabel);
        check.addEventListener("change",()=>{parent.disabled=!check.checked;caregiver.disabled=!check.checked;guardian.disabled=!check.checked;updateLimit();});
        row.append(make("p",`現在の関係 · 親 ${(context.parent_ids||[]).join(", ")||"未記録"} · 保護者/介護者 ${(context.caregiver_ids||[]).join(", ")||"未記録"} · 保護者状態 ${context.guardian_status||"未記録"} · 支援対象 ${(context.care_for_ids||[]).join(", ")||"未記録"}${(context.review_reasons||[]).length?` · 確認理由 ${(context.review_reasons||[]).join("、")}`:""}`));
        parent.dataset.correctionField="parents";caregiver.dataset.correctionField="caregivers";row.append(make("p","親・保護者・介護者は複数選択できます。空の選択を送ると、その種類の既存リンクを明示的に解除します。親または保護者リンクを追加し、保護者状態を「変更しない」にすると、サーバーが未成年者を同伴扱いに更新します。"));group.append(row);
      }
      list.append(group);
    }
    updateLimit();
    $("household-correction-status").textContent=`Preview ${detail.preview_id} · hash ${detail.approved_hash} · ${profiles.length}人 / ${groups.size}世帯。APIが対象ID、年齢、世帯内リンク、循環、保護者ルールを再検証します。`;
    button.onclick=async()=>{
      const rows=[...list.querySelectorAll('[data-correction-target]:checked')];if(!rows.length)throw new Error("修正対象の人物を選択してください。");if(rows.length>100)throw new Error("一度に修正できる人物は100人までです。");
      const corrections=rows.map(check=>{const row=check.closest("article"),parent=row.querySelector('[data-correction-field="parents"]'),caregiver=row.querySelector('[data-correction-field="caregivers"]'),guardian=row.querySelector('[data-guardian]'),correction={agent_id:check.dataset.agentId,household_id:check.dataset.householdId,parent_ids:selectedValues(parent),caregiver_ids:selectedValues(caregiver)};if(guardian.value)correction.guardian_status=guardian.value;return correction;});
      button.disabled=true;$("household-correction-status").textContent="元Preview hash、対象ID、親・保護者・介護関係をサーバーが検証中…";
      try{
        const source=state.householdPreview,result=await api(`${apiRoot()}/scenario-household-corrections`,{preview_id:source.preview_id,correction:{schema_version:1,expected_scenario_hash:source.approved_hash,corrections}});
        $("household-correction-result").textContent=JSON.stringify({source_preview_id:result.source_preview_id,source_scenario_hash:result.source_scenario_hash,new_preview_id:result.preview_id,new_approved_hash:result.approved_hash,corrected_agent_ids:result.corrected_agent_ids,can_start:result.can_start,approval_blockers:result.approval_blockers,old_preview_preserved:true},null,2);
        $("household-correction-status").textContent=`新しいPreview ${result.preview_id} · ${result.can_start?"既存の明示承認へ進めます":"承認ブロックが残っています。理由と関係を確認してください。"}。元Preview ${result.source_preview_id} は変更されません。`;
        if(typeof window.ShelterSimApplyScenarioPreview!=="function")throw new Error("Scenario Previewの画面更新接続がありません。元Previewは変更されていません。");window.ShelterSimApplyScenarioPreview(result);
      }catch(error){$("household-correction-status").textContent=`修正Previewを作成できませんでした: ${error.message}。元Scenarioは変更されていません。`;button.disabled=false;throw error;}
    };
  }
  function renderModelContracts(data){
    state.modelContracts=data;const box=$("model-contract-result");box.replaceChildren();
    const models=data.policy_models||data.models||{};
    if(!Object.keys(models).length)text(box,"p","登録モデル情報が返りませんでした。");
    for(const [id,model] of Object.entries(models)){
      const section=document.createElement("details"),summary=document.createElement("summary");summary.textContent=`${id} · ${model.domain||"domain不明"} · ${model.implementation_version||"version不明"} · evidence ${model.evidence_grade||"unknown"}`;section.append(summary);
      text(section,"p",`参照: ${model.reference||"未登録"}`);text(section,"p",`式: ${model.formula||"未登録"}`);text(section,"p",`仮定出典: ${model.assumption_source_id||"未登録"} · state schema ${model.state_schema_version??"不明"} · external_validation ${model.external_validation===true?"true":"false (未校正/保証なし)"}`);
      const parameters=model.parameters||{};if(Object.keys(parameters).length){text(section,"h4","係数・出典・適用条件");for(const [path,coefficient] of Object.entries(parameters)){const defaultValue=coefficient.default&&typeof coefficient.default==="object"?JSON.stringify(coefficient.default):coefficient.default??"default不明",scope=coefficient.scope?` · ${coefficient.scope==="per_service"?"サービス別":coefficient.scope==="per_staff_id"?"職員ID別":coefficient.scope==="per_agent_id"?"避難者ID別":coefficient.scope==="per_zone_id"?"ゾーンID別":coefficient.scope==="per_task_kind"?"業務種別ごと":coefficient.scope}`:"";text(section,"p",`${path} · ${defaultValue} ${coefficient.unit||"unit不明"}${scope} · range ${coefficient.lower??"?"}–${coefficient.upper??"?"}${coefficient.lower_exclusive?" (lower exclusive)":""} · source ${coefficient.source_id||model.assumption_source_id||"未登録"} · grade ${coefficient.evidence_grade||"unknown"} · ${coefficient.applicability||"適用条件未記録"}`);}}
      if(Object.keys(model.required_state_contracts||{}).length)text(section,"p",`必要State contracts: ${JSON.stringify(model.required_state_contracts)}`);box.append(section);
    }
    const operators=data.executable_effect_primitives||[];text(box,"h4",`実行可能primitive (${operators.length})`);for(const op of operators)text(box,"p",`${op.op} · targets ${JSON.stringify(op.target_kinds||[])} · units ${JSON.stringify(op.units||[])} · ${op.description||"説明未登録"}`);
    const contracts=data.typed_effect_contracts_only||{};text(box,"h4",`スキーマのみの型付き操作 · execution_status ${contracts.execution_status||"unknown"}`);for(const [name,schema] of Object.entries(contracts.schemas||{}))text(box,"p",`${name} · ${schema.title||"parameter schema"} · 未登録/未実行可。プレビューやschemaは物理効果を意味しません。`);
    setStatus("model-contract-status",`共通contracts APIから取得 · schema ${data.schema_version||"?"} · model catalog ${Object.keys(models).length}件 · 実行可能primitive ${operators.length}件 · 外部校正や予測精度の保証なし。`);
  }
  function renderEffectExplanation(detail){
    const box=$("effect-explanation"),result=detail?.result||{},program=result.program,operators=state.modelContracts?.executable_effect_primitives||[],supported=new Set(operators.map(item=>item.op)),registryKnown=Array.isArray(state.modelContracts?.executable_effect_primitives);box.replaceChildren();
    text(box,"h4",`型付き介入Preview · ${result.support_mode||result.status||"状態不明"} · can_apply ${result.can_apply===true?"true":"false / 未確認"}`);
    text(box,"p",`Run ${detail.run||"不明"} / Branch ${detail.branch||"不明"} · minute ${detail.minute??"不明"}。PreviewはまだWorldStateを変更していません。モデル内の差は実世界で実証された因果効果ではありません。`);
    if(program?.effects?.length){for(const effect of program.effects){const available=registryKnown?(supported.has(effect.op)?"実行可能primitive":"未登録/未実行可能") : "実行可否不明 · 共通Registry未取得";text(box,"p",`${available} · ${effect.effect_id||"effect"} / ${effect.op||"op不明"} · target ${effect.target_selector?.kind||"selector不明"} ${effect.target_selector?.id||""} · start ${effect.start_minute??"?"} · units ${effect.units||"不明"} · source ${effect.source_id||"未記録"} · assumptions ${(effect.assumption_ids||[]).join(", ")||"なし"}`);}}
    else text(box,"p",`検証済みEffectProgramはありません。${result.clarification_question||result.explanation||"未対応または曖昧な条件は実行可能な作用に変換されていません。"}`);
    if((program?.assumptions||[]).length){text(box,"h4","明示された仮定");for(const assumption of program.assumptions)text(box,"p",`${assumption.assumption_id||"仮定"} · ${assumption.statement||assumption.description||JSON.stringify(assumption)} · source ${assumption.source_id||"未記録"}`);}
    if((result.validation_issues||[]).length){text(box,"h4","サーバー検証エラー");for(const issue of result.validation_issues)text(box,"p",issue);}
    text(box,"p",`コンパイラー provenance: ${JSON.stringify(program?.compiler_provenance||result.compiler_provenance||"not returned")} · 型付きProgramがないときは適用ボタンを有効化しません。`);
  }
  function renderHistoryNavigation(detail){
    const page=detail?.page,historyKind=detail?.query?.kind||page?.kind,box=$("storyline");if(!page||!box)return;
    const section=document.createElement("section");section.className="workspace-events";text(section,"h4","履歴の範囲・関連記録へ移動");
    const total=page.total_items===null?"total count not calculated":page.total_items===undefined?"total count not returned":`total ${page.total_items}`;
    const retention=page.history_truncated===true?"truncated":page.history_truncated===false?"not marked truncated":page.retention?.status||page.retention_limit_reason||"retention boundary not reported";
    const requested=Array.isArray(page.requested_range)?`${page.requested_range[0]}–${page.requested_range[1]}分 requested`:"requested range unknown";
    const indexed=Number.isInteger(page.oldest_minute)||Number.isInteger(page.newest_minute)?`indexed ${page.oldest_minute??"?"}–${page.newest_minute??"?"}分`:"no indexed time bounds";
    const coverageScope=page.coverage_scope?` · coverage scope ${page.coverage_scope}`:"";
    text(section,"p",`Run ${page.run_id||detail.run||"unknown"} / Branch ${page.branch_id||detail.branch||"unknown"} · kind ${historyKind||"unknown"} · ${requested} · ${indexed} · completeness ${page.completeness||"unknown"}${coverageScope} · ${total} · ${retention} · next page ${page.next_cursor?"available":"not returned"}. 履歴にない項目を「発生しなかった」とは解釈しません。`,`muted`);
    if(historyKind==="dialogue")text(section,"p","dialogueは適用済み相互作用の記録です。個別状態に基づく対話例は、提案時点で記録したシミュレーション状態と適用済みモデル変化から作る架空の例で、本人が実際に話した内容ではありません。モデル状態は実測された人間の感情ではありません。utterance_recorded=false は録音・逐語発言が保存されていないことを示します。","muted");
    const rows=Array.isArray(page.items)?page.items:[];
    for(const item of rows){
      const minute=Number.isInteger(item.minute)?item.minute:Number.isInteger(item.recorded_event_minute)?item.recorded_event_minute:null;
      const identity=item.person_id||item.agent_id||item.staff_id||item.household_id||item.event_id||item.record_id;
      const semantics=item.time_semantics||page.time_semantics||"time semantics unknown";
      const evidence=[item.record_type,item.event_type,item.status,item.lifecycle_status].filter(value=>typeof value==="string");
      const dialogueTurns=Array.isArray(item.virtual_dialogue_example?.turns)?item.virtual_dialogue_example.turns:[],dialogueText=dialogueTurns.map(turn=>`${turn.speaker_id||"参加者"}: ${turn.example_text||""}`).join(" / ");
      const speech=(item.utterance_recorded===false?" · 発言記録なし":item.utterance_recorded===true?" · 発言元記録あり":"")+(dialogueText?" · 提案時状態と適用済みモデル差分に基づく架空の対話例（実際の発言ではありません）: "+dialogueText:typeof item.virtual_utterance_example==="string"?" · 架空の例文（実際の発言ではありません）: "+item.virtual_utterance_example:"");
      const actors=Array.isArray(item.actor_ids)?item.actor_ids.filter(value=>typeof value==="string"):[];
      const effectLabels={positive_affect:"肯定的感情",negative_affect:"否定的感情",reassurance:"安心感",discontent:"不満",uncertainty:"不確実性",relationship_trust_delta:"関係の信頼"};
      const effects=(Array.isArray(item.observed_model_effects)?item.observed_model_effects:[]).flatMap(effect=>Object.entries(effect?.deltas||{}).filter(([,value])=>typeof value==="number"&&Number.isFinite(value)).map(([name,value])=>`${effectLabels[name]||name} ${value>0?"+":""}${value.toFixed(3)}`));
      text(section,"p",`${item.record_kind||historyKind||"record"} · ${minute===null?"minute unknown":`${minute}分 (${semantics})`} · ${identity||"IDなし"}${actors.length?` · people ${actors.join(", ")}`:""} · ${evidence.join(" / ")||"recorded evidence"}${speech}${effects.length?` · 記録されたモデル状態変化: ${effects.join(" / ")}`:""}`,"muted");
      const rowRun=item.run_id||page.run_id||detail.run,rowBranch=item.branch_id||page.branch_id||detail.branch;
      const directTarget=item.person_id||item.agent_id?{person_id:item.person_id||item.agent_id}:item.household_id?{household_id:item.household_id}:item.staff_id?{staff_id:item.staff_id}:item.event_id?{event_id:item.event_id}:null;
      if(directTarget&&minute!==null&&rowRun&&rowBranch){
        const open=document.createElement("button");open.type="button";open.textContent=`${rowRun}/${rowBranch} · ${minute}分 · この記録の対象を開く`;
        open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.({run_id:rowRun,branch_id:rowBranch,minute,...directTarget}));section.append(open);
      }
      if(minute!==null&&rowRun&&rowBranch)for(const personId of actors){
        const open=document.createElement("button");open.type="button";open.textContent=`${rowRun}/${rowBranch} · ${minute}分 · 人物 ${personId} の記録を開く`;
        open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.({run_id:rowRun,branch_id:rowBranch,minute,person_id:personId}));section.append(open);
      }
      const refs=[item,...(Array.isArray(item.references)?item.references:[])];
      for(const ref of refs){
        const run=ref.run_id,branch=ref.branch_id;if(typeof run!=="string"||typeof branch!=="string")continue;
        const minute=Number.isInteger(ref.minute)?ref.minute:Number.isInteger(ref.recorded_event_minute)?ref.recorded_event_minute:null;
        if(minute===null||minute<0)continue;
        const target={run_id:run,branch_id:branch,minute};if(ref.person_id||ref.agent_id)target.person_id=ref.person_id||ref.agent_id;else if(ref.household_id)target.household_id=ref.household_id;else if(ref.staff_id)target.staff_id=ref.staff_id;else if(ref.event_id)target.event_id=ref.event_id;
        if(run===detail.run&&branch===detail.branch)continue;
        const open=document.createElement("button");open.type="button";open.textContent=`${run}/${branch} · ${minute}分${target.event_id?` · Event ${target.event_id}`:target.person_id?` · 人物 ${target.person_id}`:target.household_id?` · 世帯 ${target.household_id}`:target.staff_id?` · スタッフ ${target.staff_id}`:""} を開く`;open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.(target));section.append(open);
      }
    }
    box.append(section);
  }
  async function deleteArtifact(kind,id){const confirmation=prompt(`削除する${kind==="assets"?"Asset SHA-256":"草案ID"}を入力してください: ${id}`);if(confirmation!==id)return;await api(`${apiRoot()}/${kind}/${encode(id)}/delete`,{confirmation});await refreshManagement();}
  async function deleteAsset(item){await deleteArtifact("assets",item.sha256);}
  function showPdfFallback(url,name,error){
    const box=$("plan-pdf-preview");box.replaceChildren();box.hidden=false;const heading=document.createElement("p");heading.textContent=`アプリ内PDFページ表示を利用できません: ${error}. PDF.jsはローカル同梱版です。代替としてPDFをダウンロードするか、PNG/JPEG/WebPへ変換してアップロードしてください。`;const object=document.createElement("object");object.type="application/pdf";object.data=url;object.setAttribute("aria-label",`${name||"避難所図面"} PDFフォールバック`);object.width="100%";object.height="420";const fallback=document.createElement("p"),link=document.createElement("a");link.href=url;link.target="_blank";link.rel="noopener noreferrer";link.textContent="ブラウザーのPDF表示を開く";fallback.append(link);object.append(fallback);const download=document.createElement("a");download.href=url;download.download=name||"shelter-plan.pdf";download.textContent="PDFをダウンロード";download.className="button-link";box.append(heading,object,download);editor.image=null;editor.standard=null;editor.space="document";setStatus("plan-editor-status","PDF rendererが読み込めないため、画像上の手動トレースは停止しています。PDFを画像へ変換して再アップロードするか、対応ブラウザー表示をご利用ください。");
  }
  async function renderPdfPage(){
    if(!editor.pdfDocument)return;const requested=Number($("plan-page-index").value||0),serial=++editor.pdfPageSerial,box=$("plan-pdf-preview");
    if(!Number.isInteger(requested)||requested<0||requested>=editor.pdfDocument.numPages){editor.pdfPageIndex=requested;showPdfFallback(editor.pdfUrl,editor.imageName,`page ${requested} がPDFのページ範囲外です (0–${editor.pdfDocument.numPages-1})`);return;}
    try{
      const pdfjs=await import("/vendor/pdfjs/pdf.min.mjs");if(serial!==editor.pdfPageSerial)return;
      pdfjs.GlobalWorkerOptions.workerSrc="/vendor/pdfjs/pdf.worker.min.mjs";
      const page=await editor.pdfDocument.getPage(requested+1),unit=page.getViewport({scale:1}),maxDimension=Math.max(64,Math.min(2400,Number($("plan-max-dimension").value||1600))),scale=Math.min(maxDimension/unit.width,maxDimension/unit.height),viewport=page.getViewport({scale});
      const heading=document.createElement("p");heading.textContent=`PDF.js (offline bundle) · ${editor.imageName||"図面PDF"} · page ${requested} / ${editor.pdfDocument.numPages-1} · 表示・手動トレース専用 · OCRなし`;
      const canvas=document.createElement("canvas");canvas.className="pdf-page-canvas";canvas.width=Math.max(1,Math.floor(viewport.width));canvas.height=Math.max(1,Math.floor(viewport.height));canvas.setAttribute("aria-label",`PDF page ${requested}`);
      const hint=document.createElement("p");hint.className="muted";hint.textContent="下の編集キャンバスはこのページrasterと同じpixel座標を使います。サーバー抽出候補がある場合は、その候補と同じ保存rasterへ切り替えて重ねます。";
      const download=document.createElement("a");download.href=editor.pdfUrl;download.download=editor.imageName||"shelter-plan.pdf";download.textContent="元PDFをダウンロード";download.className="button-link";
      const layer=document.createElement("div");layer.className="pdf-page-view";layer.append(canvas);box.replaceChildren(heading,layer,hint,download);box.hidden=false;
      await page.render({canvasContext:canvas.getContext("2d"),viewport}).promise;if(serial!==editor.pdfPageSerial)return;
      editor.image=canvas;editor.standard=null;editor.space="raster_pixel";editor.width=canvas.width;editor.height=canvas.height;editor.metersPerPixel=null;editor.calibration=[];editor.pdfPageIndex=requested;drawPlan();
    }catch(error){if(serial===editor.pdfPageSerial)showPdfFallback(editor.pdfUrl,editor.imageName,error.message||"PDF.js読み込み失敗");}
  }
  async function openPdfDocument(bytes,name,{sha=null,stored=false,pageIndex=0}={}){
    if(editor.pdfUrl?.startsWith("blob:"))URL.revokeObjectURL(editor.pdfUrl);const normalized=bytes instanceof Uint8Array?bytes:new Uint8Array(bytes);editor.pdfBytes=normalized.slice();editor.pdfUrl=URL.createObjectURL(new Blob([normalized],{type:"application/pdf"}));editor.imageName=name||"shelter-plan.pdf";editor.assetSha=sha;editor.assetStored=stored;editor.assetMediaType="application/pdf";editor.pdfPageIndex=pageIndex;editor.space="raster_pixel";$("plan-page-index").value=String(pageIndex);
    try{const pdfjs=await import("/vendor/pdfjs/pdf.min.mjs");pdfjs.GlobalWorkerOptions.workerSrc="/vendor/pdfjs/pdf.worker.min.mjs";editor.pdfDocument=await pdfjs.getDocument({data:normalized.slice(),cMapUrl:"/vendor/pdfjs/cmaps/",cMapPacked:true,standardFontDataUrl:"/vendor/pdfjs/standard_fonts/",isEvalSupported:false,enableXfa:false,useSystemFonts:true}).promise;await renderPdfPage();}
    catch(error){showPdfFallback(editor.pdfUrl,editor.imageName,error.message||"PDF file unsupported or corrupt");throw new Error(`PDFを開けません。対応形式のPDFを選ぶかPNG/JPEG/WebPへ変換してください。${error.message||""}`);}
  }
  async function showAsset(item){
    const data=await api(`${apiRoot()}/assets/${encode(item.sha256)}/content`);
    resetPlanEditing();
    if(item.media_type?.startsWith("image/")){editor.assetMediaType=item.media_type;loadPlanImage(`data:${item.media_type};base64,${data.data_base64}`,item.file_name,item.sha256);$("create").open=true;setStatus("plan-editor-status",`Project Assetを表示中 · ${item.file_name} · SHA-256 ${item.sha256} · 図面の候補と確定済みトレースは別管理です。`);}
    else if(item.media_type==="application/pdf"){const bytes=Uint8Array.from(atob(data.data_base64),char=>char.charCodeAt(0));editor.assetSha=item.sha256;editor.assetStored=true;await openPdfDocument(bytes,item.file_name,{sha:item.sha256,stored:true,pageIndex:Number($("plan-page-index").value||0)});$("create").open=true;setStatus("plan-editor-status",`Project PDFをローカルPDF.jsで表示中 · ${item.file_name||"名称未記録"} · SHA-256 ${item.sha256} · page ${editor.pdfPageIndex} · 表示中のpageと同じpixel座標で手動トレースできます。`);}
    else if(["application/json","application/geo+json"].includes(item.media_type)){editor.assetMediaType=item.media_type;editor.assetSha=item.sha256;editor.assetStored=true;const decoded=atob(data.data_base64);const parsed=JSON.parse(new TextDecoder().decode(Uint8Array.from(decoded,c=>c.charCodeAt(0))));loadPlanCandidates(parsed,item.file_name,item.sha256);$("create").open=true;}
    else setStatus("r3-management-status",`${item.file_name||item.sha256} · ${item.media_type} は保管Assetとして確認できます。ブラウザの図面表示は対応していません。`);
  }
  async function showEvaluation(item){
    const id=item.evaluation_id||item.report_id;if(!id)return;
    const [record,runs,comparisons]=await Promise.all([api(`${apiRoot()}/evaluations/${encode(id)}`),api(`${apiRoot()}/evaluations/${encode(id)}?section=runs&limit=100`),api(`${apiRoot()}/evaluations/${encode(id)}?section=comparisons&limit=100`)]);
    const box=$("r3-evaluations"),detail=document.createElement("details"),summary=document.createElement("summary");summary.textContent=`${id} · レポート内容`;detail.append(summary);text(detail,"pre",JSON.stringify({classification:record.classification||"operator supplied / not verified",report:record.report||record,run_rows:runs.items,comparison_rows:comparisons.items},null,2),"workspace-preview");box.append(detail);
  }
  async function rerunSelected(){
    const runId=$("run").value;if(!runId)throw new Error("再実行する保存Runを選んでください。");const newRunId=`run-${crypto.randomUUID()}`;
    if(!confirm(`Run ${runId} と同じ保存Scenarioから ${newRunId} を作りますか？既存Runは変更しません。`))return;
    const created=await api(`${apiRoot()}/runs/${encode(runId)}/rerun`,{new_run_id:newRunId});$("run").value=created.run_id||newRunId;$("run").dispatchEvent(new Event("change"));await refreshManagement();
  }
  async function showRunAudit(){
    const project=projectId(),runId=$("run").value;if(!runId)throw new Error("根拠監査を開くRunを選んでください。");
    const serial=++state.auditSerial,root=apiRoot(),box=$("r3-run-audit");box.textContent=`${runId} のProject記録・モデル根拠・費用台帳を読込中…`;
    const [run,evidence,usage]=await Promise.all([api(`${root}/runs/${encode(runId)}`),api(`${root}/runs/${encode(runId)}/evidence`),api(`${root}/runs/${encode(runId)}/usage`)]);
    if(serial!==state.auditSerial||project!==projectId()||runId!==$("run").value)return;
    const record={run:{run_id:run.run_id,status:run.status,scenario_id:run.scenario_id,scenario_version:run.scenario_version,seed:run.seed,created_at:run.created_at,branches:(run.branches||[]).map(item=>({branch_id:item.branch_id,parent_branch_id:item.parent_branch_id,fork_minute:item.fork_minute,computed_until:item.computed_until}))},model_and_source_evidence:evidence,usage_ledger:usage};
    box.textContent=JSON.stringify(record,null,2);
  }
  function renderAuditPage(data){
    const box=$("r4-audit-result");box.replaceChildren();
    text(box,"p",`coverage ${data.coverage||"unknown"} · verification ${data.verified===false?"未検証":"不明"} · ${data.ordering||"記録順序情報なし"} · ${data.limits||"保存済みレコードのみ"}`);
    if(data.unrecorded_spend)text(box,"p",`過去の未記録支出: ${data.unrecorded_spend} · レシート ${data.coverage||"なし"}`,"workspace-chat-explanation");
    if(!(data.items||[]).length)text(box,"p","選択条件に一致する保存レシートはありません。legacy_account_not_initialized の場合、未記録支出の有無は不明です。","muted");
    for(const row of data.items||[]){const article=document.createElement("article");article.className="worldline-lane";text(article,"strong",`${row.receipt_id||"receipt"} · ${row.status||"status unknown"} · ${row.operation||"operation unknown"}`);text(article,"p",`stage ${row.stage||"未記録"} · model ${row.model_id||"未記録"} · branch ${row.branch_id||"準備/未指定"} · outcome ${row.error_code||row.status||"未記録"}`);text(article,"p",`sequence ${row.sequence??"未記録"} · created ${row.created_at||"未記録"} · settled ${row.settled_at||"未記録"} · calls ${row.calls??"未記録"}`);text(article,"p",`input ${row.input_tokens??"不明"} / output ${row.output_tokens??"不明"} tokens · $${(Number(row.cost_nusd||0)/1e9).toFixed(6)} · ${row.cost_semantics||"cost semantics unknown"}`);if(row.error_code)text(article,"p",`有限化された失敗コード: ${row.error_code}`,"workspace-chat-explanation");box.append(article);}
    $("r4-audit-next").disabled=!data.next_cursor;
  }
  async function loadRunAuditPage(next=false){
    const runId=$("run").value;if(!runId)throw new Error("レシートを閲覧する保存Runを選んでください。");
    const filters=next&&state.auditPage?.filters?state.auditPage.filters:{stage:$("r4-audit-stage").value,status:$("r4-audit-status").value};
    if(next&&!state.auditPage?.data?.next_cursor)throw new Error("Run監査の次ページはありません。");
    if(!next)state.auditPage={runId,filters,data:null};
    if(state.auditPage.runId!==runId)throw new Error("Runを変更したため、監査ページを最初から読み込んでください。");
    const params={kind:"usage",limit:25,...filters,...(next?{cursor:state.auditPage.data.next_cursor}:{})};
    const data=await api(`${apiRoot()}/runs/${encode(runId)}/audit?${query(params)}`);state.auditPage={runId,filters,data};renderAuditPage(data);
  }
  function experimentId(){return $("r4-experiment-select").value||$("r4-experiment-id").value.trim();}
  function renderExperimentChoices(items){
    state.experiments=items;const select=$("r4-experiment-select"),current=state.experiment?.experiment_id||select.value;options(select,items.map(item=>({value:item.experiment_id,label:`${item.experiment_id} · ${item.status||"保存済み"} · ${item.cells?.length||0} cells`})),items.some(item=>item.experiment_id===current)?[current]:[]);
  }
  function showExperimentRecord(record){
    state.experiment=record;const select=$("r4-experiment-select");if(record.experiment_id)select.value=record.experiment_id;
    $("r4-experiment-approve").checked=false;$("r4-experiment-approve").dataset.planHash=record.plan_hash||"";$("r4-experiment-fit").disabled=record.purpose!=="calibration";
    $("r4-experiment-status").textContent=`${record.experiment_id} · ${record.status||"configured_not_executed"} · ${record.cells?.length||0} cells · plan hash ${record.plan_hash||"未記録"} · 承認前に以下の内容を確認してください。`;
    $("r4-experiment-detail").textContent=JSON.stringify({purpose:record.purpose,scenario_id:record.scenario_id,scenario_version:record.scenario_version,plan:record.plan,plan_hash:record.plan_hash,cells:record.cells,dataset_checks:record.dataset_checks,external_validation:record.external_validation,limits:record.limits},null,2);
    renderExperimentCells(record.cells||[],[]);
  }
  function renderExperimentCells(cells,results){
    state.experimentResults=results;const box=$("r4-experiment-results");box.replaceChildren();const byId=new Map(results.map(row=>[row.cell_id,row]));
    const groups=new Map();for(const result of results){if(!result.metrics)continue;const rows=groups.get(result.variant_id)||[];rows.push(result);groups.set(result.variant_id,rows);}
    if(groups.size){const summary=document.createElement("section");summary.className="experiment-uncertainty";text(summary,"h4","seed間の記録値のばらつき（記述統計）");text(summary,"p","これは選択した合成モデル内で完了Runから収集した値の幅です。信頼区間、実避難所への予測精度、因果効果を示しません。未収集cellは含みません。","muted");for(const [variant,rows] of groups){text(summary,"strong",`${variant} · ${rows.length} collected cells`);const metrics=[...new Set(rows.flatMap(row=>Object.keys(row.metrics||{})))];for(const metric of metrics){const values=rows.map(row=>row.metrics[metric]).filter(value=>typeof value==="number"&&Number.isFinite(value));if(!values.length)continue;const mean=values.reduce((sum,value)=>sum+value,0)/values.length,sd=values.length>1?Math.sqrt(values.reduce((sum,value)=>sum+(value-mean)**2,0)/(values.length-1)):0;text(summary,"p",`${metric}: n=${values.length} · mean ${mean.toPrecision(4)} · SD ${sd.toPrecision(4)} · range ${Math.min(...values).toPrecision(4)}–${Math.max(...values).toPrecision(4)}`);}}box.append(summary);}
    for(const cell of cells){const result=byId.get(cell.cell_id)||{},row=document.createElement("article");row.className="worldline-lane";text(row,"strong",`${cell.cell_id} · variant ${cell.variant_id} · seed ${cell.seed} · ${result.status||"not_collected"}`);text(row,"p",`Run ${cell.run_id} · Scenario hash ${cell.scenario_hash||"unknown"} · model versions ${JSON.stringify(result.model_versions||{})}`);if(result.metrics)text(row,"pre",JSON.stringify({metrics:result.metrics,evidence:result.evidence,status:result.status,external_validation:result.external_validation},null,2),"workspace-preview");
      const prepare=document.createElement("button");prepare.type="button";prepare.textContent="承認Planからoffline Runを準備";prepare.disabled=!state.experiment||!$("r4-experiment-approve").checked;prepare.addEventListener("click",()=>prepareExperimentCell(cell.cell_id).catch(error=>setStatus("r4-experiment-status",error.message)));row.append(prepare);
      const collect=document.createElement("button");collect.type="button";collect.textContent="完了Runの記録KPIを収集";collect.disabled=!state.experiment||result.status==="recorded_not_verified";collect.addEventListener("click",()=>collectExperimentCell(cell.cell_id).catch(error=>setStatus("r4-experiment-status",error.message)));row.append(collect);box.append(row);
    }
  }
  async function createExperiment(){
    const runId=$("run").value;if(!runId)throw new Error("基準Scenarioを持つRunを選択してください。");let run=state.runDetails.get(runId);if(!run){run=await api(`${apiRoot()}/runs/${encode(runId)}`);state.runDetails.set(runId,run);}
    const id=$("r4-experiment-id").value.trim();if(!id)throw new Error("Experiment IDを入力してください。");
    const plan=JSON.parse($("r4-experiment-plan").value),observations=JSON.parse($("r4-experiment-observations").value||"[]");
    const record=await api(`${apiRoot()}/experiments/${encode(id)}`,{schema_version:1,scenario_id:run.scenario_id,scenario_version:run.scenario_version,purpose:$("r4-experiment-purpose").value,plan,observations});showExperimentRecord(record);
    const list=await api(`${apiRoot()}/experiments?limit=100`);renderExperimentChoices(list.items||[]);
  }
  async function loadExperiment(){
    const id=experimentId();if(!id)throw new Error("保存Experimentを選択するかIDを入力してください。");const record=await api(`${apiRoot()}/experiments/${encode(id)}`);showExperimentRecord(record);
    const readAll=async section=>{let cursor=null,items=[];do{const page=await api(`${apiRoot()}/experiments/${encode(id)}/${section}?${query({limit:100,cursor})}`);items=items.concat(page.items||[]);cursor=page.next_cursor;}while(cursor);return items;};const [cells,results]=await Promise.all([readAll("cells"),readAll("results")]);renderExperimentCells(cells.length?cells:record.cells||[],results);
    if(record.purpose==="calibration")try{const fit=await api(`${apiRoot()}/experiments/${encode(id)}/fit`);$("r4-experiment-detail").textContent+=`\n\n保存済み校正候補:\n${JSON.stringify(fit,null,2)}`;}catch(error){if(!/not_found|No such file|404/.test(error.message))setStatus("r4-experiment-status",`結果を表示しました。fit候補は未保存または取得不可: ${error.message}`);}
  }
  async function prepareExperimentCell(cell){
    const record=state.experiment;if(!record)throw new Error("先にExperimentを読み込んでください。");const approval=$("r4-experiment-approve");if(!approval.checked||approval.dataset.planHash!==record.plan_hash)throw new Error("現在のPlan hashを確認して承認してください。");
    const result=await api(`${apiRoot()}/experiments/${encode(record.experiment_id)}/cells/${encode(cell)}/prepare`,{approved:true,approved_plan_hash:record.plan_hash});setStatus("r4-experiment-status",`${cell} · ${result.status} · Run ${result.run_id} · Scenario v${result.scenario_version}を保存しました。通常のRun画面で明示的に実行後、セル一覧からKPIを収集してください。`);
  }
  async function collectExperimentCell(cell){
    const record=state.experiment;if(!record)throw new Error("先にExperimentを読み込んでください。");const result=await api(`${apiRoot()}/experiments/${encode(record.experiment_id)}/cells/${encode(cell)}/collect`,{});setStatus("r4-experiment-status",`${cell} · ${result.status} · KPIとCheckpoint証拠を保存しました。外部妥当性は未検証です。`);await loadExperiment();
  }
  async function fitExperiment(){const id=experimentId();if(!id)throw new Error("Experimentを選択してください。");const result=await api(`${apiRoot()}/experiments/${encode(id)}/fit`,{});$("r4-experiment-detail").textContent=JSON.stringify(result,null,2);setStatus("r4-experiment-status",`${result.status} · holdoutは候補選択に使用せず、元Run/係数は変更していません。`);}
  async function finalizeExperiment(){const id=experimentId(),evaluationId=$("r4-evaluation-id").value.trim();if(!id||!evaluationId)throw new Error("Experiment IDとEvaluation IDが必要です。");const result=await api(`${apiRoot()}/experiments/${encode(id)}/finalize`,{evaluation_id:evaluationId});setStatus("r4-experiment-status",`Evaluation ${result.evaluation_id||evaluationId} を保存しました。seed/variantの記録済みKPIと適用範囲を確認してください。`);await refreshManagement();}
  function updateRunChoices(){
    const current=$("run").value,items=state.runRecords.map(run=>({value:run.run_id,label:`${run.run_id} · ${run.status}${run.archived?" · archived":""}`}));
    const selected=[...$("r3-runs").selectedOptions].map(option=>option.value);
    options($("r3-runs"),items,selected.filter(id=>items.some(item=>item.value===id)));
    if(!$("r3-runs").selectedOptions.length&&current)for(const option of $("r3-runs").options)if(option.value===current)option.selected=true;
    if(!$("r3-minute").value)$("r3-minute").value=$("minute").value||0;
    rebuildBranchTargets().catch(error=>setStatus("r3-status",error.message));
  }
  async function rebuildBranchTargets(){
    const serial=++state.branchBuildSerial,chosen=[...$("r3-runs").selectedOptions].map(option=>option.value),box=$("r3-branch-targets"),old=new Map(state.branchControls.map(item=>[item.runId,item.select.value]));
    box.replaceChildren();state.branchControls=[];
    for(const [index,runId] of chosen.entries()){
      let run=state.runDetails.get(runId);if(!run){run=await api(`${apiRoot()}/runs/${encode(runId)}`);state.runDetails.set(runId,run);}
      if(serial!==state.branchBuildSerial)return;
      const label=document.createElement("label");label.textContent=`${runId} Branch`;
      const select=document.createElement("select");select.dataset.runId=runId;select.addEventListener("change",()=>{++state.comparisonSerial;invalidateComparison();});
      const branches=run.branches||[];options(select,branches.map(item=>({value:item.branch_id,label:`${item.branch_id} · ${item.fork_minute}–${item.computed_until}`})),[]);
      const preferred=old.get(runId)||(runId===$("run").value?$("branch").value:"main");if(branches.some(item=>item.branch_id===preferred))select.value=preferred;
      label.append(select);box.append(label);state.branchControls.push({runId,select,run});
    }
  }
  async function resolveTargets(){
    if(!state.branchControls.length)await rebuildBranchTargets();
    const targets=state.branchControls.map(({runId,select,run})=>({run_id:runId,branch_id:select.value,run,branch:run.branches.find(item=>item.branch_id===select.value)}));
    if(targets.length<2)throw new Error("同期比較には2つ以上の保存Runを選択してください。");
    if(targets.length>8)throw new Error("比較対象は最大8本です。");
    if(targets.some(item=>!item.branch))throw new Error("比較Branchを各Runで選択してください。");
    return targets;
  }
  function commonRange(targets){return [Math.max(...targets.map(item=>item.branch.fork_minute)),Math.min(...targets.map(item=>item.branch.computed_until))];}
  async function loadComparison(){
    const serial=++state.comparisonSerial,project=projectId();await rebuildBranchTargets();const targets=await resolveTargets();
    if(project!==projectId())return;
    const [lower,upper]=commonRange(targets);if(lower>upper)throw new Error("選択Branchに共通の計算済み範囲がありません。");
    let minute=Number($("r3-minute").value||$("minute").value||lower);if(minute<lower||minute>upper){minute=Math.max(lower,Math.min(upper,minute));$("r3-minute").value=String(minute);}
    setStatus("r3-status",`同期比較を取得中 · 共通範囲 ${lower}–${upper}`);
    const result=await api(`${apiRoot()}/comparison-query`,{schema_version:1,targets:targets.map(({run_id,branch_id})=>({run_id,branch_id})),minute});
    if(serial!==state.comparisonSerial||project!==projectId())return;
    state.comparison={targets,lower,upper,minute,result};state.tracks.clear();
    renderComparison(result,targets);await loadKpiSeries(state.comparison,serial);
    if(serial!==state.comparisonSerial||project!==projectId())return;
    setStatus("r3-status",`同期minute ${minute} · ${targets.length} Run/Branch · 共通範囲 ${lower}–${upper} · ${result.interpretation||"記録時点の比較"}`);
  }
  function renderExactEvidence(){
    const box=$("r4-exact-results");box.replaceChildren();const diff=state.divergence;
    if(diff){const label={world:"WorldState全体",kpis:"選択KPI",person:`人物 ${diff.query.agent_id||"未指定"}`}[diff.query.mode];text(box,"h3",`Server側の逐次走査による正確な初回${label}差分`);for(const page of diff.pages){text(box,"p",`requested ${diff.query.start}–${diff.query.end} · scanned ${page.page_scanned_range?.join("–")} · semantics ${page.time_semantics} · complete ${page.complete} · earlier missing ${page.earlier_pages_have_missing_samples} · current missing ${page.missing_samples?.length||0}`);if(page.first_divergence){text(box,"pre",JSON.stringify(page.first_divergence,null,2),"workspace-preview");const evidenceMinute=page.first_divergence.minute,targetMinute=evidenceMinute+(diff.query.mode==="kpis"?1:0);for(const target of diff.targets){const mapMinute=Math.min(target.branch.computed_until,Math.max(target.branch.fork_minute,targetMinute)),open=document.createElement("button");open.type="button";open.textContent=`${target.run_id}/${target.branch_id} · ${diff.query.mode==="kpis"?`KPI end_of_tick ${evidenceMinute}`:`before_tick ${evidenceMinute}`} · 地図minute ${mapMinute}を開く${mapMinute!==targetMinute?"（次境界は未計算）":""}`;open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.({run_id:target.run_id,branch_id:target.branch_id,minute:mapMinute,...(diff.query.mode==="person"?{person_id:diff.query.agent_id}:{})}));box.append(open);}}else if(page.complete)text(box,"p",page.missing_samples?.length?"この範囲で差は見つからず、一部minuteに記録欠落があります。":"この記録範囲では差が見つかりませんでした。","muted");else text(box,"p","このページに差はありません。正確な走査は未完了です。続きの取得が必要です。","muted");}$("r4-divergence-next").disabled=!diff.pages.length||Boolean(diff.pages.at(-1).complete)||!diff.pages.at(-1).next_cursor;}
    const conditions=state.conditionDiff;if(conditions){text(box,"h3",`Scenario入力・承認済み介入の差 · minute ${conditions.minute}`);text(box,"p",`path semantics ${conditions.data.path_semantics||"JSON pointer"} · total count ${conditions.data.total_items===null?"未計算":conditions.data.total_items} · next page ${Boolean(conditions.data.next_cursor)} · ${conditions.data.limits||"差は実行結果や因果効果を意味しません。"}`,"muted");for(const target of conditions.targets){const open=document.createElement("button");open.type="button";open.textContent=`${target.run_id}/${target.branch_id} · 条件diffのminuteを開く`;open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.({run_id:target.run_id,branch_id:target.branch_id,minute:Math.min(target.branch.computed_until,Math.max(target.branch.fork_minute,conditions.minute))}));box.append(open);}for(const row of conditions.data.items||[])text(box,"pre",JSON.stringify({path:row.path,candidate_index:row.candidate_index,baseline_reference:row.baseline_reference,candidate_reference:row.candidate_reference,baseline:row.baseline,candidate:row.candidate},null,2),"workspace-preview");$("r4-condition-next").disabled=!conditions.data.next_cursor;}
  }
  async function loadExactDivergence(next=false){
    const serial=++state.divergenceSerial,project=projectId();let targets,queryBody;
    if(next){if(!state.divergence?.pages?.length||!state.divergence.pages.at(-1).next_cursor)throw new Error("続きの厳密走査がありません。条件を選び直してください。");targets=state.divergence.targets;queryBody={...state.divergence.query,cursor:state.divergence.pages.at(-1).next_cursor};}
    else{targets=await resolveTargets();if(serial!==state.divergenceSerial||project!==projectId())return;const [lower,upper]=commonRange(targets);if(lower>upper)throw new Error("比較する世界線に共通の計算範囲がありません。");const start=Math.max(lower,Math.min(upper,Number($("r4-div-start").value||lower))),end=Math.min(upper,Number($("r3-minute").value||$("minute").value||upper));if(start>end)throw new Error("厳密走査の開始時刻は終了時刻以下にしてください。");$("r4-div-start").value=String(start);const mode=$("r4-div-mode").value;queryBody={schema_version:1,targets:targets.map(({run_id,branch_id})=>({run_id,branch_id})),start,end,mode,scan_limit:20};if(mode==="person"){queryBody.agent_id=$("r4-div-person").value.trim();if(!queryBody.agent_id)throw new Error("人物状態の厳密比較には人物IDが必要です。");}if(mode==="kpis"){queryBody.metrics=$("r4-div-metrics").value.split(",").map(value=>value.trim()).filter(Boolean);if(queryBody.metrics.some(value=>!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(value)))throw new Error("KPI名は識別子をカンマで区切ってください。");}state.divergence={targets,query:queryBody,pages:[]};}
    const modeLabel={world:"WorldState",kpis:"選択KPI",person:"人物状態"}[queryBody.mode];setStatus("r3-status",next?`${modeLabel}の正確な初回差分を継続走査中…`:`${modeLabel}の正確な初回差分をサーバーで逐次走査中…`);const data=await api(`${apiRoot()}/first-divergence`,queryBody);if(serial!==state.divergenceSerial||project!==projectId())return;state.divergence.pages.push(data);renderExactEvidence();setStatus("r3-status",`厳密差分 · requested ${state.divergence.query.start}–${state.divergence.query.end} · scanned ${data.scanned_range?.join("–")} · ${data.complete?"complete":"continue available"}${data.earliest_in_requested_interval?" · requested intervalで最初の差":" · 欠落または未走査範囲を確認"}`);
  }
  async function loadConditionDifferences(next=false){
    const serial=++state.conditionDiffSerial,project=projectId();let targets,body;
    if(next){if(!state.conditionDiff?.data?.next_cursor)throw new Error("条件diffの次ページはありません。");targets=state.conditionDiff.targets;body={...state.conditionDiff.query,cursor:state.conditionDiff.data.next_cursor};}
    else{targets=await resolveTargets();if(serial!==state.conditionDiffSerial||project!==projectId())return;const [lower,upper]=commonRange(targets);if(lower>upper)throw new Error("比較Branchに共通の計算範囲がありません。");const minute=Math.max(lower,Math.min(upper,Number($("r3-minute").value||$("minute").value||upper)));body={schema_version:1,targets:targets.map(({run_id,branch_id})=>({run_id,branch_id})),minute,limit:50};state.conditionDiff={targets,query:body,minute,data:null};}
    setStatus("r3-status","保存Scenario入力と承認済み分岐条件のdiffを読み込み中…");const data=await api(`${apiRoot()}/condition-differences`,body);if(serial!==state.conditionDiffSerial||project!==projectId())return;state.conditionDiff={...state.conditionDiff,data};renderExactEvidence();setStatus("r3-status",`条件diff · minute ${data.minute} · ${data.items?.length||0} path · page ${data.next_cursor?"続きあり":"末尾"} · 表示値は入力/承認記録で実際の物理効果ではありません。`);
  }
  async function loadHistoryIndex(rebuild=false){
    const runId=$("run").value,branchId=$("branch").value;if(!runId)throw new Error("履歴索引を確認する保存Runを選択してください。");const url=`${apiRoot()}/runs/${encode(runId)}/index${rebuild?"":`?branch_id=${encode(branchId)}`}`;
    const data=await api(url,rebuild?{branch_id:branchId}:undefined),indexedHash=data.indexed?.checkpoint_hash,indexedHead=indexedHash&&data.source_checkpoint_hash?indexedHash===data.source_checkpoint_hash?"一致":"未一致":data.status==="indexed_not_verified"?"rebuild完了（未検証）":"未作成";$("history-index-result").textContent=`Run ${runId} / ${branchId} · index ${data.status} · source ${data.source_checkpoint_hash||data.checkpoint_hash||"不明"} · indexed checkpoint ${indexedHash||"none"} at minute ${data.indexed?.computed_until??"none"} · indexed head ${indexedHead} · verified ${data.verified} · ${data.limits||"GETは索引を作成しません。"}`;
  }
  function drawMap(canvas,frame,track=[]){
    const ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);ctx.font="12px sans-serif";
    const geometry=frame.geometry;if(!geometry){textPlaceholder(ctx,"このRunに物理図面が記録されていません。");return;}
    const zoom=Math.max(.1,Number(state.currentView?.zoom)||1),scale=Math.min((w-44)/geometry.width_m,(h-44)/geometry.height_m)*zoom,point=p=>[22+p.x*scale,22+p.y*scale],layer=$("layer").value;
    const agents=frame.agents||[],people=new Map((frame.people||[]).map(person=>[person.id,person])),counts=new Map();for(const agent of agents)counts.set(agent.zone_id,(counts.get(agent.zone_id)||0)+1);
    for(const zone of geometry.zones||[]){ctx.beginPath();(zone.polygon||[]).forEach((p,i)=>{const [x,y]=point(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.closePath();const density=(counts.get(zone.zone_id)||0)/Math.max(1,zone.usable_area_m2||1);ctx.fillStyle=layer==="crowd"?"hsl("+String(120*(1-Math.min(1,density)))+",45%,85%)":"#eef2ed";ctx.fill();ctx.strokeStyle="#859b91";ctx.lineWidth=1;ctx.stroke();const first=zone.polygon?.[0];if(first){const [x,y]=point(first);ctx.fillStyle="#344a41";ctx.fillText(String(zone.zone_id)+(layer==="crowd"?" · "+density.toFixed(2)+" 人/m²":""),x+4,y+15);}}
    for(const room of Object.values(frame.geometry_overlay?.temporary_rooms||{})){ctx.beginPath();(room.polygon_m||[]).forEach((p,i)=>{const [x,y]=point(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.closePath();ctx.fillStyle=room.active?"#70a9bf35":"#78817d22";ctx.fill();ctx.strokeStyle=room.active?"#286b83":"#78817d";ctx.lineWidth=2;ctx.setLineDash(room.active?[]:[5,4]);ctx.stroke();ctx.setLineDash([]);const [rx,ry]=point(room.polygon_m?.[0]||{x:0,y:0});ctx.fillStyle=room.active?"#205d70":"#66706b";ctx.fillText(`${room.room_id} · 入室割当 ${room.admitted_agent_ids?.length||0}/${room.people_capacity} · 現在位置 ${room.observed_occupant_agent_ids?.length||0} · bed obs ${room.observed_occupied_beds?.length||0}, reserved ${room.reserved_beds?.length||0}/${room.sleeping_capacity}`,rx+4,ry+14);ctx.font="10px sans-serif";ctx.fillText("室内への入室割当 · 橙:現在位置の観測 · 紫:睡眠行動の予約",rx+4,ry+27);ctx.font="12px sans-serif";for(const [index,bed] of (room.bed_points_m||[]).entries()){const [bx,by]=point(bed),occupied=(room.observed_occupied_beds||[]).some(item=>item.bed_index===index),reserved=(room.reserved_beds||[]).some(item=>item.bed_index===index);ctx.beginPath();ctx.arc(bx,by,5,0,Math.PI*2);ctx.fillStyle=occupied?"#c46a36":reserved?"#8061aa":"#488b73";ctx.fill();ctx.strokeStyle="#fff";ctx.lineWidth=1;ctx.stroke();}}
    const usage=frame.movement?.edge_usage_agent_minutes||{},limit=window.ShelterSimMapLOD?.limitRelated,routeResult=limit?limit(geometry.routes||[],zoom,route=>Number(usage[route.edge_id]||0)>0,100,280,700):{items:geometry.routes||[]};
    for(const route of routeResult.items){ctx.beginPath();(route.points||[]).forEach((p,i)=>{const [x,y]=point(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.lineWidth=layer==="crowd"?Math.min(8,1+Math.log1p(usage[route.edge_id]||0)):2;ctx.strokeStyle=usage[route.edge_id]?"#c78550":"#9eafb2";ctx.stroke();}
    const visibleAgents=agents.filter(agent=>agent.point&&agent.position_kind==="physical"),positions=new Map(visibleAgents.map(agent=>[agent.agent_id,agent.point])),needle=$("search").value.trim().toLowerCase(),visible=visibleAgents.filter(agent=>agent.agent_id.toLowerCase().includes(needle)),selectedPerson=state.currentView?.selected?.kind==="person"?state.currentView.selected.id:null,selectedHousehold=state.currentView?.selected?.kind==="group"?state.currentView.selected.id:$("household").value,priority=new Set(visible.filter(agent=>{const person=people.get(agent.agent_id);return agent.agent_id===selectedPerson||(selectedHousehold&&(person?.household_id||person?.group_id)===selectedHousehold);}).map(agent=>agent.agent_id));
    const edgeView=limit?limit(frame.social?.edges||[],zoom,edge=>(edge.pair||[]).some(id=>priority.has(id)),80,240,600):{items:frame.social?.edges||[]};
    if(layer==="social")for(const edge of edgeView.items){if(!edge.pair||!positions.has(edge.pair[0])||!positions.has(edge.pair[1]))continue;const [x1,y1]=point(positions.get(edge.pair[0])),[x2,y2]=point(positions.get(edge.pair[1]));ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.strokeStyle=edge.trust>0.5?"#3e9b7790":"#9e778090";ctx.lineWidth=1;ctx.stroke();}
    const pointGroups=window.ShelterSimMapLOD?.pointGroups,rows=visible.map(agent=>({agent,person:people.get(agent.agent_id)||{id:agent.agent_id}})),lod=pointGroups?pointGroups(rows,row=>row.agent.point,point,{width:w,height:h,zoom,maxDirect:140,groupKey:row=>row.agent.zone_id,forceIndividual:row=>priority.has(row.agent.agent_id)}):{groups:rows.map(row=>({kind:"item",item:row,x:point(row.agent.point)[0],y:point(row.agent.point)[1]}))};
    for(const group of lod.groups){if(group.kind==="cluster"){ctx.fillStyle="#477967";ctx.beginPath();ctx.arc(group.x,group.y,Math.min(16,5+Math.sqrt(group.count)),0,Math.PI*2);ctx.fill();ctx.fillStyle="#fff";ctx.textAlign="center";ctx.textBaseline="middle";ctx.fillText(String(group.count),group.x,group.y);ctx.textAlign="start";ctx.textBaseline="alphabetic";continue;}const row=group.item,person=row.person,[x,y]=[group.x,group.y],stress=Number(person.stress||0),health=Math.max(Number(person.needs?.fatigue||0),Number(person.needs?.hunger||0),Number(person.needs?.hydration_need||0));let fill="#477967";if(layer==="stress")fill="hsl("+String(120*(1-Math.max(0,Math.min(1,stress))))+",65%,43%)";else if(layer==="health")fill="hsl("+String(120*(1-Math.max(0,Math.min(1,health))))+",65%,43%)";else if(layer==="social")fill=colorFor(person.group_id||person.household_id||person.id);else if(person.activity==="moving")fill="#267baf";else if(person.activity==="queued")fill="#cf7a31";ctx.fillStyle=fill;ctx.beginPath();ctx.arc(x,y,zoom>=2.5?4:3.5,0,Math.PI*2);ctx.fill();}
    const facilities=frame.facilities||[],facilityLod=pointGroups?pointGroups(facilities,f=>f.point,point,{width:w,height:h,zoom,maxDirect:30,forceIndividual:()=>false}):{groups:facilities.filter(f=>f.point).map(item=>({kind:"item",item,x:point(item.point)[0],y:point(item.point)[1]}))};
    for(const group of facilityLod.groups){if(group.kind==="cluster"){ctx.fillStyle="#315e87";ctx.beginPath();ctx.arc(group.x,group.y,Math.min(15,5+Math.sqrt(group.count)),0,Math.PI*2);ctx.fill();ctx.fillStyle="#fff";ctx.textAlign="center";ctx.textBaseline="middle";ctx.fillText(String(group.count),group.x,group.y);ctx.textAlign="start";ctx.textBaseline="alphabetic";continue;}const facility=group.item;ctx.fillStyle=(facility.working_units||0)>0?"#294e76":"#a53838";ctx.fillRect(group.x-5,group.y-5,10,10);}
    const staff=frame.staff||[],staffLod=pointGroups?pointGroups(staff.filter(s=>s.position?.point),s=>s.position.point,point,{width:w,height:h,zoom,maxDirect:24}):{groups:staff.filter(s=>s.position?.point).map(item=>({kind:"item",item,x:point(item.position.point)[0],y:point(item.position.point)[1]}))};
    for(const group of staffLod.groups){if(group.kind==="cluster"){ctx.fillStyle="#825ca8";ctx.beginPath();ctx.arc(group.x,group.y,Math.min(14,5+Math.sqrt(group.count)),0,Math.PI*2);ctx.fill();ctx.fillStyle="#fff";ctx.textAlign="center";ctx.textBaseline="middle";ctx.fillText(String(group.count),group.x,group.y);ctx.textAlign="start";ctx.textBaseline="alphabetic";continue;}ctx.fillStyle="#825ca8";ctx.beginPath();ctx.moveTo(group.x,group.y-6);ctx.lineTo(group.x-5,group.y+5);ctx.lineTo(group.x+5,group.y+5);ctx.closePath();ctx.fill();}
    if(track.length>1){ctx.strokeStyle="#111827";ctx.lineWidth=3;ctx.beginPath();track.forEach((p,i)=>{const [x,y]=point(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();for(const p of track){const [x,y]=point(p);ctx.fillStyle="#e15b36";ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();}}
    for(const event of frame.events||[]){if(typeof event.minute==="number"&&event.minute>=frame.minute)continue;const p=event.point||event.payload?.point;if(!p||typeof p.x!=="number"||typeof p.y!=="number")continue;const [x,y]=point(p);ctx.fillStyle="#b3261e";ctx.beginPath();ctx.moveTo(x,y-9);ctx.lineTo(x-7,y+6);ctx.lineTo(x+7,y+6);ctx.closePath();ctx.fill();}
  }
  function textPlaceholder(ctx,message){ctx.fillStyle="#53655e";ctx.fillText(message,18,28);}
  function colorFor(value){let hash=0;for(const c of String(value))hash=(hash*31+c.charCodeAt(0))%360;return `hsl(${hash},55%,43%)`;}
  function renderComparison(data,targets){
    const grid=$("r3-map-grid");grid.replaceChildren();const tracks=state.tracks;
    for(const [i,frame] of data.frames.entries()){
      const section=document.createElement("section");section.className="worldline-lane";const target=targets[i];
      text(section,"h4",`${frame.run_id} / ${frame.branch_id} · minute ${frame.minute}`);const canvas=document.createElement("canvas");canvas.width=720;canvas.height=440;canvas.setAttribute("aria-label",`${frame.run_id} ${frame.branch_id} の同期マップ`);section.append(canvas);drawMap(canvas,frame,tracks.get(`${frame.run_id}/${frame.branch_id}`)||[]);
      const b=target.branch;text(section,"p",`Branch起点 ${b.fork_minute}分 · parent ${b.parent_branch_id||"root"} · calculated ${b.computed_until}分 · seed ${target.run.seed??"不明"}`);const open=document.createElement("button");open.type="button";open.textContent="このRun/Branchをメイン画面で開く";open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.({run_id:target.run_id,branch_id:target.branch_id,minute:frame.minute}));section.append(open);grid.append(section);
    }
    renderCompatibility(data,targets);renderEvents(data,targets);
  }
  function renderCompatibility(data,targets){
    const box=$("r3-divergence");box.replaceChildren();text(box,"h3","条件・モデルの比較と観測された最初のKPI差");
    for(const [i,item] of (data.metadata||[]).entries()){const run=targets[i]?.run||{};text(box,"p",`${item.run_id}/${item.branch_id} · seed ${item.seed} · Scenario ${String(item.scenario_hash||"").slice(0,12)}… · config ${String(item.configuration_hash||"").slice(0,12)}… · models ${JSON.stringify(item.model_versions||{})}`);}
    const baseline=targets[0];if(baseline)text(box,"p",`比較基準: ${baseline.run_id}/${baseline.branch_id}`);
    for(const match of data.compatibility||[])text(box,"p",`${match.target?.run_id}/${match.target?.branch_id} vs baseline · same seed ${match.same_seed?"yes":"no"} · same model ${match.same_models?"yes":"no"} · same config ${match.same_configuration?"yes":"no"} · same initial conditions ${match.same_initial_conditions?"yes":"no"}`);
    text(box,"p","保存Branchが持つ起点・親Branchは各マップ下に表示します。公開された比較APIに介入文の完全なdiffはないため、介入の詳細が必要な場合は保存済み因果traceを参照してください。KPIの最初の差は観測点であり、介入の因果効果を証明しません。","muted");
  }
  function renderEvents(data,targets){
    const box=$("r3-events");box.replaceChildren();text(box,"h3","同期時点の出来事・因果イベント");let count=0;
    for(const [i,frame] of data.frames.entries())for(const event of (frame.events||[]).filter(item=>typeof item.minute!=="number"||item.minute<frame.minute)){count++;const row=document.createElement("div"),button=document.createElement("button");button.type="button";button.textContent=`${frame.run_id}/${frame.branch_id} · ${event.minute??frame.minute} · ${event.event_type||"Event"} · ${event.status||"記録"} · ${event.event_id||"ID未記録"}`;button.addEventListener("click",()=>loadCausalTrace(targets[i],event).catch(error=>setStatus("r3-status",error.message)));const open=document.createElement("button");open.type="button";open.textContent="Event発生後の記録へ";open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.({run_id:targets[i].run_id,branch_id:targets[i].branch_id,minute:Math.min(targets[i].branch.computed_until,Math.max(targets[i].branch.fork_minute,(event.minute??frame.minute)+1)),event_id:event.event_id}));row.append(button,open);box.append(row);}
    if(!count)text(box,"p","この同期時点に表示できるEventはありません。");
  }
  async function loadCausalTrace(target,event){
    if(!event.event_id)throw new Error("このEventには因果traceを参照するIDがありません。");
    const minute=state.comparison?.minute??Number($("r3-minute").value),start=target.branch.fork_minute,end=Math.min(minute-1,target.branch.computed_until);
    if(typeof event.minute==="number"&&event.minute>=minute)throw new Error("このEventは選択したbefore_tick境界以後なので、表示中の状態の根拠に含めません。");
    if(start>end)throw new Error("因果traceを取得できる計算済み範囲がありません。");
    const params=query({kind:"causal",start,end,step:1,limit:100,event_id:event.event_id});const result=await api(`${apiRoot()}/runs/${encode(target.run_id)}/branches/${encode(target.branch_id)}/history?${params}`);
    const box=$("r3-trajectories");box.replaceChildren();text(box,"h3",`因果trace · ${target.run_id}/${target.branch_id} · ${event.event_id}`);text(box,"p",`root ${result.root_present?"保存されています":"記録で確認できません"} · truncated ${result.causal_graph_truncated?"yes":"no"} · retained ${result.total_items} records`);const open=document.createElement("button");open.type="button";open.textContent="因果traceのEvent発生後の保存境界を開く";open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.({run_id:target.run_id,branch_id:target.branch_id,minute:Math.min(target.branch.computed_until,Math.max(target.branch.fork_minute,(event.minute??minute)+1)),event_id:event.event_id}));box.append(open);text(box,"pre",JSON.stringify(result.items,null,2),"workspace-preview");
  }
  async function loadKpiSeries(comparison,serial){
    const {targets,lower,minute}=comparison;let start=Math.max(lower,Number($("r3-start").value||lower));if(start>minute)start=minute;$("r3-start").value=String(start);
    const requested=Math.max(1,Number($("r3-step").value||60)),step=Math.max(requested,Math.ceil(Math.max(0,minute-start)/99));
    if(step!==requested)setStatus("r3-status",`KPI曲線間隔を ${step}分に広げ、APIの1ページ上限内に収めています。`);
    // Frames describe the state before tick M; KPI records are end_of_tick.
    // Exclude KPI M so the curve does not show a state from after the map boundary.
    const kpiEnd=minute-1;
    const all=await Promise.all(targets.map(async target=>{
      if(start>kpiEnd)return {...target,items:[],coverage:"no end_of_tick KPI before this frame boundary"};
      const params=query({kind:"kpis",start,end:kpiEnd,step,limit:100});const result=await api(`${apiRoot()}/runs/${encode(target.run_id)}/branches/${encode(target.branch_id)}/history?${params}`);return {...target,items:result.items||[],coverage:result.next_cursor?"additional pages not shown":"page complete"};
    }));
    if(serial!==state.comparisonSerial)return;
    comparison.series=all;comparison.seriesStep=step;drawKpiChart(comparison);
  }
  function drawKpiChart(comparison){
    const canvas=$("r3-kpi-chart"),ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);const series=comparison.series||[];
    if(!series.length){textPlaceholder(ctx,"同期比較を実行してください。");return;}
    const metrics=[["stress_mean","平均ストレス"],["toilet_queue_length","トイレ待ち"],["staff_load_ratio","スタッフ負荷"],["water_ml_remaining","残水量 mL"]],left=150,right=30,top=24,rowHeight=100,plotWidth=w-left-right,allMinutes=series.flatMap(s=>s.items.map(r=>r.minute_index));if(!allMinutes.length){textPlaceholder(ctx,"指定範囲のKPI履歴はありません。");return;}const start=Math.min(...allMinutes),end=Math.max(...allMinutes);
    metrics.forEach(([key,label],row)=>{
      const y0=top+row*rowHeight;ctx.strokeStyle="#d6dfdb";ctx.beginPath();ctx.moveTo(left,y0+70);ctx.lineTo(w-right,y0+70);ctx.stroke();ctx.fillStyle="#203b33";ctx.fillText(label,10,y0+24);
      const values=series.flatMap(s=>s.items.map(item=>item[key]).filter(value=>typeof value==="number"&&Number.isFinite(value)));if(!values.length){ctx.fillStyle="#72817b";ctx.fillText("記録なし",left+8,y0+28);return;}
      const min=Math.min(...values),max=Math.max(...values),range=max-min||1;
      series.forEach((target,index)=>{const color=colors[index%colors.length];ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=2;ctx.beginPath();let started=false;for(const item of target.items){const value=item[key];if(typeof value!=="number"||!Number.isFinite(value))continue;const x=left+(end===start?0:(item.minute_index-start)/(end-start))*plotWidth,y=y0+65-(value-min)/range*48;started?ctx.lineTo(x,y):ctx.moveTo(x,y);started=true;}ctx.stroke();ctx.fillText(`${target.run_id}/${target.branch_id}`,left+index*150,y0+88);});
      ctx.fillStyle="#65746e";ctx.fillText(`${min.toPrecision(3)} – ${max.toPrecision(3)}`,w-120,y0+20);
    });
    ctx.fillStyle="#53655e";ctx.fillText(`${start}分`,left,top+metrics.length*rowHeight-5);ctx.fillText(`${end}分`,w-right-35,top+metrics.length*rowHeight-5);
    const baseline=series[0],byMinute=new Map(baseline.items.map(item=>[item.minute_index,item]));let first=null;
    for(const minute of [...new Set(series.flatMap(s=>s.items.map(item=>item.minute_index)))].sort((a,b)=>a-b)){const base=byMinute.get(minute);if(!base)continue;const different=series.slice(1).some(s=>{const row=s.items.find(item=>item.minute_index===minute);return row&&metrics.some(([key])=>typeof base[key]==="number"&&typeof row[key]==="number"&&Math.abs(base[key]-row[key])>1e-9);});if(different){first=minute;break;}}
    const box=$("r3-divergence");if(first!==null)text(box,"p",`このサンプル間隔で最初に観測できたKPI差: end_of_tick minute ${first} · ${comparison.seriesStep}分間隔。間隔より前の差は評価できません。`);else text(box,"p",`サンプルしたKPIに差を確認できませんでした（${comparison.seriesStep}分間隔）。未取得・未記録の細かな差は含みません。`);
    text(box,"p",`KPIはend_of_tick、地図はbefore_tickです。グラフは表示境界より後の値を含みません。${comparison.series?.some(item=>item.coverage?.includes("additional pages"))?"一部Runに未取得の追加ページがあります。":"各Runの取得ページに追加ページはありません。"}`,"muted");
  }
  async function loadTrajectory(kind){
    if(!state.comparison)await loadComparison();const comparison=state.comparison,targets=comparison.targets;
    const serial=state.comparisonSerial;
    const id=$(kind==="people"?"r3-agent":"r3-household").value.trim();if(!id)throw new Error(`${kind==="people"?"人物":"世帯"}IDを入力してください。`);
    const start=Math.max(comparison.lower,Number($("r3-start").value||comparison.lower)),end=comparison.minute,step=Math.max(1,Math.ceil(Math.max(0,end-start)/19));
    setStatus("r3-status",`${kind==="people"?"人物":"世帯"}のbefore_tick位置記録を取得中…`);
    const records=await Promise.all(targets.map(async target=>{
      const params=query({kind,start,end,step,limit:20,[kind==="people"?"agent_id":"household_id"]:id});
      try{return {...target,history:await api(`${apiRoot()}/runs/${encode(target.run_id)}/branches/${encode(target.branch_id)}/history?${params}`)};}catch(error){return {...target,error:error.message};}
    }));
    if(serial!==state.comparisonSerial||comparison!==state.comparison)return;
    state.tracks.clear();const box=$("r3-trajectories");box.replaceChildren();text(box,"h3",`${kind==="people"?"人物":"世帯"} ${id} · 正確なbefore_tick位置記録`);
    for(const item of records){
      const key=`${item.run_id}/${item.branch_id}`,points=[];if(item.history)for(const sample of item.history.items||[])for(const pos of sample.positions||[])if(pos.point&&pos.position_kind==="physical")points.push(pos.point);
      if(points.length)state.tracks.set(key,points);
      text(box,"p",item.error?`${key} · ${item.error}`:`${key} · ${item.history.items?.length||0} samples · ${points.length} physical positions · step ${step}min · ${item.history.membership_basis||"person ID"}`);
      if(item.history)for(const sample of item.history.items||[]){const positions=(sample.positions||[]).filter(pos=>pos.point&&pos.position_kind==="physical");const row=document.createElement("div");text(row,"p",`minute ${sample.minute} (${sample.time_semantics}) · ${positions.length?positions.map(pos=>`${pos.agent_id} (${pos.point.x.toFixed(2)}, ${pos.point.y.toFixed(2)})`).join("; "):"物理座標なし / 人物不在"}`);const target={run_id:item.run_id,branch_id:item.branch_id,minute:sample.minute};if(kind==="people")target.person_id=id;else target.household_id=id;const open=document.createElement("button");open.type="button";open.textContent="この保存時刻をメインマップで開く";open.addEventListener("click",()=>window.ShelterSimNavigateTarget?.(target));row.append(open);box.append(row);}
    }
    const grid=$("r3-map-grid");for(const [index,frame] of (comparison.result.frames||[]).entries()){const canvas=grid.children[index]?.querySelector("canvas");if(canvas)drawMap(canvas,frame,state.tracks.get(`${frame.run_id}/${frame.branch_id}`)||[]);}
    text(box,"p","線は保存された物理座標サンプル間を結んだ表示です。サンプル間の連続移動や未記録位置を補間しません。","muted");setStatus("r3-status",`軌跡取得完了 · ${records.length} Run/Branch · サンプル間隔 ${step}分`);
  }

  const map=$("map"),mapHost=document.createElement("div"),mapOverlay=document.createElement("canvas"),mapOverlayCtx=mapOverlay.getContext("2d");
  mapHost.className="workspace-map-overlay-host";map.parentNode.insertBefore(mapHost,map);mapHost.append(map,mapOverlay);mapOverlay.className="workspace-map-overlay";mapOverlay.width=map.width;mapOverlay.height=map.height;mapOverlay.setAttribute("aria-hidden","true");
  function drawHistoryTrail(){
    mapOverlay.width=map.width;mapOverlay.height=map.height;mapOverlayCtx.clearRect(0,0,mapOverlay.width,mapOverlay.height);
    const current=state.currentView,page=state.historyTrail;if(!current?.frame?.geometry||!page)return;
    const branchScope=`${current.project}/${current.run}/${current.branch}`;if(state.historyTrailScope!==branchScope)return;
    const tracks=new Map();for(const sample of page.items||[]){if(typeof sample.minute==="number"&&sample.minute>current.minute)continue;for(const position of sample.positions||[])if(position.point&&position.position_kind==="physical"){const rows=tracks.get(position.agent_id)||[];rows.push({minute:sample.minute,point:position.point});tracks.set(position.agent_id,rows);}}
    if(![...tracks.values()].some(points=>points.length>1))return;
    const geometry=current.frame.geometry,scale=Math.min((mapOverlay.width-40)/geometry.width_m,(mapOverlay.height-40)/geometry.height_m)*(current.zoom||1),point=p=>[20+(current.panX||0)+p.x*scale,20+(current.panY||0)+p.y*scale];
    for(const [agent,rows] of tracks){if(rows.length<2)continue;rows.sort((a,b)=>a.minute-b.minute);mapOverlayCtx.strokeStyle=colorFor(agent);mapOverlayCtx.fillStyle=colorFor(agent);mapOverlayCtx.lineWidth=3;mapOverlayCtx.beginPath();rows.forEach((row,index)=>{const [x,y]=point(row.point);index?mapOverlayCtx.lineTo(x,y):mapOverlayCtx.moveTo(x,y);});mapOverlayCtx.stroke();for(const row of rows){const [x,y]=point(row.point);mapOverlayCtx.beginPath();mapOverlayCtx.arc(x,y,4,0,Math.PI*2);mapOverlayCtx.fill();mapOverlayCtx.fillText(`${agent} · ${row.minute}`,x+5,y-5);}}
  }
  window.addEventListener("sheltersim:history",event=>{
    const detail=event.detail||{},page=detail.page;if(!page||!["people","households"].includes(page.kind))return;
    state.historyTrail=page;state.historyTrailScope=`${detail.project}/${detail.run}/${detail.branch}`;drawHistoryTrail();
  });

  /* Raster/standard plan canvas. Drawn features are operator-confirmed drafts;
     imported candidates remain separate until explicitly retraced and reviewed. */
  const pc=$("plan-canvas"),ctx=pc.getContext("2d"),editor={image:null,imageName:null,assetSha:null,assetStored:false,assetMediaType:null,objectUrl:null,pdfUrl:null,pdfDocument:null,pdfBytes:null,pdfPageSerial:0,routePreviewSerial:0,standard:null,space:"blank",width:900,height:560,metersPerPixel:null,calibration:[],features:[],candidates:[],reviewDraft:{},pending:[],selected:null,next:1,extractionPreviewId:null,proposalPreviewId:null,geometryPreview:null,geometryPreviewBaseId:null,approvedGeometryId:null,approvedGeometryRecord:null,rasterPreview:null,rasterSerial:0};
  function resetPlanEditing(){++editor.rasterSerial;++editor.pdfPageSerial;++editor.routePreviewSerial;editor.rasterPreview=null;editor.pdfDocument=null;editor.pdfBytes=null;if(editor.objectUrl)URL.revokeObjectURL(editor.objectUrl);if(editor.pdfUrl?.startsWith("blob:"))URL.revokeObjectURL(editor.pdfUrl);editor.objectUrl=null;editor.pdfUrl=null;$("plan-pdf-preview").replaceChildren();$("plan-pdf-preview").hidden=true;editor.features=[];editor.candidates=[];editor.pending=[];editor.calibration=[];editor.selected=null;editor.metersPerPixel=null;editor.extractionPreviewId=null;editor.proposalPreviewId=null;editor.geometryPreview=null;editor.geometryPreviewBaseId=null;editor.approvedGeometryId=null;editor.approvedGeometryRecord=null;editor.image=null;editor.standard=null;editor.space="blank";editor.imageName=null;editor.width=900;editor.height=560;$("geometry-id").value="";$("plan-json").value="";$("plan-arrival-routes").value="";$("plan-arrival-routes").disabled=true;$("plan-route-preview").disabled=true;$("plan-route-status").textContent="最初のGeometryを保存した後、測量・運営者が記録した外部経路を追加し、新しいGeometry Revisionとして検証できます。既存Geometryは変更されません。";$("plan-geometry-approve-check").checked=false;$("plan-geometry-approve").disabled=true;$("plan-apply-geometry").disabled=true;}
  function planLayout(){
    const width=editor.width||900,height=editor.height||560,scale=Math.min((pc.width-40)/width,(pc.height-40)/height);return {scale,left:(pc.width-width*scale)/2,top:(pc.height-height*scale)/2,width,height};
  }
  function sourcePoint(x,y){const l=planLayout();return {x:(x-l.left)/l.scale,y:(y-l.top)/l.scale};}
  function canvasPoint(p){const l=planLayout();return [l.left+p.x*l.scale,l.top+p.y*l.scale];}
  function drawPlan(){
    ctx.clearRect(0,0,pc.width,pc.height);ctx.fillStyle="#fbfcfb";ctx.fillRect(0,0,pc.width,pc.height);const layout=planLayout();
    if(editor.image)ctx.drawImage(editor.image,layout.left,layout.top,layout.width*layout.scale,layout.height*layout.scale);
    else if(editor.standard){
      const g=editor.standard;ctx.fillStyle="#f3f6f3";ctx.fillRect(layout.left,layout.top,layout.width*layout.scale,layout.height*layout.scale);
      for(const zone of g.zones||[]){ctx.beginPath();(zone.polygon||[]).forEach((p,i)=>{const [x,y]=canvasPoint(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.closePath();ctx.fillStyle="#e6eee9";ctx.fill();ctx.strokeStyle="#718b7f";ctx.stroke();const [x,y]=canvasPoint(zone.polygon?.[0]||{x:0,y:0});ctx.fillStyle="#263c32";ctx.fillText(zone.zone_id,x+3,y+14);}
      for(const route of g.routes||[]){ctx.beginPath();(route.points||[]).forEach((p,i)=>{const [x,y]=canvasPoint(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.strokeStyle="#9caeb1";ctx.lineWidth=2;ctx.stroke();}
    }else{ctx.strokeStyle="#e5eae7";for(let x=layout.left;x<layout.left+layout.width*layout.scale;x+=32){ctx.beginPath();ctx.moveTo(x,layout.top);ctx.lineTo(x,layout.top+layout.height*layout.scale);ctx.stroke();}for(let y=layout.top;y<layout.top+layout.height*layout.scale;y+=32){ctx.beginPath();ctx.moveTo(layout.left,y);ctx.lineTo(layout.left+layout.width*layout.scale,y);ctx.stroke();}ctx.fillStyle="#53655e";ctx.fillText("標準図面エリア · 1 canvas unit = 1 m once calibrated",18,24);}
    for(const feature of editor.features){const coords=feature.points.map(canvasPoint);ctx.setLineDash([]);ctx.strokeStyle=feature.type==="wall"?"#34443e":feature.type==="door"?"#d58b37":feature.type==="route"?"#2a78a5":feature.type==="zone"?"#367b68":"#8153a2";ctx.fillStyle=feature.type==="zone"?"#367b6830":ctx.strokeStyle;ctx.lineWidth=feature.id===editor.selected?4:2;ctx.beginPath();coords.forEach(([x,y],i)=>{i?ctx.lineTo(x,y):ctx.moveTo(x,y);});if(feature.type==="zone")ctx.closePath();if(feature.type==="facility")ctx.arc(coords[0]?.[0]||0,coords[0]?.[1]||0,7,0,Math.PI*2);if(feature.type==="zone")ctx.fill();ctx.stroke();ctx.fillStyle="#233a32";if(coords[0])ctx.fillText(feature.name,coords[0][0]+5,coords[0][1]-5);if(feature.id===editor.selected)for(const [x,y] of coords){ctx.fillStyle="#fff";ctx.strokeStyle="#111";ctx.beginPath();ctx.arc(x,y,5,0,Math.PI*2);ctx.fill();ctx.stroke();}}
    for(const candidate of editor.candidates){const f=candidate.raw||candidate,raw=f.corrected_coordinates_px||f.coordinates_px||f.coordinates||[],coords=raw.map(value=>Array.isArray(value)?canvasPoint({x:value[0],y:value[1]}):canvasPoint(value));if(!coords.length)continue;ctx.save();ctx.setLineDash([6,4]);ctx.strokeStyle=f.review_status==="accepted"?"#27815b":f.review_status==="rejected"?"#838b87":"#d58b37";ctx.fillStyle="#e6a04422";ctx.lineWidth=2;ctx.beginPath();coords.forEach(([x,y],i)=>{i?ctx.lineTo(x,y):ctx.moveTo(x,y);});if(f.geometry_kind==="polygon"||f.kind==="zone")ctx.closePath();if(f.geometry_kind==="polygon"||f.kind==="zone")ctx.fill();ctx.stroke();ctx.setLineDash([]);ctx.fillStyle="#704b16";ctx.fillText(`候補 ${f.feature_id||f.id||"feature"}`,coords[0][0]+4,coords[0][1]-4);ctx.restore();}
    if(editor.pending.length){ctx.strokeStyle="#e09b40";ctx.setLineDash([5,4]);ctx.beginPath();editor.pending.map(canvasPoint).forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();ctx.setLineDash([]);}
    if(editor.calibration.length){ctx.fillStyle="#af4938";for(const p of editor.calibration){const [x,y]=canvasPoint(p);ctx.beginPath();ctx.arc(x,y,5,0,Math.PI*2);ctx.fill();}}
    ctx.setLineDash([]);updatePlanFeatureList();updatePlanTrace();
  }
  function slug(value){return String(value||"item").trim().toLowerCase().replace(/[^a-z0-9_-]+/g,"-").replace(/^-+|-+$/g,"").slice(0,32)||"item";}
  function updateFeatureSelectors(){
    const zones=editor.features.filter(item=>item.type==="zone"),walls=editor.features.filter(item=>item.type==="wall");
    options($("plan-wall"),[{value:"",label:"扉を紐づける壁"},...walls.map(item=>({value:item.id,label:item.name}))],[$("plan-wall").value].filter(Boolean));
    for(const id of ["plan-from","plan-to"])options($(id),[{value:"",label:"Zone"},...zones.map(item=>({value:item.id,label:item.name}))],[$(id).value].filter(Boolean));
  }
  function renderStaffShiftRows(records=[]){
    const box=$("shift-rows");box.replaceChildren();
    for(const record of records){const row=document.createElement("div");row.className="equipment-row staff-shift-row";
      for(const [field,label,type,defaultValue] of [["staff_id","スタッフID","text","staff_001"],["start_minute","不在開始 minute","number",0],["end_minute","不在終了 minute","number",60],["repeat_every_minutes","繰返し (任意)","number",1440]]){const wrap=document.createElement("label");wrap.textContent=label;const input=document.createElement("input");input.type=type;input.dataset.field=field;input.value=record[field]??defaultValue;if(type==="number"){input.min="0";input.step="1";}input.addEventListener("input",()=>$("request").dispatchEvent(new Event("input",{bubbles:true})));wrap.append(input);row.append(wrap);}
      const remove=document.createElement("button");remove.type="button";remove.textContent="予定を削除";remove.addEventListener("click",()=>{row.remove();$("request").dispatchEvent(new Event("input",{bubbles:true}));});row.append(remove);box.append(row);
    }
  }
  function updatePlanFeatureList(){
    const box=$("plan-feature-list");box.replaceChildren();updateFeatureSelectors();
    text(box,"h4",`確定済みトレース · ${editor.features.length}`);
    for(const feature of editor.features){const row=document.createElement("div"),select=document.createElement("button");select.type="button";select.textContent=`確定 · ${feature.type} · ${feature.name} · ${feature.points.length} points`;select.setAttribute("aria-pressed",String(editor.selected===feature.id));select.addEventListener("click",()=>{editor.selected=feature.id;drawPlan();});row.append(select);box.append(row);}
    text(box,"h4",`抽出・取込候補 · ${editor.candidates.length}`);
    for(const item of editor.candidates){
      const feature=item.raw||item,row=document.createElement("article");row.className="candidate-feature";row.dataset.featureId=feature.feature_id||item.id||"";
      const coords=feature.corrected_coordinates_px||feature.coordinates_px||feature.coordinates||[],attrs={...(feature.attributes||{}),...(feature.corrected_attributes||{})};
      text(row,"strong",`未確定候補 · ${feature.feature_kind||item.kind||"feature"} · ${attrs.name||item.name||feature.feature_id||item.id||"名称なし"} · confidence ${feature.confidence??"未提供"} · ${feature.review_status||"pending"}`);
      if(feature.feature_id){
        const local=editor.reviewDraft[feature.feature_id]||{};const decision=document.createElement("label");decision.textContent="人による判断";const select=document.createElement("select");select.dataset.field="decision";for(const [value,label] of [["","未確認"],["accepted","採用"],["rejected","除外"]]){const option=document.createElement("option");option.value=value;option.textContent=label;select.append(option);}select.value=local.decision??(feature.review_status==="pending"?"":feature.review_status);select.disabled=feature.review_status!=="pending";decision.append(select);row.append(decision);
        const coordinateLabel=document.createElement("label");coordinateLabel.textContent="補正座標 px (JSON)";const coordinateInput=document.createElement("textarea");coordinateInput.dataset.field="coordinates";coordinateInput.rows=2;coordinateInput.value=local.coordinates_text??JSON.stringify(local.coordinates??coords);coordinateLabel.append(coordinateInput);row.append(coordinateLabel);
        const attributesLabel=document.createElement("label");attributesLabel.textContent="属性 (JSON) · zone_id/name、wall_id/width_m等";const attributesInput=document.createElement("textarea");attributesInput.dataset.field="attributes";attributesInput.rows=3;attributesInput.value=local.attributes_text??JSON.stringify(local.attributes??attrs,null,2);attributesLabel.append(attributesInput);row.append(attributesLabel);
        const noteLabel=document.createElement("label");noteLabel.textContent="確認メモ";const note=document.createElement("input");note.dataset.field="note";note.maxLength=500;note.value=local.note??feature.review_note??"";noteLabel.append(note);row.append(noteLabel);
      }else text(row,"p","この入力は抽出APIの候補ではありません。形状は未確定のままです。");
      box.append(row);
    }
    if(!editor.features.length&&!editor.candidates.length)text(box,"p","候補・確定情報はありません。図面上で点を置いて確定してください。");
  }
  const captureReviewDraft=event=>{
    const row=event.target.closest(".candidate-feature[data-feature-id]");if(!row)return;const featureId=row.dataset.featureId,local={...(editor.reviewDraft[featureId]||{})};
    for(const input of row.querySelectorAll("[data-field]")){const value=input.value;if(input.dataset.field==="decision")local.decision=value;else if(input.dataset.field==="coordinates"){try{local.coordinates=JSON.parse(value);delete local.coordinates_text;}catch{local.coordinates_text=value;delete local.coordinates;}}else if(input.dataset.field==="attributes"){try{local.attributes=JSON.parse(value);delete local.attributes_text;}catch{local.attributes_text=value;delete local.attributes;}}else local.note=value;}
    editor.reviewDraft[featureId]=local;
  };
  $("plan-feature-list").addEventListener("input",captureReviewDraft);$("plan-feature-list").addEventListener("change",captureReviewDraft);
  function updatePlanTrace(){
    const mpp=editor.metersPerPixel,zoneFeatures=editor.features.filter(item=>item.type==="zone"),width=editor.width,height=editor.height,px=point=>[point.x,point.y];
    const trace={raster_pixel_width:Math.round(width),raster_pixel_height:Math.round(height),building_width_m:mpp?width*mpp:null,building_height_m:mpp?height*mpp:null,
      zones:zoneFeatures.map(item=>({zone_id:item.id,name:item.name,pixel_polygon:item.points.map(px),usable_fraction:item.usable_fraction})),
      facilities:editor.features.filter(item=>item.type==="facility"&&Number.isFinite(item.capacity)&&item.zone_id).map(item=>({id:item.id,kind:item.kind||"other",zone_id:item.zone_id,capacity:item.capacity,pixel_x:item.points[0].x,pixel_y:item.points[0].y})),
      routes:editor.features.filter(item=>item.type==="route"&&item.from_zone_id&&item.to_zone_id&&item.width_m>0).map(item=>({edge_id:item.id,from_zone_id:item.from_zone_id,to_zone_id:item.to_zone_id,points_px:item.points.map(px),width_m:item.width_m,accessible:item.accessible})),
      walls:editor.features.filter(item=>item.type==="wall").map(item=>({wall_id:item.id,start_px:px(item.points[0]),end_px:px(item.points[1])})),
      openings:editor.features.filter(item=>item.type==="door"&&item.wall_id&&item.width_m>0).map(item=>({opening_id:item.id,wall_id:item.wall_id,start_px:px(item.points[0]),end_px:px(item.points[1]),width_m:item.width_m,opening_kind:"doorway"})),
      reviewed_feature_ids:editor.features.map(item=>item.id)};
    $("plan-trace-json").value=JSON.stringify({editor_schema_version:1,coordinate_space:editor.space,source_asset_sha256:editor.assetSha,source_page_index:editor.pdfDocument?editor.pdfPageIndex:editor.rasterPreview?.page_index??null,manual_plan_trace:trace},null,2);
    const needs=[];if(!mpp)needs.push("縮尺が未確定");if(!zoneFeatures.length)needs.push("部屋/Zoneが未確定");if(editor.features.some(item=>item.type==="facility"&&(!Number.isFinite(item.capacity)||!item.zone_id)))needs.push("容量/Zone未入力の設備あり");if(editor.features.some(item=>item.type==="door"&&(!item.wall_id||!(item.width_m>0))))needs.push("壁との紐づけ/幅が未確定の扉あり");if(editor.features.some(item=>item.type==="route"&&(!item.from_zone_id||!item.to_zone_id||!(item.width_m>0))))needs.push("始点/終点Zone/幅が未設定の経路あり");
    setStatus("plan-editor-status",`${editor.imageName|| (editor.standard?"Scenario標準Geometry":"標準図面エリア")} · ${mpp?`縮尺 ${mpp.toFixed(4)} m / coordinate unit`:"縮尺未校正"} · ${needs.length?needs.join(" · "):"トレース草案の必須項目を入力済み"} · ${editor.assetSha?`${editor.assetStored?"保存済み":"未保存"} SHA-256 ${editor.assetSha}`:"図面Asset未登録"}。抽出候補は確定済みGeometryへ変換していません。`);
  }
  function markPlanEdited(){
    const invalidated=Boolean(editor.geometryPreview||editor.approvedGeometryId||$("geometry-id").value);
    ++editor.routePreviewSerial;editor.geometryPreview=null;editor.geometryPreviewBaseId=null;editor.approvedGeometryId=null;editor.approvedGeometryRecord=null;$("plan-arrival-routes").value="";$("plan-arrival-routes").disabled=true;$("plan-route-preview").disabled=true;$("plan-geometry-approve-check").checked=false;$("plan-geometry-approve").disabled=true;$("plan-apply-geometry").disabled=true;
    if(invalidated){$("geometry-id").value="";$("plan-json").value="";$("request").dispatchEvent(new Event("input",{bubbles:true}));$("plan-geometry-status").textContent="図面トレースが変更されたため、古いGeometryの選択を解除しました。編集内容で再検証して再承認してください。保存済みの旧Geometry記録は変更されません。";}
    drawPlan();$("plan-trace-json").dispatchEvent(new Event("input",{bubbles:true}));
  }
  function loadPlanCandidates(data,name,sha){
    const rows=Array.isArray(data?.features)?data.features:Array.isArray(data?.items)?data.items:[];
    editor.candidates=rows.map((feature,index)=>({id:feature.id||feature.properties?.id||`candidate-${index+1}`,kind:feature.geometry?.type||feature.feature_kind||feature.type||"feature",name:feature.properties?.name||feature.attributes?.name||feature.name||"",raw:feature}));
    editor.image=null;editor.standard=null;editor.space="vector-candidate";editor.imageName=name;editor.assetSha=sha||null;drawPlan();setStatus("plan-editor-status",`${name||"GeoJSON"} · ${editor.candidates.length}件を未確定候補として読込みました。座標系を確認し、必要な形状を画面上で手動トレースしてください。`);
  }
  function loadPlanImage(src,name,sha){
    if(editor.objectUrl&&editor.objectUrl!==src)URL.revokeObjectURL(editor.objectUrl);
    if(!editor.pdfDocument){if(editor.pdfUrl?.startsWith("blob:"))URL.revokeObjectURL(editor.pdfUrl);editor.pdfUrl=null;$("plan-pdf-preview").replaceChildren();$("plan-pdf-preview").hidden=true;}editor.objectUrl=src.startsWith("blob:")?src:null;editor.assetSha=sha||editor.assetSha||null;editor.assetStored=Boolean(sha||editor.assetStored);
    const image=new Image();image.onload=()=>{editor.image=image;editor.width=image.naturalWidth;editor.height=image.naturalHeight;editor.space="raster_pixel";editor.imageName=name;editor.standard=null;editor.metersPerPixel=null;editor.calibration=[];drawPlan();};image.onerror=()=>setStatus("plan-editor-status","画像を読み込めませんでした。画像形式かブラウザの表示制約を確認してください。");image.src=src;
  }
  async function onPlanFile(file){
    if(!file)return;
    resetPlanEditing();editor.assetMediaType=file.type;editor.assetSha=null;editor.assetStored=false;
    if(file.type.startsWith("image/")){const url=URL.createObjectURL(file);loadPlanImage(url,file.name,null);file.arrayBuffer().then(bytes=>crypto.subtle.digest("SHA-256",bytes)).then(hash=>{if($("plan-file").files[0]!==file)return;editor.assetSha=[...new Uint8Array(hash)].map(value=>value.toString(16).padStart(2,"0")).join("");editor.assetStored=false;updatePlanTrace();});}
    else if(["application/json","application/geo+json"].includes(file.type))file.text().then(raw=>loadPlanCandidates(JSON.parse(raw),file.name,null)).catch(error=>setStatus("plan-editor-status",`Vector候補を読み込めません: ${error.message}`));
    else if(file.type==="application/pdf"){const bytes=new Uint8Array(await file.arrayBuffer()),hash=await crypto.subtle.digest("SHA-256",bytes);editor.assetSha=[...new Uint8Array(hash)].map(value=>value.toString(16).padStart(2,"0")).join("");editor.assetStored=false;await openPdfDocument(bytes,file.name,{sha:editor.assetSha,pageIndex:Number($("plan-page-index").value||0)});setStatus("plan-editor-status",`${file.name} のpage ${editor.pdfPageIndex}をローカルPDF.jsで表示しています。表示ページ上で手動トレースできます。Geometry検証にはProject Assetへの保存が必要です。`);}
    else{editor.image=null;editor.imageName=file.name;editor.space="document";drawPlan();setStatus("plan-editor-status",`${file.name} は保管Assetとして扱います。`);}
  }
  async function applyRasterPreview(data){
    const reference=data.raster_preview,source=editor.assetSha,project=projectId(),page=Number($("plan-page-index").value||0),serial=++editor.rasterSerial;
    if(!reference){$("plan-extract-status").textContent+=" 共通raster previewがないため、候補座標と表示資料の重ね合わせは確認できません。";return;}
    const current=()=>serial===editor.rasterSerial&&project===projectId()&&source===editor.assetSha&&page===Number($("plan-page-index").value||0)&&data.preview_id===editor.proposalPreviewId;
    if(reference.source_asset_id!==source||reference.page_index!==page||reference.media_type!=="image/png"||!Number.isInteger(reference.width_px)||!Number.isInteger(reference.height_px)||reference.width_px<1||reference.height_px<1||reference.width_px>2400||reference.height_px>2400||!reference.asset_id){$("plan-extract-status").textContent+=" raster previewの元資料・ページ・座標系が一致しないため、重ね合わせません。";return;}
    const previous=editor.rasterPreview;
    try{
      const content=await api(`${apiRoot()}/assets/${encode(reference.asset_id)}/content`);
      if(!current())return;
      if(content.media_type!=="image/png"||typeof content.data_base64!=="string")throw new Error("raster previewのPNGデータがありません。");
      const image=new Image();
      image.onload=()=>{
        if(!current())return;
        if(image.naturalWidth!==reference.width_px||image.naturalHeight!==reference.height_px){$("plan-extract-status").textContent+=" raster previewの実寸と候補座標寸法が異なるため、重ね合わせません。";return;}
        const changed=editor.width!==reference.width_px||editor.height!==reference.height_px||previous&&previous.page_index!==reference.page_index;
        if(changed){editor.features=[];editor.pending=[];editor.calibration=[];editor.metersPerPixel=null;editor.selected=null;}
        editor.image=image;editor.standard=null;editor.width=reference.width_px;editor.height=reference.height_px;editor.space="raster_pixel";editor.rasterPreview=reference;
        drawPlan();$("plan-extract-status").textContent+=` 抽出と同じPNGを表示中 · page ${reference.page_index} · ${reference.width_px}×${reference.height_px}px · 元資料 ${source}。${changed?"座標系変更に伴い旧トレースと縮尺を解除しました。再校正してください。":""}`;
      };
      image.onerror=()=>{if(current())$("plan-extract-status").textContent+=" raster previewを描画できません。候補と表示画像の一致は未確認です。";};
      image.src=`data:image/png;base64,${content.data_base64}`;
    }catch(error){if(current())$("plan-extract-status").textContent+=` 共通raster previewを取得できません: ${error.message}。重ね合わせは未確認です。`;}
  }
  function showPlanValidation(data){
    const box=$("plan-validation-result");box.textContent=JSON.stringify({preview_id:data.preview_id,kind:data.kind,value_hash:data.value_hash,validation:data.validation,runtime_verified:data.runtime_verified,value:data.value,limits:data.limits},null,2);
    if(data.kind==="geometry"){
      const isApprovedBaseRevision=Boolean(editor.geometryPreviewBaseId&&editor.approvedGeometryRecord?.geometry_id===editor.geometryPreviewBaseId);editor.geometryPreview=data;editor.proposalPreviewId=null;if(!isApprovedBaseRevision){editor.approvedGeometryId=null;editor.approvedGeometryRecord=null;$("plan-arrival-routes").disabled=true;$("plan-route-preview").disabled=true;}$("plan-geometry-approve-check").checked=false;$("plan-geometry-approve").disabled=true;$("plan-apply-geometry").disabled=true;$("plan-geometry-status").textContent=`構造検証済みのGeometry候補 ${data.preview_id} · hash ${data.value_hash}。runtime到達性・実建物との一致は未検証です。内容を確認して明示承認してください。`;
    }else{
      editor.proposalPreviewId=data.preview_id;editor.extractionPreviewId=data.preview_id;editor.geometryPreview=null;editor.reviewDraft={};editor.candidates=(data.value?.features||[]).map(raw=>({id:raw.feature_id,kind:raw.feature_kind,name:raw.attributes?.name||raw.attributes?.zone_id||raw.feature_id,raw}));
      const size=data.value?.raster_size_px;if(Array.isArray(size)&&size.length===2){editor.width=size[0];editor.height=size[1];editor.space="raster_pixel";}
      $("plan-extract-status").textContent=`抽出候補 ${editor.candidates.length} 件 · method ${data.value?.extraction_method||"unknown"} · page ${data.value?.features?.[0]?.page_index??Number($("plan-page-index").value||0)} · ${JSON.stringify(data.value?.raster_size_px||null)} px · すべて未確定。抽出confidenceは精度保証ではありません。${data.raster_preview?" 抽出と同じraster previewを読み込みます。":""} ${(data.value?.notes||[]).join(" ")}`;drawPlan();void applyRasterPreview(data);
    }
  }
  async function extractPlan(){
    if(!editor.assetSha||!editor.assetStored)throw new Error("抽出前に図面Assetを権限確認のうえProjectへ保存してください。");
    const media=editor.assetMediaType||$("plan-file").files[0]?.type||"application/pdf",body=media==="application/json"||media==="application/geo+json"?{operation:"geojson"}:{operation:"extract",page_index:Number($("plan-page-index").value||0),max_dimension_px:Number($("plan-max-dimension").value||1600)};
    const source=editor.assetSha,project=projectId(),page=Number($("plan-page-index").value||0),serial=++editor.rasterSerial;
    markPlanEdited();let result;
    try{result=await api(`${apiRoot()}/assets/${encode(source)}/plan-preview`,body);}
    catch(error){
      const unavailable=String(error.message||"").includes("plan_extraction_unavailable");
      $("plan-extract-status").textContent=unavailable
        ?"サーバーのPDF/画像レンダラーが未導入です。管理者は sheltersim[plan-import] (Pillow + pypdfium2) を導入できます。利用者はPDFをダウンロードし、必要なページをPNG/JPEG/WebPへ変換して再アップロードしてください。ブラウザーPDF表示は図面エディタ用の画像ではありません。GeoJSON/JSONはベクター候補として取込できます。"
        :`図面の読み込みに失敗しました: ${error.message}。元Assetは変更されていません。PNG/JPEG/WebPまたはGeoJSON/JSONへ変換して再試行できます。`;
      return;
    }
    if(project!==projectId()||source!==editor.assetSha||page!==Number($("plan-page-index").value||0)||serial!==editor.rasterSerial)return;
    showPlanValidation(result);
  }
  async function reviewPlanCandidates(){
    if(!editor.proposalPreviewId||!editor.assetSha)throw new Error("先に画像/PDFの候補抽出を実行してください。");
    const reviewer=$("plan-operator").value.trim();if(!reviewer)throw new Error("確認者IDを入力してください。");const rows=[...$("plan-feature-list").querySelectorAll(".candidate-feature[data-feature-id]")],reviews=[];
    for(const row of rows){const feature=(editor.candidates.find(item=>(item.raw||item).feature_id===row.dataset.featureId)||{}).raw; if(!feature||feature.review_status!=="pending")continue;
      const decision=row.querySelector('[data-field="decision"]').value;if(!["accepted","rejected"].includes(decision))throw new Error(`候補 ${feature.feature_id} の採用/除外を選んでください。`);
      let coordinates,attributes;try{coordinates=JSON.parse(row.querySelector('[data-field="coordinates"]').value);attributes=JSON.parse(row.querySelector('[data-field="attributes"]').value);}catch{throw new Error(`候補 ${feature.feature_id} の座標または属性JSONが不正です。`);}
      reviews.push({feature_id:feature.feature_id,decision,reviewer_id:reviewer,corrected_coordinates_px:coordinates,corrected_attributes:attributes,note:row.querySelector('[data-field="note"]').value||undefined});
    }
    if(!reviews.length)throw new Error("新たに確認するpending候補がありません。");const result=await api(`${apiRoot()}/assets/${encode(editor.assetSha)}/plan-preview`,{operation:"review",proposal_preview_id:editor.proposalPreviewId,reviews});showPlanValidation(result);$("plan-extract-status").textContent=`人による候補確認を保存しました · preview ${result.preview_id} · 未確認候補も表示に残っています。`;
  }
  async function traceReviewedPlan(){
    if(!editor.assetSha||!editor.proposalPreviewId)throw new Error("抽出候補を先に作成してください。");const width=Number($("plan-building-width").value),height=Number($("plan-building-height").value),operator=$("plan-operator").value.trim();
    if(!(width>0&&height>0&&operator))throw new Error("実測の建物幅・奥行と作業者IDを入力してください。縮尺は画像から自動推定しません。");
    const result=await api(`${apiRoot()}/assets/${encode(editor.assetSha)}/plan-preview`,{operation:"reviewed_trace",reviewed_preview_id:editor.proposalPreviewId,building_width_m:width,building_height_m:height,operator_id:operator});showPlanValidation(result);
  }
  async function traceManualPlan(){
    if(!editor.assetSha||!editor.assetStored||editor.space!=="raster_pixel")throw new Error("手動Geometry検証には保存済みの画像/PDF図面が必要です。");
    const wrapped=JSON.parse($("plan-trace-json").value),trace=wrapped.manual_plan_trace;if(!trace?.building_width_m||!trace?.building_height_m||!trace.zones?.length)throw new Error("Zoneの描画と実測2点による縮尺補正を完了してください。");
    trace.operator_id=$("plan-operator").value.trim()||"operator";const page=editor.pdfDocument?editor.pdfPageIndex:editor.rasterPreview?.page_index??0;trace.source_page_index=page;
    const rasterPreviewId=editor.rasterPreview?.source_asset_id===editor.assetSha&&editor.rasterPreview.page_index===page?editor.extractionPreviewId:undefined;
    const result=await api(`${apiRoot()}/assets/${encode(editor.assetSha)}/plan-preview`,{operation:"trace",trace,page_index:page,...(rasterPreviewId?{raster_preview_id:rasterPreviewId}:{})});showPlanValidation(result);
  }
  async function approvePlanGeometry(){
    const preview=editor.geometryPreview;if(!preview||preview.kind!=="geometry"||!$("plan-geometry-approve-check").checked)throw new Error("Geometry内容を確認し、明示承認を選択してください。");
    const geometryId=`geometry-${crypto.randomUUID()}`,record=await api(`${apiRoot()}/geometries/${encode(geometryId)}`,{preview_id:preview.preview_id,asset_id:preview.asset_id,approved_hash:preview.value_hash,approved:true});editor.approvedGeometryId=record.geometry_id;editor.approvedGeometryRecord=record;editor.geometryPreview=null;editor.geometryPreviewBaseId=null;$("plan-geometry-approve-check").checked=false;$("plan-geometry-approve").disabled=true;$("plan-apply-geometry").disabled=false;$("plan-json").value=JSON.stringify(record.geometry,null,2);const routes=record.geometry?.geometry?.arrival_approach_routes||[];$("plan-arrival-routes").value=JSON.stringify(routes,null,2);$("plan-arrival-routes").disabled=false;$("plan-route-preview").disabled=false;$("plan-route-status").textContent=`Geometry ${record.geometry_id} を基準に経路付きRevisionを作成できます。既存の確定GeometryとScenarioは変更されません。登録済み経路 ${routes.length} 件。`;$('plan-geometry-status').textContent=`保存済み immutable Geometry ${record.geometry_id} · ${record.validation} · runtime_verified ${record.runtime_verified}。Scenarioには別のPreviewと明示承認が必要です。`;
  }
  async function previewArrivalRouteRevision(){
    const base=editor.approvedGeometryRecord;if(!base||base.geometry_id!==editor.approvedGeometryId||base.asset_id!==editor.assetSha)throw new Error("この図面Assetの確定済みGeometryが必要です。");
    let routes;try{routes=JSON.parse($("plan-arrival-routes").value);}catch{throw new Error("外部到着経路JSONの構文を確認してください。");}
    if(!Array.isArray(routes)||routes.length>128)throw new Error("外部到着経路は最大128件のJSON配列で指定してください。");
    const geometry=JSON.parse(JSON.stringify(base.geometry.geometry));geometry.arrival_approach_routes=routes;
    const serial=++editor.routePreviewSerial;editor.geometryPreview=null;editor.geometryPreviewBaseId=null;$("plan-geometry-approve-check").checked=false;$("plan-geometry-approve").disabled=true;$("plan-apply-geometry").disabled=true;
    const result=await api(`${apiRoot()}/assets/${encode(editor.assetSha)}/plan-preview`,{operation:"geometry_revision",base_geometry_id:base.geometry_id,base_geometry_hash:base.geometry_hash,geometry,editable_geometry:base.geometry.editable_geometry});
    if(serial!==editor.routePreviewSerial)return;
    if(result.kind!=="geometry")throw new Error("経路Revisionの検証応答がGeometryではありません。");
    editor.geometryPreviewBaseId=base.geometry_id;showPlanValidation(result);$("plan-geometry-approve").disabled=true;$("plan-route-status").textContent=`経路付きGeometry Revision ${result.preview_id} を構造検証しました。座標・Zone・出典IDを確認し、上の明示承認を選択してください。実行時の施設到達性は未検証です。`;
  }
  function featureByHit(point){
    for(const feature of [...editor.features].reverse())for(let i=0;i<feature.points.length;i++){const p=canvasPoint(feature.points[i]),c=canvasPoint(point);if(Math.hypot(p[0]-c[0],p[1]-c[1])<10)return {feature,index:i};}
    return null;
  }
  function makeFeature(points){
    const tool=$("plan-tool").value,name=$("plan-feature-name").value.trim()||`${tool}-${editor.next++}`,zoneId=$("plan-zone-id").value.trim(),generatedId=`${slug(tool)}-${slug(name)}-${editor.next++}`;
    if(tool==="zone"&&zoneId&&!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(zoneId))throw new Error("Zone IDは英数字で始まる64文字以内の英数字・ピリオド・アンダースコア・コロン・ハイフンで入力してください。");
    const id=tool==="zone"&&zoneId?zoneId:generatedId;
    if(editor.features.some(item=>item.id===id))throw new Error(`ID ${id} はすでに使われています。別のIDを指定してください。`);
    if(tool==="zone")return {id,name,type:tool,points,usable_fraction:Number($("plan-usable-fraction").value||0.9)};
    if(tool==="wall")return {id,name,type:tool,points};
    if(tool==="door")return {id,name,type:tool,points,wall_id:$("plan-wall").value||null,width_m:$("plan-distance").value===""?null:Number($("plan-distance").value)};
    if(tool==="route")return {id,name,type:tool,points,from_zone_id:$("plan-from").value||null,to_zone_id:$("plan-to").value||null,width_m:$("plan-distance").value===""?null:Number($("plan-distance").value),accessible:$("plan-accessible").checked};
    if(tool==="facility")return {id,name,type:tool,kind:name,zone_id:$("plan-from").value||null,capacity:$("plan-capacity").value===""?null:Number($("plan-capacity").value),points};
    return {id,name,type:tool,points};
  }
  function commitFeature(points){
    if(!points.length)return;const feature=makeFeature(points);editor.features.push(feature);editor.selected=feature.id;editor.pending=[];$("plan-feature-name").value=feature.type+"-"+(editor.next);$("plan-zone-id").value="";markPlanEdited();
  }
  function planClick(e){
    const rect=pc.getBoundingClientRect(),point=sourcePoint((e.clientX-rect.left)*pc.width/rect.width,(e.clientY-rect.top)*pc.height/rect.height),tool=$("plan-tool").value;
    const hit=featureByHit(point);if(hit){editor.selected=hit.feature.id;editor.drag={feature:hit.feature,index:hit.index};drawPlan();return;}
    if(tool==="calibrate"){
      editor.calibration.push(point);if(editor.calibration.length===2){const [a,b]=editor.calibration,pixels=Math.hypot(a.x-b.x,a.y-b.y),meters=Number($("plan-distance").value);if(pixels>0&&meters>0){editor.metersPerPixel=meters/pixels;editor.calibration=[];markPlanEdited();}else{setStatus("plan-editor-status","縮尺補正には2点と正の実距離を指定してください。");drawPlan();}}else drawPlan();return;
    }
    editor.pending.push(point);
    if(tool==="facility")commitFeature(editor.pending.slice(-1));
    else if(["wall","door","route"].includes(tool)&&editor.pending.length===2)commitFeature(editor.pending.slice(-2));
    drawPlan();
  }
  pc.addEventListener("pointerdown",e=>{pc.setPointerCapture(e.pointerId);planClick(e);});
  pc.addEventListener("pointermove",e=>{if(!editor.drag)return;const rect=pc.getBoundingClientRect(),point=sourcePoint((e.clientX-rect.left)*pc.width/rect.width,(e.clientY-rect.top)*pc.height/rect.height);editor.drag.feature.points[editor.drag.index]=point;drawPlan();});
  pc.addEventListener("pointerup",()=>{if(editor.drag){editor.drag=null;markPlanEdited();}});
  pc.addEventListener("dblclick",e=>{e.preventDefault();if($("plan-tool").value==="zone"&&editor.pending.length>=3){try{commitFeature(editor.pending.slice());}catch(error){drawPlan();setStatus("plan-editor-status",error.message);}}});
  $("plan-tool").addEventListener("change",()=>{editor.pending=[];editor.calibration=[];drawPlan();});
  $("plan-delete").addEventListener("click",()=>{editor.features=editor.features.filter(item=>item.id!==editor.selected);editor.selected=null;markPlanEdited();});
  $("plan-clear").addEventListener("click",()=>{if(editor.features.length&&!confirm("確定済みトレースをすべて消去しますか？"))return;editor.features=[];editor.pending=[];editor.selected=null;editor.metersPerPixel=null;editor.calibration=[];markPlanEdited();});
  $("plan-file").addEventListener("change",()=>onPlanFile($("plan-file").files[0]).catch(error=>setStatus("plan-editor-status",error.message)));
  bind("plan-extract","click",extractPlan);
  bind("plan-review","click",reviewPlanCandidates);
  bind("plan-reviewed-trace","click",traceReviewedPlan);
  bind("plan-manual-trace","click",traceManualPlan);
  bind("plan-geometry-approve","click",approvePlanGeometry);
  $("plan-arrival-routes").addEventListener("input",()=>{
    ++editor.routePreviewSerial;
    if(editor.geometryPreviewBaseId){editor.geometryPreview=null;editor.geometryPreviewBaseId=null;$("plan-geometry-approve-check").checked=false;$("plan-geometry-approve").disabled=true;$('plan-route-status').textContent="経路入力を変更しました。古いRevision Previewは無効です。新しい構造Previewを作成してください。";}
  });
  $("plan-route-preview").addEventListener("click",()=>previewArrivalRouteRevision().catch(error=>{$("plan-route-status").textContent=`経路Revisionを検証できません: ${error.message}`;}));
  $("plan-geometry-approve-check").addEventListener("change",()=>{$("plan-geometry-approve").disabled=!editor.geometryPreview||!$("plan-geometry-approve-check").checked;});
  bind("plan-apply-geometry","click",()=>{if(!editor.approvedGeometryId)throw new Error("保存済みGeometryがありません。");$("geometry-id").value=editor.approvedGeometryId;$("plan-json").value="";$("create").open=true;$("request").dispatchEvent(new Event("input",{bubbles:true}));$("plan-geometry-status").textContent=`Geometry ${editor.approvedGeometryId} を次のScenario Previewに指定しました。条件・GeometryのPreviewとScenario承認が必要です。`;});
  $("shift-add").addEventListener("click",()=>renderStaffShiftRows([...$("shift-rows").querySelectorAll(".staff-shift-row")].map(row=>Object.fromEntries([...row.querySelectorAll("input")].map(input=>[input.dataset.field,input.value]))).concat({staff_id:`staff_${String($("shift-rows").children.length+1).padStart(3,"0")}`,start_minute:0,end_minute:60,repeat_every_minutes:1440})));
  window.addEventListener("sheltersim:draft-restored",event=>renderStaffShiftRows(event.detail?.staff_unavailability||[]));
  window.addEventListener("sheltersim:project-view",event=>{
    const detail=event.detail||{};if(detail.frame?.geometry&&!editor.image&&!['raster_pixel','document','vector-candidate'].includes(editor.space)){editor.standard=detail.frame.geometry;editor.space="scenario_meters";editor.width=detail.frame.geometry.width_m||900;editor.height=detail.frame.geometry.height_m||560;editor.metersPerPixel=1;drawPlan();}
  });
  window.addEventListener("sheltersim:asset-saved",event=>{const detail=event.detail||{};if(detail.project===projectId()&&detail.asset?.sha256&&detail.asset.sha256===editor.assetSha){editor.assetStored=true;editor.assetMediaType=detail.asset.media_type;updatePlanTrace();setStatus("plan-status",`保存済み Asset ${detail.asset.sha256} · ${detail.asset.media_type} · 権限確認済み。PDFはプレビューを開いたまま候補抽出できます。`);}});

  // Project drafts hold references and operator edits, never embedded source rasters.
  // Approval is deliberately not restored as an action; the operator must approve again.
  window.ShelterSimPlanEditor={
    snapshot(){
      const reviewDraft={...editor.reviewDraft};
      for(const row of $("plan-feature-list").querySelectorAll(".candidate-feature[data-feature-id]")){
        const id=row.dataset.featureId,local={...(reviewDraft[id]||{})};
        for(const input of row.querySelectorAll("[data-field]")){
          if(input.dataset.field==="decision")local.decision=input.value;
          else if(input.dataset.field==="coordinates"){local.coordinates_text=input.value;try{local.coordinates=JSON.parse(input.value);}catch{delete local.coordinates;}}
          else if(input.dataset.field==="attributes"){local.attributes_text=input.value;try{local.attributes=JSON.parse(input.value);}catch{delete local.attributes;}}
          else if(input.dataset.field==="note")local.note=input.value;
        }
        reviewDraft[id]=local;
      }
      let trace=null;try{trace=JSON.parse($("plan-trace-json").value);}catch{}
      return {schema_version:1,project_id:projectId(),source_asset_sha256:editor.assetSha,asset_media_type:editor.assetMediaType,asset_file_name:editor.imageName,asset_stored:editor.assetStored,
        page_index:Number($("plan-page-index").value||0),max_dimension_px:Number($("plan-max-dimension").value||1600),raster_preview:editor.rasterPreview,
        coordinate_space:editor.space,width:editor.width,height:editor.height,meters_per_pixel:editor.metersPerPixel,building_width_m:trace?.manual_plan_trace?.building_width_m??null,building_height_m:trace?.manual_plan_trace?.building_height_m??null,
        calibration:editor.calibration,features:editor.features,pending:editor.pending,selected:editor.selected,next:editor.next,candidates:editor.candidates,review_draft:reviewDraft,
        plan_inputs:{tool:$("plan-tool").value,distance:$("plan-distance").value,feature_name:$("plan-feature-name").value,zone_id:$("plan-zone-id").value,usable_fraction:$("plan-usable-fraction").value,capacity:$("plan-capacity").value,accessible:$("plan-accessible").checked,operator_id:$("plan-operator").value,wall_id:$("plan-wall").value,from_zone_id:$("plan-from").value,to_zone_id:$("plan-to").value,arrival_routes_json:$("plan-arrival-routes").value},
        extraction_preview_id:editor.extractionPreviewId,proposal_preview_id:editor.proposalPreviewId,geometry_preview_id:editor.geometryPreview?.preview_id||null,geometry_preview_base_id:editor.geometryPreviewBaseId,approved_geometry_id:editor.approvedGeometryId};
    },
    async restore(saved){
      resetPlanEditing();
      if(!saved||saved.schema_version!==1||saved.project_id!==projectId()){
        drawPlan();if(saved) setStatus("plan-editor-status","この草案の図面編集は別Projectのため復旧しませんでした。");return;
      }
      $("plan-page-index").value=String(saved.page_index??0);$("plan-max-dimension").value=String(saved.max_dimension_px??1600);
      $("plan-building-width").value=saved.building_width_m??"";$("plan-building-height").value=saved.building_height_m??"";
      const inputs=saved.plan_inputs||{};$("plan-tool").value=inputs.tool||"zone";$("plan-distance").value=inputs.distance??"";$("plan-feature-name").value=inputs.feature_name||"zone-1";$("plan-zone-id").value=inputs.zone_id??"";$("plan-usable-fraction").value=inputs.usable_fraction??"0.9";$("plan-capacity").value=inputs.capacity??"";$("plan-accessible").checked=Boolean(inputs.accessible);$("plan-operator").value=inputs.operator_id||"operator";
      $("plan-wall").value=inputs.wall_id||"";$("plan-from").value=inputs.from_zone_id||"";$("plan-to").value=inputs.to_zone_id||"";
      editor.assetSha=saved.source_asset_sha256||null;editor.assetMediaType=saved.asset_media_type||null;editor.assetStored=false;editor.imageName=saved.asset_file_name||null;
      editor.width=Number(saved.width||900);editor.height=Number(saved.height||560);editor.metersPerPixel=Number.isFinite(saved.meters_per_pixel)?saved.meters_per_pixel:null;
      editor.calibration=Array.isArray(saved.calibration)?saved.calibration:[];editor.features=Array.isArray(saved.features)?saved.features:[];editor.pending=Array.isArray(saved.pending)?saved.pending:[];
      editor.selected=saved.selected??null;editor.next=Number(saved.next||1);editor.candidates=Array.isArray(saved.candidates)?saved.candidates:[];editor.reviewDraft=saved.review_draft&&typeof saved.review_draft==="object"?saved.review_draft:{};
      editor.space=saved.coordinate_space||"blank";editor.rasterPreview=saved.raster_preview||null;editor.extractionPreviewId=saved.extraction_preview_id||null;editor.proposalPreviewId=saved.proposal_preview_id||null;
      if(editor.assetSha&&saved.asset_stored){
        const content=await api(`${apiRoot()}/assets/${encode(editor.assetSha)}/content`);
        if(content.media_type!==editor.assetMediaType)throw new Error("草案の図面Asset形式が保存時から変わっています。");
        editor.assetStored=true;editor.assetMediaType=content.media_type;
        if(content.media_type==="application/pdf"){
          const bytes=Uint8Array.from(atob(content.data_base64),c=>c.charCodeAt(0));await openPdfDocument(bytes,editor.imageName||"shelter-plan.pdf",{sha:editor.assetSha,stored:true,pageIndex:Number(saved.page_index||0)});
        }else if(content.media_type.startsWith("image/")){
          const image=new Image();await new Promise((resolve,reject)=>{image.onload=()=>{editor.image=image;editor.width=image.naturalWidth;editor.height=image.naturalHeight;resolve();};image.onerror=()=>reject(new Error("保存済み図面画像を開けませんでした。"));image.src=`data:${content.media_type};base64,${content.data_base64}`;});
        }else if(["application/json","application/geo+json"].includes(content.media_type)){
          const binary=atob(content.data_base64),bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));loadPlanCandidates(JSON.parse(new TextDecoder().decode(bytes)),editor.imageName,editor.assetSha);
        }
      }else if(editor.assetSha){
        setStatus("plan-editor-status","この草案の図面ファイル自体はAsset保存されていません。手動トレースは戻りました。元図面を選び直すか、権限確認後にProject Assetとして保存してください。");
      }
      if(saved.approved_geometry_id&&editor.assetSha){
        const approved=await api(`${apiRoot()}/geometries/${encode(saved.approved_geometry_id)}`);
        if(approved.approved===true&&approved.asset_id===editor.assetSha){editor.approvedGeometryId=approved.geometry_id;editor.approvedGeometryRecord=approved;$("plan-json").value=JSON.stringify(approved.geometry,null,2);$("plan-arrival-routes").value=typeof saved.plan_inputs?.arrival_routes_json==="string"?saved.plan_inputs.arrival_routes_json:JSON.stringify(approved.geometry?.geometry?.arrival_approach_routes||[],null,2);$("plan-arrival-routes").disabled=false;$("plan-route-preview").disabled=false;$("plan-apply-geometry").disabled=false;$("plan-route-status").textContent=`確定済みGeometry ${approved.geometry_id}を編集基準として復旧しました。経路変更は新しいRevisionとして検証・承認します。`;$('plan-geometry-status').textContent=`確定済み immutable Geometry ${approved.geometry_id} を復旧しました。runtime_verified ${approved.runtime_verified}。必要なら明示操作でScenarioに指定できます。`;}
        else throw new Error("確定GeometryのAsset参照または承認状態を確認できません。");
      }
      const previewId=editor.proposalPreviewId||saved.geometry_preview_id;
      if(previewId&&editor.assetSha){
        const draft=await api(`${apiRoot()}/drafts/${encode(previewId)}`),record=draft.content;
        if(record?.record_type!=="plan_preview"||record.asset_id!==editor.assetSha)throw new Error("保存済み抽出候補のAsset参照が一致しません。");
        if(record.kind==="proposal"){
          editor.proposalPreviewId=previewId;editor.extractionPreviewId=previewId;
          editor.candidates=(record.value?.features||[]).map(raw=>({id:raw.feature_id,kind:raw.feature_kind,name:raw.attributes?.name||raw.attributes?.zone_id||raw.feature_id,raw}));
        }else if(record.kind==="geometry"){
          editor.geometryPreview={preview_id:previewId,asset_id:record.asset_id,kind:"geometry",value_hash:record.value_hash,value:record.value,validation:record.validation,runtime_verified:record.runtime_verified};editor.geometryPreviewBaseId=saved.geometry_preview_base_id||null;editor.proposalPreviewId=null;
          $("plan-geometry-approve-check").checked=false;$("plan-geometry-approve").disabled=true;$("plan-apply-geometry").disabled=true;
          $("plan-validation-result").textContent=JSON.stringify({preview_id:previewId,kind:record.kind,value_hash:record.value_hash,validation:record.validation,runtime_verified:false,value:record.value},null,2);
          $("plan-geometry-status").textContent="保存済みGeometry候補を復旧しました。承認は復旧していません。寸法・出典・構造検証を再確認して明示承認してください。";
        }
        editor.rasterPreview=record.raster_preview||editor.rasterPreview;
        if(editor.rasterPreview?.source_asset_id===editor.assetSha&&editor.rasterPreview.page_index===Number($("plan-page-index").value||0)){
          try{const raster=await api(`${apiRoot()}/assets/${encode(editor.rasterPreview.asset_id)}/content`);if(raster.media_type==="image/png")await new Promise((resolve,reject)=>{const image=new Image();image.onload=()=>{if(image.naturalWidth!==editor.rasterPreview.width_px||image.naturalHeight!==editor.rasterPreview.height_px){reject(new Error("保存済みraster参照の寸法が一致しません。"));return;}editor.image=image;editor.width=image.naturalWidth;editor.height=image.naturalHeight;editor.space="raster_pixel";resolve();};image.onerror=()=>reject(new Error("保存済みrasterを開けませんでした。"));image.src=`data:image/png;base64,${raster.data_base64}`;});}catch(error){setStatus("plan-extract-status",`図面草案は復旧しましたが、保存rasterを表示できません: ${error.message}`);}
        }
      }
      if(editor.geometryPreview)$("plan-geometry-approve").disabled=true;
      editor.features=Array.isArray(saved.features)?saved.features:editor.features;editor.pending=Array.isArray(saved.pending)?saved.pending:[];editor.calibration=Array.isArray(saved.calibration)?saved.calibration:[];editor.metersPerPixel=Number.isFinite(saved.meters_per_pixel)?saved.meters_per_pixel:null;
      drawPlan();$("plan-wall").value=inputs.wall_id||"";$("plan-from").value=inputs.from_zone_id||"";$("plan-to").value=inputs.to_zone_id||"";updateFeatureSelectors();
      setStatus("plan-editor-status",`${editor.imageName||"図面"} · Asset ${editor.assetSha||"未保存"} · page ${saved.page_index??0} · 縮尺 ${editor.metersPerPixel?`${editor.metersPerPixel} m/px`:"未確定"} · ${editor.candidates.length}件の抽出候補、${editor.features.length}件の手動トレースを草案として復旧。承認済みGeometryはimmutable記録から取得し、未承認候補は再確認を求めます。`);
    }
  };

  bind("r3-refresh","click",refreshManagement);
  bind("model-contract-load","click",async()=>{setStatus("model-contract-status","共通contracts APIを読み込み中…");const data=await api(`${apiRoot()}/contracts`);renderModelContracts(data);});
  window.addEventListener("sheltersim:intervention-preview",async event=>{try{if(!state.modelContracts){const data=await api(`${apiRoot()}/contracts`);renderModelContracts(data);}renderEffectExplanation(event.detail);}catch(error){state.modelContracts=null;renderEffectExplanation(event.detail);setStatus("model-contract-status",`契約取得失敗: ${error.message}。primitive対応状態は不明として表示しています。`);}});
  window.addEventListener("sheltersim:history",event=>renderHistoryNavigation(event.detail));
  bind("r3-project-open","click",async()=>{$("project").value=$("r3-projects").value;$("project-open").click();});
  bind("r3-compare","click",loadComparison);
  bind("r3-rerun","click",rerunSelected);
  const changePdfRaster=async()=>{++editor.rasterSerial;if(!editor.pdfDocument){markPlanEdited();return;}editor.features=[];editor.candidates=[];editor.pending=[];editor.calibration=[];editor.selected=null;editor.reviewDraft={};editor.metersPerPixel=null;editor.rasterPreview=null;markPlanEdited();await renderPdfPage();setStatus("plan-editor-status",`page ${editor.pdfPageIndex} を表示します。別ページの候補・トレース・縮尺は持ち越しません。`);};
  bind("plan-page-index","change",changePdfRaster);bind("plan-max-dimension","change",changePdfRaster);
  bind("r3-audit","click",showRunAudit);
  bind("r4-audit-load","click",()=>loadRunAuditPage(false));
  bind("r4-audit-next","click",()=>loadRunAuditPage(true));
  bind("r4-experiment-create","click",createExperiment);
  bind("r4-experiment-load","click",loadExperiment);
  bind("r4-experiment-fit","click",fitExperiment);
  bind("r4-experiment-finalize","click",finalizeExperiment);
  $("r4-experiment-approve").addEventListener("change",()=>{if(state.experiment)renderExperimentCells(state.experiment.cells||[],state.experimentResults);});
  $("r4-experiment-select").addEventListener("change",()=>{if($("r4-experiment-select").value)$("r4-experiment-id").value=$("r4-experiment-select").value;});
  bind("r3-traj-agent","click",()=>loadTrajectory("people"));
  bind("r3-traj-household","click",()=>loadTrajectory("households"));
  bind("r4-exact-divergence","click",()=>loadExactDivergence(false));
  bind("r4-divergence-next","click",()=>loadExactDivergence(true));
  bind("r4-condition-diff","click",()=>loadConditionDifferences(false));
  bind("r4-condition-next","click",()=>loadConditionDifferences(true));
  bind("history-index-status","click",()=>loadHistoryIndex(false));
  bind("history-index-rebuild","click",()=>loadHistoryIndex(true));
  function invalidateComparison(){state.comparison=null;state.tracks.clear();$("r3-map-grid").replaceChildren();$("r3-events").replaceChildren();$("r3-trajectories").replaceChildren();$("r3-divergence").replaceChildren();$("r3-kpi-chart").getContext("2d").clearRect(0,0,$("r3-kpi-chart").width,$("r3-kpi-chart").height);}
  $("r3-runs").addEventListener("change",()=>{++state.comparisonSerial;invalidateComparison();rebuildBranchTargets().catch(error=>setStatus("r3-status",error.message));});
  $("r3-minute").addEventListener("input",()=>{++state.comparisonSerial;invalidateComparison();});
  $("layer").addEventListener("change",()=>{if(state.comparison)renderComparison(state.comparison.result,state.comparison.targets);});
  $("r3-projects").addEventListener("change",()=>{});
  $("run").addEventListener("change",()=>updateRunChoices());
  $("branch").addEventListener("change",()=>updateRunChoices());
  for(const id of ["run","branch"])$(id).addEventListener("change",()=>{++state.divergenceSerial;++state.conditionDiffSerial;state.divergence=null;state.conditionDiff=null;state.auditPage=null;$("r4-audit-next").disabled=true;renderExactEvidence();});
  let lastProjectRun="";
  window.addEventListener("sheltersim:project-view",event=>{
    const detail=event.detail||{},scope=`${detail.project||""}/${detail.run||""}`,branchScope=`${detail.project||""}/${detail.run||""}/${detail.branch||""}`;
    if(state.historyTrailScope&&state.historyTrailScope!==branchScope){state.historyTrail=null;state.historyTrailScope=null;}
    state.currentView=detail;drawHistoryTrail();
    if(scope!==lastProjectRun){lastProjectRun=scope;refreshManagement().catch(error=>setStatus("r3-management-status",error.message));}
  });

  refreshProjects().then(refreshManagement).catch(error=>setStatus("r3-management-status",error.message));
  drawPlan();
})();
/* Small, additive UI helpers for the closeout acceptance items. */
(() => {
  "use strict";
  if (!['demo', 'admin'].includes(document.body.dataset.mode)) return;
  const $ = id => document.getElementById(`ws-${id}`);
  const setStatus = (id, message) => $(id).textContent = message;
  const json = value => JSON.stringify(value, null, 2);
  const editors = { base: null, candidate: null, original: null, features: [], selected: 0, dragged: null, vertexSelection: null, writing: false };
  const point = (x, y) => ({ x: Number(x), y: Number(y) });
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const fire = id => $(id)?.dispatchEvent(new Event('input', { bubbles: true }));
  const make = (tag, value, attrs = {}) => {
    const node = document.createElement(tag);
    if (value != null) node.textContent = value;
    for (const [key, item] of Object.entries(attrs)) node.setAttribute(key, item);
    return node;
  };

  // Anonymous household templates; kinship is always explicitly declared.
  const householdBox = $('household-template-list');
  const householdStatus = $('household-status');
  const householdJson = $('household-composition');
  const sizeBox = $('household-size-weights');
  let household = { source_ids: ['SRC-OPERATOR-POPULATION-CONDITIONS'], household_size_weights: {}, templates: [] };
  function readHousehold() {
    if (!householdJson.value.trim()) return;
    try { const parsed = JSON.parse(householdJson.value); if (Array.isArray(parsed.templates)) household = parsed; }
    catch { householdStatus.textContent = '高度なJSONに入力エラーがあります。テンプレートの編集前にJSONを修正してください。'; }
  }
  function writeHousehold() {
    householdJson.value = household.templates.length ? json(household) : '';
    fire('household-composition');
    householdStatus.textContent = household.templates.length
      ? `${household.templates.length}件の匿名テンプレートをScenario入力へ反映しました。関係 ${household.templates.reduce((n, t) => n + (t.relationships || []).length, 0)}件を明示。未成年者・支援ニーズの安全確認は別途必要です。`
      : Object.keys(household.household_size_weights || {}).length ? '人数の重みを入力済みです。共有入力契約ではテンプレートが1件以上必要です。テンプレートを追加すると重みも一緒に保存されます。' : 'テンプレートはありません。未入力の関係は不明として扱います。';
  }
  function renderSizeWeights() {
    sizeBox.replaceChildren();
    for (let size = 1; size <= 10; size++) {
      const label = make('label', `${size}人`), input = make('input'); input.type = 'number'; input.min = '0'; input.step = '0.1'; input.placeholder = '未指定'; input.value = household.household_size_weights?.[size] ?? '';
      input.addEventListener('change', () => { household.household_size_weights ||= {}; if (input.value === '') delete household.household_size_weights[size]; else household.household_size_weights[size] = Number(input.value); writeHousehold(); });
      label.append(input); sizeBox.append(label);
    }
  }
  function renderHouseholds() {
    householdBox.replaceChildren();
    household.templates.forEach((template, ti) => {
      const card = make('article', null, { class: 'household-template-card' });
      card.dataset.templateId = template.template_id || `household-${ti + 1}`;
      const head = make('div', null, { class: 'equipment-row' });
      const title = make('label', 'テンプレート名'), name = make('input'); name.maxLength = 64; name.value = template.template_id || `household-${ti + 1}`;
      name.addEventListener('change', () => { const normalized = name.value.trim().replace(/[^A-Za-z0-9._:-]+/g, '-').replace(/^[^A-Za-z0-9]+/, '').slice(0, 64); name.value = normalized || `household-${ti + 1}`; template.template_id = name.value; writeHousehold(); }); title.append(name);
      const weight = make('label', '選択重み'), weightInput = make('input'); weightInput.type = 'number'; weightInput.min = '0.01'; weightInput.step = '0.1'; weightInput.value = template.weight ?? 1; weightInput.addEventListener('input', () => { template.weight = Number(weightInput.value); writeHousehold(); }); weight.append(weightInput);
      const remove = make('button', 'テンプレート削除'); remove.type = 'button'; remove.addEventListener('click', () => { household.templates.splice(ti, 1); renderHouseholds(); writeHousehold(); }); head.append(title, weight, remove); card.append(head);
      const members = make('div', null, { class: 'equipment-rows' });
      (template.members ||= []).forEach((member, mi) => {
        const row = make('div', null, { class: 'equipment-row household-member-row' });
        const roleLabel = make('label', `メンバー${mi + 1}の役割`), role = make('select');
        for (const [value, label] of [['unspecified', '未指定'], ['adult', '成人'], ['child', '子ども'], ['caregiver', '介護者'], ['dependent', '支援を受ける人']]) role.add(new Option(label, value));
        role.value = member.role || 'unspecified'; role.addEventListener('change', () => { member.role = role.value; writeHousehold(); }); roleLabel.append(role);
        const ageLabel = make('label', '年齢区分（任意）'), age = make('input'); age.maxLength = 100; age.value = member.age_band_label || ''; age.placeholder = '例: school_age'; age.addEventListener('input', () => { if (age.value.trim()) member.age_band_label = age.value.trim(); else delete member.age_band_label; writeHousehold(); }); ageLabel.append(age);
        const del = make('button', '削除'); del.type = 'button'; del.addEventListener('click', () => { template.members.splice(mi, 1); template.relationships = (template.relationships || []).filter(r => r.caregiver_member_index !== mi && r.dependent_member_index !== mi).map(r => ({ ...r, caregiver_member_index: r.caregiver_member_index > mi ? r.caregiver_member_index - 1 : r.caregiver_member_index, dependent_member_index: r.dependent_member_index > mi ? r.dependent_member_index - 1 : r.dependent_member_index })); renderHouseholds(); writeHousehold(); });
        row.append(roleLabel, ageLabel, del); members.append(row);
      });
      card.append(members);
      const addMember = make('button', 'メンバーを追加'); addMember.type = 'button'; addMember.disabled = template.members.length >= 10; addMember.addEventListener('click', () => { template.members.push({ role: 'unspecified' }); renderHouseholds(); writeHousehold(); }); card.append(addMember);
      const relationTitle = make('h5', '明示する親子・介護・家族関係'); card.append(relationTitle);
      const relations = make('div', null, { class: 'equipment-rows' }); (template.relationships ||= []).forEach((relation, ri) => {
        const row = make('div', null, { class: 'equipment-row' });
        for (const [field, caption] of [['caregiver_member_index', '関係元'], ['dependent_member_index', '関係先']]) {
          const label = make('label', caption), select = make('select');
          template.members.forEach((_, mi) => select.add(new Option(`メンバー${mi + 1}`, String(mi)))); select.value = String(relation[field]); select.addEventListener('change', () => { relation[field] = Number(select.value); writeHousehold(); }); label.append(select); row.append(label);
        }
        const kindLabel = make('label', '関係'), kind = make('select');
        for (const [value, label] of [['parent_child', '親子'], ['caregiver_child', '保護者と子ども'], ['caregiver_support', '介護・支援'], ['partner', 'パートナー'], ['relative', '親族']]) kind.add(new Option(label, value));
        kind.value = relation.relationship; kind.addEventListener('change', () => { relation.relationship = kind.value; writeHousehold(); }); kindLabel.append(kind); row.append(kindLabel);
        const del = make('button', '関係削除'); del.type = 'button'; del.addEventListener('click', () => { template.relationships.splice(ri, 1); renderHouseholds(); writeHousehold(); }); row.append(del); relations.append(row);
      }); card.append(relations);
      const addRelation = make('button', '関係を追加'); addRelation.type = 'button'; addRelation.disabled = template.members.length < 2 || template.relationships.length >= 20; addRelation.addEventListener('click', () => { template.relationships.push({ caregiver_member_index: 0, dependent_member_index: 1, relationship: 'caregiver_support' }); renderHouseholds(); writeHousehold(); }); card.append(addRelation);
      householdBox.append(card);
    });
    renderSizeWeights(); writeHousehold();
  }
  $('household-template-add')?.addEventListener('click', () => { readHousehold(); household.templates.push({ template_id: `household-${household.templates.length + 1}`, weight: 1, members: [{ role: 'adult' }, { role: 'unspecified' }], relationships: [] }); renderHouseholds(); });
  householdJson?.addEventListener('change', () => { readHousehold(); renderHouseholds(); });
  const reviewStatus=$('household-review-status'),repreview=$('household-repreview');
  repreview?.addEventListener('click',()=>{$('create').open=true;$('preview').click();});
  window.addEventListener('sheltersim:household-review',event=>{
    const detail=event.detail||{},templateId=detail.template_id,kind=detail.kind||'guardian';
    reviewStatus.hidden=false;repreview.hidden=false;$('create').open=true;
    if(detail.action==='add-template'){
      const size=Number(detail.members);
      if(!Number.isInteger(size)||size<2||size>10){reviewStatus.textContent=`${detail.household_id||'この世帯'} は ${size||1} 人です。1人世帯には保護者を割当できません。世帯人数分布と世帯サイズ入力を確認してください。保護者ブロックを手動で解除する方法はありません。`;return;}
      const existing=household.templates.find(row=>row.members?.length===size&&row.relationships?.some(link=>kind==='guardian'?['parent_child','caregiver_child'].includes(link.relationship):link.relationship==='caregiver_support'));
      if(existing){const card=householdBox.querySelector(`[data-template-id="${CSS.escape(existing.template_id)}"]`);card?.scrollIntoView({behavior:'smooth',block:'center'});reviewStatus.textContent=`既に ${existing.template_id} に同人数の明示的な関係候補があります。関係先/役割/相対重みを確認してください。生成済み人物を直接編集した結果ではないため、必ず再プレビューしてください。`;return;}
      let templateIdBase=`review-${kind==='guardian'?'guardian-child':'caregiver-support'}-${size}`,candidateId=templateIdBase,suffix=2;while(household.templates.some(row=>row.template_id===candidateId)){candidateId=`${templateIdBase}-${suffix}`;suffix++;}
      const members=Array.from({length:size},(_,index)=>({role:index===0?'adult':index===1?(kind==='guardian'?'child':'dependent'):'unspecified'}));
      household.templates.push({template_id:candidateId,weight:1,members,relationships:[{caregiver_member_index:0,dependent_member_index:1,relationship:kind==='guardian'?'caregiver_child':'caregiver_support'}]});renderHouseholds();
      const card=householdBox.querySelector(`[data-template-id="${CSS.escape(candidateId)}"]`);card?.scrollIntoView({behavior:'smooth',block:'center'});
      reviewStatus.textContent=`候補テンプレート ${candidateId} を追加しました (重み1、世帯サイズ ${size})。既存Profileを変更したわけではなく、新しい匿名生成の候補です。入力重みを見直し、「修正内容を再プレビュー」を押して個別要件が解消したか再確認してください。`;return;
    }
    const card=templateId?householdBox.querySelector(`[data-template-id="${CSS.escape(templateId)}"]`):null;card?.scrollIntoView({behavior:'smooth',block:'center'});
    if(templateId&&card)reviewStatus.textContent=`人物 ${detail.person_id||'ID未記録'} · ${detail.household_id||'世帯不明'} はテンプレート ${templateId} から生成されました。役割・年齢区分・保護者/介護関係を確認してください。入力変更後は再プレビューし、承認ブロックが消えたことを記録で確認します。`;
    else reviewStatus.textContent=detail.members===1?`人物 ${detail.person_id||'ID未記録'} · ${detail.household_id||'世帯不明'} は単身世帯です。割り当て済みテンプレートがなく、同じ世帯に保護者を追加する確実な操作はありません。世帯サイズ分布/テンプレートを修正し、次の匿名previewを確認してください。`:`人物 ${detail.person_id||'ID未記録'} · ${detail.household_id||'世帯不明'} に適用された入力テンプレートは特定できません。候補テンプレートを追加・調整するか既存関係を確認し、再プレビューしてください。`;
  });
  try { readHousehold(); renderHouseholds(); } catch {}

  // Weekday based availability editor maps directly to the shared 0..10080-minute API.
  const shiftBox = $('positive-shift-rows'), shiftJson = $('staff-positive-shifts'), shiftStatus = $('positive-shift-status');
  let roster = [];
  const weekdays = ['1日目', '2日目', '3日目', '4日目', '5日目', '6日目', '7日目'];
  const minute = value => { const [h, m] = value.split(':').map(Number); return h * 60 + m; };
  function renderShifts(records = null) {
    shiftBox.replaceChildren();
    let rows = records;
    if (!rows) { try { rows = shiftJson.value.trim() ? JSON.parse(shiftJson.value) : []; } catch { rows = []; } }
    if (!Array.isArray(rows)) rows = [];
    for (const record of rows) {
      const row = make('div', null, { class: 'equipment-row positive-shift-row' });
      const staffLabel = make('label', 'スタッフID'), staff = make('select');
      staff.add(new Option('スタッフを選択', ''));
      roster.forEach(person => staff.add(new Option(`${person.id} · ${person.skills?.join('・') || '担当未記録'}`, person.id)));
      if (record.staff_id && !roster.some(p => p.id === record.staff_id)) staff.add(new Option(`${record.staff_id} · 現在の保存値`, record.staff_id));
      staff.value = record.staff_id || ''; staff.addEventListener('change', syncShifts); staffLabel.append(staff);
      const start = Number(record.start_minute || 0), weekday = Math.min(6, Math.floor(start / 1440)), startOfDay = start % 1440, end = Number(record.end_minute || 0), endOfDay = end % 1440;
      const dayLabel = make('label', 'Simulation内の週日'), day = make('select'); weekdays.forEach((d, i) => day.add(new Option(d, String(i)))); day.value = String(weekday); dayLabel.append(day);
      const startLabel = make('label', '開始時刻'), startInput = make('input'); startInput.type = 'time'; startInput.value = `${String(Math.floor(startOfDay / 60)).padStart(2, '0')}:${String(startOfDay % 60).padStart(2, '0')}`; startInput.addEventListener('change', syncShifts); startLabel.append(startInput);
      const endLabel = make('label', '終了時刻'), endInput = make('input'); endInput.type = 'time'; endInput.value = endOfDay === 0 && end > start ? '00:00' : `${String(Math.floor(endOfDay / 60)).padStart(2, '0')}:${String(endOfDay % 60).padStart(2, '0')}`; endInput.addEventListener('change', syncShifts); endLabel.append(endInput); const lastDayLabel = make('label', '7日目の終端'), lastDay = make('input'); lastDay.type = 'checkbox'; lastDay.checked = end === 10080; lastDay.title = '週内の最後の1分まで勤務'; lastDayLabel.hidden = weekday !== 6; lastDay.addEventListener('change', syncShifts); lastDayLabel.append(lastDay, document.createTextNode('24:00'));day.addEventListener('change',()=>{lastDayLabel.hidden=Number(day.value)!==6;syncShifts();});
      const remove = make('button', '勤務時間を削除'); remove.type = 'button'; remove.addEventListener('click', () => { row.remove(); syncShifts(); });
      row.append(staffLabel, dayLabel, startLabel, endLabel, lastDayLabel, remove); shiftBox.append(row);
    }
    if (!rows.length) shiftStatus.textContent = roster.length ? '勤務時間は未指定です。既存モデルの勤務規則が使われます。' : 'Scenarioプレビュー後に合成スタッフIDを選択できます。';
  }
  function syncShifts() {
    const values = [];
    for (const row of shiftBox.querySelectorAll('.positive-shift-row')) {
      const [staff, day, start, end, lastDay] = row.querySelectorAll('select,input');
      if (!staff.value || !start.value || !end.value) continue;
      const dayIndex = Number(day.value), offset = dayIndex * 1440, startOfDay = minute(start.value), endOfDay = minute(end.value), from = offset + startOfDay;
      if (lastDay.checked && dayIndex === 6 && endOfDay === 0 && startOfDay > 0) { values.push({ staff_id: staff.value, start_minute: from, end_minute: 10080, source_id: 'SRC-OPERATOR-INITIAL-CONDITIONS' }); continue; }
      const endOffset = endOfDay <= startOfDay ? 1440 : 0, to = offset + endOffset + endOfDay;
      if (to > 10080) { values.push({ staff_id: staff.value, start_minute: from, end_minute: 10080, source_id: 'SRC-OPERATOR-INITIAL-CONDITIONS' }); if (to > 10080) values.push({ staff_id: staff.value, start_minute: 0, end_minute: to - 10080, source_id: 'SRC-OPERATOR-INITIAL-CONDITIONS' }); continue; }
      if (from >= to) { shiftStatus.textContent = '終了時刻が開始時刻と同じ場合は一日中の勤務になります。'; continue; }
      values.push({ staff_id: staff.value, start_minute: from, end_minute: to, source_id: 'SRC-OPERATOR-INITIAL-CONDITIONS' });
    }
    shiftJson.value = values.length ? json(values) : ''; fire('staff-positive-shifts');
    shiftStatus.textContent = values.length ? `${values.length}件の週内勤務区間を入力しました。週内1日目をSimulation開始時点とする相対日です。スタッフIDの存在・重複区間はScenario Previewでサーバー検証されます。` : '有効な勤務時間はありません。指定しない場合は既存モデルの勤務規則を使います。';
  }
  $('positive-shift-add')?.addEventListener('click', () => { renderShifts([...(JSON.parse(shiftJson.value || '[]')), { staff_id: roster[0]?.id || '', start_minute: 480, end_minute: 960 }]); });
  shiftJson?.addEventListener('change', () => renderShifts());
  window.addEventListener('sheltersim:scenario-preview', event => {
    const design = event.detail?.facility_design; roster = Array.isArray(design?.staff) ? design.staff : roster;
    window.ShelterSimRound3ScenarioUI.renderHouseholdCorrection(event.detail);
    const requestShifts = event.detail?.request?.operational_conditions?.staff_shifts;
    if (Array.isArray(requestShifts) && !shiftJson.value.trim()) shiftJson.value = json(requestShifts);
    renderShifts();window.ShelterSimRound3ScenarioUI.prepareServicePoints(event.detail).catch(error=>setStatus("service-point-status",`サービス点を読み込めません: ${error.message}`));
  });
  window.addEventListener('sheltersim:scenario-invalidated',()=>window.ShelterSimRound3ScenarioUI.clearHouseholdPreview());
  window.addEventListener('sheltersim:draft-restored', () => renderShifts());
  renderShifts();

  // Canvas editor edits both shared walkable geometry and its wall/door/footprint data.
  const canvas = $('facility-canvas'), ctx = canvas?.getContext('2d'), field = $('facility-edit-json'), featureList = $('facility-features'), status = $('facility-canvas-status');
  const clone = value => value == null ? null : JSON.parse(JSON.stringify(value));
  const paths = [
    ['room', 'zones', 'polygon', true], ['corridor', 'routes', 'points', false],
    ['wall', 'editable_geometry.walls', 'start/end', false], ['door/opening', 'editable_geometry.openings', 'start/end', false],
    ['障害物', 'editable_geometry.obstacles', 'polygon', true], ['設備フットプリント', 'editable_geometry.equipment_footprints', 'polygon', true]
  ];
  function access(root, path) { return path.split('.').reduce((v, key) => v?.[key], root); }
  function buildFeatures() {
    editors.features = [];
    const g = editors.candidate?.geometry, e = editors.candidate?.editable_geometry;
    if (!g || !e) return;
    for (const [label, collection, pointsKey, closed] of paths) for (const [index, item] of ((collection.startsWith('editable_') ? access(editors.candidate, collection) : g[collection]) || []).entries()) {
      const key = pointsKey.includes('/') ? pointsKey.split('/') : [pointsKey];
      const points = key.length === 2 ? key.map(k => item[k]) : item[key[0]];
      if (!Array.isArray(points) || !points.length || points.some(p => !Number.isFinite(p?.x) || !Number.isFinite(p?.y))) continue;
      const id = item.zone_id || item.edge_id || item.wall_id || item.opening_id || item.obstacle_id || item.facility_id || `${label}-${index + 1}`;
      editors.features.push({ label, id, item, points, key, closed });
    }
    for (const [id, p] of Object.entries(g.facility_points || {})) if (Number.isFinite(p?.x) && Number.isFinite(p?.y)) editors.features.push({ label: '設備位置', id, item: g.facility_points, points: [p], pointKey: id, key: null, closed: false });
    editors.selected = Math.min(editors.selected, Math.max(0, editors.features.length - 1));
  }
  function extent() { const g = editors.candidate?.geometry || {}; return { width: Number(g.width_m || 1), height: Number(g.height_m || 1) }; }
  function transform() { const { width, height } = extent(), scale = Math.min((canvas.width - 70) / width, (canvas.height - 70) / height); return { scale, x: (canvas.width - width * scale) / 2, y: (canvas.height - height * scale) / 2 }; }
  function pixel(p) { const t = transform(); return [t.x + p.x * t.scale, t.y + p.y * t.scale]; }
  function world(event) { const r = canvas.getBoundingClientRect(), sx = canvas.width / r.width, sy = canvas.height / r.height, t = transform(); return { x: (event.clientX - r.left) * sx, y: (event.clientY - r.top) * sy, worldX: ((event.clientX - r.left) * sx - t.x) / t.scale, worldY: ((event.clientY - r.top) * sy - t.y) / t.scale }; }
  function renderFacility() {
    if (!canvas || !ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.fillStyle = '#f7faf8'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (!editors.candidate) { ctx.fillStyle = '#4c6259'; ctx.fillText('標準施設のScenarioプレビューを作成してください', 24, 32); featureList.replaceChildren(); return; }
    const { width, height } = extent(), t = transform(); ctx.fillStyle = '#edf2ee'; ctx.fillRect(t.x, t.y, width * t.scale, height * t.scale); ctx.strokeStyle = '#60756b'; ctx.strokeRect(t.x, t.y, width * t.scale, height * t.scale);
    editors.features.forEach((feature, fi) => {
      ctx.beginPath(); feature.points.forEach((p, pi) => { const [x, y] = pixel(p); pi ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      if (feature.closed) ctx.closePath(); ctx.strokeStyle = fi === editors.selected ? '#d05d34' : feature.label === 'wall' ? '#293b35' : feature.label.includes('door') ? '#bd8529' : feature.label === 'room' ? '#438369' : feature.label === '設備位置' ? '#77599e' : '#4282a6'; ctx.lineWidth = feature.label === 'wall' ? 3 : 2; if (feature.closed && feature.label === 'room') { ctx.fillStyle = '#4a987522'; ctx.fill(); } ctx.stroke();
      ctx.fillStyle = '#24382f'; const [lx, ly] = pixel(feature.points[0]); ctx.fillText(`${feature.label} · ${feature.id}`, lx + 5, ly - 5);
      feature.points.forEach((p, pi) => { const [x, y] = pixel(p); ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fillStyle = editors.vertexSelection?.feature === fi && editors.vertexSelection.point === pi ? '#f0a43b' : '#fff'; ctx.fill(); ctx.strokeStyle = '#263831'; ctx.stroke(); });
    });
    featureList.replaceChildren();
    editors.features.forEach((feature, index) => { const button = make('button', `${index === editors.selected ? '選択中 · ' : ''}${feature.label} · ${feature.id}`); button.type = 'button'; button.setAttribute('aria-pressed', String(index === editors.selected)); button.addEventListener('click', () => { editors.selected = index; editors.vertexSelection = null; renderFacility(); }); featureList.append(button); });
    const selected = editors.features[editors.selected], selectedVertex=editors.vertexSelection?.feature===editors.selected?editors.vertexSelection.point:null; $('facility-add-vertex').disabled = !selected || (!selected.closed && selected.label !== 'corridor') || selected.label === '設備フットプリント'; $('facility-delete-vertex').disabled = !selected || selectedVertex===null || selected.points.length <= (selected.closed ? 3 : 2) || selected.label === '設備フットプリント'; $('facility-reset').disabled = !editors.original;
  }
  function setPoint(feature, index, value) { feature.points[index] = value; if (feature.pointKey) feature.item[feature.pointKey] = value; else if (feature.key.length === 2) feature.item[feature.key[index]] = value; else feature.item[feature.key[0]][index] = value; }
  function writeCandidate() {
    editors.writing = true; field.value = json(editors.candidate); field.dispatchEvent(new Event('input', { bubbles: true })); editors.writing = false;
    status.textContent = 'Geometry草案に編集を反映しました。確定前に「Geometryを再検証」を実行してください。';
    window.dispatchEvent(new CustomEvent('sheltersim:facility-candidate', { detail: { base: editors.base, candidate: clone(editors.candidate), design: clone(editors.design), unvalidated: true } }));
  }
  function showFacilityValidation(result) {
    const issues = result.issues || [], lines = issues.map(item => `${item.severity || 'error'} · ${item.code || 'geometry'} · ${item.target_id || '対象未指定'}: ${item.message}`);
    const cap = result.capacity_supported == null ? '' : `\n容量 ${result.capacity_supported}人 / 要求 ${result.request?.evacuees ?? '未記録'}人`;
    const blockers = (result.approval_blockers || []).map(value => `承認ブロック: ${value}`), warnings = (result.warnings || []).map(value => `警告: ${typeof value === 'string' ? value : value.message || JSON.stringify(value)}`);
    const state = result.status === 'invalid' ? 'Geometry不正・修正が必要' : result.can_start ? '構造検証済み・Scenario承認前' : 'Geometry構造検証済み・Scenario承認ブロックあり';
    $('facility-edit-status').textContent = `${state}${cap}\n${[...lines, ...blockers, ...warnings].join('\n') || 'サーバーから追加の指摘はありません。'}\nruntime到達可能性はこの構造検証に含まれません。`;
    status.textContent = result.status === 'invalid' ? 'Geometryに問題があります。指摘された頂点・部屋・扉・通路を修正して再検証してください。' : result.can_start ? '構造検証に通りました。Scenario条件・容量・出典を確認し、明示承認してください。' : 'Geometryは構造検証済みですが、Scenario承認ブロックがあります。表示された理由を修正してください。';
  }
  window.ShelterSimCloseout = { showFacilityValidation };
  function setCandidate(detail) {
    if (!detail?.design || !detail?.candidate) { editors.candidate = null; editors.original = null; editors.base = null; editors.design = null; editors.vertexSelection=null;editors.dragged=null;renderFacility(); return; }
    let parsed = detail.candidate;
    if (typeof parsed === 'string') { try { parsed = JSON.parse(parsed); } catch { return; } }
    if (!parsed?.geometry || !parsed?.editable_geometry) return;
    if(detail.base?.preview_id!==editors.base?.preview_id){editors.vertexSelection=null;editors.dragged=null;}
    editors.design = clone(detail.design); editors.base = clone(detail.base); editors.candidate = clone(parsed);
    if (!editors.original || detail.unvalidated === false || !same(editors.original, editors.candidate)) editors.original = clone(detail.design) && { geometry: clone(detail.design.geometry), editable_geometry: clone(detail.design.editable_geometry) };
    buildFeatures(); renderFacility();
  }
  field?.addEventListener('input', () => { if (editors.writing) return; try { const parsed = JSON.parse(field.value); if (parsed.geometry && parsed.editable_geometry) { editors.candidate = parsed; buildFeatures(); renderFacility(); } } catch { status.textContent = 'JSONを編集中です。構文が直るとキャンバスに表示します。'; } });
  window.addEventListener('sheltersim:facility-candidate', event => {setCandidate(event.detail);window.ShelterSimRound3ScenarioUI.setFacilityCandidate(event.detail);});
  $('service-point-add')?.addEventListener('click',()=>window.ShelterSimRound3ScenarioUI.addServicePoint());
  $('facility-reset')?.addEventListener('click', () => { if (!editors.original) return; editors.candidate = clone(editors.original); editors.vertexSelection=null;field.value = json(editors.candidate); buildFeatures(); renderFacility(); writeCandidate(); });
  $('facility-add-vertex')?.addEventListener('click', () => { const feature = editors.features[editors.selected]; if (!feature || feature.label === '設備フットプリント') return; const index = feature.closed ? feature.points.length - 1 : feature.points.length - 2, a = feature.points[index], b = feature.points[index + 1] || feature.points[0]; feature.points.splice(index + 1, 0, point((a.x + b.x) / 2, (a.y + b.y) / 2)); writeCandidate(); buildFeatures(); renderFacility(); });
  $('facility-delete-vertex')?.addEventListener('click', () => { const selection=editors.vertexSelection,feature=editors.features[editors.selected]; if (!selection || selection.feature!==editors.selected || !feature || feature.label === '設備フットプリント' || feature.points.length <= (feature.closed ? 3 : 2)) return; feature.points.splice(selection.point, 1); editors.dragged = null; editors.vertexSelection=null; writeCandidate(); buildFeatures(); renderFacility(); });
  canvas?.addEventListener('pointerdown', event => {
    if (!editors.features.length) return; const p = world(event); let best = null, distance = 14;
    editors.features.forEach((feature, fi) => feature.points.forEach((vertex, vi) => { const [x, y] = pixel(vertex), d = Math.hypot(p.x - x, p.y - y); if (d < distance) { best = { feature: fi, point: vi }; distance = d; } }));
    if (best) { editors.selected = best.feature; editors.vertexSelection=best; editors.dragged = best; canvas.setPointerCapture(event.pointerId); renderFacility(); }
  });
  canvas?.addEventListener('pointermove', event => { if (!editors.dragged) return; const p = world(event), f = editors.features[editors.dragged.feature], bounds = extent(); setPoint(f, editors.dragged.point, point(Math.max(0, Math.min(bounds.width, p.worldX)), Math.max(0, Math.min(bounds.height, p.worldY)))); renderFacility(); });
  canvas?.addEventListener('pointerup', () => { if (!editors.dragged) return; editors.dragged = null; writeCandidate(); });
  canvas?.addEventListener('pointercancel', () => { editors.dragged = null; });
  document.addEventListener('keydown', event => { if (event.key === 'Delete' && canvas === document.activeElement && editors.dragged) $('facility-delete-vertex').click(); });
  renderFacility();

  window.ShelterSimNavigateTarget = target => {
    if (!target?.run_id || !target?.branch_id) return;
    const params = new URLSearchParams(location.search); params.set('project', $('project').value.trim()); params.set('project_run', target.run_id); params.set('project_branch', target.branch_id); params.set('project_minute', String(Math.max(0, Number(target.minute || 0))));
    for (const key of ['project_person', 'project_household', 'project_staff', 'project_event']) params.delete(key);
    for (const key of ['person_id', 'household_id', 'staff_id', 'event_id']) if (target[key]) params.set(`project_${key.replace('_id', '')}`, target[key]);
    location.search = params.toString();
  };
})();
