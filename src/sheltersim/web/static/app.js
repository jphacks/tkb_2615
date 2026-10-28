"use strict";

const state = {
  runId: null,
  finalMinute: 0,
  profilesOffset: 0,
  profileQuery: "",
  profilesTotal: 0,
  eventsOffset: 0,
  eventsTotal: 0,
  eventLimit: 100,
  activeAdminTab: "overview",
  selectedAgent: null,
  timeline: [],
  demo: null,
  precomputedReplay: null,
  demoJobs: [],
  demoBusy: false,
  demoJobId: null,
  nlPreview: null,
  nlMultiPlan: null,
  nlMultiValidation: null,
  nlMultiIdempotencyKey: null,
  nlMultiStatusText: "案を2つ以上用意してから再検証してください。",
  nlBusy: false,
  nlIdempotencyKey: null,
  nlLastBranchId: null,
  nlStatusText: "Branchを選択して一時停止すると提案できます。",
  routeMode: "demo",
  requestedRunId: null,
  requestedBranchId: null,
  requestedDemoBranchPending: false,
  demoBranchTouched: false,
  requestedMinute: null,
  runManifest: null,
  lastMap: null,
  layoutRawMap: null,
  layoutOverrideKey: null,
  layoutOverrides: { zones: {}, facilities: {} },
  layoutDrag: null,
  mapAgentPoints: [],
  selectedZoneId: null,
  currentMetrics: null,
  scenarioPresets: [],
  scenarioPreview: null,
  scenarioPreviewId: null,
  scenarioPreviewRequest: null,
  scenarioEditorBusy: false,
  timelineRequest: 0,
  mapRequest: 0,
  timeRefreshTimer: null,
};
const productView = {
  branchId: null,
  frame: null,
  minute: null,
  layer: "normal",
  overlays: { agents: true, staff: true, facilities: true, events: true },
  selectedEntity: null,
  hits: [],
  requestId: 0,
  compareRequestId: 0,
  compareMinute: null,
  chatContextId: null,
  runChatContextId: null,
  chatHistory: [],
  runChatHistory: [],
};
const $ = (id) => document.getElementById(id);

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

