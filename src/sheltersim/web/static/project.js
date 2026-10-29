/* Persistent Project flow. Display requests never execute policies or call a provider. */
(() => {
  "use strict";
  const query = new URLSearchParams(location.search);
  if (query.get("legacy") === "1") document.body.classList.add("legacy-mode");
  if (!["demo", "admin"].includes(document.body.dataset.mode) || query.get("legacy") === "1") return;
  const el = id => document.getElementById(`ws-${id}`);
  const view = { project: query.get("project") || localStorage.getItem("sheltersim-project") || "study", run: query.get("project_run") || null, branch: query.get("project_branch") || "main", minute: Number(query.get("project_minute") || 0), runData: null, projectData: null, frame: null, draft: null, intervention: null, selected: null, context: null, editedFields: new Set(), previewGeneratedFields: new Set(), playing: false, polling: null, serial: 0, scopeSerial: 0, draftSerial: 0, interventionSerial: 0, compareSerial: 0, chatSerial: 0, comparisonRange: null, zoom: 1, panX: 0, panY: 0, hits: [] };
  const frameMeter = window.ShelterSimFrameMeter ? new window.ShelterSimFrameMeter(600) : null;
  let frameMeterScope = null;
  const STALE = Symbol("stale-project-request");
  const status = text => { el("status").textContent = text; };
  const publishView = () => window.dispatchEvent(new CustomEvent("sheltersim:project-view", {detail:{project:view.project,run:view.run,branch:view.branch,minute:view.minute,frame:view.frame,selected:view.selected,zoom:view.zoom,panX:view.panX,panY:view.panY}}));
  const json = value => JSON.stringify(value, null, 2);
  const encode = encodeURIComponent;
  const root = () => `/api/v1/projects/${encode(view.project)}`;
  const runRoot = () => `${root()}/runs/${encode(view.run)}`;
  const householdId = person => person?.household_id ?? person?.group_id;
  const branchRoot = () => `${runRoot()}/branches/${encode(view.branch)}`;
  async function request(url, body) {
    const scope = view.scopeSerial;
    const response = await fetch(url, {method: body === undefined ? "GET" : "POST", headers: {Accept: "application/json", ...(body === undefined ? {} : {"Content-Type": "application/json"})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const data = await response.json();
    if (scope !== view.scopeSerial) throw STALE;
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  const on = (id, event, callback) => el(id).addEventListener(event, async e => { try { await callback(e); } catch (error) { if (error !== STALE) status(error.message); } });
  function options(select, values, chosen) {
    select.replaceChildren();
    for (const item of values) { const option = document.createElement("option"); option.value = item.value; option.textContent = item.label; option.selected = item.value === chosen; select.append(option); }
  }
  function appendText(parent, tag, value, className) {
    const node = document.createElement(tag); node.textContent = value == null ? "未記録" : String(value); if (className) node.className = className; parent.append(node); return node;
  }
  function bytesLabel(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "値なし";
    if (value < 1024) return `${value} B`;
    const units=["KiB","MiB","GiB","TiB"];let amount=value/1024,index=0;
    while(amount>=1024&&index<units.length-1){amount/=1024;index++;}
    return `${amount.toFixed(2)} ${units[index]} (${value.toLocaleString()} B)`;
  }
  const secondsLabel=value=>typeof value==="number"&&Number.isFinite(value)&&value>=0?`${value.toFixed(3)} s`:"未計測";
  function renderTelemetry(data) {
    const box=el("telemetry-result");box.replaceChildren();
    const executions=Array.isArray(data.executions)?data.executions:[],reports=Array.isArray(data.render_reports)?data.render_reports:[],totals=data.totals||{};
    appendText(box,"h3",`計測記録 · ${data.execution_count||0} 計算区間`);
    if(!data.execution_count)appendText(box,"p","保存されたCPU・メモリ・保存量の計測はありません。0秒や0 byteという測定値ではなく、計測記録が未作成です。");
    else{
      const summary=appendText(box,"section","");appendText(summary,"h4","累積記録");
      appendText(summary,"p",`wall time ${secondsLabel(totals.wall_seconds)} · worker-thread CPU ${secondsLabel(totals.thread_cpu_seconds)} · Project保存量増加 ${bytesLabel(totals.project_storage_growth_bytes)} · Checkpoint書込 ${bytesLabel(totals.checkpoint_write_bytes)} · サーバーprocess high-water RSS ${totals.process_peak_rss_bytes>0?bytesLabel(totals.process_peak_rss_bytes):"利用可能な値なし"}`);
    }
    const list=appendText(box,"section","");appendText(list,"h4","計算区間ごとの記録");
    for(const row of [...executions].reverse().slice(0,10)){
      const item=appendText(list,"article","");item.className="telemetry-row";appendText(item,"strong",`${row.branch_id||"Branch不明"} · minute ${row.from_minute??"?"}–${row.computed_until??"?"} · ${row.status||"状態不明"}`);
      const growth=typeof row.project_storage_before_bytes==="number"&&typeof row.project_storage_after_bytes==="number"?`${bytesLabel(row.project_storage_before_bytes)} → ${bytesLabel(row.project_storage_after_bytes)} (${row.project_storage_after_bytes-row.project_storage_before_bytes>=0?"+":""}${bytesLabel(Math.abs(row.project_storage_after_bytes-row.project_storage_before_bytes))})`:"Project保存量差分なし";
      const runMemory=row.memory_attribution_status==="measured"?bytesLabel(row.run_peak_memory_bytes):`Run別メモリ未計測${row.memory_attribution_reason?`（${row.memory_attribution_reason}）`:""}`;
      appendText(item,"p",`記録 ${row.recorded_at||"時刻なし"} · wall ${secondsLabel(row.wall_seconds)} · worker-thread CPU ${secondsLabel(row.thread_cpu_seconds)} · 保存量 ${growth} · Checkpoint実書込 ${bytesLabel(row.checkpoint_write_bytes)} · Run peak memory ${runMemory} · process high-water RSS ${row.process_peak_rss_bytes>0?bytesLabel(row.process_peak_rss_bytes):"利用可能な値なし"}${row.failure_code?` · エラー ${row.failure_code}`:""}`);
    }
    if(!executions.length)appendText(list,"p","実行ごとの計測レコードはありません。");
    const renderList=appendText(box,"section","");appendText(renderList,"h4","ブラウザー描画の計測");
    for(const row of [...reports].reverse().slice(0,10))appendText(renderList,"p",`${row.recorded_at||"時刻なし"} · ${row.branch_id||"Branch不明"} · ${row.visibility||"可視状態不明"} · ${typeof row.samples==="number"?row.samples:"未記録"} sample · ${typeof row.fps==="number"?`${row.fps.toFixed(1)} FPS`:"FPS未計測"} · ${typeof row.p95_frame_ms==="number"?`p95 ${row.p95_frame_ms.toFixed(1)} ms`:"p95未計測"}`);
    if(!reports.length)appendText(renderList,"p","描画FPS記録はありません。計測を有効にし、地図を表示してから保存してください。");
    appendText(box,"p","範囲と制約: CPU / wall time は明示計測したresume区間だけです。Project保存量は前後のStore全体差で、並行更新を含みます。Checkpoint byteは新規manifest/chunkの書込量です。Run別peak memoryは共有workerのため現在未計測として理由付きで記録し、RSSはサーバープロセス累積high-waterで代用しません。FPSは可視タブのrequestAnimationFrameで地図・比較Canvasを再描画した時の参考値で、ブラウザー/GPU・画面状態・計測負荷に依存し、代表性能を保証しません。","muted");
    const raw=document.createElement("details"),summary=document.createElement("summary"),pre=document.createElement("pre");summary.textContent="保存されたテレメトリJSON";pre.textContent=json(data);raw.append(summary,pre);box.append(raw);
  }
  function renderEquipmentRows(rows = []) {
    const box=el("equipment-rows");box.replaceChildren();
    for(const item of rows){
      const row=document.createElement("div");row.className="equipment-row";
      const point=item.position_m||{};
      for(const [field,label,type,value] of [["facility_id","設備ID","text",item.facility_id],["facility_kind","追加設備の種類","text",item.facility_kind],["capacity","容量","number",item.capacity],["working_units","稼働数","number",item.working_units],["zone_id","追加先Zone ID","text",item.zone_id],["x_m","位置X (m)","number",point.x],["y_m","位置Y (m)","number",point.y],["footprint_m2","占有面積 m²","number",item.footprint_m2],["source_id","出典ID","text",item.source_id]]){
        const wrap=document.createElement("label");wrap.textContent=label;const input=document.createElement("input");input.type=type;input.dataset.field=field;input.value=value??(field==="source_id"?"operator_condition":"");if(type==="number"){input.min="0";input.step=field.includes("_m")||field==="footprint_m2"?"0.1":"1";}input.addEventListener("input",()=>{view.editedFields.add("equipment-rows");invalidatePreview();});wrap.append(input);row.append(wrap);
      }
      const addLabel=document.createElement("label"),add=document.createElement("input");add.type="checkbox";add.dataset.field="add";add.checked=Boolean(item.add);addLabel.append(add,document.createTextNode("新しい設備"));row.append(addLabel);
      for(const field of ["facility_kind","zone_id"])row.querySelector(`[data-field="${field}"]`).disabled=!add.checked;
      add.addEventListener("change",()=>{for(const field of ["facility_kind","zone_id"])row.querySelector(`[data-field="${field}"]`).disabled=!add.checked;view.editedFields.add("equipment-rows");invalidatePreview();});
      const remove=document.createElement("button");remove.type="button";remove.textContent="削除";remove.addEventListener("click",()=>{row.remove();view.editedFields.add("equipment-rows");invalidatePreview();});row.append(remove);box.append(row);
    }
  }
  function fillStructuredConditions(requestValue={}) {
    const population=requestValue.population_conditions||{};
    el("behavior-policy").value=requestValue.behavior_policy_id||"utility-softmax-v1";
    el("social-policy").value=requestValue.social_policy_id||"sparse-family-zone-contact-v1";
    el("information-policy").value=requestValue.information_policy_id||"sparse-information-contact-v1";
    el("social-propagation-enabled").checked=Boolean(requestValue.social_propagation_enabled);
    el("information-spread-enabled").checked=Boolean(requestValue.information_spread_enabled);
    el("queue-policy").value=requestValue.queue_policy_id||"need-weighted-physical-queue-v1";
    el("relationship-policy").value=requestValue.relationship_policy_id||"distance-duration-repeat-trust-v1";
    el("social-proximity-policy").value=requestValue.social_proximity_policy_id||"disabled-proximity-v1";
    el("body-dynamics-policy").value=requestValue.body_dynamics_policy_id||"body-feedback-complete-v1";
    el("care-outcome-policy").value=requestValue.care_outcome_policy_id||"typed-care-support-response-v1";
    el("staff-operations-policy").value=requestValue.staff_operations_policy_id||"staff-urgency-first-v1";
    el("staff-fatigue-policy").value=requestValue.staff_fatigue_policy_id||"staff-fatigue-linear-v1";
    el("zone-temperature-policy").value=requestValue.zone_temperature_policy_id||"zone-temperature-comfort-band-v1";
    el("zone-rest-policy").value=requestValue.zone_rest_policy_id||"zone-rest-multiplicative-v1";
    el("zone-hygiene-policy").value=requestValue.zone_hygiene_policy_id||"zone-hygiene-linear-v1";
    el("zone-privacy-policy").value=requestValue.zone_privacy_policy_id||"zone-privacy-threshold-v1";
    el("policy-parameter-overrides").value=requestValue.model_parameter_overrides?json(requestValue.model_parameter_overrides):"";
    el("household-composition").value=population.household_composition?json(population.household_composition):"";
    for(const [id,key] of [["medical-rate","medical_support_rate"],["medication-rate","medication_support_rate"],["pet-rate","pet_household_rate"]])el(id).value=population[key]??"";
    el("care-support-rates").value=population.care_support_need_rates?json(population.care_support_need_rates):"";
    for(const [id,key] of [["access-rates","additional_access_need_rates"],["language-weights","language_weights"],["information-weights","information_access_weights"],["daily-rhythms","daily_rhythms"]])el(id).value=population[key]===undefined?"":json(population[key]);
    const operations=requestValue.operational_conditions||{},stock=operations.initial_resources||{};
    el("staff-positive-shifts").value=operations.staff_shifts===undefined?"":json(operations.staff_shifts);
    el("stock-status").value=stock.status||"unknown";el("water-ml").value=stock.water_ml??"";el("food-units").value=stock.food_meal_units??"";el("warehouse-id").value=stock.warehouse_id||"";el("stock-zone").value=stock.zone_id||"";
    renderEquipmentRows(operations.equipment||[]);el("scheduled-conditions").value=operations.scheduled?.length?json(operations.scheduled):"";
  }
  function renderPreview() {
    const box = el("preview-result"); box.replaceChildren();
    if (!view.draft) { appendText(box, "p", "入力・出典・仮定・不足条件をここに表示します。"); return; }
    const d = view.draft, req = d.request || {}, research = d.research || {}, scenario = d.scenario || {}, profiles=scenario.profiles||[],populationPreview=scenario.provenance?.population_generation||{};
    const profileCounts=new Map();for(const profile of profiles){if(profile.group_id)profileCounts.set(profile.group_id,(profileCounts.get(profile.group_id)||0)+1);}
    appendText(box, "h3", d.can_start ? "確認して開始できるシナリオ" : "不足・矛盾を確認してください");
    appendText(box, "p", `${req.municipality || "地域未確定"} · ${req.disaster || "災害未確定"} · ${req.evacuees ?? "人数未確定"}人 · ${req.duration_days ?? "期間未確定"}日`);
    if(d.missing_fields?.length){appendText(box,"h4","自然言語から確認できなかった必須条件");const missing=appendText(box,"ul","");for(const value of d.missing_fields)appendText(missing,"li",value);}
    if (d.approval_blockers?.length) {
      appendText(box,"h4","開始を止めている不足");appendText(box,"p","承認ブロックは手動で解除できません。生成済み人物の関係を下の個別確認フォームで修正するか、匿名の世帯テンプレート・支援条件を確認して再プレビューしてください.","muted");
      const blockers=appendText(box,"div","");
      for(const value of d.approval_blockers){const card=appendText(blockers,"article","");card.className="household-review-blocker";appendText(card,"p",value);
        const kind=value.includes("未成年者")||value.includes("保護者")?"guardian":value.includes("移動支援")||value.includes("アクセシブル")||value.includes("介護")?"support":"other";
        if(kind!=="other"){const edit=appendText(card,"button",kind==="guardian"?"世帯・保護者入力を確認/修正":"支援ニーズ・介護入力を確認/修正");edit.type="button";edit.addEventListener("click",()=>window.dispatchEvent(new CustomEvent("sheltersim:household-review",{detail:{kind,reason:value}})));}
      }
      const repreview=appendText(box,"button","修正後に世帯条件を再プレビュー");repreview.type="button";repreview.addEventListener("click",()=>el("preview").click());
      const reviews=(populationPreview.person_requirements_preview||[]).filter(item=>item.requires_review||item.guardian_status==="unaccompanied"||(item.review_reasons||[]).length);
      if(reviews.length){appendText(box,"h4","個別確認が必要な合成人物");appendText(box,"p",`${reviews.length}件。表示はこのPreviewで生成された匿名Profileです。下の個別修正フォームで、同一世帯内の保護者・介護関係をIDで確認して修正できます。`,"muted");for(const item of reviews){const household=item.household_id||"世帯不明",templateId=populationPreview.household_template_ids?.[household]||"",size=profileCounts.get(household)||0,line=appendText(box,"p",`${item.person_id} · ${household} · ${item.household_role||"役割不明"} · 保護者状態 ${item.guardian_status||"不明"} · caregivers ${(item.caregiver_ids||[]).join(", ")||"記録なし"} · ${[...(item.review_reasons||[])].join("/ ")||"要確認"}`);line.className="household-review-person";const focus=appendText(box,"button",`世帯 ${household} の入力を開く`);focus.type="button";focus.addEventListener("click",()=>window.dispatchEvent(new CustomEvent("sheltersim:household-review",{detail:{kind:item.guardian_status==="unaccompanied"?"guardian":"support",person_id:item.person_id,household_id:household,template_id:templateId,members:size,reason:(item.review_reasons||[]).join(", ")}})));if(item.guardian_status==="unaccompanied"&&size>=2){const candidate=appendText(box,"button",`世帯人数 ${size} の保護者+子ども案を追加`);candidate.type="button";candidate.addEventListener("click",()=>window.dispatchEvent(new CustomEvent("sheltersim:household-review",{detail:{kind:"guardian",action:"add-template",person_id:item.person_id,household_id:household,template_id:templateId,members:size}})));}}
      }else if(reviews.length===0&&populationPreview.person_requirements_preview)appendText(box,"p","人物要件previewに追加確認対象はありませんでした。これは未生成の人々や実在世帯の安全性を保証しません.","muted");
    }
    for (const [title, values] of [["確認事項・警告",d.warnings||[]],["取得できなかった情報",research.unknowns||[]],["仮定",research.assumptions||[]]]) {
      appendText(box,"h4",title); const ul=appendText(box,"ul","");
      for(const item of values) appendText(ul,"li",typeof item==="string"?item:`${item.subject||item.id||"仮定"}: ${item.value??item.reason??"値なし"}`);
      if(!values.length)appendText(ul,"li","なし");
    }
    const sourceList=appendText(box,"section",""); appendText(sourceList,"h4","出典と鮮度");
    for(const source of research.sources||[]) { const p=appendText(sourceList,"p",`${source.title} · ${source.publisher} · ${source.freshness} · ${source.source_kind}`); if(typeof source.url==="string"&&/^https:\/\//i.test(source.url)){const a=document.createElement("a");a.href=source.url;a.target="_blank";a.rel="noopener noreferrer";a.textContent=" 出典を開く";p.append(a);} }
    if(!(research.sources||[]).length)appendText(sourceList,"p","出典情報なし。未取得を実測値として扱いません。");
    const candidates=research.llm_source_candidates||[];appendText(sourceList,"h4","Claude検索の出典候補（未検証）");
    appendText(sourceList,"p","次のリンクは政府・自治体ドメインの検索候補です。ページ本文をこのアプリが独立取得・照合していないため、観測値や施設情報としてScenarioには取り込んでいません。適用範囲・更新日・利用条件を確認してください。","muted");
    for(const candidate of candidates){const line=appendText(sourceList,"p",`${candidate.title} · ${candidate.status} · accessed ${candidate.accessed_at||"時刻不明"}`);if(typeof candidate.url==="string"&&/^https:\/\//i.test(candidate.url)){const link=document.createElement("a");link.href=candidate.url;link.target="_blank";link.rel="noopener noreferrer";link.textContent=" ページ候補を開く";line.append(link);}if(candidate.cited_text)appendText(sourceList,"blockquote",candidate.cited_text);}
    if(!candidates.length)appendText(sourceList,"p","引用付き候補なし。検索結果は得られていないか、Claude/API検索が利用できませんでした。");
    const policy=scenario.model_configs?.policy_registry?.psychology;
    if(policy)appendText(box,"p",`心理モデル: ${policy}。身体・感情・スタッフ係数は合成の仮定で、実測による校正や現実の安全性予測ではありません。`);
    detailSection(box,"生成条件に加えた仮定",scenario.provenance?.assumptions||[]);
    detailSection(box,"モデル構成・係数 (設定/仮定)",scenario.model_configs||{});
    detailSection(box,"生成モデル・図面・人口条件の出典",scenario.provenance?.sources||[]);
    const populationContext=scenario.model_configs?.population_context||{},peopleContext=populationContext.people||{},households=new Map();
    for(const profile of profiles){const id=profile.group_id||"世帯未割当";households.set(id,[...(households.get(id)||[]),profile.id]);}
    if(profiles.length){appendText(box,"h4","生成された合成世帯・関係（入力条件の確認）");appendText(box,"p",`${profiles.length} 人 · ${households.size} 世帯 · 初期Profileのgroup_idで集計。実在の家族関係を示しません。`);for(const [household,members] of households){appendText(box,"p",`${household} · ${members.length} 人 · ${members.join(", ")}`);for(const id of members){const relation=peopleContext[id];if(relation&&(relation.parent_ids?.length||relation.caregiver_ids?.length||relation.care_for_ids?.length))appendText(box,"p",`  ${id} · 保護者 ${relation.parent_ids?.join(", ")||"未記録"} · 介護者 ${relation.caregiver_ids?.join(", ")||"未記録"} · 支援対象 ${relation.care_for_ids?.join(", ")||"未記録"}`);}}}
    for(const [id,context] of Object.entries(peopleContext)){const needs=context.care_support_needs||[];if(needs.length)appendText(box,"p",`${id} · 支援種別 ${needs.join(", ")} · 合成Scenarioの依頼分類です。臨床効果を予測しません。`);}
    if((scenario.staff||[]).length)detailSection(box,"合成スタッフ roster（予定・担当可能時間は別記録）",scenario.staff.map(item=>({id:item.id,zone_id:item.zone_id,skills:item.skills})));
    const usage=d.llm_usage||{};appendText(box,"p",`PREPARE · ${usage.mode||"状態不明"} · API呼出 ${usage.api_request_count??"不明"} · この準備の費用 $${Number(usage.spent_usd||0).toFixed(4)}`);
    appendText(box,"p",`Scenario ${scenario.id||"未確定"} · version ${scenario.version||"preview"} · 承認hash ${d.approved_hash||"なし"}`);
  }
  function updateCandidateLists(research, request, selectedShelterId = view.draft?.scenario?.provenance?.selected_shelter?.id || "") {
    const municipalities = [...(research.municipalities||[])];
    const municipalityOptions = municipalities.map(item=>({value:item.municipality_code||item.name,label:`${item.name}${item.match_kind?` · ${item.match_kind}`:""}${item.municipality_code?` · ${item.municipality_code}`:""}`}));
    options(el("municipality-candidate"),[{value:"",label:"候補を選択（任意）"},...municipalityOptions],request.municipality_code||"");
    const shelters=(research.shelters||[]).filter(item=>item.designation_type!=="emergency_site");
    options(el("shelter-candidate"),[{value:"",label:"公開候補を選択（任意）"},...shelters.map(item=>({value:item.id,label:`${item.name} · ${item.freshness} · 容量 ${item.capacity??"不明"}`}))],selectedShelterId);
  }
  let inspectedWeatherCandidate="";
  function renderRegionReview(draft){
    const box=el("region-review");box.replaceChildren();
    const research=draft?.research||{},request=research.request||draft?.request||{},sources=new Map((research.sources||[]).map(source=>[source.id,source]));
    const municipalityCode=request.municipality_code||draft?.request?.municipality_code||"",municipality=(research.municipalities||[]).find(item=>municipalityCode&&item.municipality_code===municipalityCode)|| (research.municipalities||[]).find(item=>item.name===request.selected_municipality_name)||null;
    appendText(box,"h4","地域・気象の出典確認");
    appendText(box,"p",`選択自治体 ${municipality?.name||request.selected_municipality_name||draft?.request?.municipality||"未選択"} · 自治体コード ${municipalityCode||municipality?.municipality_code||"未確定"} · 候補一致 ${municipality?.match_kind||"不明"}`);
    if(municipality){const source=sources.get(municipality.source_id),line=appendText(box,"p",`自治体候補出典 ${municipality.source_id} · ${source?.title||"題名未記録"} · ${source?.freshness||"鮮度不明"} · published ${source?.published_on||"未記録"} · accessed ${source?.accessed_at||"未記録"} · ${source?.geographic_scope||"適用範囲不明"}`);if(source&&/^https:\/\//i.test(source.url)){const link=document.createElement("a");link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";link.textContent=" 出典を開く";line.append(link);}}
    else appendText(box,"p","選択した自治体名に一致する自治体候補レコードはありません。地名だけから予報区域や観測所を推定していません。","muted");
    const targetDate=request.target_date||draft?.request?.target_date||"未指定",areaCode=request.weather_area_code||draft?.request?.weather_area_code||"";
    appendText(box,"p",`対象日 ${targetDate} · 気象庁予報ファイル区域コード ${areaCode||"未指定"}`);
    const weather=(research.weather_observations||[]).filter(item=>!areaCode||item.area_code===areaCode),keys=new Map();
    for(const item of weather){const code=item.forecast_area_code||item.station_code;if(code)keys.set(`${item.forecast_area_code?"area":"station"}:${code}`,{kind:item.forecast_area_code?"forecast_area":"station",code,label:item.area_label});}
    const status=(research.provider_statuses||[]).find(item=>item.data_kind==="weather");
    appendText(box,"p",status?`気象取得状態 ${status.status||"不明"} · 適用可能性 ${status.applicability||"不明"} · ${status.message||""}`:"気象Provider状態は記録されていません。","muted");
    if(!areaCode)appendText(box,"p","予報ファイル区域コードを確認して入力してください。選んだ自治体と区域コードの対応付けをこのAPIは検証しません。","muted");
    if(!keys.size){appendText(box,"p",areaCode?"選択コードに属する細分区域・観測所レコードが返っていません。データがない状態を気象ゼロ値として扱いません。":"候補の気象記録はありません。","muted");return;}
    const label=appendText(box,"label","");label.append(document.createTextNode("確認する予報区域・観測所 "));const select=document.createElement("select");select.setAttribute("aria-label","出典を確認する予報区域または観測所");
    for(const [key,item] of keys){const option=document.createElement("option");option.value=key;option.textContent=`${item.kind==="station"?"観測所":"細分区域"} ${item.code} · ${item.label}`;select.append(option);}
    if(!keys.has(inspectedWeatherCandidate))inspectedWeatherCandidate=keys.keys().next().value;select.value=inspectedWeatherCandidate;label.append(select);
    const details=appendText(box,"div","");details.className="workspace-events";
    const show=()=>{details.replaceChildren();const selected=keys.get(select.value);if(!selected)return;const rows=weather.filter(item=>(item.forecast_area_code||item.station_code)===selected.code);
      appendText(details,"p",`候補 ${selected.kind==="station"?"観測所":"細分区域"} ${selected.code} · ${selected.label} · 自治体への適用可能性は自動確認されません。`);
      for(const item of rows){const source=sources.get(item.source_id);appendText(details,"p",`${item.valid_at} · ${item.metric} ${item.value} ${item.unit} · ${item.status} · applicability=${item.applicability} · source ${item.source_id} · published ${item.published_at}`);if(source){const line=details.lastElementChild;appendText(line,"span",` · ${source.title} · ${source.freshness} · accessed ${source.accessed_at}`);if(/^https:\/\//i.test(source.url)){const link=document.createElement("a");link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";link.textContent=" 出典を開く";line.append(link);}}}
      if(!rows.length)appendText(details,"p","この候補の値は記録されていません。");
      appendText(details,"p","この選択は出典の閲覧だけに使われ、自治体対応の検証やScenarioへの気象適用を行いません。","muted");
    };
    select.addEventListener("change",()=>{inspectedWeatherCandidate=select.value;show();});show();
  }
  function renderEvidence(data){
    const box=el("evidence");box.replaceChildren();appendText(box,"p",`モデル選択・評価情報 · schema ${data.schema_version||"不明"} · 外部妥当性 ${data.external_validation?"あり":"未確認"}`);
    const models=appendText(box,"section","");appendText(models,"h4","選択モデル");
    for(const [domain,model] of Object.entries(data.models||{}))appendText(models,"p",`${domain}: ${model.name||model.id} · ${model.id} · ${model.version||"version不明"}`);
    detailSection(box,"モデルversion・設定・係数",{versions:data.model_versions||{},models:data.models||{}});
    detailSection(box,"モデル係数・単位・範囲・出典・確度",data.parameters||[]);
    detailSection(box,"scenario assumptions",data.scenario_assumptions||[]);
    const sources=appendText(box,"section","");appendText(sources,"h4","シナリオ出典");
    for(const source of data.scenario_sources||[]){const p=appendText(sources,"p",`${source.title||source.id} · ${source.source_kind||"source kind不明"} · ${source.freshness||"鮮度不明"}`);if(typeof source.url==="string"&&/^https:\/\//i.test(source.url)){const a=document.createElement("a");a.href=source.url;a.target="_blank";a.rel="noopener noreferrer";a.textContent=" 出典を開く";p.append(a);}}
    detailSection(box,"地域調査の不足・警告",data.region_limits||{});
    detailSection(box,"データセット・係数適用・出典・不確実性",data.dataset_registry||{});
    appendText(box,"p",data.interpretation||"評価上の制約は記録されていません。","muted");
  }
  function invalidatePreview() {
    ++view.draftSerial; view.draft=null; view.facilityBase=null;el("facility-edit-check").disabled=true; el("approve").disabled=true; el("approve-check").checked=false;el("region-review").textContent="条件が変更されたため、地域・気象の確認内容を無効にしました。再プレビューして最新候補と出典を確認してください。";renderPreview();
    window.dispatchEvent(new CustomEvent("sheltersim:scenario-invalidated"));
  }
  function setSelected(selection) {
    view.selected=selection;view.context=null;++view.chatSerial;el("ask").disabled=false;if(selection?.kind==="person")el("chat-person").value=selection.id;else el("chat-person").value="";for(const button of el("entity-list").querySelectorAll("button"))button.setAttribute("aria-pressed",String(selection?.kind===button.dataset.kind&&selection?.id===button.dataset.id));showDetail();draw();publishView();
  }
  function remember() {
    const q = new URLSearchParams(location.search);
    for (const [key,value] of Object.entries({project: view.project, project_run: view.run, project_branch: view.branch, project_minute: view.minute})) {
      if (value === null || value === undefined || value === "") q.delete(key); else q.set(key, value);
    }
    history.replaceState(null, "", `${location.pathname}?${q}`);
    localStorage.setItem("sheltersim-project", view.project);
  }
  async function openProject() {
    ++view.scopeSerial; ++view.draftSerial; ++view.interventionSerial; ++view.compareSerial; ++view.chatSerial; el("ask").disabled=false; clearTimeout(view.polling); view.playing = false; view.comparison = null; view.comparisonFrames = null; view.compareIds = null; view.comparisonRange = null; ++view.serial;
    view.project = el("project").value.trim();
    const all = await request("/api/v1/projects");
    if (!all.projects.some(p => p.project_id === view.project)) await request("/api/v1/projects", {project_id: view.project});
    const project = await request(root());
    view.projectData = project;
    if (view.run && !project.runs.some(r => r.run_id === view.run)) { view.run = null; view.runData = null; view.frame = null; }
    options(el("run"), [{value: "", label: "新しいRun / 保存Runを選択"}, ...project.runs.map(r => ({value:r.run_id, label:`${r.run_id} · ${r.status}${r.archived ? " · archived" : ""}`}))], view.run);
    status(`Project ${view.project} · 保存 ${all.storage.bytes.toLocaleString()} bytes。シナリオを作成するか保存Runを開いてください。`);
    await openRun();
    remember();
    publishView();
  }
  function selectedReference() { return view.runData?.branches.find(b => b.branch_id === view.branch); }
  async function openRun() {
    view.pendingFork=null;
    ++view.scopeSerial; ++view.interventionSerial; ++view.compareSerial; ++view.chatSerial; el("ask").disabled=false; view.playing = false; view.context = null; view.intervention = null; view.comparison = null; view.comparisonFrames = null; view.compareIds = null; view.comparisonRange = null; clearTimeout(view.polling);
    ++view.serial;view.frame=null;view.runData=null;view.selected=null;historyPage=null;el("entity-list").replaceChildren();el("events").replaceChildren();el("detail").replaceChildren();el("map").getContext("2d").clearRect(0,0,el("map").width,el("map").height);el("kpis").textContent="";
    if (!view.run) { ++view.serial; view.runData=null; view.frame=null; view.selected=null; view.minute=0; el("intervention-preview").disabled=true;el("fork").disabled=true;el("comparison").replaceChildren(); el("entity-list").replaceChildren(); el("events").replaceChildren(); el("kpis").textContent="";el("missing").textContent="";el("map").getContext("2d").clearRect(0,0,el("map").width,el("map").height);el("report").textContent="保存Runを選択してください。";el("chat").replaceChildren();el("detail").replaceChildren();el("range").textContent="";renderComparisonChart();return; }
    el("comparison").replaceChildren();
    await refreshRun(false);
    const evidence = await request(`${runRoot()}/evidence`);
    renderEvidence(evidence);
    await loadConversationHistory();
    const report = await request(`${branchRoot()}/report`);
    el("report").textContent = report.markdown || json(report);
    await seek(view.minute);
    const focusPerson=query.get("project_person"),focusHousehold=query.get("project_household"),focusEvent=query.get("project_event"),focusStaff=query.get("project_staff");
    if(focusPerson&&view.frame.people.some(item=>item.id===focusPerson))setSelected({kind:"person",id:focusPerson});
    else if(focusHousehold&&view.frame.people.some(item=>householdId(item)===focusHousehold))setSelected({kind:"group",id:focusHousehold});
    else if(focusStaff&&view.frame.staff?.some(item=>item.id===focusStaff))setSelected({kind:"staff",id:focusStaff});
    else if(focusEvent)setSelected({kind:"event",id:focusEvent});
  }
  async function refreshRun(follow) {
    const previous = selectedReference();
    const atHead = previous && view.minute === previous.computed_until;
    const oldStatus = view.runData?.status;
    const data = await request(runRoot()); view.runData = data;
    let forkNote=null;
    if(view.pendingFork&&data.status!=="running"){
      const child=view.pendingFork;view.pendingFork=null;
      if(data.branches.some(b=>b.branch_id===child)){view.branch=child;await openRun();return;}
      forkNote=`分岐を作成できませんでした: ${data.last_fork?.error||"理由を取得できませんでした"}`;
    }
    const calculationActive = data.status === "running";
    el("intervention-preview").disabled = calculationActive;
    el("fork").disabled = calculationActive;
    if (!data.branches.some(b => b.branch_id === view.branch)) view.branch = data.branches[0]?.branch_id;
    options(el("branch"), data.branches.map(b => ({value:b.branch_id,label:`${b.branch_id} · ${b.fork_minute}–${b.computed_until}`})), view.branch);
    const comparisons = new Set([...el("compare-ids").selectedOptions].map(o => o.value));
    options(el("compare-ids"), data.branches.map(b => ({value:b.branch_id,label:b.branch_id})), null);
    for (const option of el("compare-ids").options) option.selected = comparisons.has(option.value);
    const ref = selectedReference(); if (!ref) return;
    const duration = data.duration_minutes || (await request(root())).scenarios.find(s => s.scenario_id === data.scenario_id && s.scenario_version === data.scenario_version)?.scenario.duration_minutes;
    view.duration = duration;
    el("target").max = duration; if (!el("target").value) el("target").value = duration;
    if(view.compareIds?.length){
      const refs=new Map(data.branches.map(item=>[item.branch_id,item]));
      const ids=[...new Set([view.branch,...view.compareIds])];
      const lower=Math.max(...ids.map(id=>refs.get(id)?.fork_minute??Infinity));
      const upper=Math.min(...ids.map(id=>refs.get(id)?.computed_until??-Infinity));
      if(Number.isFinite(lower)&&Number.isFinite(upper)&&lower<=upper)view.comparisonRange=[lower,upper];
      else{view.compareIds=null;view.comparisonRange=null;view.comparison=null;view.comparisonFrames=null;el("comparison").replaceChildren();renderComparisonChart();}
    }
    const visibleRange=view.comparisonRange||[ref.fork_minute,ref.computed_until];
    el("seek").min = el("minute").min = visibleRange[0];
    el("seek").max = el("minute").max = visibleRange[1];
    el("range").textContent = view.comparisonRange?`同期計算済み ${visibleRange.join("–")}分 · / ${duration}分 · ${data.status}`:`計算済み ${ref.fork_minute}–${ref.computed_until} / ${duration}分 · ${data.status}`;
    if (follow && atHead) await seek(visibleRange[1]);
    else if(view.minute<visibleRange[0]||view.minute>visibleRange[1])await seek(Math.max(visibleRange[0],Math.min(visibleRange[1],view.minute)));
    if (oldStatus === "running" && data.status !== "running") {
      const report = await request(`${branchRoot()}/report`);
      el("report").textContent = report.markdown || json(report);
    }
    if(forkNote||view.pendingFork)status(forkNote||`分岐 ${view.pendingFork} を作成しています…`);
    else status(`Run ${view.run} · Branch ${view.branch} · ${data.status} · PREPARE/RUNTIME/REVIEWの費用・工程別明細は保存Project台帳から確認できます。キー有無はサーバー側で判定し、ブラウザに秘密値を出しません。`);
    if (data.status === "running") {
      clearTimeout(view.polling); view.polling = setTimeout(() => refreshRun(true).catch(e => {if(e!==STALE)status(e.message);}), 1500);
    } else { clearTimeout(view.polling); }
  }
  async function seek(minute) {
    const ref = selectedReference(); if (!ref) return;
    const bounds = view.comparisonRange || [ref.fork_minute, ref.computed_until];
    if (!Number.isInteger(minute) || minute < bounds[0] || minute > bounds[1]) throw new Error("未計算・分岐前、または同期比較範囲外の時刻は表示できません。");
    const serial = ++view.serial;
    if(minute!==view.minute){view.context=null;++view.chatSerial;++view.interventionSerial;el("ask").disabled=false;view.comparisonFrames=null;view.comparison=null;el("comparison").replaceChildren();renderComparisonChart();}
    const frame = await request(`${branchRoot()}/timeline?minute=${minute}`);
    if (serial !== view.serial) return;
    if (view.minute !== frame.minute) { view.intervention = null; ++view.interventionSerial; el("fork-check").checked = false; }
    view.frame = frame; view.minute = frame.minute;
    el("seek").value = el("minute").value = frame.minute;
    const k = frame.kpis;
    el("kpis").textContent = `minute ${frame.minute} · 人数 ${k.population} · 入場待ち ${k.pending_arrivals} · ストレス平均 ${Number(k.stress_mean).toFixed(3)} · トイレ待ち ${k.toilet_queue_length} · スタッフ負荷 ${Number(k.staff_load_ratio).toFixed(3)}`;
    el("missing").textContent = `実位置なし ${frame.agents.filter(a => !a.point || a.position_kind !== "physical").length}人。スタッフ位置は現在Zoneで、経路歩行は未実装です。会話文は未記録です。`;
    updateLegend(); renderEntityList(); renderEvents(); updatePeopleChoices();
    if(view.compareIds){el("comparison").replaceChildren();view.comparison=null;try{await loadComparison(view.compareIds,frame.minute,serial);}catch(error){if(error!==STALE)el("compare-status").textContent=error.message;}}
    remember(); draw(); showDetail(); publishView();
  }
  function color(person, layer) {
    const n = Math.max(0,Math.min(1,layer === "health" ? Math.max(person?.needs?.fatigue||0, person?.needs?.hunger||0, person?.needs?.hydration_need||0) : person?.stress||0));
    if (layer === "stress" || layer === "health") return `hsl(${120*(1-n)},65%,43%)`;
    if (layer === "social" && householdId(person)) {let hue=0;for(const c of householdId(person))hue=(hue*31+c.charCodeAt(0))%360;return `hsl(${hue},55%,43%)`;}
    return person?.activity === "moving" ? "#267baf" : person?.activity === "queued" ? "#cf7a31" : "#427b69";
  }
  function drawCluster(ctx, x, y, count, fill) {
    const radius=Math.min(17,5+Math.sqrt(count)*1.15);ctx.fillStyle=fill;ctx.beginPath();ctx.arc(x,y,radius,0,Math.PI*2);ctx.fill();ctx.strokeStyle="#fff";ctx.lineWidth=1.5;ctx.stroke();ctx.fillStyle="#fff";ctx.textAlign="center";ctx.textBaseline="middle";ctx.fillText(String(count),x,y);ctx.textAlign="start";ctx.textBaseline="alphabetic";
  }
  function render(canvas, frame, hits = []) {
    const ctx = canvas.getContext("2d"); ctx.clearRect(0,0,canvas.width,canvas.height);
    const geometry = frame.geometry;
    if (!geometry) { ctx.fillText("この記録には物理Geometryがありません。従来デモ / Inspectorで表示座標を確認できます。",20,35); return; }
    const zoom=Math.max(.1,view.zoom),scale=Math.min((canvas.width-40)/geometry.width_m,(canvas.height-40)/geometry.height_m)*zoom;
    const point = p => [20 + view.panX + p.x*scale,20 + view.panY + p.y*scale];
    const layer=el("layer").value,agents=frame.agents||[],facilities=frame.facilities||[],staffRows=frame.staff||[],counts=new Map();for(const a of agents)counts.set(a.zone_id,(counts.get(a.zone_id)||0)+1);
    ctx.lineWidth = 1; ctx.font = "12px sans-serif";
    for (const zone of geometry.zones) {
      ctx.beginPath(); zone.polygon.forEach((p,i) => {const [x,y]=point(p); i ? ctx.lineTo(x,y):ctx.moveTo(x,y);}); ctx.closePath();ctx.fillStyle=layer==="crowd"?`hsl(${120*(1-Math.min(1,(counts.get(zone.zone_id)||0)/zone.usable_area_m2))},45%,85%)`:"#eef2ed";ctx.fill();ctx.strokeStyle="#859b91";ctx.stroke();
      const [x,y]=point(zone.polygon[0]);ctx.fillStyle="#4d6259";ctx.fillText(zone.zone_id,x+4,y+15);
      if(layer==="crowd")ctx.fillText(`${((counts.get(zone.zone_id)||0)/zone.usable_area_m2).toFixed(2)} 人/m²`,x+4,y+30);
    }
    for(const room of Object.values(frame.geometry_overlay?.temporary_rooms||{})){
      ctx.beginPath();(room.polygon_m||[]).forEach((p,i)=>{const [x,y]=point(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.closePath();
      ctx.fillStyle=room.active?"#70a9bf35":"#78817d22";ctx.fill();ctx.strokeStyle=room.active?"#286b83":"#78817d";ctx.lineWidth=2;ctx.setLineDash(room.active?[]:[5,4]);ctx.stroke();ctx.setLineDash([]);
      const [rx,ry]=point(room.polygon_m?.[0]||{x:0,y:0});ctx.fillStyle=room.active?"#205d70":"#66706b";ctx.fillText(`${room.room_id} · 入室割当 ${room.admitted_agent_ids?.length||0}/${room.people_capacity} 人 · 現在位置 ${room.observed_occupant_agent_ids?.length||0}`,rx+4,ry+14);ctx.font="10px sans-serif";ctx.fillText(`ベッド位置の観測 ${room.observed_occupied_beds?.length||0}/${room.sleeping_capacity}`,rx+4,ry+27);ctx.font="12px sans-serif";
      for(const [index,bed] of (room.bed_points_m||[]).entries()){const [bx,by]=point(bed),occupied=(room.observed_occupied_beds||[]).some(item=>item.bed_index===index);ctx.beginPath();ctx.arc(bx,by,5,0,Math.PI*2);ctx.fillStyle=occupied?"#c46a36":"#488b73";ctx.fill();ctx.strokeStyle="#fff";ctx.lineWidth=1;ctx.stroke();}
    }
    const movementUsage=frame.movement?.edge_usage_agent_minutes||{},limitRelated=window.ShelterSimMapLOD?.limitRelated;
    const routeCandidates=geometry.routes||[],limitedRoutes=limitRelated?limitRelated(routeCandidates,zoom,route=>Number(movementUsage[route.edge_id]||0)>0,120,360,900):{total:routeCandidates.length,items:routeCandidates},routeView={...limitedRoutes,visible:el("routes").checked?limitedRoutes.items.length:0};
    if (el("routes").checked) for (const route of routeView.items) {
      const load=movementUsage[route.edge_id]||0;
      ctx.beginPath();route.points.forEach((p,i) => {const [x,y]=point(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.lineWidth=layer==="crowd"?Math.min(8,1+Math.log1p(load)):1;ctx.strokeStyle=load?"#ca8b55":"#a7b8be";ctx.stroke();
    }
    if(el("routes").checked)for(const route of geometry.arrival_approach_routes||[]){
      ctx.beginPath();route.points_m.forEach((p,i)=>{const [x,y]=point(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.lineWidth=2;ctx.setLineDash([7,4]);ctx.strokeStyle="#2878a5";ctx.stroke();ctx.setLineDash([]);
      const [x,y]=point(route.points_m[0]);ctx.fillStyle="#2878a5";ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();
      const [ex,ey]=point(route.points_m[route.points_m.length-1]);ctx.fillStyle="#205d70";ctx.fillText(`${route.route_id} → ${route.destination_zone_id}`,ex+5,ey-5);
    }
    const people = new Map((frame.people||[]).map(p => [p.id,p]));
    const physical=agents.filter(a=>a.point&&a.position_kind==="physical"),positions=new Map(physical.map(a=>[a.agent_id,a.point])),needle=el("search").value.trim().toLowerCase();
    const activeGroup=el("household").value||(view.selected?.kind==="group"?view.selected.id:householdId(people.get(view.selected?.id)));
    const visible=physical.filter(a=>a.agent_id.toLowerCase().includes(needle));
    const priorityIds=new Set(visible.filter(a=>{const person=people.get(a.agent_id);return(view.selected?.kind==="person"&&view.selected.id===a.agent_id)||(activeGroup&&householdId(person)===activeGroup);}).map(a=>a.agent_id));
    const edgeView=limitRelated?limitRelated(frame.social?.edges||[],zoom,edge=>(edge.pair||[]).some(id=>priorityIds.has(id))):{total:frame.social?.edges?.length||0,items:frame.social?.edges||[]};
    if (layer === "social") for (const edge of edgeView.items) {
      if (!edge.pair||!positions.has(edge.pair[0])||!positions.has(edge.pair[1])) continue;
      const [x1,y1]=point(positions.get(edge.pair[0])),[x2,y2]=point(positions.get(edge.pair[1]));ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.strokeStyle=edge.trust>0.5?"#3e9b7790":"#9e778090";ctx.lineWidth=1;ctx.stroke();if(zoom>=1.25){const angle=Math.atan2(y2-y1,x2-x1),arrow=5;ctx.beginPath();ctx.moveTo(x2,y2);ctx.lineTo(x2-arrow*Math.cos(angle-.45),y2-arrow*Math.sin(angle-.45));ctx.lineTo(x2-arrow*Math.cos(angle+.45),y2-arrow*Math.sin(angle+.45));ctx.closePath();ctx.fillStyle="#38534a";ctx.fill();}
    }
    const pointGroups=window.ShelterSimMapLOD?.pointGroups;
    const personRows=visible.map(agent=>({agent,person:people.get(agent.agent_id)||{id:agent.agent_id}}));
    const peopleView=pointGroups?pointGroups(personRows,row=>row.agent.point,point,{width:canvas.width,height:canvas.height,zoom,maxDirect:180,groupKey:row=>row.agent.zone_id,forceIndividual:row=>priorityIds.has(row.agent.agent_id)}):{total:personRows.length,clustered:0,groups:personRows.map(row=>({kind:"item",item:row,x:point(row.agent.point)[0],y:point(row.agent.point)[1]}))};
    for(const group of peopleView.groups){
      if(group.kind==="cluster"){
        const values=group.items.map(row=>Number(layer==="health"?Math.max(row.person.needs?.fatigue||0,row.person.needs?.hunger||0,row.person.needs?.hydration_need||0):row.person.stress||0));
        const average=values.reduce((sum,value)=>sum+value,0)/Math.max(1,values.length);
        const fill=layer==="stress"||layer==="health"?"hsl("+String(120*(1-Math.max(0,Math.min(1,average))))+",60%,43%)":"#427b69";
        drawCluster(ctx,group.x,group.y,group.count,fill);hits.push({x:group.x,y:group.y,kind:"cluster",ids:group.items.map(row=>row.agent.agent_id)});continue;
      }
      const row=group.item,p=row.person,personId=row.agent.agent_id,x=group.x,y=group.y;
      ctx.fillStyle=color(p,layer);ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();
      if(activeGroup&&householdId(p)===activeGroup){ctx.strokeStyle="#142e76";ctx.lineWidth=3;ctx.beginPath();ctx.arc(x,y,7,0,Math.PI*2);ctx.stroke();}
      if(view.selected?.kind==="person"&&view.selected.id===personId){ctx.strokeStyle="#111";ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,9,0,Math.PI*2);ctx.stroke();}
      hits.push({x,y,kind:"person",id:personId});
    }
    const facilitiesView=pointGroups?pointGroups(facilities,facility=>facility.point,point,{width:canvas.width,height:canvas.height,zoom,maxDirect:40,forceIndividual:facility=>view.selected?.kind==="facility"&&view.selected.id===facility.id}):{total:facilities.length,clustered:0,groups:facilities.filter(f=>f.point).map(item=>({kind:"item",item,x:point(item.point)[0],y:point(item.point)[1]}))};
    if(el("facilities").checked)for(const group of facilitiesView.groups){
      if(group.kind==="cluster"){drawCluster(ctx,group.x,group.y,group.count,"#315e87");hits.push({x:group.x,y:group.y,kind:"cluster",ids:group.items.map(item=>item.id)});continue;}
      const f=group.item,x=group.x,y=group.y;ctx.fillStyle=f.working_units?"#294e76":"#a53838";ctx.fillRect(x-6,y-6,12,12);
      if(layer==="operations"&&(facilitiesView.total<=24||zoom>=2)){ctx.fillStyle="#294e76";ctx.fillText(f.id+": 稼働 "+f.working_units+"/"+f.capacity+" 待ち "+((f.queue_agent_ids||[]).length+(f.resource_queue_agent_ids||[]).length),x+8,y);}
      hits.push({x,y,kind:"facility",id:f.id});
      if(el("facilities").checked&&(layer==="operations"||zoom>=1.75))for(const queue of f.service_queues||[]){
        if(!queue.point)continue;const [qx,qy]=point(queue.point),queued=(queue.queue_agent_ids||[]).length,active=(queue.active_agent_ids||[]).length;
        ctx.fillStyle=queue.last_stall?"#b34835":queued?"#d18a26":"#4b8d73";ctx.beginPath();ctx.arc(qx,qy,5,0,Math.PI*2);ctx.fill();ctx.strokeStyle="#fff";ctx.lineWidth=1.5;ctx.stroke();
        if(layer==="operations"||zoom>=2.5){ctx.fillStyle="#183d32";ctx.fillText(`${queue.queue_id}: 待ち${queued} 稼働${active}/${queue.service_capacity_concurrent}`,qx+7,qy-5);}
        hits.push({x:qx,y:qy,kind:"facility",id:f.id});
      }
    }
    const visibleStaff=staffRows.filter(s=>s.position?.point);
    const staffView=pointGroups?pointGroups(visibleStaff,s=>s.position.point,point,{width:canvas.width,height:canvas.height,zoom,maxDirect:36,forceIndividual:row=>view.selected?.kind==="staff"&&view.selected.id===row.id}):{total:visibleStaff.length,clustered:0,groups:visibleStaff.map(item=>({kind:"item",item,x:point(item.position.point)[0],y:point(item.position.point)[1]}))};
    if(el("staff").checked)for(const group of staffView.groups){
      if(group.kind==="cluster"){drawCluster(ctx,group.x,group.y,group.count,"#825ca8");hits.push({x:group.x,y:group.y,kind:"cluster",ids:group.items.map(item=>item.id)});continue;}
      const staff=group.item,x=group.x,y=group.y;ctx.fillStyle=staff.dynamics?.fatigue>.7?"#b75f42":"#825ca8";ctx.beginPath();ctx.moveTo(x,y-7);ctx.lineTo(x-6,y+5);ctx.lineTo(x+6,y+5);ctx.closePath();ctx.fill();
      if(layer==="operations"&&(staffView.total<=24||zoom>=2))ctx.fillText(staff.id+": "+(staff.dynamics?.last_activity||"未記録"),x+8,y);
      if(view.selected?.kind==="staff"&&view.selected.id===staff.id){ctx.strokeStyle="#111";ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,10,0,Math.PI*2);ctx.stroke();}
      hits.push({x,y,kind:"staff",id:staff.id});
    }
    for(const arrival of frame.incoming_arrivals||[]){
      const [x,y]=point(arrival.position);ctx.fillStyle=arrival.status==="approaching"?"#2878a5":"#cf7a31";ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();ctx.strokeStyle="#fff";ctx.lineWidth=1.5;ctx.stroke();
      if((frame.incoming_arrivals||[]).length<=24||zoom>=2)ctx.fillText(`${arrival.agent_id} · ${arrival.status==="approaching"?Math.round(arrival.progress*100)+"%":"入口待機"}`,x+6,y-5);
    }
    for(const event of frame.events||[]){if(typeof event.minute==="number"&&event.minute>=frame.minute)continue;const p=event.point||event.payload?.point;if(!p||typeof p.x!=="number"||typeof p.y!=="number")continue;const [x,y]=point(p);ctx.fillStyle="#b3261e";ctx.beginPath();ctx.moveTo(x,y-8);ctx.lineTo(x-6,y+5);ctx.lineTo(x+6,y+5);ctx.closePath();ctx.fill();if((frame.events||[]).length<50||zoom>=2){ctx.fillStyle="#7e211c";ctx.fillText(`${event.event_type||"Event"} · ${event.minute}`,x+8,y);}hits.push({x,y,kind:"event",id:event.event_id});}
    if(frame===view.frame)view.lodSummary={people:peopleView,facilities:facilitiesView,staff:staffView,edges:edgeView,routes:routeView};
  }
  function updatePeopleChoices(){
    if(!view.frame)return;
    const groups=[...new Set(view.frame.people.map(p=>householdId(p)).filter(Boolean))].sort();
    const priorGroup=el("household").value,priorPerson=el("chat-person").value;
    options(el("household"),[{value:"",label:"世帯を選択"},...groups.map(id=>({value:id,label:`世帯 ${id} · ${view.frame.people.filter(p=>householdId(p)===id).length}人`}))],groups.includes(priorGroup)?priorGroup:"");
    options(el("chat-person"),[{value:"",label:"全体"},...view.frame.people.map(p=>({value:p.id,label:`${p.id} · ${householdId(p)||"世帯未記録"}`}))],view.frame.people.some(p=>p.id===priorPerson)?priorPerson:"");
  }
  function updateLegend(){
    if(!view.frame)return;
    const layer=el("layer").value, messages={
      normal:"通常 / 行動: 青は記録上の移動中、橙は待機中、緑はその他。物理座標がある人物だけを点で表示します。",
      stress:"ストレス: モデルのstress値 0–1。緑 (低) から赤 (高)。実測の健康診断ではありません。",
      crowd:"混雑 / 実動線: ゾーン色と数値は現在Zoneにいる記録人数 ÷ 利用可能面積 (人/m²)。線幅は記録済みの累積edge agent-minutesです。行列の位置は描きません。",
      health:"健康 / 生活: fatigue・hunger・hydration_need の最大モデル値 0–1。緑 (低) から赤 (高)。医療評価ではありません。",
      social:`社会 / 世帯・接触: 色はこの時刻のhousehold_id、矢印は記録された有向social edge。家族関係の種類や接触会話はこの記録にありません。edges ${view.frame.social.edges.length}/${view.frame.social.total_edges}${view.frame.social.truncated?" · 一部省略":""}。`,
      operations:"運営 / 設備: 四角は設備、三角はスタッフ。設備の稼働数・待ちID・スタッフの現在Zone/taskのみ。待ち位置・スタッフ移動経路は未記録です."
    };
    const lod=view.lodSummary,detail=lod?` LOD ${view.zoom.toFixed(1)}×: 人物 ${lod.people.total}件中 ${lod.people.clustered}件を集約 · 設備 ${lod.facilities.clustered}件 · スタッフ ${lod.staff.clustered}件 · 経路描画 ${lod.routes.visible}/${lod.routes.total} · social edge ${lod.edges.items.length}/${lod.edges.total}。集約点をクリックすると拡大し、検索/一覧から個別記録を開けます。`:"";
    el("legend").textContent=`${messages[layer]}${detail} 記録再生は計算済み境界minuteを参照します。補間で未記録の状態や移動を作りません。`;
  }
  function renderEntityList(){
    if(!view.frame)return;
    const box=el("entity-list"),f=view.frame,needle=el("search").value.trim().toLowerCase(),active=document.activeElement,restoreFocus=box.contains(active)?{kind:active.dataset.kind,id:active.dataset.id}:null;box.replaceChildren();
    const entries=[...f.people.map(p=>({kind:"person",id:p.id,label:`人物 ${p.id} · ${householdId(p)||"世帯未記録"}`})),...f.staff.map(s=>({kind:"staff",id:s.id,label:`スタッフ ${s.id} · ${s.active_task_id||"担当未記録"}`})),...f.facilities.map(item=>({kind:"facility",id:item.id,label:`設備 ${item.id} · ${item.working_units}/${item.capacity} 稼働`}))].filter(item=>`${item.id} ${item.label}`.toLowerCase().includes(needle));
    const visible=entries.slice(0,300);
    for(const item of visible){const button=document.createElement("button");button.type="button";button.textContent=item.label;button.dataset.kind=item.kind;button.dataset.id=item.id;button.setAttribute("aria-pressed",String(view.selected?.kind===item.kind&&view.selected?.id===item.id));button.addEventListener("click",()=>setSelected(item));box.append(button);}
    if(entries.length>visible.length)appendText(box,"p",`${visible.length}件を表示中。ID検索で絞り込めます。`);
    if(!entries.length)appendText(box,"p","一致する記録済み対象はありません。");
    if(restoreFocus){const button=[...box.querySelectorAll("button")].find(item=>item.dataset.kind===restoreFocus.kind&&item.dataset.id===restoreFocus.id);button?.focus();}
  }
  function renderEvents(){
    const box=el("events");box.replaceChildren();
    const events=(view.frame?.events||[]).filter(item=>typeof item.minute!=="number"||item.minute<view.frame.minute);
    for(const event of [...events].reverse()){
      const button=document.createElement("button");button.type="button";button.textContent=`minute ${event.minute} · ${event.event_type} · ${event.status} · ${event.event_id}`;button.addEventListener("click",()=>setSelected({kind:"event",id:event.event_id}));box.append(button);
    }
    if(!events.length)appendText(box,"p","この時刻より前のEvent記録はありません。");
  }
  function drawMeasured(){if(!view.frame)return;render(el("map"),view.frame,[]);for(const item of view.comparison||[])render(item.canvas,item.frame);renderComparisonChart();}
  function draw(){if(!view.frame)return;view.hits=[];if(el("measure-render").checked&&frameMeter){const scope=`${view.run}/${view.branch}`;if(frameMeterScope!==scope){frameMeter.reset();frameMeterScope=scope;}}render(el("map"),view.frame,view.hits);for(const item of view.comparison || [])render(item.canvas,item.frame);renderComparisonChart();updateLegend();publishView();}
  function detailSection(box,title,value){const section=document.createElement("section");appendText(section,"h4",title);const pre=appendText(section,"pre",json(value));pre.className="workspace-detail";box.append(section);}
  function showDetail(){
    const box=el("detail");box.replaceChildren();if(!view.frame||!view.selected){appendText(box,"p","地図または対象一覧から選択してください。");return;}
    const f=view.frame,s=view.selected;
    if(s.kind==="event"){const event=f.events.find(item=>item.event_id===s.id);detailSection(box,"記録されたEvent",event||{status:"このminuteのEvent一覧にありません",event_id:s.id});return;}
    if(s.kind==="group"){
      const members=f.people.filter(p=>householdId(p)===s.id);appendText(box,"h3",`世帯 ${s.id} · ${members.length}人`);detailSection(box,"この時刻の同じ世帯の人物",members.map(p=>({id:p.id,zone_id:p.zone_id,activity:p.activity,stress:p.stress,dialogue_status:p.dialogue_status})));
      const ids=new Set(members.map(p=>p.id));detailSection(box,"世帯内で記録された有向social edge",f.social.edges.filter(e=>ids.has(e.pair[0])&&ids.has(e.pair[1])));appendText(box,"p","household_idはこの時刻の世帯記録です（旧データはgroup_idへフォールバック）。介護者・被支援者リンクは人物のcontextを参照してください。親子関係を介護者リンクから推定しません。");return;
    }
    if(s.kind==="person"){
      const person=f.people.find(p=>p.id===s.id);if(!person){appendText(box,"p","このminuteには在所していないか、人物記録がありません。");return;}
      const position=f.agents.find(a=>a.agent_id===s.id);appendText(box,"h3",`人物 ${person.id} · ${householdId(person)||"世帯未記録"}`);detailSection(box,"このminuteの記録状態",{person,position});
      detailSection(box,"現在の支援依頼",person.care_requests||[]);
      appendText(box,"p","waited_minutesは支援開始までの経過待ち時間です。wait_reason_codesは同時点で観測した要因で、単一の因果説明ではありません。route_capacity_fullは経路の移動枠不足、staff_destination_fullは移動先zoneの定員、insufficient_run_timeは残り時間内に支援を完了できない状態を示します。resident_travel_schedule_rejectedは住民の移動予約が成立しなかった記録、route_unavailable_at_assignmentは割当時に経路を解決できなかった記録です。assignment_not_attempted_at_sampled_minuteは表示時点で当該分の割当処理前、assignment_failure_reason_not_recordedは旧Checkpointなどに失敗理由が保存されていない状態です。");
      detailSection(box,"適用済み支援の履歴",person.care_history||[]);
      appendText(box,"p","履歴は適用済みの支援遷移を示します。医療判断・服薬実施・健康改善は、入力データと測定値が記録されない限り推定しません。");
      const links=person.context||{},relationBox=appendText(box,"section","");appendText(relationBox,"h4","記録された家族・支援関係");
      detailSection(relationBox,"合成contextと出典",{household_id:links.household_id,roles:links.roles,guardian_status:links.guardian_status,care_support_needs:links.care_support_needs,requires_review:links.requires_review,review_reasons:links.review_reasons,source_ids:links.source_ids});
      if(links.requires_review)appendText(relationBox,"p",`生成された関係は要確認です: ${(links.review_reasons||[]).join("、")||"確認理由の詳細なし"}。実在の家族・介護関係を示すものではありません。`);
      for(const [label,ids] of [["親",links.parent_ids||[]],["介護者",links.caregiver_ids||[]],["支援対象",links.care_for_ids||[]]])for(const id of ids){const button=document.createElement("button");button.type="button";button.textContent=`${label}: ${id}`;button.disabled=!f.people.some(item=>item.id===id);button.title=button.disabled?"このminuteの人物一覧にはいません":"関係先の記録を開きます";button.addEventListener("click",()=>setSelected({kind:"person",id}));relationBox.append(button);}
      if(!(links.parent_ids?.length||links.caregiver_ids?.length||links.care_for_ids?.length))appendText(relationBox,"p","親・介護関係は記録されていません。未記録から関係を推定しません。");
      detailSection(box,"関係する有向social edge",f.social.edges.filter(e=>e.pair.includes(s.id)));
      detailSection(box,"記録された判断・belief",{decision:person.decision,beliefs:person.beliefs});
      appendText(box,"p",person.dialogue_status==="recorded"&&person.dialogue?`発言: ${person.dialogue} (保存記録)`:"会話: 未記録。発言内容を生成・推定していません。");return;
    }
    if(s.kind==="staff"){
      const staff=f.staff.find(item=>item.id===s.id);if(!staff){appendText(box,"p","このminuteのスタッフ記録がありません。");return;}
      appendText(box,"h3",`スタッフ ${staff.id}`);detailSection(box,"現在の担当・勤務状態",staff);appendText(box,"p","operationは現在の作業・移動記録です。保存履歴のスタッフ作業を選ぶと、上限内の完了・中断理由を取得できます。室内経路や通路容量の扱いには近似があります。");return;
    }
    const facility=f.facilities.find(item=>item.id===s.id);if(!facility){appendText(box,"p","このminuteの設備記録がありません。");return;}
    appendText(box,"h3",`設備 ${facility.id} · ${facility.kind}`);detailSection(box,"稼働・待機記録",facility);appendText(box,"p","service_queuesはqueue_id別のFIFO、待機・稼働人数、アクセス条件、停止理由です。表示位置はScenarioに保存されたservice point座標です。人物の列位置は経路モデルによる推定です。");
  }
  async function navigateCitation(reference){
    if(!reference)return status("引用先がありません。");
    if(reference.run_id!==view.run){if(window.ShelterSimNavigateTarget&&reference.branch_id)return window.ShelterSimNavigateTarget({run_id:reference.run_id,branch_id:reference.branch_id,minute:reference.minute,...(reference.kind==="agent"||reference.kind==="person"?{person_id:reference.reference_id}:reference.kind==="household"?{household_id:reference.reference_id}:reference.kind==="event"?{event_id:reference.reference_id}:{})});return status("引用先の別Runを開くにはProject/Run/Branch IDが必要です。");}
    const target=view.runData?.branches.find(item=>item.branch_id===reference.branch_id);if(!target)return status("引用Branchが保存Runにありません。");
    view.branch=reference.branch_id;view.minute=Math.max(target.fork_minute,Math.min(target.computed_until,reference.minute));await openRun();
    const current=selectedReference();if(current&&reference.minute>=current.fork_minute&&reference.minute<=current.computed_until)await seek(reference.minute);
    if(reference.kind==="agent"||reference.kind==="person")setSelected({kind:"person",id:reference.reference_id});else if(reference.kind==="facility")setSelected({kind:"facility",id:reference.reference_id});else if(reference.kind==="staff")setSelected({kind:"staff",id:reference.reference_id});else if(reference.kind==="household"){if(view.frame.people.some(item=>householdId(item)===reference.reference_id))setSelected({kind:"group",id:reference.reference_id});else status("このminuteのFrameに引用世帯の在籍記録がありません。履歴範囲またはBranchの保持範囲を確認してください。");}
    else if(reference.kind==="event"){
      const values=new URLSearchParams({kind:"events",start:String(reference.minute),end:String(reference.minute),step:"1",limit:"20",event_id:String(reference.reference_id)});
      const page=await request(`${branchRoot()}/history?${values}`),box=el("detail");box.replaceChildren();appendText(box,"h3",`Q&A引用 · 保存Event ${reference.reference_id} · minute ${reference.minute}`);detailSection(box,"Branch履歴APIから取得した記録",page.items||[]);appendText(box,"p",`coverage ${page.coverage||"不明"} · ${page.next_cursor?"続きの記録があります。":"取得範囲を表示しています。"}`,"muted");
    }else if(["kpi","comparison"].includes(reference.kind)){
      const values=new URLSearchParams({kind:"kpis",start:String(Math.max(target.fork_minute,reference.minute)),end:String(Math.min(target.computed_until,reference.minute)),step:"1",limit:"20"});
      const page=await request(`${branchRoot()}/history?${values}`),box=el("detail");box.replaceChildren();appendText(box,"h3",`Q&A引用 · 保存KPI · ${reference.branch_id}@${reference.minute}`);detailSection(box,"end_of_tick 記録（選択状態はbefore_tick）",page.items||[]);appendText(box,"p",`coverage ${page.coverage||"不明"} · ${page.count_status||"count status不明"}`,"muted");
    }else if(reference.kind==="model"||reference.kind==="source"){
      const evidence=await request(`${runRoot()}/evidence`),box=el("detail");box.replaceChildren();appendText(box,"h3",reference.kind==="model"?"Q&A引用 · 選択モデルと仮定":"Q&A引用 · Scenario出典");detailSection(box,reference.kind==="model"?"登録モデル・係数・不確実性":`Source ${reference.reference_id||""}`,reference.kind==="model"?{models:evidence.models,model_versions:evidence.model_versions,parameters:evidence.parameters,interpretation:evidence.interpretation}:(evidence.scenario_sources||[]).filter(item=>item.id===reference.reference_id));appendText(box,"p",`Branch ${reference.branch_id} · minute ${reference.minute} · ${reference.status||"保存された根拠"}`,"muted");
    }
  }
  function renderCitationButtons(box,references){
    const row=document.createElement("div");row.className="workspace-citations";
    for(const source of references||[]){const reference=source.reference||source;const button=document.createElement("button");button.type="button";button.className="workspace-citation";button.textContent=`${source.alias?`${source.alias} · `:""}${source.label||reference.kind||"根拠"} · ${reference.branch_id||"?"}@${reference.minute??"?"}${reference.reference_id?` · ${reference.reference_id}`:""}${source.claimEvidence?` · ${source.claimEvidence}`:""}`;if(source.claimEvidence)button.title=source.claimEvidence;button.addEventListener("click",()=>navigateCitation(reference).catch(error=>status(error.message)));row.append(button);}
    if(row.childNodes.length)box.append(row);
  }
  async function loadConversationHistory(){
    const data=await request(`${branchRoot()}/conversations`),box=el("chat");box.replaceChildren();
    for(const turn of data.turns){const article=document.createElement("article");article.className="workspace-chat-turn";appendText(article,"p",`minute ${turn.minute??"?"} · ${turn.usage?.stage||"REVIEW"} · ${turn.model_version||"model unavailable"} · ${turn.usage?.estimated_cost_usd===undefined?"費用未記録":`$${Number(turn.usage.estimated_cost_usd).toFixed(4)}`}`);appendText(article,"strong",`質問: ${turn.question}`);appendText(article,"p",turn.answer);renderCitationButtons(article,turn.evidence||[]);box.append(article);}
    if(!data.turns.length)appendText(box,"p","このBranchに保存された会話はありません。");
  }
  function renderComparisonChart(){
    const canvas=el("kpi-chart"),ctx=canvas.getContext("2d");ctx.clearRect(0,0,canvas.width,canvas.height);
    const frames=view.comparisonFrames||[];if(!frames.length){ctx.fillStyle="#53655e";ctx.fillText("Branchを選択して、同期したKPIを比較してください。",18,32);return;}
    const metrics=[["stress_mean","平均ストレス"],["toilet_queue_length","トイレ待ち人数"],["staff_load_ratio","スタッフ負荷"]],colors=["#267baf","#c66d3d","#688d48","#8a65a7","#ba9d2c","#4e9b9a","#c25778","#6d7484"];
    metrics.forEach(([key,label],row)=>{const y=32+row*82;ctx.fillStyle="#203b33";ctx.fillText(label,12,y);const values=frames.map(frame=>typeof frame.kpis?.[key]==="number"&&Number.isFinite(frame.kpis[key])?frame.kpis[key]:null),max=Math.max(1,...values.filter(value=>value!==null));frames.forEach((frame,index)=>{const barX=150+index*100,barW=70,value=values[index];ctx.fillStyle=colors[index%colors.length];if(value!==null)ctx.fillRect(barX,y-17,barW,24*(value/max));ctx.fillStyle="#203b33";ctx.fillText(`${frame.branch_id}: ${value===null?"未記録":value.toFixed(2)}`,barX,y+18);});});
    ctx.fillStyle="#53655e";ctx.fillText(`minute ${frames[0].minute} · 現在の同期点`,12,285);
  }
  async function loadComparison(ids,minute,serial=view.serial){
    const comparisonRequest=++view.compareSerial;
    const data=await request(`${runRoot()}/compare?branch_ids=${encode(ids.join(","))}&minute=${minute}`);
    if(serial!==view.serial||comparisonRequest!==view.compareSerial)return;
    view.comparisonRange=data.common_range;view.comparisonFrames=data.frames;view.compareIds=ids;
    el("seek").min=el("minute").min=data.common_range[0];el("seek").max=el("minute").max=data.common_range[1];
    el("range").textContent=`同期計算済み ${data.common_range.join("–")}分 · minute ${data.minute} · primary ${view.branch} · 共通起点 ${data.shared_origin}`;
    const statusBox=el("compare-status");statusBox.replaceChildren();appendText(statusBox,"p",`基準Branch ${view.branch} · 同期minute ${data.minute} · 共通範囲 ${data.common_range.join("–")} · 同一起点 ${data.shared_origin}`);appendText(statusBox,"p",data.interpretation||"比較の解釈はありません。");detailSection(statusBox,"介入差・KPI差",data.differences);detailSection(statusBox,"分岐・Checkpoint根拠",data.origin_receipts||{status:"lineage receipt is not available"});
    el("comparison").replaceChildren();view.comparison=[];
    for(const frame of data.frames){if(frame.branch_id===view.branch)continue;const section=document.createElement("section"),title=document.createElement("h4"),canvas=document.createElement("canvas");title.textContent=`${frame.branch_id} · minute ${frame.minute}`;canvas.width=el("map").width;canvas.height=el("map").height;section.append(title,canvas);el("comparison").append(section);view.comparison.push({canvas,frame});render(canvas,frame);}
    renderComparisonChart();
  }
  function readScenarioRequest(){
    const advanced=el("request").value.trim();let request=advanced?JSON.parse(advanced):{};
    const explicit={municipality:el("municipality").value.trim(),disaster:el("disaster").value.trim(),evacuees:el("evacuees").value,duration_days:el("duration").value,gross_area_m2:el("area-m2").value,accessible_toilet_count:el("toilets").value,staff_count:el("staff-count").value,target_date:el("target-date").value,weather_area_code:el("weather-area-code").value.trim(),behavior_policy_id:el("behavior-policy").value,social_policy_id:el("social-policy").value,information_policy_id:el("information-policy").value,queue_policy_id:el("queue-policy").value,relationship_policy_id:el("relationship-policy").value,social_proximity_policy_id:el("social-proximity-policy").value,body_dynamics_policy_id:el("body-dynamics-policy").value,care_outcome_policy_id:el("care-outcome-policy").value,staff_operations_policy_id:el("staff-operations-policy").value,staff_fatigue_policy_id:el("staff-fatigue-policy").value,zone_temperature_policy_id:el("zone-temperature-policy").value,zone_rest_policy_id:el("zone-rest-policy").value,zone_hygiene_policy_id:el("zone-hygiene-policy").value,zone_privacy_policy_id:el("zone-privacy-policy").value};
    const fieldIds={municipality:"municipality",disaster:"disaster",evacuees:"evacuees",duration_days:"duration",gross_area_m2:"area-m2",accessible_toilet_count:"toilets",staff_count:"staff-count",target_date:"target-date",weather_area_code:"weather-area-code",behavior_policy_id:"behavior-policy",social_policy_id:"social-policy",information_policy_id:"information-policy",queue_policy_id:"queue-policy",relationship_policy_id:"relationship-policy",social_proximity_policy_id:"social-proximity-policy",body_dynamics_policy_id:"body-dynamics-policy",care_outcome_policy_id:"care-outcome-policy",staff_operations_policy_id:"staff-operations-policy",staff_fatigue_policy_id:"staff-fatigue-policy",zone_temperature_policy_id:"zone-temperature-policy",zone_rest_policy_id:"zone-rest-policy",zone_hygiene_policy_id:"zone-hygiene-policy",zone_privacy_policy_id:"zone-privacy-policy"};
    for(const [key,value] of Object.entries(explicit)){if(value!==""){request[key]=["evacuees","duration_days","gross_area_m2","accessible_toilet_count","staff_count"].includes(key)?Number(value):value;const policyDefault=["behavior_policy_id","social_policy_id","information_policy_id","queue_policy_id","relationship_policy_id","social_proximity_policy_id","body_dynamics_policy_id","care_outcome_policy_id","staff_operations_policy_id","staff_fatigue_policy_id","zone_temperature_policy_id","zone_rest_policy_id","zone_hygiene_policy_id","zone_privacy_policy_id"].includes(key)&&!view.editedFields.has(fieldIds[key]);if(!view.previewGeneratedFields.has(fieldIds[key])&&!policyDefault){request.field_origins=request.field_origins||{};request.field_origins[key]="user";}}else if(view.editedFields.has(fieldIds[key]))delete request[key];}
    for(const [id,key] of [["social-propagation-enabled","social_propagation_enabled"],["information-spread-enabled","information_spread_enabled"]]){request[key]=el(id).checked;if(view.editedFields.has(id)){request.field_origins=request.field_origins||{};request.field_origins[key]="user";}}
    const overridesText=el("policy-parameter-overrides").value.trim();
    if(overridesText){
      const overrides=JSON.parse(overridesText),validNumber=value=>typeof value==="number"&&Number.isFinite(value),validEntityId=id=>/^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(id),shiftPath="staff_dynamics.shift_windows_by_staff.*";
      const validEntityMap=value=>value&&typeof value==="object"&&!Array.isArray(value)&&Object.keys(value).length<=100&&Object.entries(value).every(([id,entry])=>validEntityId(id)&&(validNumber(entry)||entry&&typeof entry==="object"&&!Array.isArray(entry)&&Object.keys(entry).length<=4&&Object.entries(entry).every(([field,number])=>/^[a-z][a-z0-9_]{0,63}$/.test(field)&&validNumber(number))));
      const validShiftMap=value=>value&&typeof value==="object"&&!Array.isArray(value)&&Object.keys(value).length>0&&Object.keys(value).length<=100&&Object.entries(value).every(([id,rows])=>validEntityId(id)&&Array.isArray(rows)&&rows.length<=21&&rows.every(row=>row&&typeof row==="object"&&!Array.isArray(row)&&Object.keys(row).length===2&&Number.isInteger(row.start_minute)&&Number.isInteger(row.end_minute)&&row.start_minute>=0&&row.start_minute<row.end_minute&&row.end_minute<=10080)&&(()=>{const sorted=[...rows].sort((a,b)=>a.start_minute-b.start_minute||a.end_minute-b.end_minute);return sorted.every((row,index)=>index===0||sorted[index-1].end_minute<=row.start_minute)})());
      const encoded=JSON.stringify(overrides),validValue=([path,value])=>path===shiftPath?validShiftMap(value):validNumber(value)||validEntityMap(value);
      if(!overrides||typeof overrides!=="object"||Array.isArray(overrides)||Object.keys(overrides).length>64||!Object.entries(overrides).every(validValue)||encoded.length>16384)throw new Error("係数上書きJSONは、最大64項目・16 KiBまでの有限数値、ID別数値表、ID別ゾーン係数表、またはstaff_dynamics.shift_windows_by_staff.*の週内区間で入力してください。");
      request.model_parameter_overrides=overrides;request.field_origins={...(request.field_origins||{}),model_parameter_overrides:"user"};
    }
    else if(view.editedFields.has("policy-parameter-overrides")){delete request.model_parameter_overrides;if(request.field_origins)delete request.field_origins.model_parameter_overrides;}
    const population={};
    for(const [id,key] of [["medical-rate","medical_support_rate"],["medication-rate","medication_support_rate"],["pet-rate","pet_household_rate"]])if(el(id).value!=="")population[key]=Number(el(id).value);
    if(el("care-support-rates").value.trim())population.care_support_need_rates=JSON.parse(el("care-support-rates").value);
    else if(view.editedFields.has("population-conditions")&&request.population_conditions)delete request.population_conditions.care_support_need_rates;
    for(const [id,key] of [["access-rates","additional_access_need_rates"],["language-weights","language_weights"],["information-weights","information_access_weights"],["daily-rhythms","daily_rhythms"]])if(el(id).value.trim())population[key]=JSON.parse(el(id).value);
    if(el("household-composition").value.trim()){
      const composition=JSON.parse(el("household-composition").value);
      if(!composition||typeof composition!=="object"||Array.isArray(composition))throw new Error("世帯構成JSONはオブジェクトで入力してください。");
      const weights=composition.household_size_weights;
      if(weights&&(typeof weights!=="object"||Array.isArray(weights)||!Object.keys(weights).length||Object.entries(weights).some(([size,weight])=>!/^\d+$/.test(size)||Number(size)<1||Number(size)>10||typeof weight!=="number"||!Number.isFinite(weight)||weight<0)||Object.values(weights).reduce((a,b)=>a+b,0)<=0))throw new Error("世帯人数は1〜10、重みは有限の非負値で合計が正となるよう入力してください。");
      population.household_composition=composition;
    }else if(view.editedFields.has("household-composition")&&request.population_conditions)delete request.population_conditions.household_composition;
    if(Object.keys(population).length){request.population_conditions={...(request.population_conditions||{}),...population};request.field_origins=request.field_origins||{};request.field_origins.population_conditions="user";}
    const operations={...(request.operational_conditions||{})};
    const staffShiftsTouched=view.editedFields.has("staff-positive-shifts")||Boolean(el("staff-positive-shifts").value.trim());
    if(staffShiftsTouched){
      if(el("staff-positive-shifts").value.trim()){
        const shifts=JSON.parse(el("staff-positive-shifts").value);
        if(!Array.isArray(shifts)||shifts.some(shift=>!shift||typeof shift!=="object"||Array.isArray(shift)||typeof shift.staff_id!=="string"||!shift.staff_id.trim()||!Number.isInteger(shift.start_minute)||!Number.isInteger(shift.end_minute)||shift.start_minute<0||shift.end_minute>10080||shift.start_minute>=shift.end_minute))throw new Error("勤務予定はstaff_idと週内の整数分[start_minute,end_minute)を持つ配列で、0〜10080の範囲にしてください。");
        const counts=new Map();for(const shift of shifts){counts.set(shift.staff_id,(counts.get(shift.staff_id)||0)+1);if(counts.get(shift.staff_id)>21)throw new Error("1人につき週21勤務区間まで指定できます。");}
        operations.staff_shifts=shifts;
      }else delete operations.staff_shifts;
    }

    const stockTouched=["stock-status","water-ml","food-units","warehouse-id","stock-zone"].some(id=>view.editedFields.has(id))||el("stock-status").value!=="unknown"||["water-ml","food-units","warehouse-id","stock-zone"].some(id=>el(id).value.trim());
    if(stockTouched){
      const stock={...(operations.initial_resources||{}),status:el("stock-status").value};
      if(el("water-ml").value!=="")stock.water_ml=Number(el("water-ml").value);else delete stock.water_ml;
      if(el("food-units").value!=="")stock.food_meal_units=Number(el("food-units").value);else delete stock.food_meal_units;
      if(el("warehouse-id").value.trim())stock.warehouse_id=el("warehouse-id").value.trim();else delete stock.warehouse_id;
      if(el("stock-zone").value.trim())stock.zone_id=el("stock-zone").value.trim();else delete stock.zone_id;
      if(stock.status==="unknown"){delete stock.water_ml;delete stock.food_meal_units;}
      operations.initial_resources=stock;
    }
    const equipment=[...el("equipment-rows").querySelectorAll(".equipment-row")].map(row=>{
      const read=field=>row.querySelector(`[data-field="${field}"]`),add=read("add").checked,x=read("x_m").value,y=read("y_m").value;
      return {facility_id:read("facility_id").value.trim(),capacity:Number(read("capacity").value||0),working_units:Number(read("working_units").value||0),source_id:read("source_id").value.trim()||"operator_condition",source_kind:"operator",add,
        ...(add?{facility_kind:read("facility_kind").value.trim()||"other",zone_id:read("zone_id").value.trim()}:{}),
        ...(x!==""&&y!==""?{position_m:{x:Number(x),y:Number(y)}}:{}),...(read("footprint_m2").value!==""?{footprint_m2:Number(read("footprint_m2").value)}:{})};
    }).filter(row=>row.facility_id);
    if(view.editedFields.has("equipment-rows")||equipment.length){if(equipment.length)operations.equipment=equipment;else delete operations.equipment;}
    if(view.editedFields.has("scheduled-conditions")||el("scheduled-conditions").value.trim()){if(el("scheduled-conditions").value.trim())operations.scheduled=JSON.parse(el("scheduled-conditions").value);else delete operations.scheduled;}
    const unavailability=[...el("shift-rows").querySelectorAll(".staff-shift-row")].map(row=>({
      kind:"staff_unavailable",staff_id:row.querySelector('[data-field="staff_id"]').value.trim(),
      start_minute:Number(row.querySelector('[data-field="start_minute"]').value),
      end_minute:Number(row.querySelector('[data-field="end_minute"]').value),
      source_id:"operator_condition",source_kind:"operator",
      ...(row.querySelector('[data-field="repeat_every_minutes"]').value?{repeat_every_minutes:Number(row.querySelector('[data-field="repeat_every_minutes"]').value)}:{})
    })).filter(row=>row.staff_id&&Number.isFinite(row.start_minute)&&Number.isFinite(row.end_minute));
    if(unavailability.length)operations.scheduled=[...(operations.scheduled||[]),...unavailability];
    if(stockTouched||staffShiftsTouched||view.editedFields.has("equipment-rows")||view.editedFields.has("scheduled-conditions")||unavailability.length) {
      request.operational_conditions=operations;request.field_origins=request.field_origins||{};request.field_origins.operational_conditions="user";
    }
    const candidate=el("municipality-candidate").value;if(/^\d{5}$/.test(candidate))request.municipality_code=candidate;
    const conditions=el("facility-conditions").value.split("\n").map(value=>value.trim()).filter(Boolean);if(conditions.length){request.facility_conditions=conditions;if(!view.previewGeneratedFields.has("facility-conditions")){request.field_origins=request.field_origins||{};request.field_origins.facility_conditions="user";}}else if(view.editedFields.has("facility-conditions"))delete request.facility_conditions;
    const missing=["municipality","disaster","evacuees"].filter(key=>!(explicit[key]||request[key]));
    if(missing.length&&!el("text").value.trim())throw new Error(`自然言語入力または必須項目を入力してください: ${missing.join("、")}`);
    return Object.keys(request).length?request:null;
  }
  function markEditorFieldsAsCurrentRequest(requestValue){
    for(const [id,key] of [["municipality","municipality"],["disaster","disaster"],["evacuees","evacuees"],["duration","duration_days"],["area-m2","gross_area_m2"],["toilets","accessible_toilet_count"],["staff-count","staff_count"],["target-date","target_date"],["weather-area-code","weather_area_code"]])if(requestValue[key]!==undefined)view.previewGeneratedFields.add(id);
    for(const [id,key] of [["behavior-policy","behavior_policy_id"],["social-policy","social_policy_id"],["information-policy","information_policy_id"],["queue-policy","queue_policy_id"],["relationship-policy","relationship_policy_id"],["social-proximity-policy","social_proximity_policy_id"]])if(requestValue[key]!==undefined)view.previewGeneratedFields.add(id);
    for(const [id,key] of [["body-dynamics-policy","body_dynamics_policy_id"],["care-outcome-policy","care_outcome_policy_id"],["staff-operations-policy","staff_operations_policy_id"],["staff-fatigue-policy","staff_fatigue_policy_id"],["zone-temperature-policy","zone_temperature_policy_id"],["zone-rest-policy","zone_rest_policy_id"],["zone-hygiene-policy","zone_hygiene_policy_id"],["zone-privacy-policy","zone_privacy_policy_id"]])if(requestValue[key]!==undefined)view.previewGeneratedFields.add(id);
    for(const [id,key] of [["social-propagation-enabled","social_propagation_enabled"],["information-spread-enabled","information_spread_enabled"]])if(requestValue[key]!==undefined)view.previewGeneratedFields.add(id);
    if(requestValue.facility_conditions!==undefined)view.previewGeneratedFields.add("facility-conditions");
  }
  function renderLiveAnswer(answer,question){
    const box=el("chat");const article=document.createElement("article");article.className="workspace-chat-turn";
    appendText(article,"p",`minute ${answer.context?.captured_minute??view.minute} · ${answer.model||"モデル不明"} · ${answer.mode||"状態不明"}`);
    appendText(article,"strong",`質問: ${question}`);
    const sources=answer.sources||[],citationSources=item=>sources.filter(source=>(item.evidence||[]).some(proof=>proof.alias===source.alias)).map(source=>({...source,claimEvidence:(item.evidence||[]).filter(proof=>proof.alias===source.alias).map(proof=>`${proof.field} = ${JSON.stringify(proof.value)}`).join("; ")}));
    for(const item of answer.explanations||[]){const section=document.createElement("section");section.className="workspace-chat-explanation";appendText(section,"h4",item.kind==="synthetic_dialogue"?"架空の合成会話 · 記録された発言ではありません":item.kind==="suggestion"?"検討案 · 効果未確認":"AIの解釈・仮説 · 因果未確認");appendText(section,"p",item.text);renderCitationButtons(section,citationSources(item));article.append(section);}
    if((answer.facts||[]).length){const section=document.createElement("section");appendText(section,"h4","サーバーで照合した記録値");for(const item of answer.facts){const row=document.createElement("div");row.className="workspace-chat-fact";appendText(row,"p",item.text);renderCitationButtons(row,citationSources(item));section.append(row);}article.append(section);}
    if(!(answer.explanations||[]).length&&!(answer.facts||[]).length)appendText(article,"p",answer.answer||"回答なし");
    for(const item of answer.limitations||[])appendText(article,"p",item,"muted");box.prepend(article);
    const usage=answer.usage||{},cost=Number(usage.estimated_cost_usd||0),state=usage.error_kind==="budget_exceeded"?"予算不足":usage.error_kind?`通信/処理エラー ${usage.error_kind}`:answer.availability||"状態不明",remaining=usage.budget_remaining_usd===undefined?"残額未取得":`REVIEW残額 $${Number(usage.budget_remaining_usd).toFixed(4)}`;el("chat-status").textContent=`${state} · stage ${usage.stage||"REVIEW"} · ${answer.mode||"mode不明"} · ${answer.model||"modelなし"} · ${usage.api_request_count??0} calls · 今回 $${cost.toFixed(4)} · ${remaining}${usage.fallback_used?" · fallback":""}`;
  }
  function fillFacilityEditor(draft){
    const design=draft.facility_design;
    view.facilityCandidateUnvalidated=Boolean(view.restoreFacilityCandidate&&design);
    view.facilityBase=design&&draft.preview_id&&draft.geometry_hash?{preview_id:draft.preview_id,hash:draft.geometry_hash}:null;
    el("facility-edit-check").disabled=!view.facilityBase;
    if(view.facilityBase){
      if(!view.restoreFacilityCandidate)el("facility-edit-json").value=json({geometry:design.geometry,editable_geometry:design.editable_geometry});
      view.restoreFacilityCandidate=false;
      el("facility-edit-status").textContent="標準施設の候補です。編集後は検証し、更新されたScenario全体を確認・承認してください。";
    }else el("facility-edit-status").textContent="標準施設のプレビューを作成すると補正できます。アップロード図面は図面エディタを使用してください。";
    let candidate=null;try{candidate=el("facility-edit-json").value.trim()?JSON.parse(el("facility-edit-json").value):null;}catch{}
    window.dispatchEvent(new CustomEvent("sheltersim:facility-candidate",{detail:{base:view.facilityBase,candidate,design:design||null,unvalidated:view.facilityCandidateUnvalidated}}));
  }
  function applyScenarioPreview(draft){
    view.draft=draft;view.previewGeneratedFields.clear();if(view.draft.request){el("request").value=json(view.draft.request);for(const [id,key] of [["municipality","municipality"],["disaster","disaster"],["evacuees","evacuees"],["duration","duration_days"],["area-m2","gross_area_m2"],["toilets","accessible_toilet_count"],["staff-count","staff_count"],["target-date","target_date"],["weather-area-code","weather_area_code"]])if(view.draft.request[key]!==undefined){el(id).value=view.draft.request[key];view.previewGeneratedFields.add(id);}el("facility-conditions").value=(view.draft.request.facility_conditions||[]).join("\n");view.previewGeneratedFields.add("facility-conditions");fillStructuredConditions(view.draft.request);updateCandidateLists(view.draft.research||{},view.draft.request);}renderRegionReview(view.draft);fillFacilityEditor(draft);renderPreview();el("approve").disabled=!view.draft.can_start;el("approve-check").checked=false;status(view.draft.can_start?"出典・仮定・条件を確認して承認してください。":"不足または矛盾を修正して再プレビューしてください。");
    window.ShelterSimApplyScenarioPreview = applyScenarioPreview;
    window.dispatchEvent(new CustomEvent("sheltersim:scenario-preview",{detail:{scenario:view.draft?.scenario||null,request:view.draft?.request||null,facility_design:view.draft?.facility_design||null,preview_id:view.draft?.preview_id||null,approved_hash:view.draft?.approved_hash||null,approval_blockers:view.draft?.approval_blockers||[]}}));
    if(view.facilityCandidateUnvalidated){view.draft=null;el("approve").disabled=true;el("approve-check").checked=false;renderPreview();status("補正草案を復旧しました。今回の基準Geometryと一致するか確認してから再検証してください。未検証の草案は承認できません。");}
  }
  on("facility-edit-json","input",()=>{
    ++view.draftSerial;view.draft=null;el("approve").disabled=true;el("approve-check").checked=false;renderPreview();
    el("facility-edit-status").textContent="補正内容が変わりました。再検証とScenarioの明示承認が必要です。";
  });
  on("facility-edit-check","click",async()=>{
    const base=view.facilityBase;if(!base)throw new Error("先に標準施設のScenarioプレビューを作成してください。");
    const candidate=JSON.parse(el("facility-edit-json").value);
    if(!candidate||typeof candidate!=="object"||Array.isArray(candidate)||!candidate.geometry||!candidate.editable_geometry)throw new Error("geometry と editable_geometry を含むJSONオブジェクトを入力してください。");
    const serial=++view.draftSerial;view.draft=null;el("approve").disabled=true;el("approve-check").checked=false;
    const result=await request(`${root()}/facility-preview`,{base_preview_id:base.preview_id,base_geometry_hash:base.hash,geometry:candidate.geometry,editable_geometry:candidate.editable_geometry,edit_source_id:"SRC-OPERATOR-FACILITY-CORRECTION"});
    if(serial!==view.draftSerial)return;
    if(result.status==="invalid"||!result.scenario){if(window.ShelterSimCloseout?.showFacilityValidation)window.ShelterSimCloseout.showFacilityValidation(result);else el("facility-edit-status").textContent=json({status:result.status,issues:result.issues||[],runtime_verified:false});renderPreview();return;}
    applyScenarioPreview(result);if(window.ShelterSimCloseout?.showFacilityValidation)window.ShelterSimCloseout.showFacilityValidation(result);else el("facility-edit-status").textContent=json({status:result.status,can_start:result.can_start,capacity_supported:result.capacity_supported,requested:result.request?.evacuees,approval_blockers:result.approval_blockers,issues:result.issues,runtime_verified:false});
  });
  on("project-open","click",openProject);
  on("run","change",async()=>{view.run=el("run").value;view.minute=0;el("target").value="";await openRun();});
  on("branch","change",async()=>{view.branch=el("branch").value;view.minute=selectedReference().fork_minute;await openRun();});
  on("preview","click",async()=>{status("条件と出典を整理しています。公開情報を取得できない項目は不明として返します。");const facilityText=el("facility-conditions").value.trim();const scenarioRequest=readScenarioRequest();const inputHints=scenarioRequest?[]:[["自治体",el("municipality").value], ["災害",el("disaster").value], ["避難者数",el("evacuees").value], ["期間日数",el("duration").value], ["延床面積m2",el("area-m2").value], ["トイレ基数",el("toilets").value], ["スタッフ人数",el("staff-count").value]].filter(([,value])=>value!=="").map(([label,value])=>`${label}=${value}`);const body={text:[el("text").value,facilityText?`設備・運営条件: ${facilityText}`:"",inputHints.length?`入力欄で指定された追加条件: ${inputHints.join("、")}`:""].filter(Boolean).join("\n")};if(scenarioRequest)body.request=scenarioRequest;if(el("table").value)body.estat_stats_data_id=el("table").value;if(el("area").value)body.estat_area_code=el("area").value;if(el("period").value)body.estat_period_code=el("period").value;if(el("categories").value.trim())body.estat_category_codes=JSON.parse(el("categories").value);if(el("shelter-candidate").value)body.selected_shelter_id=el("shelter-candidate").value;if(el("geometry-id").value.trim())body.geometry_id=el("geometry-id").value.trim();else if(el("plan-json").value.trim())body.plan_override=JSON.parse(el("plan-json").value);const draftSerial=++view.draftSerial;const draft=await request(`${root()}/scenario-preview`,body);if(draftSerial!==view.draftSerial)return;applyScenarioPreview(draft);});
  on("approve","click",async()=>{if(!view.draft?.can_start||!el("approve-check").checked)throw new Error("プレビューを確認して承認欄を選択してください。");const runId=`run-${crypto.randomUUID()}`;await request(`${root()}/scenario-approve`,{preview_id:view.draft.preview_id,approved_hash:view.draft.approved_hash,approved:true,run_id:runId});view.run=runId;view.branch="main";view.minute=0;el("target").value="";await openProject();el("create").open=false;});
  on("edit-saved","click",()=>{if(!view.runData||!view.projectData)throw new Error("編集する保存Runを選択してください。");const saved=view.projectData.scenarios.find(item=>item.scenario_id===view.runData.scenario_id&&item.scenario_version===view.runData.scenario_version);if(!saved)throw new Error("保存済みScenarioが見つかりません。");const scenario=saved.scenario,requestValue=scenario.provenance?.natural_language_input||scenario.provenance?.initial_scenario_request;if(!requestValue)throw new Error("このScenarioには再編集用の入力記録がありません。詳細条件を確認してください。");el("text").value="";el("request").value=json(requestValue);for(const [id,key] of [["municipality","municipality"],["disaster","disaster"],["evacuees","evacuees"],["duration","duration_days"],["area-m2","gross_area_m2"],["toilets","accessible_toilet_count"],["staff-count","staff_count"],["target-date","target_date"],["weather-area-code","weather_area_code"]])el(id).value=requestValue[key]??"";el("facility-conditions").value=(requestValue.facility_conditions||[]).join("\n");fillStructuredConditions(requestValue);el("plan-json").value="";el("geometry-id").value="";view.previewGeneratedFields.clear();view.editedFields.clear();markEditorFieldsAsCurrentRequest(requestValue);updateCandidateLists(scenario.provenance?.region_research||{},requestValue,scenario.provenance?.selected_shelter?.id||"");el("create").open=true;invalidatePreview();status(`保存Run ${view.run} のScenario v${saved.scenario_version}を編集フォームへ読み込みました。元Runは不変で、次の承認は新しいScenario/Runになります。${scenario.provenance?.plan_upload?" 図面はProjectのアセットとして保存されます。編集時は保存図面に対応する補正済みGeometryを指定してください。":""}`);});
  on("clone","click",async()=>{if(!view.runData||!view.projectData)throw new Error("複製する保存Runを選択してください。");const saved=view.projectData.scenarios.find(item=>item.scenario_id===view.runData.scenario_id&&item.scenario_version===view.runData.scenario_version);if(!saved)throw new Error("保存済みScenarioが見つかりません。");if(!confirm(`Scenario ${saved.scenario_id} v${saved.scenario_version} の条件を新しいRunとして複製しますか？元Runは変更しません。`))return;const scenario=await request(`${root()}/scenarios`,{scenario:saved.scenario,approved:true,approved_hash:saved.scenario_hash});const runId=`run-${crypto.randomUUID()}`;await request(`${root()}/runs`,{run_id:runId,scenario_id:scenario.scenario_id,scenario_version:scenario.scenario_version});view.run=runId;view.branch="main";view.minute=0;await openProject();status(`新しいRun ${runId} を複製しました。Scenarioは不変の新revisionとして保存されました。`);});
  on("compute","click",async()=>{await request(`${runRoot()}/resume`,{branch_id:view.branch,target_minute:Number(el("target").value),measure_performance:el("measure-performance").checked});await refreshRun(true);});
  on("measure-render","change",()=>{if(!frameMeter)throw new Error("画面計測機能を読み込めません。");if(el("measure-render").checked){frameMeterScope=`${view.run}/${view.branch}`;frameMeter.start(drawMeasured);el("telemetry-status").textContent="可視タブで地図・比較CanvasをrequestAnimationFrameごとに再描画して計測中です。測定負荷を含む参考値です。";}else{frameMeter.stop();el("telemetry-status").textContent="画面FPS収集を停止しました。保存を押すまで計測値はサーバーへ送られません。";}});
  on("telemetry-load","click",async()=>{if(!view.run)throw new Error("Runを選択してください。");renderTelemetry(await request(`${runRoot()}/telemetry`));});
  on("render-telemetry-save","click",async()=>{if(!view.run||!frameMeter)throw new Error("Runまたは画面計測機能がありません。");if(!el("measure-render").checked)throw new Error("先に画面FPS収集を有効にしてください。");const report=frameMeter.report();const saved=await request(`${runRoot()}/telemetry`,{kind:"render",branch_id:view.branch,samples:report.samples,fps:report.fps,p95_frame_ms:report.p95_frame_ms,visibility:report.visibility});renderTelemetry(saved);el("telemetry-status").textContent=`Branch ${view.branch} の描画測定を保存しました。user agent等の端末識別情報は保存していません。`;frameMeter.reset();frameMeterScope=`${view.run}/${view.branch}`;});
  on("cancel","click",async()=>{await request(`${runRoot()}/cancel`,{});status("現在のtick完了後に計算を中断し、保存します。");});
  on("minute","change",()=>seek(Number(el("minute").value)));on("seek","change",()=>seek(Number(el("seek").value)));
  on("play","click",async()=>{if(view.playing||!selectedReference())return;view.playing=true;try{while(view.playing){const limit=(view.comparisonRange||[selectedReference().fork_minute,selectedReference().computed_until])[1];const next=Math.min(limit,view.minute+Number(el("speed").value));if(next===view.minute)break;await seek(next);await new Promise(resolve=>setTimeout(resolve,500));}}finally{view.playing=false;}});
  on("pause","click",()=>{view.playing=false;});
  for(const id of ["layer","routes","staff","facilities","search"])on(id,id==="search"?"input":"change",()=>{if(id==="layer")updateLegend();if(id==="search")renderEntityList();draw();});
  on("household","change",()=>setSelected(el("household").value?{kind:"group",id:el("household").value}:null));
  on("reset-view","click",()=>{view.zoom=1;view.panX=view.panY=0;draw();});
  let drag=null;
  el("map").addEventListener("pointerdown",e=>{drag={x:e.clientX,y:e.clientY,moved:false};el("map").setPointerCapture(e.pointerId);});
  el("map").addEventListener("pointermove",e=>{if(!drag)return;const factor=el("map").width/el("map").getBoundingClientRect().width;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>2)drag.moved=true;view.panX+=dx*factor;view.panY+=dy*factor;drag.x=e.clientX;drag.y=e.clientY;draw();});
  el("map").addEventListener("pointerup",e=>{if(drag&&!drag.moved){const canvas=el("map"),r=canvas.getBoundingClientRect(),x=(e.clientX-r.left)*canvas.width/r.width,y=(e.clientY-r.top)*canvas.height/r.height,hit=view.hits.findLast(h=>Math.hypot(h.x-x,h.y-y)<18);if(hit?.kind==="cluster"){const previous=view.zoom;view.zoom=Math.min(12,Math.max(previous*1.8,previous+.8));const ratio=view.zoom/previous;view.panX=canvas.width/2-20-(hit.x-20-view.panX)*ratio;view.panY=canvas.height/2-20-(hit.y-20-view.panY)*ratio;status(`${hit.ids.length}件を地図上で集約表示しています。絞り込み検索または一覧から個別記録を開けます。`);draw();}else if(hit)setSelected(hit);}drag=null;});
  el("map").addEventListener("wheel",e=>{e.preventDefault();view.zoom=Math.max(.5,Math.min(12,view.zoom*(e.deltaY<0?1.15:1/1.15)));draw();},{passive:false});
  el("map").addEventListener("keydown",e=>{if(e.key==="+"||e.key==="=")view.zoom=Math.min(12,view.zoom*1.15);else if(e.key==="-")view.zoom=Math.max(.5,view.zoom/1.15);else if(e.key==="ArrowLeft")view.panX+=24;else if(e.key==="ArrowRight")view.panX-=24;else if(e.key==="ArrowUp")view.panY+=24;else if(e.key==="ArrowDown")view.panY-=24;else return;e.preventDefault();draw();});
  // A clarification's choices become buttons; a choice adds a zone_id line (or the ID
  // of another target) to the request and previews it again, as /demo-v2 does (#35).
  function renderInterventionChoices(result) {
    const box = el("intervention-choices"); box.replaceChildren();
    if (result?.status !== "clarification_required" || !result.choices?.length) return;
    const zones = new Map((view.frame?.zones || []).map(zone => [zone.id, zone.name]));
    for (const choice of result.choices) {
      const button = document.createElement("button");
      button.type = "button"; button.className = "compact";
      button.textContent = zones.has(choice) ? `${zones.get(choice)} (${choice})` : choice;
      button.addEventListener("click", () => {
        const line = zones.has(choice) ? `zone_id: ${choice}` : `対象ID: ${choice}`;
        el("intervention").value = `${el("intervention").value.replace(/\n(?:zone_id|対象ID): [^\n]+/g, "")}\n${line}`;
        el("intervention").dispatchEvent(new Event("input"));
        el("intervention-preview").click();
      });
      box.append(button);
    }
  }
  on("intervention-preview","click",async()=>{const requestSerial=++view.interventionSerial,requestText=el("intervention").value,requestProgram=el("intervention-program").value,requestMinute=view.minute,requestBranch=view.branch,inputKey=json([requestText,requestProgram]);if(requestText.trim()&&requestProgram.trim())throw new Error("自然言語とEffectProgram JSONはどちらか一方を指定してください。");const body={minute:requestMinute,...(requestProgram.trim()?{program:JSON.parse(requestProgram)}:{text:requestText})};const result=await request(`${branchRoot()}/intervention-preview`,body);if(requestSerial!==view.interventionSerial||inputKey!==json([el("intervention").value,el("intervention-program").value])||requestMinute!==view.minute||requestBranch!==view.branch)return;view.intervention=result;view.interventionInputKey=inputKey;el("intervention-result").textContent=json(view.intervention);renderInterventionChoices(result);el("fork-check").checked=false;window.dispatchEvent(new CustomEvent("sheltersim:intervention-preview",{detail:{result,minute:view.minute,run:view.run,branch:view.branch}}));});
  on("fork","click",async()=>{if(!el("fork-check").checked)throw new Error("分岐条件を確認して承認してください。");const child=`branch-${crypto.randomUUID()}`;let result;const requestText=el("intervention").value,requestProgram=el("intervention-program").value;if(requestText.trim()||requestProgram.trim()){if(view.interventionInputKey!==json([requestText,requestProgram]))throw new Error("入力内容がPreview後に変更されています。作用を再プレビューしてください。");if(!view.intervention?.can_apply)throw new Error("作用のプレビューと検証が必要です。");result=await request(`${branchRoot()}/intervention-apply`,{preview_id:view.intervention.preview_id,approved_program_hash:view.intervention.approved_program_hash,approved:true,child_id:child});}else result=await request(`${branchRoot()}/fork`,{minute:view.minute,child_id:child,approved:true});view.interventionInputKey=null;
    // The server creates the fork in a worker process and returns at once; the Run stays running until the child exists (#48).
    if(result.status==="running"){view.pendingFork=child;await refreshRun(false);}else{view.branch=child;await openRun();}});
  on("compare","click",async()=>{view.playing=false;const selected=[...el("compare-ids").selectedOptions].map(o=>o.value);const ids=[...new Set([view.branch,...selected])];if(ids.length<2)throw new Error("基準Branchを含めて2つ以上選択してください。");const refs=new Map(view.runData.branches.map(item=>[item.branch_id,item]));const lower=Math.max(...ids.map(id=>refs.get(id)?.fork_minute??Infinity)),upper=Math.min(...ids.map(id=>refs.get(id)?.computed_until??-Infinity));if(lower>upper)throw new Error("選択Branchに共通の計算済みminuteがありません。");view.comparisonRange=[lower,upper];view.compareIds=null;view.comparison=null;view.comparisonFrames=null;const minute=Math.max(lower,Math.min(upper,view.minute));if(minute!==view.minute)await seek(minute);await loadComparison(ids,minute);});
  on("ask","click",async()=>{const question=el("question").value.trim();if(!question)throw new Error("質問を入力してください。");const chatRequest=++view.chatSerial;el("ask").disabled=true;const scope={run:view.run,branch:view.branch,minute:view.minute,person:el("chat-person").value,comparisons:[...el("compare-ids").selectedOptions].map(o=>o.value)};el("chat-status").textContent="根拠を取得しています。計算状態は変更されません。";try{const comparisons=scope.comparisons.filter(id=>id!==scope.branch);if(el("chat-use-history").checked&&historyPage?.branchRoot!==branchRoot())throw new Error("このBranchの履歴ページを先に取得してください。");const answer=await request(`${branchRoot()}/questions`,{question,...(el("chat-use-history").checked?{history_query:historyPage.query}:{}),minute:scope.minute,...(scope.person?{agent_id:scope.person}:{}),comparison_branch_ids:comparisons,context_id:view.context});const stillCurrent=chatRequest===view.chatSerial&&scope.run===view.run&&scope.branch===view.branch&&scope.minute===view.minute&&scope.person===el("chat-person").value&&json(scope.comparisons)===json([...el("compare-ids").selectedOptions].map(o=>o.value));if(!stillCurrent)return;view.context=answer.context_id;renderLiveAnswer(answer,question);}catch(error){if(error===STALE)return;el("chat-status").textContent=`通信/予算/回答エラー: ${error.message}。保存済みレポートと記録は確認できます。`;throw error;}finally{if(chatRequest===view.chatSerial)el("ask").disabled=false;}});
  on("archive","click",async()=>{if(!view.run)return;await request(`${runRoot()}/archive`,{});await openProject();});
  on("delete","click",async()=>{if(!view.run)return;const confirmation=prompt(`削除するRun IDを入力してください: ${view.run}`);if(confirmation!==view.run)return;await request(`${runRoot()}/delete`,{confirmation});view.run=null;view.frame=null;await openProject();});
  for(const id of ["text","request","table","area","period","categories","shelter-candidate","municipality","disaster","evacuees","duration","area-m2","toilets","staff-count","target-date","weather-area-code","facility-conditions","plan-json"])on(id,"input",()=>{if(id==="plan-json"&&el("plan-json").value.trim())el("geometry-id").value="";if(["municipality","disaster","evacuees","duration","area-m2","toilets","staff-count","target-date","weather-area-code","facility-conditions"].includes(id)){view.editedFields.add(id);view.previewGeneratedFields.delete(id);}invalidatePreview();if(id==="text"){for(const field of view.previewGeneratedFields)el(field).value="";view.previewGeneratedFields.clear();view.editedFields.clear();el("request").value="";options(el("municipality-candidate"),[{value:"",label:"候補をプレビュー後に選択"}],"");options(el("shelter-candidate"),[{value:"",label:"候補をプレビュー後に選択"}],"");}if(id==="municipality"&&el("municipality-candidate").selectedOptions[0]?.textContent.split(" · ")[0]!==el("municipality").value.trim())el("municipality-candidate").value="";});
  for(const id of ["behavior-policy","social-policy","information-policy","social-propagation-enabled","information-spread-enabled","queue-policy","relationship-policy","social-proximity-policy","body-dynamics-policy","care-outcome-policy","staff-operations-policy","staff-fatigue-policy","zone-temperature-policy","zone-rest-policy","zone-hygiene-policy","zone-privacy-policy"])on(id,"change",()=>{view.editedFields.add(id);view.previewGeneratedFields.delete(id);invalidatePreview();});
  on("policy-parameter-overrides","input",()=>{view.editedFields.add("policy-parameter-overrides");invalidatePreview();});
  on("plan-trace-json","input",()=>invalidatePreview());
  on("household-composition","input",()=>{view.editedFields.add("household-composition");view.editedFields.add("population-conditions");invalidatePreview();});
  for(const id of ["medical-rate","medication-rate","pet-rate","care-support-rates","access-rates","language-weights","information-weights","daily-rhythms"])on(id,"input",()=>{view.editedFields.add("population-conditions");invalidatePreview();});
  for(const id of ["stock-status","water-ml","food-units","warehouse-id","stock-zone"])on(id,"input",()=>{if(id==="water-ml"||id==="food-units"){const hasWater=el("water-ml").value!=="",hasFood=el("food-units").value!=="";el("stock-status").value=hasWater&&hasFood?"entered":hasWater||hasFood?"incomplete":"unknown";view.editedFields.add("stock-status");}view.editedFields.add(id);invalidatePreview();});
  on("stock-status","change",()=>{view.editedFields.add("stock-status");invalidatePreview();});
  on("staff-positive-shifts","input",()=>{view.editedFields.add("staff-positive-shifts");invalidatePreview();});
  on("scheduled-conditions","input",()=>{view.editedFields.add("scheduled-conditions");invalidatePreview();});
  on("equipment-add","click",()=>{const rows=[...el("equipment-rows").querySelectorAll(".equipment-row")].map(row=>Object.fromEntries([...row.querySelectorAll("input")].map(input=>[input.dataset.field,input.type==="checkbox"?input.checked:input.value])));rows.push({facility_id:"",facility_kind:"other",capacity:"",working_units:"",zone_id:"",source_id:"operator_condition",add:true});renderEquipmentRows(rows);view.editedFields.add("equipment-rows");invalidatePreview();});
  on("municipality-candidate","change",()=>{const candidate=view.draft?.research?.municipalities?.find(item=>(item.municipality_code||item.name)===el("municipality-candidate").value);if(candidate){el("municipality").value=candidate.name;if(candidate.municipality_code){const requestValue=el("request").value.trim()?JSON.parse(el("request").value):{};requestValue.municipality_code=candidate.municipality_code;el("request").value=json(requestValue);}}invalidatePreview();});
  on("shelter-candidate","change",()=>invalidatePreview());
  on("chat-person","change",()=>{view.context=null;++view.chatSerial;el("ask").disabled=false;el("chat-status").textContent="人物条件が変わりました。次の質問では根拠スコープを更新します。";});
  on("compare-ids","change",()=>{++view.compareSerial;view.context=null;++view.chatSerial;el("ask").disabled=false;view.compareIds=null;view.comparison=null;view.comparisonFrames=null;view.comparisonRange=null;el("comparison").replaceChildren();renderComparisonChart();const ref=selectedReference();if(ref){el("seek").min=el("minute").min=ref.fork_minute;el("seek").max=el("minute").max=ref.computed_until;el("range").textContent=`計算済み ${ref.fork_minute}–${ref.computed_until} 分 · ${view.runData.status}`;}draw();});
  let pendingPlan = null;
  let historyPage = null;
  let draftRevision = null;
  let draftSaveSerial = 0;
  on("plan-file","change",async()=>{
    pendingPlan=null;el("plan-permission").checked=false;
    const file=el("plan-file").files[0];
    if(!file){el("plan-status").textContent="ファイル未選択";return;}
    if(!file.size||file.size>15*1024*1024)throw new Error("図面は1 byte以上15 MiB以下にしてください。");
    const bytes=new Uint8Array(await file.arrayBuffer());
    const sha=[...new Uint8Array(await crypto.subtle.digest("SHA-256",bytes))].map(v=>v.toString(16).padStart(2,"0")).join("");
    if(el("plan-file").files[0]!==file)return;
    pendingPlan={file,bytes,sha};
    el("plan-status").textContent=`未保存 · ${file.name} · ${file.size.toLocaleString()} bytes · SHA-256 ${sha}`;
    if(["application/json","application/geo+json"].includes(file.type)){
      try{const parsed=JSON.parse(await file.text());if(parsed.asset&&parsed.geometry&&parsed.zones)el("plan-json").value=json(parsed);}catch(error){el("plan-status").textContent+=` JSON補正データとしては読み込めません: ${error.message}`;}
    }
    invalidatePreview();
  });
  on("plan-upload","click",async()=>{
    const plan=pendingPlan;if(!plan||!el("plan-permission").checked)throw new Error("図面を選択し、保存権限を確認してください。");
    const project=view.project,upload=`upload-${crypto.randomUUID()}`,base=`${root()}/uploads/${encode(upload)}`;
    const media=plan.file.type;
    if(!["application/pdf","image/png","image/jpeg","image/webp","application/json","application/geo+json"].includes(media))throw new Error("対応するMIME形式の図面を選択してください。");
    el("plan-upload").disabled=true;
    try{
      await request(base,{schema_version:1,media_type:media,byte_count:plan.bytes.length,sha256:plan.sha,permission_confirmed:true,sensitive_building:true,file_name:plan.file.name.replace(/[\\/\0]/g,"_").slice(0,240)||"plan-upload",source_kind:"operator_upload",coordinate_system:["application/pdf","image/png","image/jpeg","image/webp"].includes(media)?"raster_pixel":"unknown"});
      for(let offset=0,index=0;offset<plan.bytes.length;offset+=256*1024,index++){
        const part=plan.bytes.subarray(offset,offset+256*1024);
        let binary="";for(let i=0;i<part.length;i+=8192)binary+=String.fromCharCode(...part.subarray(i,i+8192));
        await request(`${base}/chunks`,{index,data_base64:btoa(binary)});
      }
      const asset=await request(`${base}/finish`,{});
      if(project===view.project&&plan===pendingPlan){el("plan-status").textContent=`Project保存済み · SHA-256 ${asset.sha256} · ${asset.byte_count} bytes。画像は上の図面エディタへ表示できます。PDFはAssetとして保存しました。PDF表示と抽出候補の作成はオプションdecoderの有無に従います。`;window.dispatchEvent(new CustomEvent("sheltersim:asset-saved",{detail:{asset,project}}));}
    }finally{el("plan-upload").disabled=false;}
  });
  async function loadEvidenceHistory(next=false){
    if(!view.run)throw new Error("保存Runを選択してください。");
    const kind=el("history-kind").value,identity=el("history-id").value.trim();
    const endOfTick=["kpis","events","causal","social","staff"].includes(kind);let start=Number(el("history-start").value||0),end=Number(el("history-end").value||view.minute);
    if(endOfTick&&end>=view.minute){if(view.minute<=start)throw new Error("この時刻より前に取得できる終了tick記録がありません。人物・世帯のbefore_tick履歴を選ぶか、開始/終了時刻を調整してください。");end=view.minute-1;el("history-end").value=String(end);}
    const values={kind,start,end,step:el("history-step").value,limit:"20",include_related:kind==="causal"&&el("history-related").checked,event_type:el("history-event-type").value.trim(),source_id:el("history-source").value.trim(),status:el("history-status").value.trim(),zone_id:el("history-zone").value.trim(),text:el("history-text").value.trim(),conversation_id:el("history-conversation").value.trim()};
    if(["people","households"].includes(kind)&&["event_type","source_id","status","zone_id","text"].some(key=>values[key]))throw new Error("人物・世帯のbefore_tick履歴はmetadata検索に対応していません。検索可能な記録履歴を選んでください。");
    if(values.conversation_id&&kind!=="conversations")throw new Error("会話IDの絞り込みには結果Q&A履歴を選んでください。");
    if((kind==="people"||kind==="households"||kind==="causal")&&!identity)throw new Error(`${kind} 履歴には対象IDが必要です。`);
    if(identity){const selectedKind=el("history-id-kind")?.value||"agent_id",field={people:"agent_id",households:"household_id",causal:"event_id",social:"agent_id",staff:selectedKind,events:selectedKind,actions:selectedKind,care:selectedKind,operations:selectedKind,transitions:selectedKind,dialogue:selectedKind}[kind];if(field&&["agent_id","household_id","staff_id","event_id"].includes(field))values[field]=identity;}
    for(const key of ["event_type","source_id","status","zone_id","text","conversation_id"])if(!values[key])delete values[key];
    const scope=`${branchRoot()}/history?${new URLSearchParams(values)}`;
    if(next){if(historyPage?.scope!==scope||!historyPage.cursor)throw new Error("条件が変わりました。先頭から取得してください。");values.cursor=historyPage.cursor;}
    const branch=view.branch,run=view.run,serial=++view.historySerial;
    const page=await request(`${branchRoot()}/history?${new URLSearchParams(values)}`);
    if(serial!==view.historySerial||branch!==view.branch||run!==view.run)return;
    historyPage={scope,branchRoot:branchRoot(),cursor:page.next_cursor,query:{...values,start:Number(values.start),end:Number(values.end),step:Number(values.step),limit:20}};el("history-next").disabled=!page.next_cursor;el("history-result").textContent=json(page);renderHistoryStory(page);window.dispatchEvent(new CustomEvent("sheltersim:history",{detail:{project:view.project,run:view.run,branch:view.branch,minute:view.minute,query:historyPage.query,page}}));
  }
  function renderHistoryStory(page){
    const box=el("storyline");box.replaceChildren();appendText(box,"h3","保存記録に沿った経過");appendText(box,"p","以下はRunに保存された履歴の要約です。会話・心理状態・原因を記録が示していない場合は補っていません。","muted");
    for(const item of page.items||[]){
      const minute=item.minute??item.minute_index??"?",line=document.createElement("p");line.className="history-story-row";
      if(item.question){line.textContent=`${minute}分 · 保存Q&A · ${item.question}`;box.append(line);const answer=document.createElement("p");answer.textContent=item.answer||"回答本文がありません。";box.append(answer);renderCitationButtons(box,item.evidence||item.sources||[]);continue;}
      if(Array.isArray(item.people)){
        const people=item.people.map(person=>`${person.id}: 行動 ${person.activity||"未記録"} · Zone ${person.zone_id||"未記録"} · stress ${person.stress??"未記録"}`).join(" / ");line.textContent=`${minute}分 (${item.time_semantics||"保存状態"}) · ${people||"人物不在/未記録"}`;
      }else if(item.record_kind==="actions"||item.transition){
        const transitionLabels={started:"開始",continued:"継続",completed:"完了",interrupted:"中断",abandoned:"中止",censored:"Run終了時点で継続中"};
        const transition=transitionLabels[item.transition||item.status]||item.status||"状態未記録";
        line.textContent=`${minute}分 · 行動 ${item.action||"未記録"} · ${transition}${item.transition==="censored"?"（完了時刻なし）":""} · ${item.reason_code||item.causation_id||"理由/因果未記録"}`;
      }else if(item.record_kind==="care"){
        const observationLabels={observed_change:"モデル状態の変化を記録",observed_no_change:"状態を測定、変化なし",not_measured:"効果未測定",not_applicable:"測定対象外"};
        const observation=observationLabels[item.outcome_observation]||"結果観測の種別未記録";
        const changes=(Array.isArray(item.state_changes)?item.state_changes:[]).map(change=>{
          const value=number=>typeof number==="number"&&Number.isFinite(number)?number.toFixed(3):"未記録";
          const subject=[change.entity_kind,change.entity_id,change.field_path].filter(part=>typeof part==="string"&&part).join("/");
          return `${subject}: ${value(change.before)} → ${value(change.after)} ${change.unit||"unit不明"}`;
        });
        line.textContent=`${minute}分 · 支援 ${item.support_kind||"種別未記録"} · ${item.status||"状態不明"}${item.status==="completed"?` · ${observation}`:" · 完了/結果は未確認"}${changes.length?` · Runの記録値: ${changes.join(" / ")}`:""} · Event ${item.event_id||"未記録"}`;
      }
      else if(item.record_kind==="operations")line.textContent=`${minute}分 · 運営 ${item.record_type||item.operation||"種別未記録"} · ${item.staff_id||item.facility_id||"対象ID未記録"} · ${item.status||"状態不明"} · Event ${item.event_id||"未記録"}`;
      else if(item.record_kind==="dialogue"){const participants=Array.isArray(item.actor_ids)?item.actor_ids:Array.isArray(item.participant_ids)?item.participant_ids:[],dialogueTurns=Array.isArray(item.virtual_dialogue_example?.turns)?item.virtual_dialogue_example.turns:[],dialogueText=dialogueTurns.map(turn=>`${turn.speaker_id||"参加者"}: ${turn.example_text||""}`).join(" / "),speech=(item.utterance_recorded===false?"構造化された相互作用 · 発言記録なし":item.synthetic===true?"架空の合成発言":item.synthetic===false?"synthetic=false (実音声を意味する保証なし)":"発言内容は未記録。録音/逐語発言として扱いません")+(dialogueText?" · 提案時状態と適用済みモデル差分に基づく架空の対話例（実際の発言ではありません）: "+dialogueText:typeof item.virtual_utterance_example==="string"?" · 架空の例文（実際の発言ではありません）: "+item.virtual_utterance_example:"");const labels={positive_affect:"肯定的感情",negative_affect:"否定的感情",reassurance:"安心感",discontent:"不満",uncertainty:"不確実性",relationship_trust_delta:"関係の信頼"},effects=(Array.isArray(item.observed_model_effects)?item.observed_model_effects:[]).flatMap(effect=>Object.entries(effect?.deltas||{}).filter(([,value])=>typeof value==="number"&&Number.isFinite(value)).map(([name,value])=>`${labels[name]||name} ${value>0?"+":""}${value.toFixed(3)}`));line.textContent=`${minute}分 · シミュレーション社会記録 · ${participants.join("、")||"参加者未記録"} · ${speech}${effects.length?" · 記録されたモデル状態変化: "+effects.join(" / "):""}`;}
      else if(item.record_kind==="transitions")line.textContent=`${minute}分 · 人口遷移 ${item.action||item.transition_type||"種別未記録"} · person ${item.agent_id||"未記録"} · household ${item.household_id||"未記録"} · Event ${item.event_id||"未記録"}`;
      else if(item.event_id)line.textContent=`${minute}分 · Event ${item.event_type||"未記録"} · ${item.status||"状態未記録"} · cause ${item.causation_id||"親cause未記録"}`;
      else if(item.staff_id)line.textContent=`${minute}分 · スタッフ ${item.staff_id} · 担当 ${item.task_id||item.operation||"未記録"} · 結果 ${item.status||item.outcome||"未記録"}`;
      else if(item.actor_ids)line.textContent=`${minute}分 · 接触/社会記録 · ${(Array.isArray(item.actor_ids)?item.actor_ids:[]).map(String).join("、")} · ${item.interaction_kind||item.kind||"記録種別なし"}`;
      else line.textContent=`${minute}分 · KPI/その他の保存記録 · ${Object.entries(item).filter(([key,value])=>key!=="state_hash"&&["string","number","boolean"].includes(typeof value)).slice(0,6).map(([key,value])=>`${key}=${value}`).join(" · ")}`;
      box.append(line);
      const frameMinute=Number(minute)+(item.status==="censored"?0:(item.time_semantics==="end_of_tick"||item.event_id?1:0)),unique=values=>[...new Set(values.filter(value=>typeof value==="string"&&value.trim()))],list=value=>Array.isArray(value)?value:[],peopleRows=list(item.people);
      const personIds=unique([...peopleRows.map(person=>person?.id),item.person_id,item.agent_id,item.subject_agent_id,item.target_agent_id,item.participant_id,item.source_agent_id,...list(item.agent_ids),...list(item.actor_ids),...list(item.participant_ids)]);
      const householdIds=unique([item.household_id,item.group_id,item.from_household_id,item.to_household_id,...list(item.household_ids),...peopleRows.flatMap(person=>[person?.household_id,person?.group_id])]);
      const staffIds=unique([item.staff_id,item.assigned_staff_id,...list(item.staff_ids)]);
      for(const person of personIds){const button=document.createElement("button");button.type="button";button.textContent=`${minute}分 · 人物 ${person} の状態/履歴へ`;button.addEventListener("click",()=>seek(frameMinute).then(()=>setSelected({kind:"person",id:person})).catch(error=>status(error.message)));box.append(button);}
      for(const household of householdIds){const button=document.createElement("button");button.type="button";button.textContent=`${minute}分 · 世帯 ${household} の履歴へ`;button.addEventListener("click",()=>seek(frameMinute).then(()=>setSelected({kind:"group",id:household})).catch(error=>status(error.message)));box.append(button);}
      for(const staff of staffIds){const button=document.createElement("button");button.type="button";button.textContent=`${minute}分 · スタッフ ${staff} の担当/履歴へ`;button.addEventListener("click",()=>seek(frameMinute).then(()=>setSelected({kind:"staff",id:staff})).catch(error=>status(error.message)));box.append(button);}
      if(item.event_id){const button=document.createElement("button");button.type="button";button.textContent=`Event ${item.event_id} を表示`;button.addEventListener("click",()=>seek(frameMinute).then(()=>setSelected({kind:"event",id:item.event_id})).catch(error=>status(error.message)));box.append(button);}
    }
    if(!(page.items||[]).length)appendText(box,"p","この範囲に保存された履歴はありません。");
    appendText(box,"p",`表示 ${page.items?.length||0} · ${page.count_status|| (page.total_items===undefined?"総件数未取得":page.total_items===null?"総件数は未計算":`総件数 ${page.total_items}`)} · ${page.next_cursor?"続きがあります。次ページを取得できます。":"このページの末尾です。"}${page.causal_graph_truncated?" · 因果traceは切り詰められています。":""}`,"muted");
    const ask=document.createElement("button");ask.type="button";ask.textContent="この履歴を根拠にストーリー/仮説を質問";ask.addEventListener("click",()=>{el("chat-use-history").checked=true;el("question").value=page.kind==="social"?"保存された接触・社会記録を根拠に、実際の発言と区別した架空の短い合成会話例を作り、根拠と限界を説明してください。":`保存された${page.kind}履歴から、記録された変化と考えられる要因を区別して説明してください。因果関係の仮説には仮説と明示してください。`;el("question").focus();});box.append(ask);
  }
  view.historySerial=0;
  const historyIdKinds={people:["agent_id"],households:["household_id"],events:["agent_id","event_id"],causal:["event_id"],social:["agent_id","event_id"],staff:["staff_id","event_id"],actions:["agent_id","household_id","staff_id","event_id"],care:["agent_id","household_id","event_id"],operations:["staff_id","event_id"],transitions:["agent_id","household_id","event_id"],dialogue:["agent_id","household_id","staff_id","event_id"]};
  const updateHistoryIdKinds=()=>{const select=el("history-id-kind"),kind=el("history-kind").value,allowed=historyIdKinds[kind]||["agent_id"];for(const option of select.options)option.hidden=!allowed.includes(option.value);if(!allowed.includes(select.value))select.value=allowed[0];el("history-related").disabled=kind!=="causal";if(kind!=="causal")el("history-related").checked=false;};
  on("history-kind","change",updateHistoryIdKinds);updateHistoryIdKinds();
  on("history-load","click",()=>loadEvidenceHistory());on("history-next","click",()=>loadEvidenceHistory(true));
  on("usage-load","click",async()=>{el("usage-result").textContent=json(await request(`${root()}/usage?limit=100`));});
  on("draft-save","click",async()=>{
    // An unfinished editor is a draft, not a validated Scenario. Preserve incomplete
    // fields/JSON verbatim; validation belongs to the explicit preview action.
      const formIds=["request","plan-json","plan-trace-json","municipality","target-date","weather-area-code","disaster","evacuees","duration","area-m2","toilets","staff-count","behavior-policy","social-policy","information-policy","social-propagation-enabled","information-spread-enabled","queue-policy","relationship-policy","social-proximity-policy","body-dynamics-policy","care-outcome-policy","staff-operations-policy","staff-fatigue-policy","zone-temperature-policy","zone-rest-policy","zone-hygiene-policy","zone-privacy-policy","facility-conditions","table","area","period","categories","medical-rate","medication-rate","pet-rate","care-support-rates","access-rates","language-weights","information-weights","daily-rhythms","household-composition","facility-edit-json","stock-status","water-ml","food-units","warehouse-id","stock-zone","scheduled-conditions","staff-positive-shifts","geometry-id"];
    const content={editor_schema_version:1,text:el("text").value,form:Object.fromEntries(formIds.map(id=>[id,el(id).type==="checkbox"?el(id).checked:el(id).value])),equipment:[...el("equipment-rows").querySelectorAll(".equipment-row")].map(row=>Object.fromEntries([...row.querySelectorAll("input")].map(input=>[input.dataset.field,input.type==="checkbox"?input.checked:input.value]))),staff_unavailability:[...el("shift-rows").querySelectorAll(".staff-shift-row")].map(row=>Object.fromEntries([...row.querySelectorAll("input")].map(input=>[input.dataset.field,input.value]))),plan_editor:window.ShelterSimPlanEditor?.snapshot?.()||null};
    const project=view.project,id=el("draft-id").value.trim(),serial=++draftSaveSerial,base=`/api/v1/projects/${encode(project)}/drafts/${encode(id)}`;
    if(!id)throw new Error("草案IDを入力してください。");
    if(!draftRevision||draftRevision.project!==view.project||draftRevision.id!==id){
      const listing=await request(`/api/v1/projects/${encode(project)}/drafts?limit=100`);
      if(serial!==draftSaveSerial||project!==view.project||id!==el("draft-id").value.trim())return;
      if(listing.next_cursor||listing.items.some(item=>item.draft_id===id))throw new Error("既存草案は先に復旧してから保存してください。競合を防ぐため自動上書きしません。");
    }
    const saved=await request(base,{schema_version:1,kind:"scenario_input",content,...(draftRevision?.project===view.project&&draftRevision.id===id?{expected_hash:draftRevision.hash}:{})});
    if(serial!==draftSaveSerial||project!==view.project||id!==el("draft-id").value.trim())return;
    draftRevision={project:view.project,id,hash:saved.content_hash};el("draft-status").textContent=`草案 ${saved.draft_id} · revision ${saved.revision} 保存済み`;
  });
  el("draft-id").addEventListener("input",()=>{++draftSaveSerial;});
  let draftRestoreSerial=0;
  on("draft-restore","click",async()=>{
    const serial=++draftRestoreSerial,project=view.project,id=el("draft-id").value.trim(),saved=await request(`${root()}/drafts/${encode(id)}`);
    if(serial!==draftRestoreSerial||project!==view.project||id!==el("draft-id").value.trim())return;
    if(saved.kind!=="scenario_input")throw new Error("入力条件の草案を選択してください。");
    const content=saved.content;el("text").value=content.text||"";el("request").value=content.request?json(content.request):"";el("plan-json").value=content.plan_override?json(content.plan_override):"";
    for(const field of ["municipality","target-date","weather-area-code","disaster","evacuees","duration","area-m2","toilets","staff-count","facility-conditions"])el(field).value="";
    if(content.form){
      for(const id of ["request","plan-json","plan-trace-json","municipality","target-date","weather-area-code","disaster","evacuees","duration","area-m2","toilets","staff-count","behavior-policy","social-policy","information-policy","social-propagation-enabled","information-spread-enabled","queue-policy","relationship-policy","social-proximity-policy","body-dynamics-policy","care-outcome-policy","staff-operations-policy","staff-fatigue-policy","zone-temperature-policy","zone-rest-policy","zone-hygiene-policy","zone-privacy-policy","facility-conditions","table","area","period","categories","medical-rate","medication-rate","pet-rate","care-support-rates","access-rates","language-weights","information-weights","daily-rhythms","household-composition","facility-edit-json","stock-status","water-ml","food-units","warehouse-id","stock-zone","scheduled-conditions","staff-positive-shifts","geometry-id"])
        if(el(id).type==="checkbox"&&typeof content.form[id]==="boolean")el(id).checked=content.form[id];else if(typeof content.form[id]==="string")el(id).value=content.form[id];
    }
    view.restoreFacilityCandidate=Boolean(el("facility-edit-json").value.trim());
    renderEquipmentRows(Array.isArray(content.equipment)?content.equipment.map(item=>Object.fromEntries(Object.entries(item).map(([key,value])=>[key,key==="source_id"?value:value]))):[]);
    view.editedFields.clear();view.previewGeneratedFields.clear();invalidatePreview();
    if(window.ShelterSimPlanEditor?.restore)await window.ShelterSimPlanEditor.restore(content.plan_editor||null);
    draftRevision={project:view.project,id,hash:saved.content_hash};el("draft-status").textContent=`草案 ${id} · revision ${saved.revision} を復旧しました。図面候補・採否・縮尺・トレースは未承認草案として戻しました。実行には再プレビューと明示承認が必要です。`;window.dispatchEvent(new CustomEvent("sheltersim:draft-restored",{detail:{staff_unavailability:content.staff_unavailability||[]}}));
  });
  on("intervention","input",()=>{++view.interventionSerial;view.intervention=null;el("intervention-choices").replaceChildren();el("intervention-result").textContent="入力が変わりました。もう一度作用をプレビューしてください。";el("fork-check").checked=false;});
  el("project").value=view.project;openProject().catch(e=>status(e.message));
})();
