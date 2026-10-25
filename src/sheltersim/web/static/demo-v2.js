/* Independent backup UI. WorldState and persisted API artifacts are authoritative. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;
  const json = (value) => JSON.stringify(value, null, 2);
  const llmModeLabel = (mode) =>
    ({
      record: "Claude API接続",
      offline: "API未接続",
      replay: "記録応答リプレイ",
    })[mode] ||
    mode ||
    "未記録";
  const query = new URLSearchParams(location.search);
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem("hinanjo-if-backup") || "{}");
  } catch {
    /* Invalid local preference only. */
  }
  const state = {
    project:
      query.get("project") || saved.project || `backup-${crypto.randomUUID()}`,
    run: query.get("run") || saved.run || null,
    branch: query.get("branch") || saved.branch || "main",
    minute: 0,
    data: null,
    frame: null,
    preview: null,
    intervention: null,
    comparison: null,
    history: [],
    report: null,
    evidence: null,
    zoom: 1,
    mapPan: { x: 0, y: 0 },
    selectedGroup: [],
    selected: null,
    playing: false,
    sceneMode: "schematic",
    motion: null,
    playToken: 0,
    replay: null,
    visualMs: 0,
    busy: false,
    page: "scenario",
    inputVersion: 0,
    seekVersion: 0,
    scope: 0,
    chatVersion: 0,
    context: null,
    target: null,
    poll: null,
    mode: null,
  };
  const metricDefs = [
    {
      key: "stress_mean",
      label: "平均ストレス",
      unit: "/100",
      factor: 100,
      icon: "♟",
      note: "状態境界の平均",
    },
    {
      key: "toilet_wait_max_minutes",
      label: "最大トイレ待機時間",
      unit: "分",
      factor: 1,
      icon: "◷",
      note: "記録上の累積最大",
    },
    {
      key: "staff_load_ratio",
      label: "スタッフ負荷",
      unit: "%",
      factor: 100,
      icon: "♟",
      note: "稼働スタッフ比率",
    },
    {
      key: "unresolved_support_request_count",
      label: "未解決の支援要請",
      unit: "件",
      factor: 1,
      icon: "!",
      note: "記録された未解決件数",
    },
    {
      key: "population",
      label: "現在の避難者数",
      unit: "人",
      factor: 1,
      icon: "♟",
      note: "現在のWorldState",
    },
    {
      key: "toilet_queue_length",
      label: "トイレ待機人数",
      unit: "人",
      factor: 1,
      icon: "◷",
      note: "現在の待機列",
    },
  ];
  const zoneNames = {
    sleeping: "体育館・避難スペース",
    quiet: "福祉・休養スペース",
    medical: "救護室",
    food: "物資配布",
    water: "給水",
    toilet: "トイレ",
    intake: "受付",
    staff: "スタッフ本部",
    corridor: "通路",
  };
  const eventNames = {
    resource_reserve: "物資の受け取りを予約",
    resource_consume: "物資を使用（在庫が減少）",
    resource_service_complete: "食事・水の受け取りが完了",
    resource_service_cancel: "食事・水の受け取りを中止",
    resource_delivery: "物資が届いた",
    delivery_delayed: "物資の到着が遅れている",
    staff_operation_transition: "スタッフ業務の段階が変わった",
    staff_operation_arrive: "スタッフが現場に到着",
    staff_operation_complete: "スタッフ業務が完了",
    staff_operation_relief: "スタッフが交代",
    staff_operation_preempted: "スタッフ業務を中断（別の対応を優先）",
    staff_support_complete: "スタッフによる避難者支援が完了",
    staff_task_complete: "スタッフの作業が完了",
    staff_task_start: "スタッフの作業を開始",
    staff_unavailable: "スタッフが対応できなくなった",
    staff_restored: "スタッフが対応に復帰",
    toilet_service_complete: "トイレの利用が完了",
    facility_queue_transition: "待ち列の状況が変わった",
    facility_queue_abandon: "待ち列をあきらめて離れた",
    support_request: "避難者が支援を求めた（対応待ち）",
    support_request_timeout: "支援要請が対応されないまま時間切れ",
    social_interaction: "避難者どうしの会話・助け合い",
    care_support_transition: "介助・見守り支援の段階が変わった",
    facility_outage: "設備が使えなくなった",
    facility_recovery: "設備が使えるようになった",
    facility_failure: "設備が故障",
    facility_repair: "設備の修理が完了",
    water_outage: "断水が発生",
    water_restored: "断水が復旧",
    agent_action_lifecycle: "避難者の行動の開始・終了",
    agent_travel_complete: "避難者の移動が完了",
    agent_travel_start: "避難者が移動を開始",
    zone_edge_capacity_wait: "通路が混んで移動待ち",
    arrival: "避難者が到着",
    agent_arrival: "避難者が到着",
    population_approach_complete: "避難者が避難所に到着",
    population_return_admission: "一時外出した避難者が戻った",
    information: "情報を伝えた",
    information_contact_update: "情報が避難者に伝わった",
    proximity_contact_update: "避難者どうしが近くで接触",
    intervention: "介入（もしもの条件）を適用",
    effect_program_apply: "介入の効果が始まった",
    effect_program_expire: "介入の効果が終わった",
  };
  // 色分け：resolved=青（完了・解決）、unresolved=赤（未解決・問題）、それ以外=灰（経過の記録）
  const eventTones = {
    resource_service_complete: "resolved",
    resource_delivery: "resolved",
    staff_operation_complete: "resolved",
    staff_support_complete: "resolved",
    staff_task_complete: "resolved",
    staff_restored: "resolved",
    toilet_service_complete: "resolved",
    facility_recovery: "resolved",
    facility_repair: "resolved",
    water_restored: "resolved",
    resource_service_cancel: "unresolved",
    delivery_delayed: "unresolved",
    staff_operation_preempted: "unresolved",
    staff_unavailable: "unresolved",
    facility_queue_abandon: "unresolved",
    support_request: "unresolved",
    support_request_timeout: "unresolved",
    facility_outage: "unresolved",
    facility_failure: "unresolved",
    water_outage: "unresolved",
  };
  const eventTone = (e) =>
    ["rejected", "failed"].includes(e.status)
      ? "unresolved"
      : eventTones[e.event_type] || "record";
  const activityNames = {
    moving: "移動中",
    queued: "待機中",
    sleeping: "休息中",
    idle: "待機",
    resting: "休息中",
    seeking_help: "支援を求めている",
    receiving_support: "支援対応中",
    using_facility: "施設利用中",
    eating: "食事中",
    drinking: "給水中",
    toilet: "トイレ利用中",
  };
  const base = () => `/api/v1/projects/${enc(state.project)}`;
  const runBase = () => `${base()}/runs/${enc(state.run)}`;
  const branchBase = (id = state.branch) => `${runBase()}/branches/${enc(id)}`;
  const reference = (id = state.branch) =>
    state.data?.branches.find((b) => b.branch_id === id);
  const lower = () => reference()?.fork_minute ?? 0;
  const upper = () => reference()?.computed_until ?? 0;
  const duration = () => state.total ?? upper();
  const time = (m) =>
    `${Math.floor(m / 1440) + 1}日目 ${String(Math.floor((m % 1440) / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const shortBranch = (id) =>
    id === "main" ? "元のシナリオ" : `介入分岐 ${id.slice(-6)}`;
  const runStatusLabel = (status) =>
    ({
      running: "計算中",
      paused: "計算待機",
      completed: "計算完了",
      failed: "計算失敗",
      interrupted: "計算中断",
      created: "作成済み",
    })[status] ||
    status ||
    "未作成";
  const num = (n) =>
    typeof n === "number" && Number.isFinite(n)
      ? Math.abs(n) >= 100
        ? Math.round(n).toLocaleString("ja-JP")
        : Number(
            n.toFixed(Math.abs(n) > 0 && Math.abs(n) < 1 ? 4 : 1),
          ).toLocaleString("ja-JP")
      : "—";
  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (cls) node.className = cls;
    return node;
  }
  const iconPaths = {
    scenario: ["M6 3h9l4 4v14H5V3z", "M14 3v5h5M8 12h8M8 16h6"],
    simulation: [
      "M4 19a9 9 0 1 1 16 0",
      "M12 13l5-6",
      "M6 15h1M7 8l1 1M12 5v1M18 14h1",
    ],
    comparison: [
      "M6 5v14M6 12h6l6-6",
      "M8 5a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
      "M8 19a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
      "M20 5a2 2 0 1 1-4 0 2 2 0 0 1 4 0z",
    ],
    report: ["M4 20h17M6 17v-6h3v6M11 17V5h3v12M16 17V8h3v9"],
    people: [
      "M9 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
      "M3 21v-5a6 6 0 0 1 12 0v5z",
      "M17 4a3 3 0 0 1 0 6M17 13a5 5 0 0 1 5 5v3h-4",
    ],
    clock: ["M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z", "M12 6v6l4 2"],
    staff: [
      "M12 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
      "M4 22v-5a8 8 0 0 1 16 0v5z",
      "M9 15l3 3 3-3",
    ],
    alert: ["M12 3l10 18H2L12 3z", "M12 9v5M12 17h.01"],
    home: ["M2 11L12 2l10 9", "M5 9v13h14V9M9 22v-8h6v8"],
    queue: [
      "M5 7a2 2 0 1 0 0-4 2 2 0 0 0 0 4z",
      "M2 16v-5a3 3 0 0 1 6 0v5M3 16v6M7 16v6M12 5h10M12 11h10M12 17h10",
    ],
  };
  function uiIcon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.9");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    for (const d of iconPaths[name] || iconPaths.people) {
      const path = document.createElementNS(svg.namespaceURI, "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  }
  const chatWelcome = $("chat").firstElementChild.cloneNode(true);
  function resetChat() {
    $("chat").replaceChildren(chatWelcome.cloneNode(true));
  }
  let noticeTimer;
  function note(text, kind = "") {
    clearTimeout(noticeTimer);
    $("notice").hidden = false;
    $("notice").textContent = text;
    $("notice").className = `notice ${kind}`;
    if (kind === "success")
      noticeTimer = setTimeout(() => {
        $("notice").hidden = true;
      }, 1800);
  }
  function remember() {
    localStorage.setItem(
      "hinanjo-if-backup",
      JSON.stringify({
        project: state.project,
        run: state.run,
        branch: state.branch,
      }),
    );
    const params = new URLSearchParams({ project: state.project });
    if (state.run) {
      params.set("run", state.run);
      params.set("branch", state.branch);
    }
    history.replaceState(null, "", `/demo-v2?${params}#${state.page}`);
  }
  async function api(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const result = await response.json();
    if (!response.ok)
      throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  function stopPlayback(preserve = false) {
    state.playing = false;
    state.playToken++;
    if (state.replay) {
      if (preserve) state.replay.pause();
      else state.replay.dispose();
    }
    if (!preserve) {
      state.replay = null;
      state.motion = null;
      state.visualMs = 0;
    }
    $("play").textContent = "▶ シミュレーションを再生";
    $("timeline-play").textContent = "▶ シミュレーションを再生";
    $("replay-status").textContent = preserve
      ? "一時停止 · 位置と姿勢を保持"
      : "計算済みの記録を再生";
    if (window.HinanjoReplayView)
      window.HinanjoReplayView = {
        ...window.HinanjoReplayView,
        playing: false,
      };
  }
  async function action(label, task) {
    if (state.busy) return;
    state.busy = true;
    stopPlayback();
    document.body.classList.add("is-busy");
    note(label, "loading");
    try {
      await task();
      note("操作が完了しました。記録された結果を表示しています。", "success");
    } catch (error) {
      note(`操作を完了できませんでした：${error.message}`, "error");
    } finally {
      state.busy = false;
      document.body.classList.remove("is-busy");
      controls();
    }
  }
  const on = (id, callback) =>
    $(id).addEventListener("click", () =>
      action("処理中です。APIの応答を待っています…", callback),
    );
  function show(page) {
    if (!["scenario", "simulation", "comparison", "report"].includes(page))
      page = "scenario";
    state.page = page;
    document.body.dataset.view = page;
    stopPlayback();
    document
      .querySelectorAll(".page")
      .forEach((n) => n.classList.toggle("active", n.id === `page-${page}`));
    document.querySelectorAll("[data-page]").forEach((n) => {
      n.classList.toggle("active", n.dataset.page === page);
      n.setAttribute(
        "aria-current",
        n.dataset.page === page ? "page" : "false",
      );
    });
    remember();
    controls();
    requestAnimationFrame(drawAll);
    if (page === "report" && state.run)
      void action("レポートと記録済みの根拠を取得しています…", loadReport);
  }
  function invalidateScenario() {
    state.inputVersion++;
    state.preview = null;
    $("scenario-check").checked = false;
    $("scenario-check").disabled = true;
    $("approve").disabled = true;
    $("preview-result").hidden = true;
    $("preview-empty").hidden = false;
    $("preview-step").classList.remove("current");
    $("blockers").replaceChildren(
      el("p", "入力が変更されました。再プレビューしてください。", "muted"),
    );
  }
  function invalidateIntervention() {
    state.intervention = null;
    $("intervention-check").checked = false;
    $("intervention-check").disabled = true;
    $("intervention-result").replaceChildren();
    $("intervention-apply").disabled = true;
  }
  function renderKpis(container, kpis, defs = metricDefs) {
    container.replaceChildren();
    for (const d of defs) {
      const card = el("div", undefined, "kpi");
      const image = el("span", undefined, "kpi-icon");
      image.append(
        uiIcon(
          {
            stress_mean: "people",
            toilet_wait_max_minutes: "clock",
            staff_load_ratio: "staff",
            unresolved_support_request_count: "alert",
            population: "people",
            toilet_queue_length: "queue",
          }[d.key],
        ),
      );
      card.append(image);
      card.title = d.note;
      const body = el("div");
      body.append(el("small", d.label));
      body.append(
        el(
          "strong",
          num(
            typeof kpis?.[d.key] === "number" ? kpis[d.key] * d.factor : null,
          ),
        ),
        el("em", d.unit),
        el("div", d.note, "fine"),
      );
      card.append(body);
      container.append(card);
    }
  }
  function controls() {
    const has = Boolean(state.run && state.frame),
      running = state.data?.status === "running",
      working = state.busy || running;
    document
      .querySelector(".simulation-side")
      .classList.toggle("has-intervention", Boolean(state.intervention));
    for (const id of ["compute", "compute-end", "intervention-preview"])
      $(id).disabled = !has || working;
    $("cancel").disabled = !running;
    $("target").disabled = !has || running;
    $("timeline-play").disabled = !has || state.busy || upper() <= lower();
    $("play").disabled =
      state.page === "simulation"
        ? $("timeline-play").disabled
        : state.page === "comparison"
          ? !state.comparison ||
            state.busy ||
            state.comparison.common_range[1] <= state.comparison.common_range[0]
          : true;
    $("seek").disabled = !has || state.busy;
    $("branch-select").disabled = !has || working;
    $("saved-runs").disabled = working;
    $("approve").disabled =
      !state.preview?.can_start || !$("scenario-check").checked || working;
    $("intervention-apply").disabled =
      !state.intervention?.can_apply ||
      !$("intervention-check").checked ||
      working;
    for (const id of ["report-refresh", "save-report", "ask"])
      $(id).disabled = !has || state.busy;
    const branches = state.data?.branches || [],
      pair = branches.length > 1;
    for (const id of ["compare-refresh", "compare-branch", "compute-pair"])
      $(id).disabled = !pair || working;
    $("compare-seek").disabled = !state.comparison || state.busy;
    $("compute-status").textContent = running
      ? `実計算中 · 保存済み ${upper()} 分 / 目標 ${state.target ?? "不明"} 分`
      : `${runStatusLabel(state.data?.status)} · 計算速度と再生速度は独立しています`;
    if (has) {
      $("seek").min = lower();
      $("seek").max = upper();
      $("seek").value = state.minute;
      $("seek-label").textContent = time(state.minute);
      $("clock").textContent = time(
        state.page === "comparison" && state.comparison
          ? state.comparison.minute
          : state.minute,
      );
      $("computed-label").textContent =
        `実計算の記録 · ${upper()}分まで計算済み`;
      $("progress").max = Math.max(1, state.target ?? upper());
      $("progress").value = upper();
    }
  }
  async function ensureProject() {
    const list = await api("/api/v1/projects");
    if (!list.projects.some((p) => p.project_id === state.project))
      await api("/api/v1/projects", { project_id: state.project });
  }
  async function refreshRuns() {
    const result = await api(`${base()}/runs`);
    $("saved-runs").replaceChildren(el("option", "新しいシナリオ"));
    $("saved-runs").firstChild.value = "";
    for (const r of result.runs) {
      const option = el("option", `${r.scenario_id} · ${r.run_id.slice(-6)}`);
      option.value = r.run_id;
      option.selected = r.run_id === state.run;
      $("saved-runs").append(option);
    }
  }
  function scenarioRequest() {
    const req = {},
      origins = {};
    for (const [id, key, type] of [
      ["municipality", "municipality", "string"],
      ["disaster", "disaster", "string"],
      ["evacuees", "evacuees", "number"],
      ["duration", "duration_days", "number"],
      ["staff-count", "staff_count", "number"],
      ["area", "gross_area_m2", "number"],
      ["toilets", "accessible_toilet_count", "number"],
    ]) {
      const value = $(id).value.trim();
      if (value !== "") {
        req[key] = type === "number" ? Number(value) : value;
        origins[key] = "user";
      }
    }
    if ($("facility-type").value) {
      req.facility_conditions = [
        `施設種別（利用者入力）: ${$("facility-type").value}`,
      ];
      origins.facility_conditions = "user";
    }
    if ($("food").value !== "" || $("water").value !== "") {
      req.operational_conditions = {
        initial_resources: {
          status:
            $("food").value !== "" && $("water").value !== ""
              ? "entered"
              : "incomplete",
          food_meal_units:
            $("food").value === "" ? null : Number($("food").value),
          water_ml:
            $("water").value === ""
              ? null
              : Math.round(Number($("water").value) * 1000),
          source_kind: "operator",
        },
      };
      origins.operational_conditions = "user";
    }
    req.field_origins = origins;
    return req;
  }
  async function previewScenario() {
    if (!$("scenario-form").reportValidity()) return;
    const version = state.inputVersion;
    let req = scenarioRequest();
    const text = $("scenario-text").value.trim();
    const hasFields = Object.keys(req).some((k) => k !== "field_origins");
    if (
      hasFields &&
      (!req.municipality || !req.disaster || !req.evacuees) &&
      !text
    )
      throw new Error(
        "自治体・災害種別・避難者数を項目か文章で指定してください。",
      );
    if (!hasFields && !text)
      throw new Error("自然言語または基本設定を入力してください。");
    await ensureProject();
    if (hasFields && (!req.municipality || !req.disaster || !req.evacuees)) {
      const hints = [
        req.municipality ? `${req.municipality}で` : null,
        req.disaster,
        req.evacuees ? `${req.evacuees}人` : null,
        req.duration_days ? `${req.duration_days}日間` : null,
      ]
        .filter(Boolean)
        .join("、");
      const extracted = await api(`${base()}/scenario-preview`, {
        text: `${text}\n入力欄の指定: ${hints}`,
      });
      if (version !== state.inputVersion)
        throw new Error("入力が変更されました。再プレビューしてください。");
      if (!extracted.request)
        throw new Error(
          `不足項目：${(extracted.missing_fields || []).join("、")}。基本設定を確認してください。`,
        );
      req = {
        ...extracted.request,
        ...req,
        field_origins: {
          ...extracted.request.field_origins,
          ...req.field_origins,
        },
      };
    }
    const result = await api(`${base()}/scenario-preview`, {
      text,
      ...(hasFields ? { request: req } : {}),
    });
    if (version !== state.inputVersion)
      throw new Error(
        "入力が変更されました。最新の条件で再プレビューしてください。",
      );
    if (!result.scenario) {
      $("blockers").replaceChildren(
        el(
          "p",
          `不足項目：${(result.missing_fields || []).join("、")}。基本設定を揃えて再プレビューしてください。`,
          "warning-item",
        ),
      );
      throw new Error(
        "必要な条件を読み取れませんでした。自治体・災害種別・避難者数を入力してください。",
      );
    }
    state.preview = result;
    renderPreview();
    await refreshRuns();
  }
  function sourceRows(box, rows) {
    box.replaceChildren();
    for (const row of rows || []) {
      const card = el("div", undefined, "source-row");
      card.append(el("strong", row.title || row.subject || row.id || "出典"));
      card.append(
        el(
          "span",
          `${row.source_kind || row.classification || "仮定・適用範囲は原文参照"} · ${row.reason || row.description || row.source_id || ""}`,
        ),
      );
      if (row.url) {
        try {
          const url = new URL(row.url);
          if (url.protocol === "https:" || url.protocol === "http:") {
            const a = el("a", " 出典を開く ↗");
            a.href = url.href;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            card.append(a);
          }
        } catch {
          /* Preserve as text only. */
        }
      }
      if (row.value !== undefined) card.append(el("pre", json(row.value)));
      box.append(card);
    }
  }
  function renderPreview() {
    const p = state.preview;
    if (!p) return;
    const r = p.request,
      s = p.scenario,
      f = p.facility_design;
    $("preview-result").hidden = false;
    $("preview-empty").hidden = true;
    $("preview-step").classList.add("current");
    $("preview-mode").textContent =
      `${llmModeLabel(p.llm_usage?.mode)} · 補完値を要確認`;
    if (!state.run) {
      $("scenario-name").textContent = r.municipality + " · " + r.disaster;
      $("scenario-summary").textContent =
        r.evacuees + "人 / " + num(r.duration_days) + "日間の試算";
    }
    $("preview-summary").textContent =
      `${r.municipality}での${r.disaster}を想定し、${r.evacuees}人・${num(r.duration_days)}日間の避難所運営を試算します。施設・人口は合成モデルです。`;
    $("condition-chips").replaceChildren(
      ...[
        r.municipality,
        r.disaster,
        `${r.evacuees}人`,
        `${num(r.duration_days)}日間`,
      ].map((t) => el("span", t, "chip")),
    );
    $("facility-summary").replaceChildren(
      ...[
        ["避難スペース容量", `${f?.capacity_supported ?? "—"}人`],
        ["スタッフ", `${s.staff?.length ?? "—"}人`],
      ].map(([label, value]) => {
        const d = el("div", label);
        d.append(el("strong", value));
        return d;
      }),
    );
    $("scale-summary").replaceChildren(
      ...[
        ["想定避難者数", `${r.evacuees}人`],
        ["想定期間", `${num(r.duration_days)}日間`],
      ].map(([label, value]) => {
        const d = el("div", label);
        d.append(el("strong", value));
        return d;
      }),
    );
    $("input-origins").replaceChildren();
    for (const [key, label] of [
      ["municipality", "自治体"],
      ["disaster", "災害"],
      ["evacuees", "人数"],
      ["duration_days", "期間（日）"],
      ["gross_area_m2", "延床面積"],
      ["staff_count", "スタッフ数"],
      ["accessible_toilet_count", "車いす対応トイレ"],
    ]) {
      const row = el("div", undefined, "origin-row");
      row.append(
        el(
          "span",
          `${label}：${r[key] ?? (key === "staff_count" ? `${s.staff.length}人（生成）` : key === "gross_area_m2" ? `${num(f.geometry.width_m * f.geometry.height_m)} m²（生成）` : "未指定（生成仮定を参照）")}`,
        ),
        el(
          "span",
          r.field_origins?.[key] === "user"
            ? "入力値"
            : r.field_origins?.[key] === "llm_extracted"
              ? "文章から抽出"
              : "補完・仮定",
          "badge",
        ),
      );
      $("input-origins").append(row);
    }
    sourceRows($("preview-sources"), [
      ...(p.research?.sources || []),
      ...(s.provenance?.sources || []),
      ...(s.provenance?.assumptions || []),
      ...(f?.assumptions || []),
    ]);
    $("scenario-json").textContent = json(s);
    $("warnings").replaceChildren(
      ...(p.warnings || []).map((t) => el("p", t, "warning-item")),
    );
    $("blockers").replaceChildren(
      ...(p.approval_blockers?.length
        ? p.approval_blockers.map((t) => el("p", t, "warning-item"))
        : [
            el(
              "p",
              "承認ブロッカーはありません。補完値と警告を確認してください。",
              "quiet-message",
            ),
          ]),
    );
    $("scenario-check").checked = false;
    $("scenario-check").disabled = !p.can_start;
    state.mode = p.llm_usage?.mode ?? "未記録";
    $("mode-badge").textContent = llmModeLabel(state.mode);
    drawMap(
      $("preview-map"),
      {
        geometry: f?.geometry,
        zones: f?.zones,
        agents: [],
        people: [],
        facilities: (f?.facilities || []).map((x) => ({
          ...x,
          point: f.geometry.facility_points[x.id],
        })),
        staff: [],
      },
      "normal",
      1,
    );
    drawPopulation(s);
    controls();
  }
  async function approveScenario() {
    const p = state.preview;
    if (!p?.can_start || !$("scenario-check").checked)
      throw new Error("確認と明示承認が必要です。");
    const runId = `run-${crypto.randomUUID()}`;
    await api(`${base()}/scenario-approve`, {
      preview_id: p.preview_id,
      approved_hash: p.approved_hash,
      approved: true,
      run_id: runId,
    });
    state.run = runId;
    state.branch = "main";
    state.minute = 0;
    state.scope++;
    state.history = [];
    state.comparison = null;
    state.context = null;
    state.selected = null;
    state.selectedGroup = [];
    state.mapPan = { x: 0, y: 0 };
    state.zoom = 1;
    invalidateIntervention();
    state.preview = null;
    $("scenario-check").checked = false;
    $("scenario-check").disabled = true;
    $("start-step").classList.add("done");
    await refreshRuns();
    await refreshRun();
    show("simulation");
  }
  async function refreshRun() {
    const scope = state.scope,
      atHead = state.minute === upper(),
      previousHead = reference()?.checkpoint_hash,
      data = await api(runBase());
    if (scope !== state.scope) return;
    if (state.loadedRun !== state.run) {
      const project = await api(base());
      if (scope !== state.scope) return;
      state.scenario = project.scenarios.find(
        (s) =>
          s.scenario_id === data.scenario_id &&
          s.scenario_version === data.scenario_version,
      )?.scenario;
      state.total = state.scenario?.duration_minutes;
      state.loadedRun = state.run;
      const req =
        state.scenario?.provenance?.natural_language_input ||
        state.scenario?.provenance?.initial_scenario_request;
      state.displayName = req
        ? `${req.municipality} · ${req.disaster}`
        : data.scenario_id;
      const selectedOption = [...$("saved-runs").options].find(
        (o) => o.value === state.run,
      );
      if (selectedOption)
        selectedOption.textContent =
          state.displayName +
          " · " +
          (state.scenario?.profiles?.length ?? "—") +
          "人";
      $("target").max = state.total;
      $("compare-target").max = state.total;
      $("target").value = Math.min(state.total, 60);
      $("compare-target").value = $("target").value;
    }
    state.data = data;
    const branches = data.branches || [];
    if (!branches.some((b) => b.branch_id === state.branch))
      state.branch = branches[0]?.branch_id || "main";
    $("branch-select").replaceChildren(
      ...branches.map((b) => {
        const o = el("option", shortBranch(b.branch_id));
        o.value = b.branch_id;
        o.selected = b.branch_id === state.branch;
        return o;
      }),
    );
    const previous = $("compare-branch").value;
    $("compare-branch").replaceChildren(
      ...branches
        .filter((b) => b.parent_branch_id || b.branch_id !== "main")
        .map((b) => {
          const o = el("option", shortBranch(b.branch_id));
          o.value = b.branch_id;
          o.selected =
            b.branch_id === previous ||
            (!previous && b.branch_id === state.branch);
          return o;
        }),
    );
    if (!$("compare-branch").value && branches.length > 1)
      $("compare-branch").value = branches[1].branch_id;
    state.minute =
      atHead && data.status === "running"
        ? upper()
        : Math.max(lower(), Math.min(upper(), state.minute));
    // A saved boundary is immutable. Poll status cheaply while its checkpoint
    // and displayed target stay unchanged; full reconstruction can be expensive.
    if (
      !state.frame ||
      state.frame.run_id !== state.run ||
      state.frame.branch_id !== state.branch ||
      state.frame.minute !== state.minute ||
      previousHead !== reference()?.checkpoint_hash
    ) await seek(state.minute);
    remember();
    controls();
    if (data.status === "running") {
      clearTimeout(state.poll);
      state.poll = setTimeout(() => {
        refreshRun().catch((e) => note(e.message, "error"));
      }, 1200);
    }
  }
  async function seek(minute, expectedPlayToken) {
    if (!Number.isInteger(minute) || minute < lower() || minute > upper())
      throw new Error("計算済み時刻だけを表示できます。");
    const serial = ++state.seekVersion,
      scope = state.scope,
      branch = state.branch;
    const frame = await api(`${branchBase()}/timeline?minute=${minute}`);
    if (
      (expectedPlayToken !== undefined &&
        expectedPlayToken !== state.playToken) ||
      serial !== state.seekVersion ||
      scope !== state.scope ||
      branch !== state.branch
    )
      return;
    if (state.minute !== minute) {
      invalidateIntervention();
      state.context = null;
    }
    state.frame = frame;
    state.minute = frame.minute;
    renderFrame();
  }
  function renderFrame() {
    const frame = state.frame;
    if (!frame) return;
    if (
      state.sceneMode === "schematic" &&
      frame.geometry &&
      !window.HinanjoScene.supported(frame)
    ) {
      state.sceneMode = "physical";
      document
        .querySelectorAll("[data-scene-mode]")
        .forEach((s) => (s.value = "physical"));
      document
        .querySelectorAll(".map-caption")
        .forEach(
          (n) => (n.textContent = "実座標 · この施設形状は模式図の対象外です"),
        );
    }
    renderKpis($("sim-kpis"), frame.kpis);
    renderKpis($("report-kpis"), frame.kpis);
    $("scenario-name").textContent =
      state.displayName || state.data?.scenario_id || "保存シナリオ";
    $("scenario-summary").textContent =
      `${num(frame.kpis.population)}人 / ${shortBranch(state.branch)}\n${time(state.minute)}（経過${state.minute}分）`;
    const geometryOrigin = {
      synthetic: "合成施設",
      measured: "実測形状",
      operator: "入力された形状",
      inferred: "推定された形状",
    };
    $("map-location").textContent = frame.geometry
      ? `${geometryOrigin[frame.geometry.source_kind] || "施設"} / 記録された形状`
      : "物理座標なし";
    const missing = frame.agents.filter(
      (a) => !a.point || a.position_kind === "display_only",
    ).length;
    $("position-note").textContent =
      state.sceneMode === "schematic" && window.HinanjoScene.supported(frame)
        ? "模式表示：個別の配置は所属を表す記号です。距離・動線は実寸ではありません。"
        : `実座標 ${frame.agents.length - missing}/${frame.agents.length}人記録 · 同じ位置は集合表示 · ドラッグで移動`;
    const issues = [];
    if (frame.kpis.toilet_queue_length > 0)
      issues.push([
        "トイレの待機列",
        `${frame.kpis.toilet_queue_length}人が待機。累積最大待機時間 ${num(frame.kpis.toilet_wait_max_minutes)}分。`,
      ]);
    if (frame.kpis.unresolved_support_request_count > 0)
      issues.push([
        "未解決の支援要請",
        `${frame.kpis.unresolved_support_request_count}件がスタッフの対応待ちです。対応が完了すると下の「イベントタイムライン」に青で、対応されないまま時間切れになると赤で表示されます。`,
      ]);
    for (const f of frame.facilities.filter(
      (f) =>
        typeof f.working_units === "number" && f.working_units < f.capacity,
    ))
      issues.push([
        `${f.id} の稼働制限`,
        `稼働 ${f.working_units}/${f.capacity}`,
      ]);
    if (frame.kpis.staff_load_ratio >= 0.8)
      issues.push([
        "スタッフ負荷が高い状態",
        `稼働比率 ${num(frame.kpis.staff_load_ratio * 100)}%`,
      ]);
    $("issues").replaceChildren(
      ...(issues.length
        ? issues.slice(0, 5).map(([a, b]) => {
            const n = el("div", undefined, "issue");
            n.append(el("strong", a), el("span", b));
            return n;
          })
        : [
            el(
              "p",
              "この時点では表示対象の問題は記録されていません。",
              "quiet-message",
            ),
          ]),
    );
    renderEvents($("events"), frame.events, true);
    renderPeople();
    controls();
    drawAll();
  }
  function renderEvents(box, events, clickable = false) {
    box.replaceChildren();
    if (!events?.length) {
      box.append(el("p", "この取得範囲にイベント記録はありません。", "fine"));
      return;
    }
    const grouped = new Map();
    for (const event of [...events].slice(-100)) {
      const key = `${event.minute}:${event.event_type}:${event.status}`;
      const item = grouped.get(key);
      if (item) item.count++;
      else grouped.set(key, { ...event, count: 1 });
    }
    for (const e of [...grouped.values()].slice(-12)) {
      const node = el(
        clickable ? "button" : "div",
        undefined,
        `event-item event-${eventTone(e)}`,
      );
      node.append(
        el(
          "b",
          `${eventNames[e.event_type] || e.event_type}${e.count > 1 ? ` ×${e.count}件` : ""}`,
        ),
        el(
          "span",
          `${time(e.minute)} · ${{ applied: "適用済み", suppressed: "適用抑制", rejected: "却下", failed: "失敗", scheduled: "予定" }[e.status] || e.status}`,
        ),
      );
      node.title = `${e.event_id} / source: ${e.source_id || "未記録"}`;
      if (clickable) {
        node.disabled = e.minute < lower() || e.minute > upper();
        node.addEventListener("click", () =>
          action("イベントの時刻を取得しています…", () => seek(e.minute)),
        );
      }
      box.append(node);
    }
  }

  function personSummary(p) {
    return `${typeof p.traits?.age_years === "number" ? p.traits.age_years + "歳" : "年齢未記録"} · ${activityNames[p.activity] || p.activity} · ストレス ${num(p.stress * 100)}/100`;
  }
  function renderPeople() {
    const f = state.frame;
    $("people-list").replaceChildren();
    const group = state.selectedGroup?.length
      ? f.people.filter((p) => state.selectedGroup.includes(p.id))
      : f.people.slice(0, 8);
    if (state.selectedGroup?.length > 1)
      $("people-list").append(
        el("span", `同じ記録位置の${group.length}人`, "group-caption"),
      );
    for (const p of group) {
      const button = el("button", p.id, "person-chip");
      button.classList.toggle("selected", p.id === state.selected);
      button.addEventListener("click", () =>
        selectPerson(p.id, state.selectedGroup),
      );
      $("people-list").append(button);
    }
    if (state.selected) {
      const p = f.people.find((p) => p.id === state.selected);
      const staff = f.staff.find((s) => s.id === state.selected);
      const item = p || staff;
      $("person-detail").replaceChildren();
      if (item) {
        const box = el("div", undefined, "person-state");
        box.append(
          el("strong", `♟ ${item.id} ${staff ? "（スタッフ）" : ""}`),
          el(
            "p",
            p
              ? personSummary(p)
              : `担当 ${staff.active_task_id || "なし"} / ${staff.zone_id}`,
          ),
        );
        const detail = el("details");
        detail.append(
          el("summary", "記録された状態の詳細"),
          el("pre", json(item)),
        );
        box.append(detail);
        $("person-detail").append(box);
      } else
        $("person-detail").append(
          el("p", "選択した人物はこの時点の記録に含まれません。", "fine"),
        );
    } else {
      $("person-detail").replaceChildren();
      for (const p of [...f.people]
        .sort((a, b) => (b.stress || 0) - (a.stress || 0))
        .slice(0, 2)) {
        const card = el("button", undefined, "person-preview");
        const avatar = el("span", undefined, "avatar");
        avatar.append(uiIcon("people"));
        const body = el("span");
        body.append(el("strong", p.id), el("small", personSummary(p)));
        card.append(avatar, body);
        card.addEventListener("click", () => selectPerson(p.id));
        $("person-detail").append(card);
      }
    }
  }
  function selectPerson(id, group = []) {
    state.selected = id;
    state.selectedGroup = group;
    renderPeople();
    drawAll();
    openPersonDialog(id);
  }
  const mobilityNames = {
    independent: "自力で移動できる",
    assisted: "介助があれば移動できる",
    wheelchair: "車いすで移動",
    bedridden: "寝たきり",
  };
  // 0〜1 の値を、ラベル付きの横棒で表示する（high=true は値が大きいほど悪い指標）
  function meter(label, value, high = true) {
    const row = el("div", undefined, "pd-meter");
    if (typeof value !== "number" || !Number.isFinite(value)) {
      row.append(el("span", label), el("em", "未記録"));
      return row;
    }
    const pct = Math.max(0, Math.min(100, value * 100));
    const bad = high ? pct : 100 - pct;
    const bar = el("span", undefined, "pd-bar");
    const fill = el(
      "i",
      undefined,
      bad >= 66 ? "pd-bad" : bad >= 33 ? "pd-mid" : "pd-good",
    );
    fill.style.width = `${pct}%`;
    bar.append(fill);
    row.append(el("span", label), bar, el("em", `${num(pct)}`));
    return row;
  }
  function pdSection(title, ...children) {
    const s = el("section", undefined, "pd-section");
    s.append(el("h3", title), ...children);
    return s;
  }
  function pdFacts(pairs) {
    const dl = el("dl", undefined, "pd-facts");
    for (const [k, v] of pairs) dl.append(el("dt", k), el("dd", v ?? "未記録"));
    return dl;
  }
  function openPersonDialog(id) {
    const f = state.frame,
      dialog = $("person-dialog");
    if (!f || !dialog) return;
    const p = f.people.find((x) => x.id === id),
      staff = f.staff.find((x) => x.id === id),
      body = $("person-dialog-body");
    body.replaceChildren();
    if (p) {
      $("person-dialog-title").textContent = `避難者 ${p.id}`;
      body.append(
        el("p", `${time(state.minute)} 時点の記録`, "fine"),
        pdFacts([
          ["年齢", typeof p.traits?.age_years === "number" ? `${p.traits.age_years}歳` : null],
          ["いる場所", zoneNames[p.zone_id] || p.zone_id],
          ["いまの行動", activityNames[p.activity] || p.activity],
          ["移動", mobilityNames[p.mobility] || p.mobility],
          ["世帯", `${p.household_id || "—"}${p.traits?.household_role === "alone" ? "（単身）" : ""}`],
          ["言語", (p.traits?.languages || []).join("、") || null],
          ["配慮が必要なこと", (p.traits?.accessibility_needs || []).join("、") || "なし"],
          ["家族と離れている", p.family_separated ? "はい" : "いいえ"],
        ]),
        pdSection(
          "こころの状態",
          meter("ストレス", p.stress),
          meter("安心感", p.body?.reassurance, false),
          meter("不安", p.body?.uncertainty),
          meter("不満", p.body?.discontent),
          meter("前向きな気持ち", p.body?.positive_affect, false),
          meter("落ち込み", p.body?.negative_affect),
        ),
        pdSection(
          "からだの状態・ニーズ",
          meter("支援の必要度", p.support_need),
          meter("空腹", p.needs?.hunger),
          meter("のどの渇き", p.needs?.hydration_need),
          meter("トイレに行きたい", p.needs?.toilet_urgency),
          meter("疲れ", p.needs?.fatigue),
          meter("衛生面の不快", p.body?.hygiene_need),
          pdFacts([
            [
              "睡眠不足",
              typeof p.body?.sleep_debt_minutes === "number"
                ? `${num(p.body.sleep_debt_minutes / 60)}時間分`
                : null,
            ],
          ]),
        ),
      );
    } else if (staff) {
      $("person-dialog-title").textContent = `スタッフ ${staff.id}`;
      body.append(
        el("p", `${time(state.minute)} 時点の記録`, "fine"),
        pdFacts([
          ["いる場所", zoneNames[staff.zone_id] || staff.zone_id],
          ["担当中の対応", staff.active_task_id || "なし"],
          ["連続勤務", `${staff.dynamics?.consecutive_work_minutes ?? "—"}分`],
          ["できること", (staff.skills || []).join("、") || null],
        ]),
        pdSection("負担", meter("疲れ", staff.dynamics?.fatigue)),
      );
    } else {
      $("person-dialog-title").textContent = id;
      body.append(el("p", "この人物はこの時点の記録に含まれません。", "fine"));
    }
    const raw = el("details");
    raw.append(el("summary", "記録データ（開発者向け）"), el("pre", json(p || staff || {})));
    body.append(raw);
    if (!dialog.open) dialog.showModal();
  }
  function replayTime(minute) {
    const seconds = Math.floor(minute * 60 + 0.00001),
      m = Math.floor(seconds / 60);
    return time(m) + ":" + String(seconds % 60).padStart(2, "0");
  }
  function motionFraction() {
    return state.motion?.fraction || 0;
  }
  async function togglePlayback() {
    if (state.playing) {
      stopPlayback(true);
      return;
    }
    const comparing = state.page === "comparison";
    if (comparing && !state.comparison)
      throw new Error("比較結果を先に取得してください。");
    const hi = comparing ? state.comparison.common_range[1] : upper();
    let current = comparing ? state.comparison.minute : state.minute;
    if (current >= hi) {
      state.replay?.dispose();
      state.replay = null;
      if (comparing) await loadComparison(state.comparison.common_range[0]);
      else await seek(lower());
      current = comparing ? state.comparison.minute : state.minute;
    }
    if (!state.replay) {
      const scope = state.scope,
        root = runBase(),
        branch = branchBase(),
        ids = comparing ? pairIds() : [state.branch];
      state.replay = new window.HinanjoReplayPlayer({
        minute: current,
        hi,
        rate: Number($("speed").value),
        payload: comparing ? state.comparison : state.frame,
        mode: () => state.sceneMode,
        fetch: (minute) =>
          comparing
            ? api(
                `${root}/compare?branch_ids=${enc(ids.join(","))}&minute=${minute}`,
              )
            : api(`${branch}/timeline?minute=${minute}`),
        fetchRange: (start, end) =>
          api(
            `${root}/replay-frames?branch_ids=${enc(ids.join(","))}&start=${start}&end=${end}`,
          ).then((r) => r.samples),
        onFrame: (payload, minute) => {
          if (scope !== state.scope) return;
          state.motion = null;
          if (comparing) void loadComparison(minute, undefined, payload);
          else {
            state.frame = payload;
            state.minute = minute;
            invalidateIntervention();
            state.context = null;
            renderFrame();
          }
        },
        onDraw: (player) => {
          state.motion = player.motion;
          state.visualMs = player.visualMs;
          window.HinanjoReplayView = {
            minute: player.clock.position,
            rate: player.clock.rate,
            bufferedUntil: player.head,
            fraction: player.motion?.fraction || 0,
            from: player.motion?.from.map((f) => f.minute) || [],
            to: player.motion?.to.map((f) => f.minute) || [],
            playing: state.playing,
            comparison: comparing,
          };
          drawAll();
          $("clock").textContent = replayTime(player.clock.position);
          if (comparing)
            $("compare-time").textContent = replayTime(player.clock.position);
          const minute = Math.floor(player.clock.position);
          $("replay-status").textContent =
            player.clock.position >= player.head && player.head < player.hi
              ? "記録を読込中 · 時刻を保持"
              : `${player.clock.rate}× · 記録 ${minute}→${Math.min(minute + 1, player.hi)}分を補間`;
        },
        onState: (status) => {
          if (status === "paused" || status === "end") {
            state.playing = false;
            if (window.HinanjoReplayView)
              window.HinanjoReplayView = {
                ...window.HinanjoReplayView,
                playing: false,
              };
            $("replay-status").textContent = "一時停止 · 位置と姿勢を保持";
            $("play").textContent = "▶ シミュレーションを再生";
            $("timeline-play").textContent = "▶ シミュレーションを再生";
          }
          if (status === "end")
            $("replay-status").textContent = "計算済み範囲の終点";
        },
        onError: (e) => note(e.message, "error"),
        onStats: (stats) => {
          window.HinanjoPlayback = { ...stats, comparison: comparing };
        },
      });
    }
    state.playing = true;
    state.replay.setRate(Number($("speed").value));
    $("play").textContent = "Ⅱ 再生を一時停止";
    $("timeline-play").textContent = "Ⅱ 再生を一時停止";
    state.replay.play();
  }
  async function compute(
    target = Number($("target").value),
    branch = state.branch,
  ) {
    if (
      !Number.isInteger(target) ||
      target <= reference(branch).computed_until ||
      target > duration()
    )
      throw new Error(
        `計算時刻は計算済みの先から終了時刻 ${duration()} 分までの整数で指定してください。`,
      );
    state.target = target;
    await api(`${runBase()}/resume`, {
      branch_id: branch,
      target_minute: target,
    });
    await refreshRun();
  }
  async function waitComputed(branch, target) {
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 900));
      const scope = state.scope,
        data = await api(runBase());
      if (scope !== state.scope)
        throw new Error("表示するRunが変更されました。");
      state.data = data;
      controls();
      if (data.status !== "running") {
        if (reference(branch).computed_until < target)
          throw new Error(
            `計算は「${runStatusLabel(data.status)}」で停止しました。保存済み範囲は保持されています。`,
          );
        return;
      }
    }
  }
  async function previewIntervention() {
    if (state.data.status === "running")
      throw new Error("計算を停止してから介入してください。");
    const text = $("intervention-text").value.trim();
    if (!text) throw new Error("介入内容を入力してください。");
    const captured = {
      text,
      minute: state.minute,
      branch: state.branch,
      scope: state.scope,
    };
    const result = await api(`${branchBase()}/intervention-preview`, {
      text,
      minute: state.minute,
    });
    if (
      text !== $("intervention-text").value.trim() ||
      captured.minute !== state.minute ||
      captured.branch !== state.branch ||
      captured.scope !== state.scope
    )
      return;
    state.intervention = { ...result, captured };
    $("intervention-result").replaceChildren();
    const box = el("div", undefined, "subcard");
    box.append(
      el(
        "strong",
        result.can_apply
          ? "検証済み · 承認すると独立した分岐を作成"
          : "適用できません · 条件の修正が必要",
      ),
      el(
        "p",
        result.explanation || result.mechanism_summary || result.status,
        "fine",
      ),
    );
    box.append(
      el(
        "span",
        `${result.support_mode || "未記録"} / fallback ${result.fallback_used === true ? "使用" : result.fallback_used === false ? "なし" : "未記録"} / 計算済み${state.minute}分`,
        "badge",
      ),
    );
    for (const question of result.questions || [])
      box.append(el("p", question, "fine"));
    if (result.status === "clarification_required" && result.choices?.length) {
      const choices = el("div", undefined, "chips");
      for (const choice of result.choices) {
        const button = el(
          "button",
          `${zoneNames[choice] || choice} (${choice})`,
          "compact",
        );
        button.addEventListener("click", () =>
          action("選択した対象で作用を再プレビューしています…", async () => {
            $("intervention-text").value =
              `${text.replace(/\nzone_id: [^\n]+/g, "")}\nzone_id: ${choice}`;
            invalidateIntervention();
            await previewIntervention();
          }),
        );
        choices.append(button);
      }
      box.append(choices);
    }
    for (const issue of [
      ...(result.validation_issues || []),
      ...(result.limitations || []),
    ])
      box.append(
        el("p", typeof issue === "string" ? issue : json(issue), "fine"),
      );
    const d = el("details");
    d.append(
      el("summary", "変換された作用・対象・出典を確認"),
      el("pre", json(result.program || result)),
    );
    box.append(d);
    $("intervention-result").append(box);
    $("intervention-check").checked = false;
    $("intervention-check").disabled = !result.can_apply;
    controls();
  }
  async function applyIntervention() {
    const i = state.intervention;
    if (
      !i?.can_apply ||
      !$("intervention-check").checked ||
      i.captured.text !== $("intervention-text").value.trim() ||
      i.captured.minute !== state.minute ||
      i.captured.branch !== state.branch
    )
      throw new Error(
        "現在の条件で再プレビューし、作用を確認して承認してください。",
      );
    const child = `branch-${crypto.randomUUID()}`;
    const parent = state.branch;
    await api(`${branchBase()}/intervention-apply`, {
      preview_id: i.preview_id,
      approved_program_hash: i.approved_program_hash,
      approved: true,
      child_id: child,
    });
    state.branch = child;
    state.scope++;
    state.comparison = null;
    state.history = [];
    state.context = null;
    resetChat();
    $("comparison-instruction").textContent =
      `${time(i.captured.minute)}に分岐：${i.captured.text}`;
    $("comparison-effect").replaceChildren(
      el("span", "作用検証を通過・明示承認済み", "badge teal-badge"),
      effectSummary(i.program),
    );
    sessionStorage.setItem(
      `hinanjo-if-intervention-${child}`,
      json({
        parent,
        text: i.captured.text,
        minute: i.captured.minute,
        program: i.program,
      }),
    );
    invalidateIntervention();
    await refreshRun();
    $("compare-branch").value = child;
    await loadComparison();
    show("comparison");
  }
  function effectSummary(program) {
    const box = el("div"),
      names = {
        add_toilet: "トイレを追加",
        add_staff: "スタッフを追加",
        facility_outage: "設備の稼働を停止",
        staff_task_order: "スタッフへ対応を指示",
        resource_delivery: "物資を搬入",
      };
    for (const e of program?.effects || []) {
      const target = e.target_selector?.target_id,
        amount = e.parameters?.count ?? e.parameters?.units;
      box.append(
        el(
          "p",
          `${names[e.op] || e.op}${typeof amount === "number" ? `（${amount}）` : ""} · 対象 ${zoneNames[target] || target || "作用詳細を参照"} · ${time(e.start_minute)}`,
          "effect-row",
        ),
      );
    }
    const details = el("details");
    details.append(
      el("summary", "変換された作用・出典の詳細"),
      el("pre", json(program)),
    );
    box.append(details);
    return box;
  }
  function pairIds() {
    const child = $("compare-branch").value,
      b = reference(child);
    if (!b) throw new Error("介入後の分岐を選択してください。");
    return [b.parent_branch_id || "main", child];
  }
  async function loadComparison(minute, expectedPlayToken, providedResult) {
    const ids = pairIds(),
      refs = ids.map(reference),
      lo = Math.max(...refs.map((r) => r.fork_minute)),
      hi = Math.min(...refs.map((r) => r.computed_until));
    if (lo > hi)
      throw new Error(
        "共通の計算済み範囲がありません。両分岐を計算してください。",
      );
    const m = minute ?? hi;
    if (m < lo || m > hi)
      throw new Error("共通の計算済み範囲だけ比較できます。");
    const scope = state.scope,
      selected = $("compare-branch").value;
    const result =
      providedResult ||
      (await api(
        `${runBase()}/compare?branch_ids=${enc(ids.join(","))}&minute=${m}`,
      ));
    if (
      (expectedPlayToken !== undefined &&
        expectedPlayToken !== state.playToken) ||
      scope !== state.scope ||
      selected !== $("compare-branch").value
    )
      return;
    state.comparison = result;
    if (
      state.sceneMode === "schematic" &&
      !result.frames.every(window.HinanjoScene.supported)
    ) {
      state.sceneMode = "physical";
      document
        .querySelectorAll("[data-scene-mode]")
        .forEach((s) => (s.value = "physical"));
    }
    $("compare-seek").min = lo;
    $("compare-seek").max = hi;
    $("compare-seek").value = m;
    $("comparison-range").textContent = `共通計算済み範囲 ${lo}–${hi} 分`;
    $("compare-time").textContent = time(m);
    $("branch-a-name").textContent = shortBranch(ids[0]);
    $("branch-b-name").textContent = shortBranch(ids[1]);
    const defs = metricDefs.slice(0, 4);
    renderKpis($("compare-kpis-a"), result.frames[0].kpis, defs);
    renderKpis($("compare-kpis-b"), result.frames[1].kpis, defs);
    $("difference-summary").replaceChildren();
    for (const d of defs) {
      const raw = result.differences[0]?.metrics[d.key],
        a = result.frames[0].kpis[d.key],
        b = result.frames[1].kpis[d.key],
        good = typeof raw === "number" && raw < 0,
        bad = raw > 0;
      const card = el(
        "div",
        undefined,
        `difference-card ${good ? "good" : bad ? "bad" : ""}`,
      );
      card.append(
        el("span", d.label),
        el(
          "strong",
          typeof raw === "number"
            ? `${raw > 0 ? "+" : ""}${num(raw * d.factor)} ${d.unit === "/100" || d.unit === "%" ? "pt" : d.unit}`
            : "取得不可",
        ),
        el(
          "span",
          `${num(a * d.factor)} → ${num(b * d.factor)} · ${typeof raw !== "number" ? "未提供" : good ? "減少" : bad ? "増加" : "変化なし"}`,
        ),
      );
      const track = el("div", undefined, "difference-track"),
        bar = el("i");
      bar.style.width = `${typeof raw === "number" ? Math.min(100, (Math.abs(raw) / Math.max(Math.abs(a), Math.abs(b), 1)) * 100) : 0}%`;
      track.append(bar);
      card.append(track);
      $("difference-summary").append(card);
    }
    for (const [index, key] of ["a", "b"].entries()) {
      const frame = result.frames[index],
        missing = frame.facilities.filter(
          (f) => !f.point && !f.service_queues?.some((q) => q.point),
        ).length;
      $(`compare-conditions-${key}`).textContent =
        `${state.sceneMode === "schematic" && window.HinanjoScene.supported(frame) ? "模式図（距離非対応）" : "実座標"} · 稼働トイレ ${num(frame.kpis.toilet_working_units)}基 · 実座標未記録設備 ${missing}件`;
    }
    $("comparison-provenance").textContent = json({
      minute: result.minute,
      shared_origin: result.shared_origin,
      origin_receipts: result.origin_receipts,
      interpretation: result.interpretation,
    });
    try {
      const record = JSON.parse(
        sessionStorage.getItem(`hinanjo-if-intervention-${ids[1]}`) || "null",
      );
      if (record) {
        $("comparison-instruction").textContent =
          `${time(record.minute)}：${record.text}`;
        $("comparison-effect").replaceChildren(
          el("span", "明示承認して適用した作用", "badge teal-badge"),
          effectSummary(record.program),
        );
      } else {
        $("comparison-instruction").textContent =
          `${time(refs[1].fork_minute)} に分岐`;
        $("comparison-effect").replaceChildren(
          el(
            "span",
            result.shared_origin
              ? "同一起点のCheckpointを確認"
              : "分岐の根拠を要確認",
            "badge teal-badge",
          ),
          el(
            "p",
            `稼働トイレ ${num(result.frames[0].kpis.toilet_working_units)} → ${num(result.frames[1].kpis.toilet_working_units)}基 · 介入文の端末記録なし。保存された分岐と施設条件を比較しています。`,
            "fine",
          ),
        );
      }
    } catch {
      /* Session preference is optional. */
    }
    controls();
    drawAll();
  }
  async function computePair() {
    const ids = pairIds();
    const fallback = Math.min(
        duration(),
        Math.max(...ids.map((id) => reference(id).computed_until)) + 60,
      ),
      target = $("compare-target").value
        ? Number($("compare-target").value)
        : fallback;
    if (
      !Number.isInteger(target) ||
      target > duration() ||
      target < Math.max(...ids.map((id) => reference(id).computed_until))
    )
      throw new Error(
        "計算目標を両分岐の計算済み時刻以上、シナリオの終了時刻以下に設定してください。",
      );
    for (const id of ids) {
      if (reference(id).computed_until >= target) continue;
      note(`${shortBranch(id)}を ${target}分まで計算しています…`, "loading");
      await compute(target, id);
      await waitComputed(id, target);
    }
    await refreshRun();
    await loadComparison(target);
  }
  async function loadHistory() {
    const lo = lower(),
      hi = upper(),
      step = Math.max(1, Math.ceil((hi - lo) / 90)),
      params = new URLSearchParams({
        kind: "kpis",
        start: String(lo),
        end: String(hi),
        step: String(step),
        limit: "100",
      });
    const rows = [];
    let cursor = null;
    do {
      if (cursor) params.set("cursor", cursor);
      const page = await api(`${branchBase()}/history?${params}`);
      rows.push(...page.items);
      cursor = page.next_cursor;
    } while (cursor && rows.length < 1000);
    state.history = rows;
    $("history-note").title =
      "グラフはtick終了時、マップはtick直前境界の記録です。避難者数の時系列は現在のKPI履歴に含まれていません。";
    $("history-note").textContent = rows.length
      ? `記録 ${rows.length}点 · ${step}分間隔 · 経過 ${lo}–${hi}分（記録時点の詳細はここにマウスを重ねて確認）`
      : "KPI履歴はまだありません。少なくとも1分計算して更新してください。";
    drawHistory();
  }
  async function loadReport() {
    const scope = state.scope;
    const [report, evidence, llm] = await Promise.all([
      api(`${branchBase()}/report`),
      api(`${runBase()}/evidence`),
      api(`${branchBase()}/llm-status`),
    ]);
    if (scope !== state.scope) return;
    state.report = report;
    state.evidence = evidence;
    state.mode = llm.mode;
    $("mode-badge").textContent =
      `${llmModeLabel(llm.mode)} / fallback ${llm.outcomes?.fallback_count ?? "集計を参照"}`;
    const draft = report.status === "not_generated";
    $("report-draft-note").hidden = !draft;
    $("report-status").classList.toggle("draft-badge", draft);
    $("report-status").textContent =
      draft
        ? "簡易版 · 計算途中のためAIレポート未生成"
        : report.status === "rule_fallback"
          ? "規則による代替レポート"
          : report.status === "unavailable"
            ? "レポート生成不可"
            : report.status === "validated"
              ? "AIレポート生成済み"
              : report.status || "取得済み";
    $("report-raw").textContent = report.markdown || json(report);
    $("report-evidence").textContent = json({ evidence, llm });
    $("report-minute").textContent =
      `${time(state.minute)} / ${shortBranch(state.branch)}`;
    $("report-summary").replaceChildren();
    for (const [title, text] of [
      [
        "記録から確認できること",
        `現在の平均ストレス ${num(state.frame.kpis.stress_mean * 100)}/100。トイレ待機人数 ${num(state.frame.kpis.toilet_queue_length)}人。未解決支援要請 ${num(state.frame.kpis.unresolved_support_request_count)}件。`,
      ],
      [
        "課題と次の検討",
        state.frame.kpis.unresolved_support_request_count > 0
          ? "支援要請とスタッフの資格・稼働の記録を確認し、介入を分岐比較で検討してください。"
          : "取得範囲に記録された事象を確認し、別条件での結果を比較してください。",
      ],
      [
        "解釈上の限界",
        "これは合成人物とモデル内の試算です。実測で校正された予測ではありません。感染症リスク・情報到達率など未提供の指標は表示しません。",
      ],
    ]) {
      const box = el("div", undefined, "report-finding");
      box.append(el("strong", title), el("span", text));
      $("report-summary").append(box);
    }
    const review = report.review?.review;
    const recommended = {
      compare_capacity_intervention:
        "同じ条件を起点に、施設容量の介入を比較する",
      inspect_event_chain: "記録されたイベントと、その後の状態変化を確認する",
      repeat_across_seeds: "複数のseedで同じ分岐比較を繰り返す",
      calibrate_with_aggregate_data: "適用可能な集計データで仮定を校正する",
      review_branch_differences: "結論を出す前に、元の分岐との違いを確認する",
    };
    if (review?.recommendations?.length) {
      const box = el("div", undefined, "report-finding ai-finding");
      box.append(
        el(
          "strong",
          report.status === "validated"
            ? "AIが選んだ検討案（効果は未検証）"
            : "記録に基づく検討案",
        ),
      );
      const list = el("ul");
      for (const item of review.recommendations) {
        if (recommended[item.action])
          list.append(el("li", recommended[item.action]));
      }
      box.append(list);
      $("report-summary").insertBefore(box, $("report-summary").children[2]);
    }
    if (report.markdown)
      $("report-summary").append(
        el(
          "p",
          "Branch reportの生成状態・観測・仮定・提案は、以下の原文から確認できます。",
          "fine",
        ),
      );
    else
      $("report-summary").append(
        el(
          "p",
          report.status === "not_generated"
            ? "終了まで計算するとBranch reportを取得できます。途中の記録とKPIは参照できます。"
            : report.limitation ||
                report.limits ||
                "AIレポートは生成されていません。記録済みの数値を確認してください。",
          "fine",
        ),
      );
    $("report-people").replaceChildren();
    for (const p of state.frame.people.slice(0, 4)) {
      const row = el("div", undefined, "person-row"),
        body = el("div");
      body.append(el("strong", p.id), el("span", personSummary(p)));
      const avatar = el("span", undefined, "avatar");
      avatar.append(uiIcon("people"));
      row.append(avatar, body);
      $("report-people").append(row);
    }
    for (const s of state.frame.staff.slice(0, 2)) {
      const row = el("div", undefined, "person-row");
      const avatar = el("span", undefined, "avatar");
      avatar.append(uiIcon("staff"));
      row.append(
        avatar,
        el(
          "span",
          `${s.id} / ${s.zone_id} / 担当 ${s.active_task_id || "なし"}`,
        ),
      );
      $("report-people").append(row);
    }
    renderEvents($("report-events"), state.frame.events);
    await loadHistory();
    drawAll();
  }
  async function askQuestion() {
    const question = $("question").value.trim();
    if (!question) throw new Error("質問を入力してください。");
    const version = ++state.chatVersion,
      scope = state.scope,
      minute = state.minute,
      branch = state.branch;
    $("chat").querySelector(".chat-intro")?.remove();
    $("chat").append(el("div", question, "chat-bubble user"));
    const ids =
      state.comparison?.frames
        .map((f) => f.branch_id)
        .filter(
          (id) =>
            id !== state.branch &&
            minute >= reference(id).fork_minute &&
            minute <= reference(id).computed_until,
        ) || [];
    const answer = await api(`${branchBase()}/questions`, {
      question,
      minute,
      context_id: state.context,
      comparison_branch_ids: ids,
      ...(state.selected &&
      state.frame.people.some((p) => p.id === state.selected)
        ? { agent_id: state.selected }
        : {}),
    });
    if (
      version !== state.chatVersion ||
      scope !== state.scope ||
      minute !== state.minute ||
      branch !== state.branch
    )
      return;
    state.context = answer.context_id;
    const bubble = el("div", undefined, "chat-bubble answer");
    const usage = answer.usage || {};
    bubble.append(
      el(
        "small",
        `${llmModeLabel(answer.mode)} · ${usage.fallback_used ? "代替処理あり" : usage.outcome === "recorded" ? "API応答を記録・検証" : "記録を参照"} / ${time(minute)}`,
      ),
    );
    if (answer.facts?.length)
      bubble.append(
        el("strong", "記録された事実"),
        ...answer.facts.map((c) => el("p", c.text)),
      );
    if (answer.explanations?.length)
      bubble.append(
        el("strong", "AIによる解釈・提案（記録そのものではありません）"),
        ...answer.explanations.map((c) =>
          el(
            "p",
            `${c.kind === "synthetic_dialogue" ? "合成の声（実発言ではありません）：" : ""}${c.text}`,
          ),
        ),
      );
    if (!answer.facts?.length && !answer.explanations?.length)
      bubble.append(el("p", answer.answer));
    const sourceChips = el("div", undefined, "source-chips");
    for (const source of answer.sources || []) {
      const chip = el(
        "span",
        `${source.alias} · ${source.label}`,
        "source-chip",
      );
      chip.title = json(source.reference);
      sourceChips.append(chip);
    }
    bubble.append(sourceChips);
    const detail = el("details");
    detail.append(
      el(
        "summary",
        `出典・生成情報・回答の限界（${answer.sources?.length || 0}件の出典）`,
      ),
      el(
        "pre",
        json({
          sources: answer.sources || [],
          mode: answer.mode,
          model: answer.model,
          usage,
          limitations: answer.limitations || [],
        }),
      ),
    );
    bubble.append(detail);
    $("chat").append(bubble);
    $("chat").scrollTop = $("chat").scrollHeight;
    $("question").value = "";
  }
  function saveReport() {
    const blob = new Blob(
        [
          json({
            project_id: state.project,
            run_id: state.run,
            branch_id: state.branch,
            minute: state.minute,
            kpis: state.frame.kpis,
            history: state.history,
            report: state.report,
            evidence: state.evidence,
            comparison: state.comparison,
          }),
        ],
        { type: "application/json" },
      ),
      url = URL.createObjectURL(blob),
      a = el("a");
    a.href = url;
    a.download = `hinanjo-if-${state.branch}-${state.minute}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
  // Canvas rendering uses geometry, physical points, and recorded staff positions only.
  function surface(canvas) {
    const r = canvas.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const width = Math.round(r.width * dpr),
      height = Math.round(r.height * dpr);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w: r.width, h: r.height };
  }
  const hits = new Map();
  function drawMap(canvas, frame, layer = "normal", zoom = state.zoom) {
    const sf = surface(canvas);
    if (!sf) return;
    const interactive = [
      "simulation-map",
      "compare-map-a",
      "compare-map-b",
      "report-map",
    ].includes(canvas.id);
    hits.set(
      canvas,
      window.HinanjoMap.draw(sf, frame, {
        viewportBounds:
          state.page === "comparison" &&
          state.sceneMode === "physical" &&
          state.comparison
            ? {
                width: Math.max(
                  ...state.comparison.frames.map(
                    (f) => f.geometry?.width_m || 1,
                  ),
                ),
                height: Math.max(
                  ...state.comparison.frames.map(
                    (f) => f.geometry?.height_m || 1,
                  ),
                ),
              }
            : undefined,
        mode: state.sceneMode,
        animate:
          state.playing &&
          !matchMedia("(prefers-reduced-motion: reduce)").matches,
        time: state.visualMs,
        reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
        gaitDistances: state.replay?.gaits.get(frame?.branch_id),
        layer,
        zoom,
        selected: state.selected,
        pan: interactive ? state.mapPan : { x: 0, y: 0 },
      }),
    );
  }
  function drawPopulation(scenario) {
    const sf = surface($("population-chart"));
    if (!sf) return;
    const { ctx, w, h } = sf,
      people = Object.values(scenario.model_configs?.population?.people || {}),
      groups = [
        ["子ども", 0, "#ffb449"],
        ["成人", 0, "#3c98ff"],
        ["高齢者", 0, "#f26b8c"],
        ["未記録", 0, "#bdcbd7"],
      ];
    for (const p of people) {
      const a = p.age_years;
      groups[typeof a !== "number" ? 3 : a < 18 ? 0 : a >= 65 ? 2 : 1][1]++;
    }
    const total = people.length;
    ctx.font = "10px sans-serif";
    if (!total) {
      ctx.fillText("年齢構成の記録なし", 8, h / 2);
      return;
    }
    let angle = -Math.PI / 2;
    const cx = w / 2,
      cy = h * 0.36,
      r = Math.min(w * 0.28, h * 0.23);
    for (const [label, count, color] of groups) {
      ctx.beginPath();
      ctx.arc(cx, cy, r, angle, angle + (count / total) * Math.PI * 2);
      ctx.arc(
        cx,
        cy,
        r * 0.6,
        angle + (count / total) * Math.PI * 2,
        angle,
        true,
      );
      ctx.closePath();
      ctx.fillStyle = color;
      ctx.fill();
      angle += (count / total) * Math.PI * 2;
      const index = groups.findIndex((g) => g[0] === label);
      ctx.fillRect(10, h * 0.68 + index * 13, 7, 7);
      ctx.fillStyle = "#36577c";
      ctx.fillText(`${label} ${count}人`, 22, h * 0.68 + index * 13 + 7);
    }
  }
  function drawHistory() {
    const sf = surface($("history-chart"));
    if (!sf) return;
    const { ctx, w, h } = sf;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, w, h);
    const key = $("chart-metric").value,
      overview = key === "overview";
    const definitions = overview
      ? [
          {
            key: "stress_mean",
            label: "平均ストレス /100",
            factor: 100,
            color: "#ff365e",
            axis: "left",
          },
          {
            key: "staff_load_ratio",
            label: "スタッフ負荷 %",
            factor: 100,
            color: "#efa023",
            axis: "left",
          },
        ]
      : [
          {
            ...metricDefs.find((d) => d.key === key),
            color:
              key === "stress_mean"
                ? "#ff365e"
                : key === "staff_load_ratio"
                  ? "#efa023"
                  : "#0878ff",
            axis: "left",
          },
        ];
    const raw = state.history
      .map((row) => row.data || row.snapshot || row)
      .filter((r) => typeof r.minute_index === "number");
    const series = definitions
      .map((d) => ({
        ...d,
        points: raw.filter((r) => typeof r[d.key] === "number"),
      }))
      .filter((d) => d.points.length);
    ctx.font = "11px sans-serif";
    ctx.fillStyle = "#6783a4";
    if (!series.length) {
      ctx.fillText("この指標の時系列記録はありません", 20, h / 2);
      return;
    }
    const left = 43,
      right = 18,
      top = 40,
      bottom = 32,
      pw = w - left - right,
      ph = h - top - bottom;
    const minX = Math.min(...raw.map((p) => p.minute_index)),
      maxX = Math.max(minX + 1, ...raw.map((p) => p.minute_index));
    const leftValues = series
      .filter((d) => d.axis === "left")
      .flatMap((d) => d.points.map((p) => p[d.key] * d.factor));
    const high = Math.max(1, ...leftValues),
      maxY = overview
        ? 100
        : high >= 10
          ? Math.ceil((high * 1.05) / 10) * 10
          : high * 1.1;
    let legendX = left;
    for (const d of series) {
      ctx.strokeStyle = d.color;
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(legendX, 13);
      ctx.lineTo(legendX + 16, 13);
      ctx.stroke();
      ctx.fillStyle = "#47658f";
      ctx.textAlign = "left";
      ctx.font = "10px sans-serif";
      ctx.fillText(d.label, legendX + 21, 17);
      legendX += ctx.measureText(d.label).width + 38;
    }
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = top + (ph * i) / 4;
      ctx.strokeStyle = "#e5eef7";
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(w - right, y);
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.fillStyle = "#7186a8";
      ctx.font = "10px sans-serif";
      ctx.fillText(num(maxY * (1 - i / 4)), left - 7, y + 4);
    }
    const xOf = (m) => left + ((m - minX) / (maxX - minX)) * pw;
    for (const d of series) {
      const pos = (p) => [
        xOf(p.minute_index),
        top +
          ph -
          ((p[d.key] * d.factor) / (d.axis === "right" ? 100 : maxY)) * ph,
      ];
      ctx.beginPath();
      d.points.forEach((p, i) => {
        const [x, y] = pos(p);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.strokeStyle = d.color;
      ctx.lineWidth = 2.3;
      ctx.stroke();
      if (d.key === "population") {
        const last = pos(d.points.at(-1)),
          first = pos(d.points[0]);
        ctx.lineTo(last[0], top + ph);
        ctx.lineTo(first[0], top + ph);
        ctx.closePath();
        ctx.fillStyle = "#0878ff0b";
        ctx.fill();
      }
      for (
        let i = 0;
        i < d.points.length;
        i += Math.max(1, Math.ceil(d.points.length / 14))
      ) {
        const [x, y] = pos(d.points[i]);
        ctx.fillStyle = d.color;
        ctx.beginPath();
        ctx.arc(x, y, 2.1, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.textAlign = "center";
    ctx.font = "10px sans-serif";
    ctx.fillStyle = "#647fa3";
    for (let i = 0; i <= 3; i++) {
      const m = Math.round(minX + ((maxX - minX) * i) / 3);
      ctx.textAlign = i === 0 ? "left" : i === 3 ? "right" : "center";
      ctx.fillText(time(m), left + (pw * i) / 3, h - 10);
    }
  }
  function drawAll() {
    const visible = (frame, index = 0) =>
      state.motion
        ? window.HinanjoScene.sample(
            state.motion.from[index],
            state.motion.to[index],
            motionFraction(),
            state.sceneMode,
          )
        : frame;
    if (state.page === "scenario" && state.preview) {
      const f = state.preview.facility_design;
      drawMap(
        $("preview-map"),
        {
          geometry: f?.geometry,
          zones: f?.zones,
          agents: [],
          people: [],
          facilities: (f?.facilities || []).map((x) => ({
            ...x,
            point: f.geometry.facility_points[x.id],
          })),
          staff: [],
        },
        "normal",
        1,
      );
      drawPopulation(state.preview.scenario);
    }
    if (state.page === "simulation")
      drawMap($("simulation-map"), visible(state.frame), $("layer").value);
    if (state.page === "comparison") {
      drawMap(
        $("compare-map-a"),
        visible(state.comparison?.frames[0], 0),
        $("compare-layer").value,
      );
      drawMap(
        $("compare-map-b"),
        visible(state.comparison?.frames[1], 1),
        $("compare-layer").value,
      );
    }
    if (state.page === "report") {
      drawMap($("report-map"), state.frame, $("report-layer").value);
      drawHistory();
    }
  }
  document
    .querySelectorAll("[data-page]")
    .forEach((b) => b.addEventListener("click", () => show(b.dataset.page)));
  $("scenario-form").addEventListener("submit", (e) => {
    e.preventDefault();
    void action(
      "条件を構造化し、施設・人口・出典をプレビューしています…",
      previewScenario,
    );
  });
  $("scenario-form").addEventListener("input", invalidateScenario);
  $("scenario-form").addEventListener("change", invalidateScenario);
  $("example").addEventListener("click", () => {
    $("scenario-text").value =
      "墨田区で地震が発生。避難者100人、7日間。高齢者や子ども連れの支援ニーズも見たい。";
    for (const id of [
      "municipality",
      "disaster",
      "evacuees",
      "duration",
      "staff-count",
      "area",
      "toilets",
      "food",
      "water",
      "facility-type",
    ])
      $(id).value = "";
    invalidateScenario();
  });
  $("scenario-check").addEventListener("change", controls);
  on("approve", approveScenario);
  $("play").addEventListener("click", () => {
    togglePlayback().catch((e) => note(e.message, "error"));
  });
  $("timeline-play").addEventListener("click", () => {
    togglePlayback().catch((e) => note(e.message, "error"));
  });
  $("speed").addEventListener("change", () =>
    state.replay?.setRate(Number($("speed").value)),
  );
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state.playing) stopPlayback(true);
  });
  $("seek").addEventListener("change", () => {
    stopPlayback();
    void action("記録された時刻へ移動しています…", () =>
      seek(Number($("seek").value)),
    );
  });
  on("compute", () => compute());
  on("compute-end", () => compute(duration()));
  $("cancel").addEventListener("click", async () => {
    try {
      await api(`${runBase()}/cancel`, {});
      note("計算の停止を要求しました。保存済み範囲は保持されます。");
      await refreshRun();
    } catch (e) {
      note(e.message, "error");
    }
  });
  $("intervention-text").addEventListener("input", invalidateIntervention);
  on("intervention-preview", previewIntervention);
  $("intervention-check").addEventListener("change", controls);
  on("intervention-apply", applyIntervention);
  $("person-dialog-close").addEventListener("click", () =>
    $("person-dialog").close(),
  );
  // 枠の外（背景）をクリックしても閉じる
  $("person-dialog").addEventListener("click", (e) => {
    if (e.target === $("person-dialog")) $("person-dialog").close();
  });
  on("compare-refresh", () => loadComparison());
  on("compute-pair", computePair);
  $("compare-branch").addEventListener("change", () => {
    state.comparison = null;
    drawAll();
    void action("共通の計算済み範囲を取得しています…", () => loadComparison());
  });
  $("compare-seek").addEventListener("change", () =>
    action("両分岐の同じ時刻を取得しています…", () =>
      loadComparison(Number($("compare-seek").value)),
    ),
  );
  $("target").addEventListener("input", () => {
    $("compare-target").value = $("target").value;
  });
  $("compare-target").addEventListener("input", () => {
    $("target").value = $("compare-target").value;
  });
  function syncLayerTabs() {
    document
      .querySelectorAll("[data-layer]")
      .forEach((b) =>
        b.setAttribute(
          "aria-pressed",
          String(b.dataset.layer === $("layer").value),
        ),
      );
    const layer = $("layer").value,
      colored = ["stress", "health", "crowd"].includes(layer);
    for (const id of ["legend-agents", "legend-queue", "legend-staff"])
      $(id).hidden = colored;
    $("layer-scale").hidden = !colored;
    $("layer-scale").textContent =
      layer === "stress"
        ? "ストレス 低 → 高（0–100）"
        : layer === "health"
          ? "記録ニーズ 低 → 高（診断ではありません）"
          : "Zone内の人数 / 面積（高密度ほど赤）";
  }
  for (const id of ["layer", "compare-layer", "chart-metric"])
    $(id).addEventListener("change", () => {
      syncLayerTabs();
      drawAll();
    });
  document.querySelectorAll("[data-layer]").forEach((b) =>
    b.addEventListener("click", () => {
      $("layer").value = b.dataset.layer;
      syncLayerTabs();
      drawAll();
    }),
  );
  document.querySelectorAll("[data-zoom]").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.zoom === "reset") state.mapPan = { x: 0, y: 0 };
      state.zoom =
        b.dataset.zoom === "reset"
          ? 1
          : Math.max(
              0.6,
              Math.min(
                3,
                state.zoom * (b.dataset.zoom === "in" ? 1.2 : 1 / 1.2),
              ),
            );
      drawAll();
    }),
  );
  for (const id of [
    "simulation-map",
    "compare-map-a",
    "compare-map-b",
    "report-map",
  ]) {
    const canvas = $(id);
    let drag = null;
    canvas.tabIndex = 0;
    const hitAt = (e) => {
      const rect = canvas.getBoundingClientRect();
      return (hits.get(canvas) || [])
        .map((p) => ({
          ...p,
          d: Math.hypot(
            e.clientX - rect.left - (p.hitX ?? p.x),
            e.clientY - rect.top - (p.hitY ?? p.y),
          ),
        }))
        .filter((p) => p.d <= p.r)
        .sort((a, b) => a.d - b.d)[0];
    };
    canvas.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      drag = {
        x: e.clientX,
        y: e.clientY,
        px: state.mapPan.x,
        py: state.mapPan.y,
        moved: false,
      };
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointermove", (e) => {
      if (drag) {
        const dx = e.clientX - drag.x,
          dy = e.clientY - drag.y;
        if (Math.hypot(dx, dy) > 4) drag.moved = true;
        if (drag.moved) {
          state.mapPan = { x: drag.px + dx, y: drag.py + dy };
          canvas.classList.add("dragging");
          drawAll();
        }
      } else {
        const hit = hitAt(e);
        canvas.title =
          hit?.label || "ドラッグで移動 · Ctrl＋ホイールで拡大縮小";
        canvas.style.cursor = hit ? "pointer" : "grab";
      }
    });
    canvas.addEventListener("pointerup", (e) => {
      if (drag && !drag.moved) {
        const hit = hitAt(e);
        if (hit?.type === "person") {
          if (id === "simulation-map" || id === "report-map")
            selectPerson(hit.id, hit.ids);
          else {
            state.selected = hit.id;
            drawAll();
          }
          if (id !== "simulation-map") {
            const f =
              id === "report-map"
                ? state.frame
                : state.comparison.frames[id === "compare-map-a" ? 0 : 1];
            const p = f.people.find((p) => p.id === hit.id),
              a = f.agents.find((a) => a.agent_id === hit.id);
            let detail = canvas.parentElement.querySelector(".map-inspector");
            if (!detail) {
              detail = el("div", undefined, "map-inspector");
              canvas.parentElement.append(detail);
            }
            detail.replaceChildren(
              el("strong", hit.id),
              el("span", p ? personSummary(p) : "スタッフ"),
              el(
                "small",
                `所属 ${a?.zone_id || "—"} · 記録座標 ${a?.point ? num(a.point.x) + ", " + num(a.point.y) + "m" : "未提供"}`,
              ),
            );
          }
        } else if (hit?.type === "facility") {
          $("person-detail").replaceChildren(
            el("div", hit.label, "person-state"),
          );
          state.selected = hit.id;
          drawAll();
        }
      }
      drag = null;
      canvas.classList.remove("dragging");
    });
    canvas.addEventListener("pointercancel", () => {
      drag = null;
      canvas.classList.remove("dragging");
    });
    canvas.addEventListener(
      "wheel",
      (e) => {
        if (!e.ctrlKey && !e.metaKey) return;
        e.preventDefault();
        state.zoom = Math.max(
          0.6,
          Math.min(3, state.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12)),
        );
        drawAll();
      },
      { passive: false },
    );
    canvas.addEventListener("keydown", (e) => {
      const moves = {
        ArrowLeft: [24, 0],
        ArrowRight: [-24, 0],
        ArrowUp: [0, 24],
        ArrowDown: [0, -24],
      };
      if (moves[e.key]) {
        e.preventDefault();
        state.mapPan.x += moves[e.key][0];
        state.mapPan.y += moves[e.key][1];
        drawAll();
      } else if (e.key === "Home") {
        e.preventDefault();
        state.mapPan = { x: 0, y: 0 };
        state.zoom = 1;
        drawAll();
      } else if (e.key === "Escape") {
        state.selected = null;
        state.selectedGroup = [];
        document.querySelectorAll(".map-inspector").forEach((n) => n.remove());
        renderPeople();
        drawAll();
      }
    });
  }
  on("report-refresh", loadReport);
  on("save-report", saveReport);
  $("question-form").addEventListener("submit", (e) => {
    e.preventDefault();
    void action("結果Q&Aが記録済みの根拠を確認しています…", askQuestion);
  });
  document.querySelectorAll("[data-question]").forEach((b) =>
    b.addEventListener("click", () => {
      $("question").value = b.dataset.question;
      $("question").focus();
    }),
  );
  $("branch-select").addEventListener("change", () =>
    action("選択した分岐を取得しています…", async () => {
      state.branch = $("branch-select").value;
      state.scope++;
      state.context = null;
      state.selected = null;
      state.selectedGroup = [];
      state.history = [];
      resetChat();
      invalidateIntervention();
      await refreshRun();
    }),
  );
  $("saved-runs").addEventListener("change", () =>
    action("保存したRunを取得しています…", async () => {
      clearTimeout(state.poll);
      state.scope++;
      state.run = $("saved-runs").value || null;
      state.branch = "main";
      state.data = null;
      state.frame = null;
      state.comparison = null;
      state.history = [];
      state.context = null;
      state.selected = null;
      state.selectedGroup = [];
      state.mapPan = { x: 0, y: 0 };
      state.zoom = 1;
      state.report = null;
      invalidateIntervention();
      resetChat();
      if (state.run) {
        await refreshRun();
        show("simulation");
      } else {
        renderKpis($("sim-kpis"));
        renderKpis($("report-kpis"));
        $("clock").textContent = "開始前";
        $("branch-select").replaceChildren();
        $("compare-branch").replaceChildren();
        $("issues").replaceChildren();
        $("events").replaceChildren();
        $("people-list").replaceChildren();
        $("person-detail").replaceChildren();
        show("scenario");
      }
      remember();
    }),
  );
  new ResizeObserver(() => requestAnimationFrame(drawAll)).observe(
    document.querySelector("main"),
  );
  async function init() {
    const requestedPage = location.hash.slice(1);
    const template = document.querySelector("[data-scene-mode]").parentElement;
    document
      .querySelector(".compare-controls")
      .append(template.cloneNode(true));
    const reportControls = el("div", undefined, "report-map-controls");
    reportControls.append(template.cloneNode(true));
    const reportLayer = el("select");
    reportLayer.id = "report-layer";
    reportLayer.setAttribute("aria-label", "レポートのマップレイヤー");
    for (const [value, label] of [
      ["normal", "通常"],
      ["stress", "ストレス"],
      ["crowd", "混雑"],
      ["health", "健康・ニーズ"],
      ["operations", "運営"],
    ]) {
      const option = el("option", label);
      option.value = value;
      reportLayer.append(option);
    }
    reportControls.append(reportLayer);
    $("report-map").before(reportControls);
    reportLayer.addEventListener("change", drawAll);
    for (const id of ["compare-map-a", "compare-map-b", "report-map"])
      $(id).parentElement.classList.add("map-host");
    document.querySelectorAll("[data-scene-mode]").forEach((select) =>
      select.addEventListener("change", () => {
        state.sceneMode = select.value;
        state.replay?.gaits.clear();
        document
          .querySelectorAll("[data-scene-mode]")
          .forEach((s) => (s.value = state.sceneMode));
        state.mapPan = { x: 0, y: 0 };
        state.zoom = 1;
        if (state.frame) renderFrame();
        else drawAll();
        updateSceneCaption();
      }),
    );
    function updateSceneCaption() {
      document
        .querySelectorAll(".map-caption")
        .forEach(
          (n) =>
            (n.textContent =
              state.sceneMode === "schematic"
                ? "模式図 · 部屋形状・人物の個別配置は表示用／距離は非対応"
                : "実座標 · 床材と背景は表示上の演出"),
        );
      $("report-map").setAttribute(
        "aria-description",
        state.sceneMode === "schematic"
          ? "模式図。個別の配置は所属を表す記号です。"
          : "記録された実座標",
      );
    }
    updateSceneCaption();
    syncLayerTabs();
    document
      .querySelectorAll("[data-icon]")
      .forEach((n) => n.replaceChildren(uiIcon(n.dataset.icon)));
    document.querySelector(".compare-controls").prepend($("compare-refresh"));
    $("compare-refresh").textContent = "比較を更新";
    document.querySelectorAll(".compare-controls [data-zoom]").forEach((b) => {
      b.title =
        "両マップを" +
        (b.dataset.zoom === "in"
          ? "拡大"
          : b.dataset.zoom === "out"
            ? "縮小"
            : "リセット");
      b.setAttribute("aria-label", b.title);
      b.textContent =
        b.dataset.zoom === "in" ? "＋" : b.dataset.zoom === "out" ? "−" : "⌖";
    });
    renderKpis($("sim-kpis"));
    renderKpis($("report-kpis"));
    $("mode-badge").textContent = "実計算 / modeはRun記録で確認";
    const projects = await api("/api/v1/projects");
    if (projects.projects.some((p) => p.project_id === state.project)) {
      await refreshRuns();
      if (state.run) {
        await refreshRun();
        const llm = await api(`${branchBase()}/llm-status`);
        state.mode = llm.mode;
        $("mode-badge").textContent = llmModeLabel(llm.mode);
      }
    } else if (state.run) {
      state.run = null;
      note(
        "保存先に指定Runがありません。新しいシナリオを作成してください。",
        "error",
      );
    }
    show(requestedPage || (state.run ? "simulation" : "scenario"));
    controls();
  }
  init().catch((e) => {
    note(
      `読み込みに失敗しました：${e.message}。URLのproject/run指定を確認してください。`,
      "error",
    );
    controls();
  });
})();