async function demoPost(action, payload = {}) {
  const response = await fetch(`/api/v1/demo/${action}`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

async function naturalLanguagePost(action, payload = {}) {
  const response = await fetch(`/api/v1/nl/${action}`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

async function jobPost(jobId, action) {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/${action}`, {
    method: "POST",
    headers: { Accept: "application/json" },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function setError(error) { $("error").textContent = error ? String(error.message || error) : ""; }
function minuteText(value) { const day = Math.floor(value / 1440) + 1; const hour = Math.floor((value % 1440) / 60); const minute = value % 60; return `Day ${day} · ${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`; }
function cloneJson(value) { return JSON.parse(JSON.stringify(value)); }

function layoutOverrideKey(map) {
  const layoutId = map.layout?.id || state.demo?.layout?.id;
  return layoutId ? `sheltersim.layout-display.${layoutId}` : null;
}

function currentLayoutOverrides(map) {
  const key = layoutOverrideKey(map);
  if (state.layoutOverrideKey !== key) {
    state.layoutOverrideKey = key;
    try {
      const stored = key ? JSON.parse(localStorage.getItem(key) || "null") : null;
      state.layoutOverrides = stored && typeof stored === "object"
        ? { zones: stored.zones || {}, facilities: stored.facilities || {} }
        : { zones: {}, facilities: {} };
    } catch {
      state.layoutOverrides = { zones: {}, facilities: {} };
    }
  }
  return state.layoutOverrides;
}

function withVisualLayoutOverrides(source) {
  const map = cloneJson(source);
  const overrides = currentLayoutOverrides(map);
  for (const zone of map.zones || []) {
    const edit = overrides.zones[zone.id];
    if (!edit || !Array.isArray(edit.polygon) || edit.polygon.length < 3) continue;
    zone.polygon = edit.polygon;
    zone.x = edit.x;
    zone.y = edit.y;
  }
  for (const facility of map.facilities || []) {
    const edit = overrides.facilities[facility.id];
    if (!edit || !Number.isFinite(edit.x) || !Number.isFinite(edit.y)) continue;
    facility.x = edit.x;
    facility.y = edit.y;
  }
  return map;
}

function saveVisualLayoutOverrides() {
  const key = state.layoutOverrideKey;
  if (!key) return;
  try {
    localStorage.setItem(key, JSON.stringify(state.layoutOverrides));
  } catch {
    $("map-note").textContent = "ブラウザ保存が使えないため、表示変更はこのページ内だけです";
  }
}

function clearNaturalLanguagePreview(message = "入力を変更したため、もう一度プレビューしてください。") {
  state.nlPreview = null;
  state.nlIdempotencyKey = null;
  state.nlMultiPlan = null;
  state.nlMultiValidation = null;
  state.nlMultiIdempotencyKey = null;
  state.nlStatusText = message;
  refreshNaturalLanguagePanel();
}

function appendTextItems(list, items, renderItem = (item) => String(item)) {
  list.replaceChildren();
  for (const item of items) {
    const row = document.createElement("li");
    row.textContent = renderItem(item);
    list.append(row);
  }
  return items.length;
}

function renderStoryCard(card, reviewStatus = null) {
  const container = $("demo-story-card");
  container.replaceChildren();
  if (!card) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "根拠付きのストーリーはありません。";
    container.append(empty);
    return;
  }
  const mode = document.createElement("p");
  mode.className = "muted";
  const modeLabel = card.mode === "opus" ? "Opusの検証済みコードを使用" : "固定テンプレート（オフライン）";
  mode.textContent = reviewStatus ? `${modeLabel} · レビュー状態: ${reviewStatus}` : modeLabel;
  container.append(mode);
  const title = document.createElement("h4");
  title.textContent = card.title || "イベント記録とKPI差";
  container.append(title);
  const addSection = (heading, values) => {
    if (!values?.length) return;
    const sectionTitle = document.createElement("h4");
    sectionTitle.textContent = heading;
    const list = document.createElement("ul");
    appendTextItems(list, values);
    container.append(sectionTitle, list);
  };
  addSection("観測された事実", card.observed_facts);
  addSection("仮説", card.hypotheses);
  addSection("適用範囲", card.model_limits);
}

function renderNaturalLanguagePreview() {
  const preview = state.nlPreview;
  const resultPanel = $("nl-result");
  resultPanel.hidden = !preview;
  if (!preview) return;
  const result = preview.result;
  const plan = result.draft_plan;
  const modeLabels = {
    native: "標準の仕組みで表現",
    composed: "複数の仕組みを組み合わせて表現",
    approximate: "仮定を置いて近似",
    clarification_required: "確認が必要",
    not_simulatable: "現在のモデルでは表現できません",
  };
  const statusLabels = {
    compiled: "プレビューを作成しました",
    clarification_required: "追加の選択が必要です",
    not_simulatable: "プレビューを作成できません",
  };
  $("nl-mode").textContent = `${statusLabels[result.status] || result.status} · ${modeLabels[result.support_mode] || result.support_mode}`;
  const usage = result.usage || {};
  const cost = Number(usage.estimated_cost_usd || 0).toFixed(6);
  $("nl-usage").textContent = `${usage.model_id || "offline rule"} · API ${usage.api_request_count ?? 0}回 · ${usage.input_tokens ?? 0}/${usage.output_tokens ?? 0} tokens · 推定 $${cost}${result.fallback_used ? " · fallback" : ""}`;
  $("nl-summary").textContent = result.mechanism_summary || "作用の説明はありません。";
  $("nl-preview-notice").textContent = result.preview_notice || "これは作用のプレビューで、観測結果ではありません。";
  const effects = plan?.effects || [];
  const effectCount = appendTextItems($("nl-effects"), effects, (effect) => {
    const target = effect.target_selector || {};
    const time = effect.end_minute === null || effect.end_minute === undefined
      ? `開始 ${minuteText(effect.start_minute)}`
      : `${minuteText(effect.start_minute)} から ${minuteText(effect.end_minute)} まで`;
    const parameters = Object.keys(effect.parameters || {}).length
      ? ` · 設定 ${JSON.stringify(effect.parameters)}`
      : "";
    return `${effect.op} · ${target.kind || "対象"} ${target.target_id || "(未指定)"} · ${time} · 単位 ${effect.units || "—"}${parameters}`;
  });
  if (!effectCount) appendTextItems($("nl-effects"), ["適用できる作用はありません。"]);
  const assumptions = plan?.assumptions || [];
  const assumptionCount = appendTextItems($("nl-assumptions"), assumptions, (item) => {
    const value = item.value === null || item.value === undefined ? "" : ` 値 ${item.value}${item.units ? ` ${item.units}` : ""}`;
    return `${item.description}${value}`;
  });
  if (!assumptionCount) appendTextItems($("nl-assumptions"), ["追加の仮定はありません。"]);
  const ambiguities = (plan?.ambiguities || []).map((item) => `確認: ${item.message}${item.choices?.length ? ` (${item.choices.join(", ")})` : ""}`);
  const limitations = [...(result.limitations || []), ...(result.questions || []).map((question) => `確認: ${question}`), ...ambiguities];
  for (const choice of result.choices || []) limitations.push(`候補: ${choice}`);
  if (preview.validation) {
    const validation = preview.validation;
    limitations.push(validation.stale ? "このプレビューは古くなりました。新しいプレビューを作成してください。" : validation.valid ? "現在のBranch状態に対する検証に成功しました。" : "検証できません: " + (validation.issues || []).join("; "));
  }
  if (limitations.length) appendTextItems($("nl-limitations"), limitations);
  else appendTextItems($("nl-limitations"), ["未対応事項はありません。"]);
}

function invalidateMultiPlan(message = "案を編集しました。全案を再検証してください。") {
  state.nlMultiValidation = null;
  state.nlMultiIdempotencyKey = null;
  state.nlMultiStatusText = message;
  syncMultiPlanButtons();
}

function syncMultiPlanButtons() {
  const plan = state.nlMultiPlan;
  const validation = state.nlMultiValidation;
  const busy = state.nlBusy;
  const invalidEditor = Boolean($("nl-multi-branches").querySelector('[aria-invalid="true"]'));
  $("nl-multi-add-branch").disabled = !plan || busy || plan.branches.length >= 8;
  $("nl-multi-validate").disabled = !plan || busy || plan.branches.length < 2 || invalidEditor;
  $("nl-multi-approve").disabled = busy || !plan || !validation?.valid ||
    validation.stale || !state.nlMultiIdempotencyKey;
  $("nl-multi-status").textContent = busy
    ? "全案を検証しています…"
    : state.nlMultiStatusText;
}

function renderMultiPlanEditor() {
  const root = $("nl-multi-editor");
  const plan = state.nlMultiPlan;
  root.hidden = !plan || !state.nlPreview || state.nlPreview.result.status !== "compiled";
  if (!plan || root.hidden) {
    $("nl-multi-branches").replaceChildren();
    syncMultiPlanButtons();
    return;
  }
  const forkInput = $("nl-multi-fork-minute");
  if (document.activeElement !== forkInput) forkInput.value = String(plan.fork_at_minute);
  forkInput.max = String(Math.max(0, (state.demo?.duration_minutes || 1) - 1));
  forkInput.oninput = () => {
    const value = Number(forkInput.value);
    if (!Number.isInteger(value) || value < 0) {
      invalidateMultiPlan("共通分岐時刻は0以上の整数で指定してください。");
      return;
    }
    plan.fork_at_minute = value;
    for (const alternative of plan.branches) {
      alternative.plan.fork_at_minute = value;
      alternative.plan.simulation_start_minute = value;
    }
    invalidateMultiPlan();
  };

  const container = $("nl-multi-branches");
  container.replaceChildren();
  plan.branches.forEach((alternative, branchIndex) => {
    const card = document.createElement("article");
    card.className = "multi-plan-branch";
    const heading = document.createElement("h5");
    heading.textContent = `${alternative.label || `比較案 ${branchIndex + 1}`} · ${alternative.variant_id}`;
    card.append(heading);
    const fields = document.createElement("div");
    fields.className = "multi-plan-branch-fields";
    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.maxLength = 80;
    labelInput.value = alternative.label;
    labelInput.setAttribute("aria-label", `比較案 ${branchIndex + 1} の名前`);
    labelInput.oninput = () => {
      alternative.label = labelInput.value;
      heading.textContent = `${alternative.label || `比較案 ${branchIndex + 1}`} · ${alternative.variant_id}`;
      invalidateMultiPlan();
    };
    const removeBranch = document.createElement("button");
    removeBranch.type = "button";
    removeBranch.textContent = "案を削除";
    removeBranch.disabled = plan.branches.length <= 1;
    removeBranch.onclick = () => {
      plan.branches.splice(branchIndex, 1);
      renderMultiPlanEditor();
      invalidateMultiPlan();
    };
    fields.append(labelInput, removeBranch);
    card.append(fields);

    alternative.plan.effects.forEach((effect, effectIndex) => {
      const row = document.createElement("div");
      row.className = "multi-plan-effect";
      const addField = (labelText, control) => {
        const label = document.createElement("label");
        label.textContent = labelText;
        label.append(control);
        row.append(label);
      };
      const opInput = document.createElement("input");
      opInput.type = "text";
      opInput.maxLength = 64;
      opInput.value = effect.op;
      opInput.setAttribute("aria-label", `案${branchIndex + 1} 作用${effectIndex + 1} のoperation`);
      opInput.oninput = () => { effect.op = opInput.value; invalidateMultiPlan(); };
      addField("作用", opInput);

      const startInput = document.createElement("input");
      startInput.type = "number";
      startInput.min = String(plan.fork_at_minute);
      startInput.max = String(Math.max(0, (state.demo?.duration_minutes || 1) - 1));
      startInput.step = "1";
      startInput.value = String(effect.start_minute);
      startInput.setAttribute("aria-label", `案${branchIndex + 1} 作用${effectIndex + 1} の開始時刻`);
      startInput.oninput = () => {
        const value = Number(startInput.value);
        if (!Number.isInteger(value) || value < 0) {
          invalidateMultiPlan("作用の開始時刻は0以上の整数で指定してください。");
          return;
        }
        effect.start_minute = value;
        invalidateMultiPlan();
      };
      addField("開始時刻（minute）", startInput);

      const endInput = document.createElement("input");
      endInput.type = "number";
      endInput.min = String(plan.fork_at_minute + 1);
      endInput.max = String(state.demo?.duration_minutes || 1);
      endInput.step = "1";
      endInput.value = effect.end_minute === null || effect.end_minute === undefined
        ? "" : String(effect.end_minute);
      endInput.setAttribute("aria-label", `案${branchIndex + 1} 作用${effectIndex + 1} の終了時刻`);
      endInput.oninput = () => {
        if (!endInput.value.trim()) effect.end_minute = null;
        else {
          const value = Number(endInput.value);
          if (!Number.isInteger(value) || value < 0) {
            invalidateMultiPlan("終了時刻は空欄または0以上の整数で指定してください。");
            return;
          }
          effect.end_minute = value;
        }
        invalidateMultiPlan();
      };
      addField("終了時刻（任意）", endInput);

      const targetInput = document.createElement("input");
      targetInput.type = "text";
      targetInput.maxLength = 64;
      targetInput.value = effect.target_selector?.target_id || "";
      targetInput.setAttribute("aria-label", `案${branchIndex + 1} 作用${effectIndex + 1} の対象ID`);
      targetInput.oninput = () => {
        effect.target_selector.target_id = targetInput.value || null;
        effect.target_selector.candidate_ids = [];
        invalidateMultiPlan();
      };
      addField(`対象ID · ${effect.target_selector?.kind || "対象"}`, targetInput);

      const parametersInput = document.createElement("textarea");
      parametersInput.rows = 2;
      parametersInput.value = JSON.stringify(effect.parameters || {}, null, 2);
      parametersInput.setAttribute("aria-label", `案${branchIndex + 1} 作用${effectIndex + 1} のtyped parameters JSON`);
      parametersInput.oninput = () => {
        try {
          const value = JSON.parse(parametersInput.value);
          if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("object required");
          effect.parameters = value;
          parametersInput.removeAttribute("aria-invalid");
          invalidateMultiPlan();
        } catch {
          parametersInput.setAttribute("aria-invalid", "true");
          invalidateMultiPlan("設定JSONに構文エラーがあります。");
        }
      };
      addField("設定（JSON）", parametersInput);

      const removeEffect = document.createElement("button");
      removeEffect.type = "button";
      removeEffect.textContent = "作用行を削除";
      removeEffect.disabled = alternative.plan.effects.length <= 1;
      removeEffect.onclick = () => {
        alternative.plan.effects.splice(effectIndex, 1);
        renderMultiPlanEditor();
        invalidateMultiPlan();
      };
      row.append(removeEffect);
      card.append(row);
    });
    container.append(card);
  });
  syncMultiPlanButtons();
}

function initializeMultiPlan(preview) {
  const plan = preview?.result?.draft_plan;
  if (!plan || preview.result.status !== "compiled" || !preview.validation?.valid) {
    state.nlMultiPlan = null;
    state.nlMultiValidation = null;
    state.nlMultiIdempotencyKey = null;
    return;
  }
  state.nlMultiPlan = {
    schema_version: 1,
    branch_origin: preview.branchId,
    source_state_hash: preview.source_state_hash,
    fork_at_minute: plan.fork_at_minute,
    parser_provenance: cloneJson(plan.compiler_provenance),
    branches: [{
      variant_id: "alternative-1",
      label: "比較案 1",
      plan: cloneJson(plan),
    }],
  };
  state.nlMultiValidation = null;
  state.nlMultiIdempotencyKey = null;
  state.nlMultiStatusText = "比較案を複製し、各作用を編集してから全案を再検証してください。";
}

function refreshNaturalLanguagePanel() {
  const branch = selectedDemoBranch();
  const ready = branch && ["paused", "completed"].includes(branch.status);
  const maxMinute = Math.max(0, (state.demo?.duration_minutes || 1) - 1);
  if (state.nlLastBranchId !== (branch?.branch_id || null)) {
    state.nlLastBranchId = branch?.branch_id || null;
    if (document.activeElement?.id !== "nl-fork-minute") {
      $("nl-fork-minute").value = String(Math.min(branch?.minute || 0, maxMinute));
    }
  }
  const inputChanged = state.nlPreview && (
    state.nlPreview.sourceText !== $("nl-text").value.trim() ||
    state.nlPreview.branchId !== branch?.branch_id ||
    state.nlPreview.selectedMinute !== Number($("nl-fork-minute").value) ||
    state.nlPreview.source_state_hash !== branch?.state_hash
  );
  if (inputChanged) {
    state.nlPreview = null;
    state.nlIdempotencyKey = null;
    state.nlMultiPlan = null;
    state.nlMultiValidation = null;
    state.nlMultiIdempotencyKey = null;
    state.nlStatusText = "入力またはBranch状態が変わりました。最新状態で再プレビューしてください。";
  }
  $("nl-fork-minute").max = String(maxMinute);
  $("nl-preview").disabled = !ready || state.nlBusy || !$("nl-text").value.trim();
  $("nl-approve").disabled = state.nlBusy || !ready || !state.nlPreview ||
    state.nlPreview.result.status !== "compiled" || !state.nlPreview.validation?.valid ||
    state.nlPreview.validation?.stale;
  $("nl-status").textContent = state.nlBusy
    ? "プレビューを準備しています…"
    : state.nlStatusText;
  renderNaturalLanguagePreview();
  renderMultiPlanEditor();
  $("nl-audit").textContent = state.demo?.natural_language_audit?.length
    ? JSON.stringify(state.demo.natural_language_audit, null, 2)
    : "自然言語の提案はまだありません。";
}

async function previewNaturalLanguage() {
  const branch = selectedDemoBranch();
  if (!branch || !["paused", "completed"].includes(branch.status) || state.nlBusy) return;
  const sourceText = $("nl-text").value.trim();
  const selectedMinute = Number($("nl-fork-minute").value);
  if (!sourceText) return;
  if (!Number.isInteger(selectedMinute) || selectedMinute < 0 || selectedMinute >= (state.demo?.duration_minutes || 0)) {
    state.nlStatusText = "分岐時刻はシナリオ終了より前の0以上の整数にしてください。";
    refreshNaturalLanguagePanel();
    return;
  }
  state.nlBusy = true;
  state.nlPreview = null;
  state.nlStatusText = "Opusまたはoffline ruleで作用案を準備しています…";
  refreshNaturalLanguagePanel();
  try {
    const response = await naturalLanguagePost("interpret", {
      text: sourceText,
      branch_id: branch.branch_id,
      selected_minute: selectedMinute,
    });
    const validation = await naturalLanguagePost("validate", {
      branch_id: branch.branch_id,
      preview_id: response.preview_id,
    });
    state.nlPreview = {
      ...response,
      validation,
      sourceText,
      branchId: branch.branch_id,
      selectedMinute,
    };
    initializeMultiPlan(state.nlPreview);
    state.nlIdempotencyKey = globalThis.crypto?.randomUUID?.() || `nl-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    state.nlStatusText = validation.valid
      ? "内容を確認してください。明示的に承認するまでBranchは作成されません。"
      : validation.stale
        ? "Branchが変わりました。最新状態で再プレビューしてください。"
        : "検証に通らないため承認できません。説明を確認してください。";
    setError("");
  } catch (error) {
    state.nlStatusText = "プレビューを作成できませんでした。Branchを一時停止し、入力と時刻を確認してください。";
    setError(error);
  } finally {
    state.nlBusy = false;
    refreshNaturalLanguagePanel();
  }
}

async function approveNaturalLanguagePreview() {
  const preview = state.nlPreview;
  if (!preview || state.nlBusy || !state.nlIdempotencyKey) return;
  if (!window.confirm("表示中の作用案を承認し、元のBranchを保ったまま新しいBranchを作成しますか？")) return;
  state.nlBusy = true;
  state.nlStatusText = "承認内容を検証してBranchを作成しています…";
  refreshNaturalLanguagePanel();
  try {
    const response = await naturalLanguagePost("apply", {
      source_branch_id: preview.branchId,
      preview_id: preview.preview_id,
      idempotency_key: state.nlIdempotencyKey,
    });
    state.nlPreview = null;
    state.nlIdempotencyKey = null;
    state.nlMultiPlan = null;
    state.nlMultiValidation = null;
    state.nlMultiIdempotencyKey = null;
    state.nlStatusText = `${response.branch_id} を作成しました。元のシナリオと両方を最後まで実行して比較できます。`;
    state.requestedDemoBranchPending = true;
    state.requestedBranchId = response.branch_id;
    state.demoBranchTouched = true;
    await refreshDemo();
    setError("");
  } catch (error) {
    // Keep the same key so a retry after a lost response cannot create a second branch.
    state.nlStatusText = "承認処理を完了できませんでした。内容が古い場合は再プレビューしてください。再試行は同じ承認キーを使います。";
    setError(error);
  } finally {
    state.nlBusy = false;
    refreshNaturalLanguagePanel();
  }
}

async function validateMultiPlan() {
  const plan = state.nlMultiPlan;
  if (!plan || state.nlBusy || plan.branches.length < 2) return;
  state.nlBusy = true;
  state.nlMultiValidation = null;
  state.nlMultiIdempotencyKey = null;
  state.nlMultiStatusText = "全案を共通のCheckpointへ再生して検証しています…";
  syncMultiPlanButtons();
  try {
    const validation = await naturalLanguagePost("multi-validate", {
      source_branch_id: plan.branch_origin,
      multi_plan: plan,
    });
    state.nlMultiValidation = validation;
    state.nlMultiStatusText = validation.valid
      ? `${plan.branches.length}案を検証しました。共通Checkpoint・seed・モデル設定を確認済みです。明示承認後にBranch群を作成します。`
      : validation.stale
        ? "元Branchが変わりました。自然言語プレビューからやり直してください。"
        : `全案の再検証に失敗しました: ${(validation.issues || []).join("; ")}`;
    if (validation.valid) {
      state.nlMultiIdempotencyKey = globalThis.crypto?.randomUUID?.()
        || `multi-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    setError("");
  } catch (error) {
    state.nlMultiStatusText = "全案を検証できませんでした。入力と元Branchを確認してください。";
    setError(error);
  } finally {
    state.nlBusy = false;
    syncMultiPlanButtons();
  }
}

async function approveMultiPlan() {
  const plan = state.nlMultiPlan;
  const validation = state.nlMultiValidation;
  if (!plan || !validation?.valid || !state.nlMultiIdempotencyKey || state.nlBusy) return;
  if (!window.confirm(`${plan.branches.length}つの比較案を承認し、共通の元Branchから比較用のBranch群を作成しますか？`)) return;
  state.nlBusy = true;
  state.nlMultiStatusText = "承認内容を再検証して比較Branch群を作成しています…";
  syncMultiPlanButtons();
  try {
    const response = await naturalLanguagePost("multi-apply", {
      source_branch_id: plan.branch_origin,
      approved_plan: {
        multi_plan: plan,
        approval_hash: validation.approval_hash,
        approval_method: "explicit_user_confirmation",
      },
      idempotency_key: state.nlMultiIdempotencyKey,
    });
    state.nlMultiStatusText = `${response.branches.length}案を作成しました。全案を同じ元Branchと比較できます。`;
    state.nlMultiPlan = null;
    state.nlMultiValidation = null;
    state.nlMultiIdempotencyKey = null;
    state.requestedDemoBranchPending = true;
    state.requestedBranchId = response.branches[0]?.branch_id || plan.branch_origin;
    state.demoBranchTouched = true;
    await refreshDemo();
    setError("");
  } catch (error) {
    // The same key and approval hash make a retry safe after a lost response.
    state.nlMultiStatusText = "承認処理を完了できませんでした。再試行は同じ承認キーを使います。";
    setError(error);
  } finally {
    state.nlBusy = false;
    syncMultiPlanButtons();
    refreshNaturalLanguagePanel();
  }
}

function modeSwitchHref() {
  const params = new URLSearchParams(location.search);
  if (state.routeMode === "demo" && state.precomputedReplay) {
    params.set("run_id", state.precomputedReplay.candidate_run_id);
    params.set("branch_id", state.precomputedReplay.candidate_branch_id);
    params.set("minute", String(state.precomputedReplay.fork_minute));
    return `/admin?${params.toString()}`;
  }
  const selectedRun = $("run-select")?.value || state.runId || state.requestedRunId;
  const liveDemoContext = state.routeMode === "demo"
    && Boolean(selectedDemoBranch())
    && !state.requestedRunId;
  if (liveDemoContext && !state.requestedRunId) params.delete("run_id");
  else if (selectedRun) params.set("run_id", selectedRun);

  const filteredBranch = $("branch-query")?.value.trim();
  const demoBranch = $("demo-branch")?.value;
  const branch = liveDemoContext
    ? demoBranch || state.requestedBranchId
    : state.runId
    ? filteredBranch || state.runManifest?.branch_id || state.requestedBranchId
      : $("admin-branch-select")?.value || filteredBranch || state.requestedBranchId || demoBranch;
  if (branch) params.set("branch_id", branch);

  let minute = null;
  if (liveDemoContext) {
    const demoMinute = selectedDemoBranch()?.minute;
    minute = state.demoBranchTouched || !Number.isInteger(state.requestedMinute)
      ? demoMinute
      : state.requestedMinute;
  }
  else if (state.runId) minute = Number($("minute-slider")?.value);
  else if (state.routeMode === "demo") minute = selectedDemoBranch()?.minute;
  else if ($("admin-snapshot-minute")?.value) minute = Number($("admin-snapshot-minute").value);
  if (!Number.isInteger(minute)) minute = Number(state.requestedMinute);
  if (Number.isInteger(minute) && minute >= 0) params.set("minute", String(minute));

  const target = state.routeMode === "admin" ? "/demo" : "/admin";
  const query = params.toString();
  return query ? `${target}?${query}` : target;
}

function refreshModeSwitch() {
  const link = $("mode-switch");
  link.href = modeSwitchHref();
}

function renderPrecomputedReplay(report) {
  state.precomputedReplay = report;
  const result = $("precomputed-replay-result");
  result.replaceChildren();
  const heading = document.createElement("h3");
  const firstDifference = report.first_divergence_minute === null
    ? "観測差なし"
    : minuteText(report.first_divergence_minute);
  heading.textContent = `Day 4 ${String(Math.floor((report.fork_minute % 1440) / 60)).padStart(2, "0")}:${String(report.fork_minute % 60).padStart(2, "0")} に分岐 · 最初の観測差 ${firstDifference}`;
  result.append(heading);
  const explanation = document.createElement("p");
  explanation.textContent = report.interpretation || report.note || "同じseedで行った合成シナリオの比較です。";
  result.append(explanation);
  const metrics = document.createElement("ul");
  const labels = {
    toilet_queue_length: "トイレ待ち",
    toilet_working_units: "使用可能なトイレ",
    stress_mean: "平均ストレス",
    staff_load_ratio: "スタッフ稼働率",
  };
  for (const [key, values] of Object.entries(report.final_metric_differences || {})) {
    if (!labels[key] || !Number.isFinite(values.delta)) continue;
    const item = document.createElement("li");
    const delta = Number(values.delta);
    item.textContent = `${labels[key]}: ${delta > 0 ? "+" : ""}${delta.toFixed(1)}（故障案 − baseline）`;
    metrics.append(item);
  }
  result.append(metrics);
  $("precomputed-replay-status").textContent = `固定offlineリプレイ · ${report.agent_count}人 · ${report.duration_minutes / 1440}日 · API要求 ${report.api_request_count}回`;
  const admin = $("precomputed-replay-admin");
  const params = new URLSearchParams({
    run_id: report.candidate_run_id,
    branch_id: report.candidate_branch_id,
    minute: String(report.fork_minute),
  });
  admin.href = `/admin?${params.toString()}`;
  admin.hidden = false;
  result.hidden = false;
  refreshModeSwitch();
}

async function loadPrecomputedReplay() {
  const status = $("precomputed-replay-status");
  status.textContent = "固定リプレイを読み込み中…";
  try {
    renderPrecomputedReplay(await api("/api/v1/demo/precomputed-comparison"));
  } catch (error) {
    state.precomputedReplay = null;
    status.textContent = "固定リプレイを読み込めません。READMEのdemo-replay起動手順を確認してください。";
    refreshModeSwitch();
  }
}

function selectedScenarioPreset() {
  return state.scenarioPresets.find((preset) => preset.preset_id === $("scenario-preset").value) || null;
}

function scenarioEditorRequest() {
  const preset = selectedScenarioPreset();
  if (!preset) return null;
  const overrides = {};
  const capabilities = preset.capabilities || {};
  const integerInput = (id) => {
    const input = $(id);
    if (!input.value.trim() || !Number.isInteger(Number(input.value))) return null;
    return Number(input.value);
  };
  if (capabilities.staff_count) {
    const value = integerInput("scenario-staff-count");
    if (value === null) return null;
    if (value !== preset.staff_count) overrides.staff_count = value;
  }
  if (capabilities.toilet_units) {
    const value = integerInput("scenario-toilet-units");
    if (value === null) return null;
    if (value !== preset.toilet_units) overrides.toilet_units = value;
  }
  if (capabilities.inventory) {
    const water = integerInput("scenario-water-ml");
    const food = integerInput("scenario-food-units");
    if (water === null || food === null) return null;
    if (water !== preset.water_ml) overrides.water_ml = water;
    if (food !== preset.food_meal_units) overrides.food_meal_units = food;
  }
  const presetEventChoice = preset.scheduled_event_count ? "preset" : "disabled";
  if (capabilities.event_frequency && $("scenario-event-frequency").value !== presetEventChoice) {
    overrides.event_frequency = $("scenario-event-frequency").value;
  }
  if (capabilities.social_propagation) {
    const enabled = $("scenario-social-enabled").checked;
    if (enabled !== Boolean(preset.social_propagation_enabled)) {
      overrides.social_propagation_enabled = enabled;
    }
  }
  const policy = $("scenario-behavior-policy").value;
  if (policy !== "preset" && policy !== preset.behavior_policy) {
    overrides.behavior_policy = policy;
  }
  return { schema_version: 1, preset_id: preset.preset_id, overrides };
}

function renderScenarioPreset(preset, { setValues = false } = {}) {
  if (!preset) return;
  const capabilities = preset.capabilities || {};
  for (const [id, enabled] of [
    ["scenario-staff-count", capabilities.staff_count],
    ["scenario-toilet-units", capabilities.toilet_units],
    ["scenario-water-ml", capabilities.inventory],
    ["scenario-food-units", capabilities.inventory],
    ["scenario-event-frequency", capabilities.event_frequency],
    ["scenario-behavior-policy", true],
    ["scenario-social-enabled", capabilities.social_propagation],
  ]) $(id).disabled = !enabled || Boolean(state.demo?.started) || state.scenarioEditorBusy;
  if (setValues) {
    $("scenario-staff-count").value = String(preset.staff_count);
    $("scenario-toilet-units").value = String(preset.toilet_units);
    $("scenario-water-ml").value = String(preset.water_ml ?? 0);
    $("scenario-food-units").value = String(preset.food_meal_units ?? 0);
    $("scenario-event-frequency").value = preset.scheduled_event_count ? "preset" : "disabled";
    $("scenario-behavior-policy").value = "preset";
    $("scenario-social-enabled").checked = Boolean(preset.social_propagation_enabled);
  }
  const groupSize = state.scenarioPresets.filter((item) =>
    item.comparison_group === preset.comparison_group,
  ).length;
  $("scenario-editor-status").textContent = `${preset.agent_count}人 · seed ${preset.seed} · ${preset.duration_minutes / 1440}日 · ${preset.scheduled_event_count}件の予定イベント · 同じseed・基盤のプリセット ${groupSize}件`;
  state.scenarioPreview = null;
  state.scenarioPreviewRequest = null;
  renderScenarioEditor();
}

function renderScenarioEditor() {
  if (!state.scenarioPresets.length || !state.demo) return;
  const locked = Boolean(state.demo.started || state.scenarioEditorBusy);
  const request = scenarioEditorRequest();
  const requestKey = request ? JSON.stringify(request) : null;
  $("scenario-preview").disabled = locked || !request;
  $("scenario-freeze").disabled = locked || !state.scenarioPreview || state.scenarioPreviewRequest !== requestKey;
  if (state.demo.started) {
    $("scenario-editor-status").textContent = `Run ${state.demo.run_id || ""} は開始済みです。設定は固定されています。`;
  } else if (state.scenarioEditorBusy) {
    $("scenario-editor-status").textContent = "シナリオ条件を検証しています…";
  }
}

async function loadScenarioPresets() {
  const response = await api("/api/v1/scenario-presets");
  state.scenarioPresets = response.presets || [];
  const select = $("scenario-preset");
  select.replaceChildren();
  for (const preset of state.scenarioPresets) {
    select.add(new Option(
      `${preset.preset_id} · ${preset.agent_count}人 · seed ${preset.seed}`,
      preset.preset_id,
    ));
  }
  const selectedId = state.demo?.scenario_manifest?.preset_id || "failure_day3";
  const initial = state.scenarioPresets.find((preset) => preset.preset_id === selectedId)
    || state.scenarioPresets[0];
  if (initial) {
    select.value = initial.preset_id;
    const manifest = state.demo?.scenario_manifest;
    renderScenarioPreset(initial, { setValues: !manifest });
    if (manifest) renderSavedScenarioManifest(manifest);
  }
}

function renderSavedScenarioManifest(manifest) {
  const fields = manifest.fields || {};
  const values = {
    "scenario-staff-count": fields.staff_count?.value,
    "scenario-toilet-units": fields.toilet_units?.value,
    "scenario-water-ml": fields.water_ml?.value,
    "scenario-food-units": fields.food_meal_units?.value,
    "scenario-event-frequency": fields.event_frequency?.value,
    "scenario-behavior-policy": fields.behavior_policy?.origin === "operator"
      ? fields.behavior_policy.value
      : "preset",
    "scenario-social-enabled": fields.social_propagation_enabled?.value,
  };
  for (const [id, value] of Object.entries(values)) {
    if (value === null || value === undefined) continue;
    if (id === "scenario-social-enabled") $(id).checked = Boolean(value);
    else $(id).value = String(value);
  }
  renderScenarioPreview(manifest);
  $("scenario-editor-status").textContent = `復元済み · ${manifest.run_id} · 作成 ${manifest.created_at}`;
  state.scenarioPreview = null;
  state.scenarioPreviewRequest = null;
  renderScenarioEditor();
}

function renderScenarioPreview(preview) {
  const familyMatches = state.scenarioPresets.filter((preset) =>
    preset.comparison_group === `${preview.seed}:${preview.scenario_family_hash}`,
  );
  const lines = [
    `新規ライブ計算 · ${preview.agent_count}人 · ${preview.duration_minutes / 1440}日 · seed ${preview.seed}`,
    `設定hash: ${preview.scenario_hash}`,
    preview.run_id
      ? `Run ID: ${preview.run_id} · 作成: ${preview.created_at || "時刻不明"}`
      : "Run IDは固定時に個別発行されます。設定値のorigin: operator=人が変更、preset=選択プリセットから継承。",
    `同じseed・基盤の比較候補: ${familyMatches.map((item) => item.preset_id).join(", ") || "この組み合わせの他プリセットなし"}`,
    `モデル仮定ID: ${(preview.model_assumption_ids || []).join(", ") || "記録なし"}`,
    `Agent行動Policy: ${preview.behavior_policy || "未記録"} · ${preview.fields.behavior_policy?.origin || "由来未記録"}`,
    ...Object.entries(preview.fields).map(([key, field]) => `${key}: ${field.value ?? "対象外"} · ${field.origin}`),
  ];
  $("scenario-editor-summary").textContent = lines.join("\n");
  $("scenario-editor-summary").hidden = false;
}

async function previewScenarioDraft() {
  const request = scenarioEditorRequest();
  if (!request) return;
  state.scenarioEditorBusy = true;
  state.scenarioPreview = null;
  renderScenarioEditor();
  try {
    const result = await demoPost("scenario-preview", request);
    state.scenarioPreview = result.preview;
    state.scenarioPreviewRequest = JSON.stringify(request);
    renderScenarioPreview(result.preview);
    $("scenario-editor-status").textContent = "入力を検証しました。表示中の内容で新しいRunを固定できます。";
    setError("");
  } catch (error) {
    $("scenario-editor-status").textContent = "入力を検証できません。現在のシナリオは変更されていません。";
    setError(error);
  } finally {
    state.scenarioEditorBusy = false;
    renderScenarioEditor();
  }
}

async function freezeScenarioDraft() {
  const request = scenarioEditorRequest();
  if (!request || !state.scenarioPreview || state.scenarioPreviewRequest !== JSON.stringify(request)) return;
  state.scenarioEditorBusy = true;
  renderScenarioEditor();
  try {
    const result = await demoPost("scenario-freeze", request);
    await refreshDemo(result.state);
    renderScenarioPreview(result.scenario);
    $("scenario-editor-status").textContent = `固定しました · ${result.scenario.run_id} · 作成 ${result.scenario.created_at} · 新しいライブ計算を開始できます。`;
    setError("");
  } catch (error) {
    $("scenario-editor-status").textContent = "固定に失敗しました。現在のシナリオは変更されていません。";
    setError(error);
  } finally {
    state.scenarioEditorBusy = false;
    renderScenarioEditor();
  }
}

function setAdminTab(tabName, focus = false) {
  const tabs = [...document.querySelectorAll("[data-admin-tab]")];
  const active = tabs.some((tab) => tab.dataset.adminTab === tabName) ? tabName : "overview";
  state.activeAdminTab = active;
  for (const tab of tabs) {
    const selected = tab.dataset.adminTab === active;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focus) tab.focus();
  }
  for (const panel of document.querySelectorAll("[data-admin-panel]")) {
    panel.hidden = panel.dataset.adminPanel !== active;
  }
}

function setupAdminTabs() {
  const tabs = [...document.querySelectorAll("[data-admin-tab]")];
  for (const tab of tabs) {
    tab.addEventListener("click", () => setAdminTab(tab.dataset.adminTab));
    tab.addEventListener("keydown", (event) => {
      const index = tabs.indexOf(tab);
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length
        : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length
          : event.key === "Home" ? 0
            : event.key === "End" ? tabs.length - 1 : index;
      if (next !== index) {
        event.preventDefault();
        setAdminTab(tabs[next].dataset.adminTab, true);
      }
    });
  }
}

function setupMode() {
  const adminMode = location.pathname === "/admin";
  state.routeMode = adminMode ? "admin" : "demo";
  const params = new URLSearchParams(location.search);
  state.requestedRunId = params.get("run_id");
  state.requestedBranchId = params.get("branch_id");
  state.requestedDemoBranchPending = state.requestedBranchId !== null;
  const requestedMinute = params.get("minute");
  state.requestedMinute = requestedMinute !== null && /^\d+$/.test(requestedMinute) ? Number(requestedMinute) : null;
  document.body.dataset.mode = state.routeMode;
  document.title = adminMode ? "記録・判断を検証 · ShelterSim" : "避難所運営シナリオ · ShelterSim";
  $("page-eyebrow").textContent = adminMode ? "SHELTER SIM / ADMIN" : "SHELTER SIM / DEMO";
  $("page-title").textContent = adminMode ? "記録・判断を検証" : "避難所運営シナリオ";
  $("mode-description").textContent = adminMode ? "保存済みRun・参照専用" : "合成シナリオ・デモモード";
  $("mode-switch").textContent = adminMode ? "デモへ戻る" : "検証・管理へ";
  $("demo-branch-label").textContent = adminMode ? "操作対象Branch" : "操作する案";
  $("demo-fork").textContent = adminMode ? "staff-plusへ分岐" : "対応案を作る";
  $("demo-finish").textContent = adminMode ? "選択Branchを最後まで実行" : "選んだ案を最後まで実行";
  $("demo-compare").textContent = adminMode ? "baseline / 選択案 比較" : "元のシナリオ / 選択案を比較";
  $("branch-query").value = state.requestedBranchId || "";
  if (adminMode) setAdminTab("overview");
  setupAdminTabs();
  refreshModeSwitch();
}

async function initialize() {
  await refreshDemo();
  try { await loadScenarioPresets(); } catch (error) { setError(error); }
  await loadPrecomputedReplay();
  if (state.routeMode === "demo") void resumeActiveDemoJob();
  renderAdminBranchOptions();
  try {
    const result = await api("/api/v1/runs");
    const select = $("run-select");
    select.replaceChildren();
    if (!result.runs.length) {
      select.add(new Option("runs/ に保存済みRunがありません", ""));
      state.runId = null;
      state.runManifest = null;
      refreshModeSwitch();
      return;
    }
    for (const run of result.runs) {
      select.add(new Option(`${run.scenario_id || run.id} · seed ${run.seed ?? "?"} · ${run.id}`, run.id));
    }
    const liveBranchContext = state.routeMode === "admin"
      && !state.requestedRunId
      && Boolean(state.requestedBranchId);
    if (liveBranchContext) select.add(new Option("保存Runを選択…", ""));
    if (result.runs.some((run) => run.id === state.requestedRunId)) select.value = state.requestedRunId;
    else if (liveBranchContext) select.value = "";
    else select.value = result.runs[0].id;
    state.runId = select.value;
    if (!state.runId) {
      state.runManifest = null;
      $("run-summary").textContent = "保存Runなし · 未保存BranchのCheckpointスナップショットを表示中";
      $("run-overview").textContent = "保存Runは選択されていません。未保存BranchはBranch / Compareタブを参照してください。";
      $("run-branch-lineage").textContent = "保存Runは選択されていません。";
      $("run-audit").textContent = "保存Runは選択されていません。";
      refreshModeSwitch();
      return;
    }
    await loadRun();
  } catch (error) { setError(error); }
}

async function refreshDemo(document = null) {
  try {
    const [demoState, jobState] = await Promise.all([
      document ? Promise.resolve(document) : api("/api/v1/demo"),
      api("/api/v1/jobs"),
    ]);
    state.demo = demoState;
    state.demoJobs = jobState.jobs || [];
    renderScenarioEditor();
    const select = $("demo-branch");
    const previous = select.value;
    select.replaceChildren();
    if (!state.demo.branches.length) select.add(new Option("未開始", ""));
    for (const branch of state.demo.branches) {
      const label = state.routeMode === "admin"
        ? branch.branch_id
        : branch.branch_id === "baseline"
          ? "元のシナリオ"
          : branch.approved_plan
            ? "生成した対応案"
          : branch.multi_plan?.label
            ? branch.multi_plan.label
            : branch.branch_id === "staff-plus"
              ? "対応案"
              : "分岐案";
      select.add(new Option(`${label} · ${branch.status} · ${minuteText(branch.minute)}`, branch.branch_id));
    }
    const requestedBranch = state.requestedDemoBranchPending ? state.requestedBranchId : null;
    const activeJobBranch = state.demoJobs.find((job) =>
      ["queued", "running", "cancelling"].includes(job.status),
    )?.branch_id;
    const preferredBranch = [requestedBranch, previous, activeJobBranch].find((branchId) =>
      state.demo.branches.some((item) => item.branch_id === branchId),
    );
    select.value = preferredBranch || state.demo.branches[0]?.branch_id || "";
    state.requestedDemoBranchPending = false;
    const selected = state.demo.branches.find((item) => item.branch_id === select.value);
    if (state.routeMode === "demo" && state.demoBranchTouched && selected) {
      state.requestedMinute = selected.minute;
      state.requestedBranchId = selected.branch_id;
    }
    const zoneSelect = $("demo-zone");
    const previousZone = zoneSelect.value;
    zoneSelect.replaceChildren();
    for (const zone of state.demo.zones || []) zoneSelect.add(new Option(`${zone.name} · ${zone.id}`, zone.id));
    zoneSelect.value = (state.demo.zones || []).some((zone) => zone.id === previousZone) ? previousZone : state.demo.default_toilet_zone_id;
    const checkpointSelect = $("demo-checkpoint-select");
    checkpointSelect.replaceChildren(new Option("最新Checkpoint", ""));
    for (const checkpointId of selected?.checkpoint_ids || []) checkpointSelect.add(new Option(checkpointId, checkpointId));
    $("demo-progress").max = String(state.demo.duration_minutes || 10080);
    $("demo-progress").value = String(selected?.minute || 0);
    $("demo-execution-mode").textContent = state.demo.execution_mode === "live_compute"
      ? "ライブ計算 · オフライン・LLM未使用"
      : "実行モードを確認できません";
    const latestJob = state.demoJobs.find((job) => job.branch_id === selected?.branch_id);
    const selectedLabel = state.routeMode === "admin"
      ? selected?.branch_id
      : selected?.branch_id === "baseline"
        ? "元のシナリオ"
        : selected?.approved_plan
          ? "生成した対応案"
        : selected?.multi_plan?.label
          ? selected.multi_plan.label
          : selected?.branch_id === "staff-plus"
            ? "対応案"
            : "シナリオ";
    const branchStatus = selected
      ? `${selectedLabel} · ${selected.status === "completed" ? "完了" : selected.status === "paused" ? "一時停止" : selected.status === "running" ? "実行中" : "待機中"} · ${minuteText(selected.minute)}`
      : "デモを開始すると進行状況を表示します。";
    $("demo-branch-status").textContent = !state.demoBusy && latestJob && ["interrupted", "failed"].includes(latestJob.status)
      ? branchStatus + " · 前回計算の状態: " + latestJob.status + " — " + latestJob.error
      : branchStatus;
    $("demo-start").disabled = state.demo.started || state.demoBusy;
    $("demo-day3").disabled = !selected || state.demoBusy || selected.minute >= 2880 || selected.status === "completed";
    $("demo-pause").disabled = !selected || (!state.demoBusy && selected.status !== "running");
    $("demo-pause").textContent = state.demoBusy ? "計算をキャンセル" : "一時停止";
    $("demo-resume").disabled = !selected || selected.status !== "paused" || state.demoBusy;
    $("demo-checkpoint").disabled = !selected || selected.status !== "paused" || state.demoBusy;
    $("demo-rewind").disabled = !selected || selected.status !== "paused" || state.demoBusy;
    $("demo-fork").disabled = !selected || selected.status !== "paused" || state.demoBusy;
    $("demo-staff").disabled = !selected || selected.status !== "paused" || state.demoBusy;
    $("demo-toilet").disabled = !selected || selected.status !== "paused" || state.demoBusy;
    $("demo-finish").disabled = !selected || selected.status === "completed" || state.demoBusy;
    const baselineCompleted = state.demo.branches.some((item) => item.branch_id === "baseline" && item.status === "completed");
    const candidateCompleted = selected?.branch_id !== "baseline" && selected?.status === "completed";
    $("demo-compare").disabled = !baselineCompleted || !candidateCompleted;
    $("demo-story-review").disabled = !baselineCompleted || !candidateCompleted || state.demoBusy;
    $("demo-compare").textContent = state.routeMode === "admin" ? "baseline / 選択案 比較" : "元のシナリオ / 選択案を比較";
    renderLayoutEditor(selected);
    renderBranchTree();
    renderMultiCompareOptions();
    refreshNaturalLanguagePanel();
    $("demo-branches").textContent = JSON.stringify({ scenario_id: state.demo.scenario_id, seed: state.demo.seed, offline: state.demo.offline, branches: state.demo.branches }, null, 2);
    $("demo-operations").textContent = state.demo.operations.length ? JSON.stringify(state.demo.operations.slice().reverse(), null, 2) : "操作ログはありません。";
    renderDemoLayout();
    if (!state.demo.branches.length) {
      $("demo-comparison").textContent = "両Branchの完了後に比較できます。";
      $("demo-comparison-summary").textContent = "元のシナリオと対応案を最後まで動かして、比較できます。";
      $("demo-story-review-status").textContent = "Opusレビューは押したときだけ実行します。";
      renderStoryCard(null);
    }
    renderScenarioEditor();
    refreshModeSwitch();
  } catch (error) { setError(error); }
}

async function demoAction(action, payload = {}) {
  state.demoBranchTouched = true;
  try {
    const document = await demoPost(action, payload);
    await refreshDemo(document);
    setError("");
    return document;
  } catch (error) { setError(error); return null; }
}

async function pollDemoJob(initialJob) {
  state.demoJobId = initialJob.job_id;
  let job = initialJob;
  while (["queued", "running", "cancelling"].includes(job.status)) {
    await refreshDemo();
    const mode = job.status === "cancelling" ? "キャンセル処理中" : "ライブ計算";
    $("demo-branch-status").textContent =
      mode + " · " + job.percent + "% · " + minuteText(job.minute) + " / " + minuteText(job.target_minute);
    await new Promise((resolve) => setTimeout(resolve, 1000));
    job = await api("/api/v1/jobs/" + encodeURIComponent(state.demoJobId));
  }
  await refreshDemo();
  if (job.status === "failed" || job.status === "interrupted") {
    throw new Error(job.error || "job " + job.status);
  }
  if (job.status === "cancelled") {
    $("demo-branch-status").textContent = "キャンセル済み · " + minuteText(job.minute) + " で再開できます";
  }
}

async function runDemoJob(branchId, targetMinute) {
  if (state.demoBusy) return;
  state.demoBranchTouched = true;
  state.demoBusy = true;
  try {
    await refreshDemo();
    const accepted = await demoPost("advance-job", { branch_id: branchId, target_minute: targetMinute });
    await pollDemoJob(accepted);
    setError("");
  } catch (error) { setError(error); }
  finally { state.demoBusy = false; state.demoJobId = null; await refreshDemo(); }
}

async function resumeActiveDemoJob() {
  if (state.demoBusy) return;
  const selected = selectedDemoBranch();
  const job = state.demoJobs.find((item) =>
    item.branch_id === selected?.branch_id && ["queued", "running", "cancelling"].includes(item.status),
  );
  if (!job) return;
  state.demoBusy = true;
  try { await pollDemoJob(job); }
  catch (error) { setError(error); }
  finally { state.demoBusy = false; state.demoJobId = null; await refreshDemo(); }
}

async function finishSelectedDemoBranch() {
  const branchId = $("demo-branch").value;
  if (!branchId) return;
  const branch = state.demo.branches.find((item) => item.branch_id === branchId);
  if (branch) await runDemoJob(branchId, branch.duration_minutes);
}

function selectedDemoBranch() { return state.demo?.branches.find((item) => item.branch_id === $("demo-branch").value); }

function renderLayoutEditor(branch) {
  const edgeSelect = $("demo-layout-edge");
  const priorEdge = edgeSelect.value;
  edgeSelect.replaceChildren();
  for (const edge of branch?.route_edges || []) {
    edgeSelect.add(new Option(`${edge.id} · ${edge.from_zone_id} → ${edge.to_zone_id}`, edge.id));
  }
  edgeSelect.value = (branch?.route_edges || []).some((edge) => edge.id === priorEdge)
    ? priorEdge : branch?.route_edges?.[0]?.id || "";
  const edge = branch?.route_edges?.find((item) => item.id === edgeSelect.value);
  $("demo-layout-travel").value = edge ? String(edge.travel_minutes) : "";
  $("demo-layout-capacity").value = edge?.max_agents_in_transit == null
    ? "" : String(edge.max_agents_in_transit);
  $("demo-layout-closed").checked = Boolean(edge?.closed);

  const facilitySelect = $("demo-layout-facility");
  const priorFacility = facilitySelect.value;
  facilitySelect.replaceChildren();
  for (const facility of branch?.facility_locations || []) {
    facilitySelect.add(new Option(`${facility.id} · ${facility.kind} · ${facility.zone_id}`, facility.id));
  }
  facilitySelect.value = (branch?.facility_locations || []).some((item) => item.id === priorFacility)
    ? priorFacility : branch?.facility_locations?.[0]?.id || "";
  const zones = state.demo?.zones || [];
  const zoneSelect = $("demo-layout-zone");
  const currentZone = branch?.facility_locations?.find((item) => item.id === facilitySelect.value)?.zone_id;
  const previousZone = zoneSelect.value;
  zoneSelect.replaceChildren();
  for (const zone of zones) zoneSelect.add(new Option(`${zone.name} · ${zone.id}`, zone.id));
  zoneSelect.value = zones.some((zone) => zone.id === previousZone)
    ? previousZone : currentZone || zones[0]?.id || "";

  const canEdit = branch?.status === "paused" && !state.demoBusy;
  for (const id of ["demo-layout-edge", "demo-layout-travel", "demo-layout-capacity", "demo-layout-closed", "demo-layout-route-apply", "demo-layout-facility", "demo-layout-zone", "demo-layout-facility-apply"]) {
    $(id).disabled = !canEdit;
  }
  $("demo-layout-status").textContent = !branch
    ? "デモを開始してください。"
    : branch.status !== "paused"
      ? "選択中の案を一時停止すると、変更を子Branchとして記録できます。"
      : "表示位置の変更はブラウザ内だけです。ここで適用する経路・設備変更だけが子Branchのシミュレーション条件になります。";
}

function demoLayoutMap() {
  const layout = state.demo?.layout;
  if (!layout) return null;
  const branch = selectedDemoBranch();
  const scenarioZones = new Map((state.demo.zones || []).map((zone) => [zone.id, zone]));
  const zones = layout.zones.map((zone) => {
    const x = zone.polygon.reduce((sum, point) => sum + point[0], 0) / zone.polygon.length;
    const y = zone.polygon.reduce((sum, point) => sum + point[1], 0) / zone.polygon.length;
    const scenarioZone = scenarioZones.get(zone.zone_id);
    return {
      id: zone.zone_id,
      name: zone.label,
      modelled: zone.modelled,
      polygon: zone.polygon,
      x,
      y,
      capacity: scenarioZone?.capacity ?? null,
      occupancy: 0,
    };
  });
  const centers = new Map(zones.map((zone) => [zone.id, zone]));
  const liveEdges = new Map((branch?.route_edges || []).map((edge) => [edge.id, edge]));
  const edges = layout.route_edges.map((edge) => {
    const from = centers.get(edge.from_zone_id);
    const to = centers.get(edge.to_zone_id);
    const live = liveEdges.get(edge.edge_id);
    return {
      id: edge.edge_id,
      from_zone_id: edge.from_zone_id,
      to_zone_id: edge.to_zone_id,
      x1: from?.x,
      y1: from?.y,
      x2: to?.x,
      y2: to?.y,
      travel_minutes: live?.travel_minutes ?? null,
      max_agents_in_transit: live?.max_agents_in_transit ?? null,
      closed: live?.closed ?? false,
    };
  });
  const branchFacilities = new Map((branch?.facility_locations || []).map((item) => [item.id, item]));
  const facilities = layout.facilities.map((item) => {
    const live = branchFacilities.get(item.facility_id);
    const relocated = live && live.zone_id !== item.zone_id;
    const displayZoneId = relocated ? live.zone_id : item.display_zone_id;
    const displayZone = centers.get(displayZoneId);
    return {
      id: item.facility_id,
      kind: live?.kind || "facility",
      zone_id: live?.zone_id || item.zone_id,
      display_zone_id: displayZoneId,
      x: relocated ? displayZone?.x ?? item.point[0] : item.point[0],
      y: relocated ? displayZone?.y ?? item.point[1] : item.point[1],
      capacity: live?.capacity ?? 1,
      working_units: live?.working_units ?? 1,
      status: live?.working_units === 0 ? "stopped" : "active",
    };
  });
  return {
    layout: { id: layout.id, title: layout.title, source: layout.source, note: layout.note, width: layout.width, height: layout.height },
    zones,
    edges,
    facilities,
    agents: [],
    queue_summary: null,
    synthetic_layout: true,
  };
}

async function createLayoutBranch(commands) {
  const branch = selectedDemoBranch();
  if (!branch) return;
  if (branch.status !== "paused") {
    setError("経路や設備の変更前に、選択中の案を一時停止してください。");
    return;
  }
  try {
    const idempotencyKey = typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `layout-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const result = await demoPost("layout-branch", {
      source_branch_id: branch.branch_id,
      idempotency_key: idempotencyKey,
      commands,
    });
    state.requestedBranchId = result.branch_id;
    state.requestedDemoBranchPending = true;
    state.demoBranchTouched = true;
    await refreshDemo();
    $("demo-branch").value = result.branch_id;
    await refreshDemo();
    setError("");
  } catch (error) { setError(error); }
}

function renderDemoLayout() {
  if (state.runId || state.routeMode !== "admin") return;
  const map = demoLayoutMap();
  if (map) drawMap(map);
}

function renderBranchTree() {
  const container = $("demo-branch-tree");
  const branches = state.demo?.branches || [];
  container.replaceChildren();
  if (!branches.length) {
    container.textContent = "デモ開始前です。";
    return;
  }
  const byId = new Map(branches.map((branch) => [branch.branch_id, branch]));
  const children = new Map();
  for (const branch of branches) {
    const parent = byId.has(branch.parent_branch_id) ? branch.parent_branch_id : null;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(branch);
  }
  const visited = new Set();
  const appendBranch = (branch, depth) => {
    if (visited.has(branch.branch_id)) return;
    visited.add(branch.branch_id);
    const metadata = branch.multi_plan;
    const label = metadata?.label ? ` · ${metadata.label}` : "";
    const node = document.createElement("div");
    node.className = depth ? "branch-tree-node child" : "branch-tree-node";
    node.style.marginLeft = `${depth * 16}px`;
    node.textContent = `${branch.branch_id}${label} · ${branch.status} · ${minuteText(branch.minute)}`;
    container.append(node);
    for (const child of children.get(branch.branch_id) || []) appendBranch(child, depth + 1);
  };
  for (const root of children.get(null) || []) appendBranch(root, 0);
  for (const branch of branches) appendBranch(branch, 0);
}

function renderMultiCompareOptions() {
  const select = $("demo-multi-compare-branches");
  const button = $("demo-multi-compare");
  select.replaceChildren();
  const selected = selectedDemoBranch();
  const metadata = selected?.multi_plan;
  if (!metadata?.multi_plan_id || !metadata.source_branch_id) {
    select.add(new Option("MultiPlanの代替案を選択してください", ""));
    button.disabled = true;
    return;
  }
  const alternatives = (state.demo?.branches || []).filter(
    (branch) => branch.multi_plan?.multi_plan_id === metadata.multi_plan_id,
  );
  for (const branch of alternatives) {
    const option = new Option(
      `${branch.multi_plan.label} · ${branch.status} · ${branch.branch_id}`,
      branch.branch_id,
    );
    option.selected = branch.status === "completed";
    select.add(option);
  }
  button.disabled = alternatives.filter((branch) => branch.status === "completed").length < 2;
  button.textContent = `選択案をまとめて比較（元 ${metadata.source_branch_id}）`;
}

function renderAdminBranchOptions() {
  const branches = state.demo?.branches || [];
  const branchSelect = $("admin-branch-select");
  const baselineSelect = $("compare-baseline");
  const candidateSelect = $("compare-candidate");
  for (const select of [branchSelect, baselineSelect, candidateSelect]) {
    select.replaceChildren();
    if (!branches.length) select.add(new Option("ライブBranchなし", ""));
    for (const branch of branches) {
      select.add(new Option(branch.branch_id + " · " + branch.status + " · " + minuteText(branch.minute), branch.branch_id));
    }
  }
  const preferred = [state.requestedBranchId, selectedDemoBranch()?.branch_id]
    .find((id) => branches.some((branch) => branch.branch_id === id));
  branchSelect.value = preferred || branches[0]?.branch_id || "";
  baselineSelect.value = branches.find((branch) => branch.branch_id === "baseline")?.branch_id
    || branches[0]?.branch_id || "";
  candidateSelect.value = branches.find((branch) => branch.branch_id === "staff-plus")?.branch_id
    || branches.find((branch) => branch.branch_id !== baselineSelect.value)?.branch_id || "";
  const requestedMinute = state.requestedMinute;
  const selected = branches.find((branch) => branch.branch_id === branchSelect.value);
  $("admin-snapshot-minute").value = Number.isInteger(requestedMinute)
    ? String(requestedMinute)
    : String(selected?.minute ?? 0);
  $("admin-branch-snapshot").textContent = branches.length
    ? "Checkpointスナップショットを読み込みます。"
    : "デモに未保存のBranchはありません。保存済みRunの表示はOverview / WorldStateタブを使ってください。";
  if (branchSelect.value) void loadAdminSnapshot();
  refreshModeSwitch();
}

async function loadAdminSnapshot() {
  const branchId = $("admin-branch-select").value;
  if (!branchId) return;
  const params = new URLSearchParams({ branch_id: branchId });
  const minuteValue = $("admin-snapshot-minute").value;
  if (minuteValue !== "") params.set("minute", minuteValue);
  if ($("admin-snapshot-agent").value.trim()) params.set("agent_id", $("admin-snapshot-agent").value.trim());
  try {
    const snapshot = await api("/api/v1/demo/snapshot?" + params);
    $("admin-branch-snapshot").textContent = JSON.stringify(snapshot, null, 2);
    refreshModeSwitch();
  } catch (error) {
    $("admin-branch-snapshot").textContent = String(error.message || error);
  }
}

async function loadAdminComparison() {
  const baseline = $("compare-baseline").value;
  const candidate = $("compare-candidate").value;
  if (!baseline || !candidate) return;
  const params = new URLSearchParams({
    baseline_branch_id: baseline,
    candidate_branch_id: candidate,
  });
  try {
    const report = await api("/api/v1/demo/comparison?" + params);
    $("admin-comparison").textContent = JSON.stringify(report, null, 2);
  } catch (error) {
    $("admin-comparison").textContent = String(error.message || error);
  }
}

async function addDemoCommands(kind) {
  const branch = selectedDemoBranch();
  if (!branch) return;
  const commands = [];
  if (kind === "add_staff") {
    for (let index = 1; index <= 2; index += 1) {
      commands.push({ kind, staff_id: `demo-staff-${String(branch.staff_count + index).padStart(2, "0")}`, zone_id: $("demo-zone").value, skills: ["support"] });
    }
  } else {
    for (let index = 1; index <= 2; index += 1) {
      commands.push({ kind, facility_id: `demo-toilet-${String(branch.facility_count + index).padStart(2, "0")}`, zone_id: $("demo-zone").value, capacity: 1, working_units: 1 });
    }
  }
  await demoAction("commands", { branch_id: branch.branch_id, commands });
}

async function loadRun() {
  state.runId = $("run-select").value;
  if (!state.runId) return;
  try {
    const [overview, profiles, timeline, events, audit, resourceLedger] = await Promise.all([
      api(`/api/v1/runs/${encodeURIComponent(state.runId)}`),
      api(`/api/v1/runs/${encodeURIComponent(state.runId)}/profiles?limit=100`),
      api(`/api/v1/runs/${encodeURIComponent(state.runId)}/timeline?limit=1200`),
      api(`/api/v1/runs/${encodeURIComponent(state.runId)}/events?limit=100`),
      api(`/api/v1/runs/${encodeURIComponent(state.runId)}/audit?limit=100`),
      api(`/api/v1/runs/${encodeURIComponent(state.runId)}/resource-ledger`),
    ]);
    const lineage = await api("/api/v1/runs/" + encodeURIComponent(state.runId) + "/branch-lineage");
    const manifest = overview.manifest;
    state.runManifest = manifest;
    state.finalMinute = Number(manifest.final_minute ?? manifest.duration_minutes ?? 0);
    state.timeline = timeline.items;
    state.currentMetrics = null;
    state.selectedAgent = null;
    state.selectedZoneId = null;
    $("agent-id").value = "";
    const branch = manifest.branch_id ? ` · branch ${manifest.branch_id}` : "";
    $("run-summary").textContent = `${manifest.scenario_id || "scenario"} · seed ${manifest.seed} · ${overview.profile_count} Agents · ${overview.realized_event_count} realized events · ${manifest.execution_mode || "offline"}${branch}`;
    renderBehaviorPolicySummary(manifest);
    $("run-overview").textContent = JSON.stringify({
      manifest,
      generated_profiles: {
        count: overview.profile_count,
        provenance: manifest.scenario_provenance?.profile_generation || null,
        source_ids: manifest.scenario_input_source_ids || {},
      },
      planned_event_count: overview.planned_event_count,
      scheduled_event_source_ids: manifest.scheduled_event_source_ids || [],
      final_metrics: overview.final_metrics,
      resource_ledger: resourceLedger.enabled ? resourceLedger.ledger : null,
    }, null, 2);
    $("run-audit").textContent = JSON.stringify({
      llm_calls: audit.llm_calls,
      social_decisions: audit.social_decisions,
    }, null, 2);
    const callCount = audit.llm_calls.length;
    const socialCount = audit.social_decisions.length;
    const requestCount = manifest.llm_usage?.api_request_count;
    $("llm-coverage").textContent = callCount || socialCount
      ? "記録 " + callCount + " LLM calls / " + socialCount + " social decisions · API requests " + (requestCount ?? "未記録")
      : requestCount === 0
        ? "LLM利用記録なし · manifest の API request count は 0"
        : "LLM利用記録なし · このtraceから未使用とは断定できません";
    $("run-branch-lineage").textContent = JSON.stringify(lineage, null, 2);
    $("decision-coverage").textContent = "選択Runの時刻・Agent条件で記録済みの方針判断を表示します。LLM提案はLLMタブにあります。";
    const finalMinute = Math.max(0, state.finalMinute);
    const requestedMinute = state.requestedMinute;
    const selectedMinute = Number.isInteger(requestedMinute) && requestedMinute >= 0 && requestedMinute <= finalMinute
      ? requestedMinute
      : finalMinute;
    $("minute-slider").max = String(finalMinute);
    $("minute-slider").value = String(selectedMinute);
    $("minute-label").textContent = minuteText(selectedMinute);
    state.profilesOffset = 0;
    state.profileQuery = "";
    $("profile-query").value = "";
    renderProfiles(profiles);
    state.eventsOffset = 0;
    renderEvents(events);
    renderAdminBranchOptions();
    drawTimeline();
    await Promise.all([refreshMapAndAgent(), refreshTimeSelection(), loadAgentStories()]);
    await loadDecisionAudit();
    refreshModeSwitch();
    setError("");
  } catch (error) { setError(error); }
}

function renderProfiles(result) {
  state.profilesTotal = result.total;
  const body = $("profiles");
  body.replaceChildren();
  for (const profile of result.items) {
    const row = document.createElement("tr");
    row.dataset.agentId = profile.id;
    for (const value of [profile.id, profile.group_id || "—", profile.initial_zone_id, profile.mobility]) {
      const cell = document.createElement("td");
      cell.textContent = String(value ?? "—");
      row.append(cell);
    }
    row.addEventListener("click", () => selectAgent(profile.id));
    if (profile.id === state.selectedAgent) row.classList.add("selected");
    body.append(row);
  }
  const first = result.total === 0 ? 0 : result.offset + 1;
  const last = Math.min(result.total, result.offset + result.limit);
  $("profile-count").textContent = `${result.total} 件`;
  $("profiles-page").textContent = `${first}–${last} / ${result.total}`;
  $("profiles-prev").disabled = result.offset === 0;
  $("profiles-next").disabled = result.offset + result.limit >= result.total;
}

async function searchProfiles(offset = 0) {
  if (!state.runId) return;
  try {
    state.profilesOffset = offset;
    state.profileQuery = $("profile-query").value;
    const params = new URLSearchParams({ q: state.profileQuery, offset: String(offset), limit: "100" });
    const result = await api(`/api/v1/runs/${encodeURIComponent(state.runId)}/profiles?${params}`);
    renderProfiles(result);
  } catch (error) { setError(error); }
}

async function selectAgent(agentId) {
  state.selectedAgent = agentId;
  state.selectedZoneId = null;
  $("agent-id").value = agentId;
  for (const row of $("profiles").querySelectorAll("tr")) row.classList.toggle("selected", row.dataset.agentId === agentId);
  setAdminTab("decisions");
  await Promise.all([refreshMapAndAgent(), loadDecisionAudit()]);
}

async function loadAgent() {
  const agentId = $("agent-id").value.trim();
  if (!agentId || !state.runId) return;
  state.selectedAgent = agentId;
  await Promise.all([refreshMapAndAgent(), loadDecisionAudit()]);
}

async function refreshMapAndAgent() {
  if (!state.runId) return;
  const requestId = ++state.mapRequest;
  const minute = Number($("minute-slider").value);
  const base = `/api/v1/runs/${encodeURIComponent(state.runId)}`;
  const params = new URLSearchParams({ at_minute: String(minute) });
  if (state.selectedAgent) params.set("agent_id", state.selectedAgent);
  try {
    const map = await api(`${base}/map?${params}`);
    let agent = null;
    if (state.selectedAgent) {
      const agentParams = new URLSearchParams({ at_minute: String(minute) });
      agent = await api(`${base}/agents/${encodeURIComponent(state.selectedAgent)}?${agentParams}`);
    }
    if (requestId !== state.mapRequest) return;
    drawMap(map);
    if (agent) {
      $("agent-detail").textContent = JSON.stringify(agent, null, 2);
      const location = map.selected_agent;
      $("selected-location").textContent = location ? `${state.selectedAgent}: ${location.zone_id} · ${location.activity} · ${minuteText(minute)}` : "";
      const snapshot = agent.state || {};
      const needs = snapshot.needs || {};
      const decision = agent.decisions?.[agent.decisions.length - 1];
      const reason = decision ? `${decision.trigger || "記録済み"} → ${decision.selected_action || decision.action || "判断内容未記録"}` : "判断記録なし";
      const support = snapshot.support_status
        ? `支援 ${snapshot.support_status} · 待ち ${displayRecorded(snapshot.support_wait_minutes, "分")}`
        : "支援状態 未記録";
      const queue = snapshot.queue_kind
        ? `${snapshot.queue_kind}待ち ${displayRecorded(snapshot.queue_wait_minutes, "分")}`
        : "行列状態 未記録";
      $("map-agent-detail").textContent = `${agent.profile.id} · ${agent.profile.group_id || "グループ未設定"} · ${agent.profile.mobility || "属性未記録"} · ${snapshot.activity || "状態未記録"} · ストレス ${displayRecorded(snapshot.stress)} · トイレ ${displayRecorded(needs.toilet_urgency)} / 空腹 ${displayRecorded(needs.hunger)} / 疲労 ${displayRecorded(needs.fatigue)} · ${queue} · ${support} · 判断 ${reason} · ${minuteText(minute)}`;
    } else {
      $("agent-detail").textContent = "プロフィールかAgent IDを選ぶと記録を表示します。";
      const zone = state.lastMap?.zones.find((item) => item.id === state.selectedZoneId);
      $("selected-location").textContent = zone ? `${zone.name || zone.id} · ${minuteText(minute)}` : minuteText(minute);
      $("map-agent-detail").textContent = zone
        ? `この区画には選択時刻のAgent記録がありません · ${minuteText(minute)}`
        : "Agentの点または区画を選ぶと、選択時刻の記録を表示します。";
    }
    setError("");
  } catch (error) { setError(error); }
}

async function loadDecisionAudit() {
  if (!state.runId) return;
  const minute = Number($("minute-slider").value);
  const params = new URLSearchParams({ at_minute: String(minute), limit: "100" });
  if (state.selectedAgent) params.set("agent_id", state.selectedAgent);
  try {
    const decisions = await api("/api/v1/runs/" + encodeURIComponent(state.runId) + "/decisions?" + params);
    $("decision-audit").textContent = JSON.stringify({
      run_branch_id: state.runManifest?.branch_id || null,
      selected_agent_id: state.selectedAgent,
      at_minute: minute,
      trace_level: decisions.trace_level,
      available: decisions.available,
      total: decisions.total,
      items: decisions.items,
    }, null, 2);
    $("decision-coverage").textContent = !decisions.available
      ? "このRunに decisions.jsonl はありません。欠損記録を補完していません。"
      : decisions.total
        ? decisions.total + " records · 最新 " + decisions.items.length + "件 · " + minuteText(minute)
        : decisions.trace_level === "off"
          ? "trace_level=off のため方針判断の詳細は記録されていません。"
          : "この時刻・Agent条件に該当する判断記録はありません。";
  } catch (error) { setError(error); }
}

function renderBehaviorPolicySummary(manifest) {
  const container = $("behavior-policy-summary");
  container.replaceChildren();
  const behavior = manifest.model_assumptions?.behavior;
  if (!behavior || typeof behavior !== "object") {
    container.textContent = "このRunには行動Policyの設定記録がありません。値を推測していません。";
    return;
  }
  const active = document.createElement("p");
  active.textContent = `現在のvariant: ${behavior.variant || "未記録"} · version: ${behavior.policy_version || "未記録"} · 選択規則: ${behavior.selection_rule || "未記録"}`;
  container.append(active);
  const provenance = document.createElement("p");
  provenance.className = "muted";
  provenance.textContent = `Run ID: ${manifest.run_id || "未記録"} · Scenario hash: ${manifest.scenario_hash || "未記録"} · 仮定ID: ${behavior.source_id || "未記録"} · 設定値: ${JSON.stringify(behavior.values || {})}`;
  container.append(provenance);
  const catalog = Array.isArray(behavior.catalog) ? behavior.catalog : [];
  if (!catalog.length) {
    const missing = document.createElement("p");
    missing.className = "muted";
    missing.textContent = "このRunにはPolicy registryの比較情報がありません。";
    container.append(missing);
    return;
  }
  const list = document.createElement("div");
  list.className = "policy-variant-list";
  for (const variant of catalog) {
    const card = document.createElement("article");
    card.className = "policy-variant-card";
    const heading = document.createElement("h4");
    heading.textContent = `${variant.label || variant.variant} · ${variant.variant} · ${variant.registry_version || "registry version未記録"}${variant.variant === behavior.variant ? " · このRun" : ""}`;
    const description = document.createElement("p");
    description.textContent = variant.description || "説明未記録";
    const rule = document.createElement("p");
    rule.className = "muted";
    rule.textContent = `選択規則: ${variant.selection_rule || "未記録"}`;
    const parameters = document.createElement("p");
    parameters.className = "muted";
    parameters.textContent = `登録parameter: ${(variant.parameters || []).join(" · ") || "未記録"}`;
    const capabilities = document.createElement("p");
    capabilities.className = "muted";
    capabilities.textContent = `機能: ${(variant.capabilities || []).join(" · ") || "未記録"}`;
    card.append(heading, description, rule, parameters, capabilities);
    list.append(card);
  }
  container.append(list);
}

async function loadAgentStories() {
  const container = $("agent-story-cards");
  if (!state.runId) {
    container.textContent = "Runを選択してください。";
    return;
  }
  const requestId = (state.agentStoryRequest || 0) + 1;
  state.agentStoryRequest = requestId;
  const minute = Number($("minute-slider").value);
  try {
    const result = await api(`/api/v1/runs/${encodeURIComponent(state.runId)}/agent-stories?at_minute=${minute}`);
    if (requestId !== state.agentStoryRequest) return;
    container.replaceChildren();
    for (const story of result.stories || []) {
      const card = document.createElement("article");
      card.className = "policy-variant-card agent-story-card";
      const heading = document.createElement("h4");
      heading.textContent = story.label;
      const agentButton = document.createElement("button");
      agentButton.type = "button";
      agentButton.textContent = `${story.profile.id} · ${story.profile.group_id || "Group未設定"} · ${story.profile.mobility || "移動区分未記録"}`;
      agentButton.addEventListener("click", () => selectAgent(story.profile.id));
      const reason = document.createElement("p");
      reason.textContent = `選定根拠: ${story.selection_reason}`;
      const stateLine = document.createElement("p");
      const current = story.state_at_minute || {};
      const needs = current.needs || {};
      stateLine.textContent = `記録状態 · ${current.source || "状態未記録"} · ${minuteText(current.minute_index ?? 0)} · activity=${current.activity || "未記録"} · stress=${displayRecorded(current.stress)} · needs=${JSON.stringify(needs)}`;
      card.append(heading, agentButton, reason, stateLine);
      if (story.history?.length) {
        const historyTitle = document.createElement("p");
        historyTitle.textContent = "記録された状態と判断:";
        const history = document.createElement("ul");
        for (const sample of story.history) {
          const row = document.createElement("li");
          row.textContent = `${minuteText(sample.minute)} · ${sample.selected_action || "判断未記録"} · ${sample.trigger || "trigger未記録"} · stress=${displayRecorded(sample.state?.stress)} · needs=${JSON.stringify(sample.state?.needs || {})} · ${sample.policy_version || "policy version未記録"}`;
          history.append(row);
        }
        card.append(historyTitle, history);
      } else {
        const missing = document.createElement("p");
        missing.className = "muted";
        missing.textContent = "このRunにAgentの判断traceが記録されていないため、時間経過は表示しません。";
        card.append(missing);
      }
      if (story.event_references?.length) {
        const eventTitle = document.createElement("p");
        eventTitle.textContent = "関連する記録Event ID:";
        const events = document.createElement("div");
        events.className = "agent-story-events";
        for (const item of story.event_references) {
          const eventButton = document.createElement("button");
          eventButton.type = "button";
          eventButton.textContent = `${item.event_id} · ${item.event_type} · ${minuteText(item.minute)} · ${item.status}`;
          eventButton.addEventListener("click", () => jumpToTimelineEvent({
            event_id: item.event_id,
            minute: item.minute,
            related_agent_id: story.profile.id,
          }));
          events.append(eventButton);
        }
        card.append(eventTitle, events);
      }
      container.append(card);
    }
    if (!(result.stories || []).length) container.textContent = "表示できる合成プロフィールがありません。";
  } catch (error) {
    if (requestId === state.agentStoryRequest) container.textContent = `Agent storyを読み込めません: ${error.message || error}`;
  }
}

function displayRecorded(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "未記録";
  const number = Number(value);
  if (!Number.isFinite(number)) return "未記録";
  return `${number.toLocaleString("ja-JP", { maximumFractionDigits: 2 })}${suffix}`;
}

async function refreshTimeSelection() {
  if (!state.runId) return;
  const requestId = ++state.timelineRequest;
  const minute = Number($("minute-slider").value);
  const base = `/api/v1/runs/${encodeURIComponent(state.runId)}`;
  const params = new URLSearchParams({ at_minute: String(minute), limit: "1" });
  const events = new URLSearchParams({
    start_minute: String(Math.max(0, minute - 720)),
    end_minute: String(Math.min(state.finalMinute, minute + 720)),
    limit: "100",
    status: "all",
  });
  try {
    const [timeline, eventPage] = await Promise.all([
      api(`${base}/timeline?${params}`),
      api(`${base}/events?${events}`),
    ]);
    if (requestId !== state.timelineRequest) return;
    state.currentMetrics = timeline.selected;
    renderPrimaryKpis(timeline.selected, minute);
    renderTimelineEvents(eventPage.items, minute);
    drawTimeline();
    setError("");
  } catch (error) {
    if (requestId === state.timelineRequest) setError(error);
  }
}

function renderPrimaryKpis(snapshot, selectedMinute) {
  const container = $("primary-kpis");
  container.replaceChildren();
  const cards = [
    {
      title: "待ち列",
      lines: [["トイレ", snapshot?.toilet_queue_length, " 人"], ["水", snapshot?.water_queue_length, " 人"], ["食事", snapshot?.meal_queue_length, " 人"]],
    },
    {
      title: "資源残量",
      lines: [["水", snapshot?.water_ml_remaining, " ml"], ["食料", snapshot?.food_meal_units_remaining, " 食分"]],
    },
    {
      title: "スタッフ",
      lines: [["稼働", snapshot?.staff_busy_count, " 人"], ["稼働率", snapshot?.staff_load_ratio, "%", true]],
    },
    {
      title: "高ストレス",
      lines: [["該当人数", snapshot?.stress_high_count, " 人"], ["平均", snapshot?.stress_mean]],
    },
  ];
  for (const card of cards) {
    const element = document.createElement("div");
    element.className = "primary-kpi";
    const title = document.createElement("strong");
    title.textContent = card.title;
    element.append(title);
    for (const [label, rawValue, suffix = "", percent = false] of card.lines) {
      const line = document.createElement("span");
      const value = percent && rawValue !== null && rawValue !== undefined
        ? Number(rawValue) * 100
        : rawValue;
      line.textContent = `${label} ${displayRecorded(value, suffix)}`;
      element.append(line);
    }
    container.append(element);
  }
  const recordedMinute = snapshot?.minute_index;
  $("kpi-snapshot-time").textContent = Number.isInteger(recordedMinute)
    ? recordedMinute === selectedMinute
      ? `${minuteText(recordedMinute)} の記録`
      : `${minuteText(selectedMinute)} 表示 · ${minuteText(recordedMinute)} の最新記録`
    : `${minuteText(selectedMinute)} より前の記録なし`;
}

function renderTimelineEvents(events, selectedMinute) {
  const list = $("timeline-events");
  list.replaceChildren();
  const nearby = events
    .filter((item) => Number.isInteger(item.minute))
    .sort((left, right) => Math.abs(left.minute - selectedMinute) - Math.abs(right.minute - selectedMinute)
      || left.minute - right.minute
      || String(left.event_id).localeCompare(String(right.event_id)))
    .slice(0, 5)
    .sort((left, right) => left.minute - right.minute || String(left.event_id).localeCompare(String(right.event_id)));
  $("timeline-event-count").textContent = `${nearby.length} 件 · 前後12時間`;
  if (!nearby.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "この前後に記録された出来事はありません。";
    list.append(empty);
    return;
  }
  for (const item of nearby) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "timeline-event";
    const title = document.createElement("span");
    title.className = "timeline-event-title";
    title.textContent = `${minuteText(item.minute)} · ${eventLabel(item.event_type)}`;
    const status = document.createElement("span");
    status.className = "timeline-event-status";
    status.textContent = item.status || "状態未記録";
    const timeLabel = document.createElement("span");
    timeLabel.className = "timeline-event-times";
    timeLabel.textContent = `作成 ${minuteValue(item.created_minute)} · 予定 ${minuteValue(item.scheduled_minute)} · 実行 ${minuteValue(item.realized_minute)}`;
    const summary = document.createElement("span");
    summary.className = "timeline-event-explanation";
    summary.textContent = eventSummary(item.event_type);
    button.append(title, status, timeLabel, summary);
    button.addEventListener("click", () => jumpToTimelineEvent(item));
    list.append(button);
  }
}

function eventLabel(eventType) {
  const labels = {
    delivery_delayed: "配送遅延",
    water_outage: "断水",
    water_restored: "給水再開",
    staff_unavailable: "スタッフ対応不可",
  };
  return labels[eventType] || (eventType ? `記録イベント · ${eventType}` : "種別未記録");
}

function eventSummary(eventType) {
  if (eventType === "water_outage") return "断水の記録。水の残量と給水待ちを同じ時刻のKPIで確認できます。";
  if (eventType === "delivery_delayed") return "配送遅延の記録。予定・実行時刻と資源残量の推移を照合できます。";
  if (eventType === "water_restored") return "給水再開の記録。給水待ちや水残量の推移を照合できます。";
  if (eventType === "staff_unavailable") return "スタッフ稼働に関する記録。担当数と稼働率の推移を照合できます。";
  return "影響はこの記録だけから判断できません。未記録の効果は補っていません。";
}

function minuteValue(value) {
  return Number.isInteger(value) && value >= 0 ? `${value}分` : "未記録";
}

async function jumpToTimelineEvent(item) {
  const minute = Math.min(state.finalMinute, Math.max(0, item.minute));
  $("minute-slider").value = String(minute);
  $("minute-label").textContent = minuteText(minute);
  state.selectedAgent = item.related_agent_id || null;
  $("agent-id").value = state.selectedAgent || "";
  state.selectedZoneId = item.related_agent_id ? null : item.related_zone_id || null;
  if (!state.selectedZoneId && item.related_facility_id) {
    const facility = state.lastMap?.facilities.find((entry) => entry.id === item.related_facility_id);
    state.selectedZoneId = facility?.zone_id || null;
  }
  drawTimeline();
  refreshModeSwitch();
  await Promise.all([refreshMapAndAgent(), refreshTimeSelection()]);
  if (!item.related_agent_id && state.selectedZoneId) selectRepresentativeAgent(state.selectedZoneId);
}

function representativeAgent(zoneId, agents) {
  return agents
    .filter((agent) => agent.zone_id === zoneId)
    .sort((left, right) =>
      (right.support_wait_minutes ?? 0) - (left.support_wait_minutes ?? 0)
      || (right.queue_wait_minutes ?? 0) - (left.queue_wait_minutes ?? 0)
      || (right.stress ?? 0) - (left.stress ?? 0)
      || String(left.id).localeCompare(String(right.id)))[0] || null;
}

function selectRepresentativeAgent(zoneId) {
  const representative = representativeAgent(zoneId, state.lastMap?.agents || []);
  state.selectedZoneId = zoneId;
  if (!representative) {
    state.selectedAgent = null;
    refreshMapAndAgent();
    return;
  }
  state.selectedAgent = representative.id;
  $("agent-id").value = representative.id;
  for (const row of $("profiles").querySelectorAll("tr")) row.classList.toggle("selected", row.dataset.agentId === representative.id);
  refreshMapAndAgent();
}

function drawMap(map) {
  const canvas = $("zone-map"); const ctx = canvas.getContext("2d");
  state.layoutRawMap = map;
  map = withVisualLayoutOverrides(map);
  state.lastMap = map;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const positions = new Map(map.zones.map((zone) => [zone.id, zone]));
  const showAgents = $("map-show-agents").checked;
  const showFacilities = $("map-show-facilities").checked;
  const showQueues = $("map-show-queues").checked;
  const showStress = $("map-show-stress").checked;
  const showStops = $("map-show-stops").checked;
  ctx.fillStyle = "#f7f8f4"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = "#c2d2c8"; ctx.lineWidth = 2;
  for (const edge of map.edges) {
    const from = positions.get(edge.from_zone_id); const to = positions.get(edge.to_zone_id);
    if (!from || !to) continue;
    ctx.beginPath(); ctx.moveTo(from.x, from.y); ctx.lineTo(to.x, to.y);
    ctx.strokeStyle = edge.closed ? "#c8774c" : "#c2d2c8";
    ctx.setLineDash(edge.closed ? [8, 6] : []); ctx.stroke(); ctx.setLineDash([]);
    if (Number.isFinite(edge.travel_minutes)) {
      const x = (from.x + to.x) / 2; const y = (from.y + to.y) / 2;
      const capacity = edge.max_agents_in_transit == null ? "無制限" : `枠${edge.max_agents_in_transit}`;
      ctx.fillStyle = "#173b35"; ctx.font = "10px system-ui"; ctx.textAlign = "center";
      ctx.fillText(`${edge.travel_minutes}分 · ${capacity}`, x, y - 5);
    }
    if (edge.closed && showStops) {
      const x = (from.x + to.x) / 2; const y = (from.y + to.y) / 2;
      ctx.fillStyle = "#a52c22"; ctx.font = "bold 15px system-ui"; ctx.textAlign = "center";
      ctx.fillText("×", x, y - 5); ctx.font = "10px system-ui"; ctx.fillText("通行止", x, y + 10);
    }
  }
  for (const zone of map.zones) {
    ctx.beginPath();
    if (Array.isArray(zone.polygon) && zone.polygon.length >= 3) {
      ctx.moveTo(zone.polygon[0][0], zone.polygon[0][1]);
      for (const point of zone.polygon.slice(1)) ctx.lineTo(point[0], point[1]);
      ctx.closePath();
    } else {
      ctx.arc(zone.x, zone.y, 28, 0, Math.PI * 2);
    }
    const active = map.selected_agent && map.selected_agent.zone_id === zone.id
      || state.selectedZoneId === zone.id;
    ctx.fillStyle = zone.modelled === false ? "#f0f1ed" : active ? "#e0efe5" : "#e9f1e9";
    ctx.fill(); ctx.strokeStyle = zone.modelled === false ? "#b5c0b8" : active ? "#196b5a" : "#89a99a";
    ctx.lineWidth = active ? 3 : 1.5; ctx.setLineDash(zone.modelled === false ? [5, 4] : []); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = "#173b35"; ctx.textAlign = "center"; ctx.font = "bold 14px system-ui";
    ctx.fillText(String(zone.name || zone.id).slice(0, 18), zone.x, zone.y - 4);
    ctx.font = "11px system-ui"; ctx.fillStyle = "#64716e";
    ctx.fillText(zone.modelled === false ? "表示のみ" : `${zone.occupancy} 人`, zone.x, zone.y + 15);
  }
  if (showFacilities) {
    for (const facility of map.facilities || []) {
      const color = facility.status === "stopped" ? "#a52c22" : facility.status === "outage" ? "#b5682c" : "#196b5a";
      ctx.fillStyle = color; ctx.fillRect(facility.x - 10, facility.y - 10, 20, 20);
      ctx.strokeStyle = "white"; ctx.lineWidth = 1.5; ctx.strokeRect(facility.x - 10, facility.y - 10, 20, 20);
      ctx.fillStyle = "white"; ctx.font = "bold 10px system-ui"; ctx.textAlign = "center";
      ctx.fillText(facility.kind === "toilet" ? "T" : facility.kind === "water_station" ? "水" : facility.kind === "meal_distribution" ? "食" : "設", facility.x, facility.y + 4);
      ctx.fillStyle = color; ctx.font = "10px system-ui";
      ctx.fillText(`${facility.id} ${facility.working_units}/${facility.capacity}`, facility.x, facility.y + 22);
      if (facility.status !== "active" && showStops) {
        ctx.font = "bold 11px system-ui"; ctx.fillText(facility.status === "stopped" ? "停止" : "一部停止", facility.x, facility.y - 14);
      }
    }
  }
  if (showQueues && map.queue_summary && map.queue_summary.toilet_queue_length !== null) {
    const toilet = map.zones.find((zone) => zone.id === "toilet");
    if (toilet) {
      ctx.fillStyle = map.queue_summary.toilet_queue_length > 0 ? "#fff0e3" : "#fffefa";
      ctx.strokeStyle = "#c77b45"; ctx.lineWidth = 1.5;
      ctx.fillRect(toilet.x - 62, toilet.y + 35, 124, 31); ctx.strokeRect(toilet.x - 62, toilet.y + 35, 124, 31);
      ctx.fillStyle = "#6e4428"; ctx.font = "11px system-ui"; ctx.textAlign = "center";
      ctx.fillText(`待ち ${map.queue_summary.toilet_queue_length} · 利用 ${map.queue_summary.toilet_active_users}`, toilet.x, toilet.y + 55);
    }
  }
  state.mapAgentPoints = showAgents ? (map.agents || []) : [];
  for (const agent of state.mapAgentPoints) {
    const selected = state.selectedAgent === agent.id;
    const queued = showQueues && (agent.queue_kind || agent.queue_wait_minutes > 0);
    const stressColor = agent.stress >= 0.7 ? "#bb443b" : agent.stress >= 0.4 ? "#d88c36" : "#196b5a";
    ctx.beginPath(); ctx.arc(agent.x, agent.y, selected ? 7 : 4.5, 0, Math.PI * 2);
    ctx.fillStyle = selected ? "#f1bc30" : showStress ? stressColor : "#277c67";
    ctx.fill(); ctx.lineWidth = queued ? 2.5 : selected ? 2 : 0.8;
    ctx.strokeStyle = queued ? "#c35b31" : selected ? "#173b35" : "#fffefa"; ctx.stroke();
    if (showStops && agent.activity === "seeking_help") {
      ctx.fillStyle = "#a52c22"; ctx.font = "bold 10px system-ui"; ctx.textAlign = "center";
      ctx.fillText("!", agent.x + 7, agent.y - 6);
    }
  }
  const visualEdit = $("layout-visual-edit")?.checked;
  $("zone-map").classList.toggle("visual-layout-edit", Boolean(visualEdit));
  $("map-note").textContent = mapNote(map);
}

function mapNote(map) {
  const count = (map.agents || []).length;
  const note = map.layout
    ? `${map.layout.title} · ${count}人表示 · 座標は表示用 · 配置編集はブラウザ内のみ`
    : map.synthetic_layout ? `${count}人 · 合成配置` : `${count}人 · Scenario座標`;
  // Agents in a zone without a shape on this map are reported, not silently dropped (#161).
  const undrawn = map.undrawn_agents || [];
  if (!undrawn.length) return note;
  const zones = [...new Set(undrawn.map((agent) => agent.zone_id ?? "区画不明"))].join("、");
  return `${note} · ${undrawn.length}人は地図にない区画（${zones}）にいるため非表示`;
}

$("zone-map").addEventListener("click", async (event) => {
  if ($("layout-visual-edit").checked) return;
  const canvas = $("zone-map"); const bounds = canvas.getBoundingClientRect();
  const x = (event.clientX - bounds.left) * canvas.width / bounds.width;
  const y = (event.clientY - bounds.top) * canvas.height / bounds.height;
  const selected = state.mapAgentPoints.find((agent) => Math.hypot(agent.x - x, agent.y - y) <= 11);
  if (selected) {
    await selectAgent(selected.id);
    return;
  }
  const zone = state.lastMap?.zones.find((item) => pointInZone(x, y, item));
  if (!zone) return;
  state.selectedZoneId = zone.id;
  selectRepresentativeAgent(zone.id);
});

function pointInZone(x, y, zone) {
  const polygon = zone.polygon;
  if (!Array.isArray(polygon) || polygon.length < 3) return Math.hypot(zone.x - x, zone.y - y) <= 28;
  let inside = false;
  for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index, index += 1) {
    const [currentX, currentY] = polygon[index];
    const [previousX, previousY] = polygon[previous];
    const crosses = (currentY > y) !== (previousY > y)
      && x < ((previousX - currentX) * (y - currentY)) / (previousY - currentY) + currentX;
    if (crosses) inside = !inside;
  }
  return inside;
}

function mapPoint(event) {
  const canvas = $("zone-map"); const bounds = canvas.getBoundingClientRect();
  return {
    x: (event.clientX - bounds.left) * canvas.width / bounds.width,
    y: (event.clientY - bounds.top) * canvas.height / bounds.height,
  };
}

function layoutDragTarget(point) {
  const facility = (state.lastMap?.facilities || [])
    .filter((item) => Math.hypot(item.x - point.x, item.y - point.y) <= 20)
    .sort((left, right) => Math.hypot(left.x - point.x, left.y - point.y) - Math.hypot(right.x - point.x, right.y - point.y))[0];
  if (facility) return { type: "facility", id: facility.id, x: facility.x, y: facility.y };
  const zones = (state.lastMap?.zones || [])
    .filter((zone) => pointInZone(point.x, point.y, zone))
    .sort((left, right) => Number(right.modelled !== false) - Number(left.modelled !== false));
  const zone = zones[0];
  return zone ? { type: "zone", id: zone.id, x: zone.x, y: zone.y, polygon: zone.polygon } : null;
}

$("zone-map").addEventListener("pointerdown", (event) => {
  if (!$("layout-visual-edit").checked || !state.lastMap?.layout) return;
  const point = mapPoint(event);
  const target = layoutDragTarget(point);
  if (!target) return;
  state.layoutDrag = { pointerId: event.pointerId, start: point, target };
  event.currentTarget.setPointerCapture(event.pointerId);
  event.preventDefault();
});

$("zone-map").addEventListener("pointermove", (event) => {
  const drag = state.layoutDrag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const point = mapPoint(event);
  const dx = point.x - drag.start.x; const dy = point.y - drag.start.y;
  const overrides = currentLayoutOverrides(state.layoutRawMap || state.lastMap);
  if (drag.target.type === "zone") {
    const polygon = drag.target.polygon.map(([x, y]) => [x + dx, y + dy]);
    const layout = state.layoutRawMap.layout;
    if (polygon.some(([x, y]) => x < 0 || y < 0 || x > layout.width || y > layout.height)) {
      $("demo-layout-status").textContent = "Zoneは図面の範囲内に移動してください。";
      return;
    }
    overrides.zones[drag.target.id] = { x: drag.target.x + dx, y: drag.target.y + dy, polygon };
  } else {
    const x = drag.target.x + dx; const y = drag.target.y + dy;
    const displayZoneId = state.lastMap.facilities.find((item) => item.id === drag.target.id)?.display_zone_id;
    const displayZone = state.lastMap.zones.find((item) => item.id === displayZoneId);
    const layout = state.layoutRawMap.layout;
    if (x < 0 || y < 0 || x > layout.width || y > layout.height || !displayZone || !pointInZone(x, y, displayZone)) {
      $("demo-layout-status").textContent = "設備は図面の範囲内かつ表示区画の中に配置してください。";
      return;
    }
    overrides.facilities[drag.target.id] = { x, y };
  }
  drawMap(state.layoutRawMap);
  event.preventDefault();
});

function finishLayoutDrag(event) {
  if (!state.layoutDrag || state.layoutDrag.pointerId !== event.pointerId) return;
  state.layoutDrag = null;
  saveVisualLayoutOverrides();
  $("demo-layout-status").textContent = "表示位置をこのブラウザに保存しました。ScenarioやBranchの条件は変わりません。";
}

$("zone-map").addEventListener("pointerup", finishLayoutDrag);
$("zone-map").addEventListener("pointercancel", finishLayoutDrag);
$("layout-visual-edit").addEventListener("change", () => {
  state.layoutDrag = null;
  if (state.layoutRawMap) drawMap(state.layoutRawMap);
  $("demo-layout-status").textContent = $("layout-visual-edit").checked
    ? "Zoneまたは設備をドラッグできます。変更はこのブラウザ内だけで、ScenarioやBranchには反映されません。"
    : "表示位置の編集を終了しました。";
});
$("layout-visual-reset").addEventListener("click", () => {
  const key = state.layoutOverrideKey;
  try { if (key) localStorage.removeItem(key); } catch { /* in-memory reset still works */ }
  state.layoutOverrides = { zones: {}, facilities: {} };
  state.layoutDrag = null;
  if (state.layoutRawMap) drawMap(state.layoutRawMap);
  $("demo-layout-status").textContent = "表示位置を初期化しました。シミュレーション条件は変わっていません。";
});
for (const id of ["map-show-agents", "map-show-facilities", "map-show-queues", "map-show-stress", "map-show-stops"]) {
  $(id).addEventListener("change", () => { if (state.lastMap) drawMap(state.lastMap); });
}

function drawTimeline() {
  const canvas = $("timeline"); const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const key = $("metric-select").value;
  const points = state.timeline.filter((row) => row[key] !== null && row[key] !== undefined && Number.isFinite(Number(row[key])));
  if (!points.length) { ctx.fillStyle = "#64716e"; ctx.fillText("KPIデータがありません", 20, 30); return; }
  const values = points.map((row) => Number(row[key]));
  const min = Math.min(...values); const max = Math.max(...values); const spread = max - min || 1;
  const left = 42; const right = canvas.width - 16; const top = 18; const bottom = canvas.height - 28;
  const firstMinute = Number(points[0].minute_index);
  const lastMinute = Number(points[points.length - 1].minute_index);
  const xForMinute = (minute) => left + ((right - left) * (minute - firstMinute)) / Math.max(1, lastMinute - firstMinute);
  ctx.strokeStyle = "#dce5df"; ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) { const y = top + ((bottom - top) * i) / 3; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke(); }
  ctx.strokeStyle = "#196b5a"; ctx.lineWidth = 2.5; ctx.beginPath();
  points.forEach((row, index) => {
    const x = xForMinute(Number(row.minute_index));
    const y = bottom - ((Number(row[key]) - min) / spread) * (bottom - top);
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  const minute = Number($("minute-slider").value);
  const nearest = points.reduce((best, point) => Math.abs(point.minute_index - minute) < Math.abs(best.minute_index - minute) ? point : best, points[0]);
  const x = xForMinute(minute);
  const y = bottom - ((Number(nearest[key]) - min) / spread) * (bottom - top);
  ctx.fillStyle = "#c8774c"; ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#64716e"; ctx.font = "11px system-ui"; ctx.textAlign = "left"; ctx.fillText(`${min.toFixed(2)} – ${max.toFixed(2)}`, 8, 12);
}

async function searchEvents(offset = 0) {
  if (!state.runId) return;
  state.eventsOffset = offset;
  const params = new URLSearchParams({
    status: $("event-status").value,
    q: $("event-query").value,
    limit: String(state.eventLimit),
    offset: String(offset),
  });
  if ($("event-start-minute").value !== "") params.set("start_minute", $("event-start-minute").value);
  if ($("event-end-minute").value !== "") params.set("end_minute", $("event-end-minute").value);
  if ($("branch-query").value.trim()) params.set("branch_id", $("branch-query").value.trim());
  if ($("event-agent-query").value.trim()) params.set("agent_id", $("event-agent-query").value.trim());
  if ($("event-zone-query").value.trim()) params.set("zone_id", $("event-zone-query").value.trim());
  try {
    const result = await api(`/api/v1/runs/${encodeURIComponent(state.runId)}/events?${params}`);
    renderEvents(result);
  } catch (error) { setError(error); }
}

function renderEvents(result) {
  state.eventsTotal = result.total;
  const list = $("events"); list.replaceChildren();
  for (const item of result.items) {
    const row = document.createElement("div"); row.className = "event-row";
    const time = document.createElement("span"); time.textContent = `${item.minute}m`;
    const kind = document.createElement("span"); kind.className = "kind"; kind.textContent = `${item.event_type} · ${item.event_id}`;
    const status = document.createElement("span"); status.className = "badge " + item.status; status.textContent = item.status;
    const branch = document.createElement("span"); branch.className = "branch muted"; branch.textContent = item.branch_id || "共有";
    const evidence = document.createElement("span");
    evidence.className = "muted";
    evidence.textContent = [item.source_id && "source " + item.source_id, item.causation_id && "cause " + item.causation_id]
      .filter(Boolean).join(" · ");
    row.append(time, kind, status, branch, evidence); list.append(row);
  }
  const statusLabels = {
    planned: "予定",
    applied: "適用",
    suppressed: "抑制",
    rejected: "拒否",
    failed: "失敗",
  };
  const statusSummary = Object.entries(result.status_counts || {})
    .map(([status, count]) => (statusLabels[status] || status) + " " + count)
    .join(" / ");
  $("event-count").textContent = result.total + " 件" + (statusSummary ? " · " + statusSummary : "");
  const first = result.total === 0 ? 0 : result.offset + 1;
  const last = Math.min(result.total, result.offset + result.limit);
  $("events-page").textContent = first + "–" + last + " / " + result.total;
  $("events-prev").disabled = result.offset === 0;
  $("events-next").disabled = result.offset + result.limit >= result.total;
  if (!result.items.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "該当するイベントはありません。"; list.append(empty); }
}

$("run-select").addEventListener("change", () => {
  state.requestedMinute = null;
  loadRun();
});
$("profile-search").addEventListener("submit", (event) => { event.preventDefault(); searchProfiles(0); });
$("profiles-prev").addEventListener("click", () => searchProfiles(Math.max(0, state.profilesOffset - 100)));
$("profiles-next").addEventListener("click", () => searchProfiles(state.profilesOffset + 100));
$("agent-search").addEventListener("submit", (event) => { event.preventDefault(); loadAgent(); });
$("event-search").addEventListener("submit", (event) => { event.preventDefault(); searchEvents(); });
$("events-prev").addEventListener("click", () => searchEvents(Math.max(0, state.eventsOffset - state.eventLimit)));
$("events-next").addEventListener("click", () => searchEvents(state.eventsOffset + state.eventLimit));
$("admin-branch-select").addEventListener("change", () => {
  const branch = state.demo?.branches.find((item) => item.branch_id === $("admin-branch-select").value);
  if (branch && $("admin-snapshot-minute").value === "") $("admin-snapshot-minute").value = String(branch.minute);
  refreshModeSwitch();
  loadAdminSnapshot();
});
$("admin-snapshot-minute").addEventListener("change", loadAdminSnapshot);
$("admin-snapshot-load").addEventListener("click", loadAdminSnapshot);
$("admin-compare-load").addEventListener("click", loadAdminComparison);
$("minute-slider").addEventListener("input", () => {
  const minute = Number($("minute-slider").value);
  $("minute-label").textContent = minuteText(minute);
  drawTimeline();
  refreshModeSwitch();
  clearTimeout(state.timeRefreshTimer);
  state.timeRefreshTimer = setTimeout(() => {
    Promise.all([refreshMapAndAgent(), refreshTimeSelection()]);
  }, 140);
});
$("minute-slider").addEventListener("change", () => {
  clearTimeout(state.timeRefreshTimer);
  Promise.all([refreshMapAndAgent(), refreshTimeSelection(), loadDecisionAudit(), loadAgentStories()]);
});
$("branch-query").addEventListener("input", refreshModeSwitch);
$("event-status").addEventListener("change", searchEvents);
$("metric-select").addEventListener("change", drawTimeline);
$("scenario-preset").addEventListener("change", () => {
  renderScenarioPreset(selectedScenarioPreset(), { setValues: true });
  $("scenario-editor-summary").hidden = true;
});
for (const id of [
  "scenario-staff-count", "scenario-toilet-units", "scenario-water-ml",
  "scenario-food-units", "scenario-event-frequency", "scenario-social-enabled",
]) $(id).addEventListener("input", () => {
  state.scenarioPreview = null;
  state.scenarioPreviewRequest = null;
  $("scenario-editor-summary").hidden = true;
  renderScenarioEditor();
});
$("scenario-behavior-policy").addEventListener("change", () => {
  state.scenarioPreview = null;
  state.scenarioPreviewRequest = null;
  $("scenario-editor-summary").hidden = true;
  renderScenarioEditor();
});
$("scenario-preview").addEventListener("click", previewScenarioDraft);
$("scenario-freeze").addEventListener("click", freezeScenarioDraft);
$("demo-start").addEventListener("click", () => demoAction("start"));
$("precomputed-replay-load").addEventListener("click", loadPrecomputedReplay);
$("demo-day3").addEventListener("click", () => {
  const branchId = $("demo-branch").value;
  if (branchId) runDemoJob(branchId, 2880);
});
$("demo-branch").addEventListener("change", () => {
  state.demoBranchTouched = true;
  refreshDemo(state.demo).then(resumeActiveDemoJob);
  refreshModeSwitch();
});
$("demo-pause").addEventListener("click", () => {
  if (state.demoBusy && state.demoJobId) jobPost(state.demoJobId, "cancel").catch(setError);
  else demoAction("pause", { branch_id: $("demo-branch").value });
});
$("demo-resume").addEventListener("click", () => demoAction("resume", { branch_id: $("demo-branch").value }));
$("demo-checkpoint").addEventListener("click", () => demoAction("checkpoint", { branch_id: $("demo-branch").value }));
$("demo-rewind").addEventListener("click", () => {
  const payload = { branch_id: $("demo-branch").value };
  if ($("demo-checkpoint-select").value) payload.checkpoint_id = $("demo-checkpoint-select").value;
  demoAction("rewind", payload);
});
$("demo-fork").addEventListener("click", async () => {
  const result = await demoAction("fork", { branch_id: $("demo-branch").value, new_branch_id: "staff-plus" });
  if (result) { $("demo-branch").value = "staff-plus"; await refreshDemo(result); }
});
$("demo-staff").addEventListener("click", () => addDemoCommands("add_staff"));
$("demo-toilet").addEventListener("click", () => addDemoCommands("add_toilet"));
$("demo-layout-edge").addEventListener("change", () => renderLayoutEditor(selectedDemoBranch()));
$("demo-layout-facility").addEventListener("change", () => {
  const facility = selectedDemoBranch()?.facility_locations?.find((item) => item.id === $("demo-layout-facility").value);
  if (facility) $("demo-layout-zone").value = facility.zone_id;
});
$("demo-layout-route-apply").addEventListener("click", () => {
  const travelMinutes = Number($("demo-layout-travel").value);
  const rawCapacity = $("demo-layout-capacity").value.trim();
  const capacity = rawCapacity === "" ? null : Number(rawCapacity);
  if (!Number.isInteger(travelMinutes) || travelMinutes < 1 || travelMinutes > 1440) {
    setError("移動時間は1〜1440分の整数で入力してください。");
    return;
  }
  if (capacity !== null && (!Number.isInteger(capacity) || capacity < 1 || capacity > 100)) {
    setError("通行枠は空欄（無制限）または1〜100人で入力してください。");
    return;
  }
  createLayoutBranch([{
    kind: "set_zone_edge",
    edge_id: $("demo-layout-edge").value,
    travel_minutes: travelMinutes,
    closed: $("demo-layout-closed").checked,
    max_agents_in_transit: capacity,
  }]);
});
$("demo-layout-facility-apply").addEventListener("click", () => {
  createLayoutBranch([{
    kind: "relocate_facility",
    facility_id: $("demo-layout-facility").value,
    zone_id: $("demo-layout-zone").value,
  }]);
});
$("demo-finish").addEventListener("click", finishSelectedDemoBranch);
$("nl-preview").addEventListener("click", previewNaturalLanguage);
$("nl-approve").addEventListener("click", approveNaturalLanguagePreview);
$("nl-multi-add-branch").addEventListener("click", () => {
  const plan = state.nlMultiPlan;
  if (!plan || plan.branches.length >= 8) return;
  const used = new Set(plan.branches.map((item) => item.variant_id));
  let index = 1;
  while (used.has(`alternative-${index}`)) index += 1;
  const source = plan.branches[plan.branches.length - 1];
  const alternative = JSON.parse(JSON.stringify(source));
  alternative.variant_id = `alternative-${index}`;
  alternative.label = `比較案 ${plan.branches.length + 1}`;
  alternative.plan.program_id = `${String(source.plan.program_id).slice(0, 42)}-alt-${index}`;
  plan.branches.push(alternative);
  renderMultiPlanEditor();
  invalidateMultiPlan("比較案を複製しました。同じ作用のままでは承認できないため、時刻や設定を編集してください。");
});
$("nl-multi-validate").addEventListener("click", validateMultiPlan);
$("nl-multi-approve").addEventListener("click", approveMultiPlan);
$("nl-text").addEventListener("input", () => clearNaturalLanguagePreview());
$("nl-fork-minute").addEventListener("input", () => clearNaturalLanguagePreview());
$("demo-compare").addEventListener("click", async () => {
  try {
    const candidateBranch = selectedDemoBranch();
    if (!candidateBranch || candidateBranch.branch_id === "baseline") {
      throw new Error("比較する候補Branchを選択してください。");
    }
    const report = await demoPost("compare", { baseline_branch_id: "baseline", candidate_branch_id: candidateBranch.branch_id });
    $("demo-comparison").textContent = JSON.stringify(report, null, 2);
    renderStoryCard(report.story_card);
    const labels = {
      toilet_queue_length: "トイレ待ち",
      toilet_active_users: "トイレ利用中",
      toilet_working_units: "使用可能なトイレ",
      toilet_wait_count: "待ちが発生した回数",
      toilet_wait_total_minutes: "総待ち時間",
      toilet_wait_mean_minutes: "平均待ち時間",
      toilet_wait_max_minutes: "最大待ち時間",
      stress_mean: "平均ストレス",
      stress_high_count: "高ストレス者数",
      staff_assigned_task_count: "担当タスク数",
      staff_busy_count: "稼働中スタッフ",
      staff_load_ratio: "スタッフ稼働率",
      unresolved_support_request_count: "未解決の支援要請",
      support_no_qualified_worker_count: "資格のあるスタッフがいない支援依頼",
      support_no_qualified_worker_request_minutes: "資格のあるスタッフがいない依頼分数",
      support_fairness_promoted_assignment_count: "公平性ルールで優先された依頼",
      support_wait_staff_busy_request_minutes: "対応中の資格者がいる支援待ち依頼分数",
      support_wait_staff_off_shift_request_minutes: "勤務していない資格者がいる支援待ち依頼分数（時間外・不在）",
    };
    const differences = Object.entries(report.final_metric_differences || {}).map(([key, values]) => {
      const delta = values.delta;
      return labels[key] && Number.isFinite(delta)
        ? `${labels[key]} ${delta > 0 ? "+" : ""}${Number(delta).toFixed(1)}`
        : null;
    }).filter(Boolean);
    const routingLabels = {
      travel_trip_count: "移動回数",
      travel_minutes_total: "移動合計",
      capacity_wait_observation_count: "通路容量待ち観測",
      capacity_wait_unique_agent_count: "容量待ちの人数",
    };
    const routingDifferences = Object.entries(report.routing_metrics || {}).map(([key, values]) => {
      const delta = values.delta;
      return routingLabels[key] && Number.isFinite(delta)
        ? `${routingLabels[key]} ${delta > 0 ? "+" : ""}${Number(delta).toLocaleString("ja-JP")}${key === "travel_minutes_total" ? "分" : ""}`
        : null;
    }).filter(Boolean);
    const comparisonDifferences = [...differences.slice(0, 2), ...routingDifferences];
    const firstChange = report.first_divergence_minute === null
      ? "状態に差はありません"
      : `最初の差 ${minuteText(report.first_divergence_minute)}`;
    $("demo-comparison-summary").textContent = comparisonDifferences.length
      ? `${firstChange} · 終了時の差（選択案 − 元のシナリオ）: ${comparisonDifferences.join(" / ")}`
      : `${firstChange} · KPIの差はありません`;
    await refreshDemo();
    setError("");
  } catch (error) { setError(error); }
});
$("demo-multi-compare-branches").addEventListener("change", () => {
  const selected = [...$("demo-multi-compare-branches").selectedOptions];
  const completed = selected.filter((option) => option.value &&
    state.demo?.branches.some((branch) => branch.branch_id === option.value && branch.status === "completed"));
  $("demo-multi-compare").disabled = completed.length < 2;
});
$("demo-multi-compare").addEventListener("click", async () => {
  try {
    const selectedBranch = selectedDemoBranch();
    const metadata = selectedBranch?.multi_plan;
    const candidateBranchIds = [...$("demo-multi-compare-branches").selectedOptions]
      .map((option) => option.value).filter(Boolean);
    if (!metadata?.source_branch_id || candidateBranchIds.length < 2) {
      throw new Error("同じMultiPlanの完了済み代替案を2つ以上選択してください。");
    }
    const report = await demoPost("compare-set", {
      baseline_branch_id: metadata.source_branch_id,
      candidate_branch_ids: candidateBranchIds,
    });
    $("demo-multi-comparison").textContent = JSON.stringify(report, null, 2);
    setError("");
  } catch (error) { setError(error); }
});
$("demo-story-review").addEventListener("click", async () => {
  try {
    const candidateBranch = selectedDemoBranch();
    if (!candidateBranch || candidateBranch.branch_id === "baseline") {
      throw new Error("レビューする候補Branchを選択してください。");
    }
    $("demo-story-review").disabled = true;
    $("demo-story-review-status").textContent = "根拠を検証しています…";
    const result = await demoPost("story-review", {
      baseline_branch_id: "baseline",
      candidate_branch_id: candidateBranch.branch_id,
    });
    renderStoryCard(result.card, result.review_status);
    const usage = result.usage || {};
    const cost = Number(usage.estimated_cost_usd || 0).toFixed(6);
    $("demo-story-review-status").textContent = `API ${usage.api_request_count ?? 0}回 · ${usage.input_tokens ?? 0}/${usage.output_tokens ?? 0} tokens · 推定 $${cost}${usage.fallback_used ? " · fallback" : ""}`;
    setError("");
  } catch (error) {
    $("demo-story-review-status").textContent = "根拠レビューに失敗しました。";
    setError(error);
  } finally {
    const selected = selectedDemoBranch();
    $("demo-story-review").disabled = !selected || selected.branch_id === "baseline" || selected.status !== "completed" || state.demoBusy;
  }
});
// Persistent Project mode has its own controller; opening it must not resume a legacy job.
if (document.body.dataset.mode !== "demo" || new URLSearchParams(location.search).get("legacy") === "1") {
  setupMode();
  initialize();
}
