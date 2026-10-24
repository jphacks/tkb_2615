/* Independent backup UI. WorldState and persisted API artifacts are authoritative. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;
  const json = (value) => JSON.stringify(value, null, 2);
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
    selected: null,
    playing: false,
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
    agent_action_lifecycle: "行動の状態変化",
    agent_travel_complete: "移動完了",
    agent_travel_start: "移動開始",
    staff_task_complete: "スタッフの対応完了",
    staff_task_start: "スタッフの対応開始",
    facility_failure: "設備故障",
    facility_repair: "設備復旧",
    arrival: "避難者到着",
    agent_arrival: "避難者到着",
    resource_delivery: "物資到着",
    information: "情報提供",
    support_request: "支援要請",
    intervention: "介入",
  };
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
  function note(text, kind = "") {
    $("notice").textContent = text;
    $("notice").className = `notice ${kind}`;
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
  function stopPlayback() {
    state.playing = false;
    $("play").textContent = "▶ 再生";
    $("timeline-play").textContent = "▶ 再生";
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
      card.append(el("span", d.icon, "kpi-icon"));
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
      : `${state.data?.status || "未作成"} · 計算速度と再生速度は独立しています`;
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
      `${p.llm_usage?.mode || "記録なし"} · 補完値を要確認`;
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
    $("mode-badge").textContent = `LLM: ${state.mode}`;
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
    await seek(state.minute);
    remember();
    controls();
    if (data.status === "running") {
      clearTimeout(state.poll);
      state.poll = setTimeout(() => {
        refreshRun().catch((e) => note(e.message, "error"));
      }, 1200);
    }
  }
  async function seek(minute) {
    if (!Number.isInteger(minute) || minute < lower() || minute > upper())
      throw new Error("計算済み時刻だけを表示できます。");
    const serial = ++state.seekVersion,
      scope = state.scope,
      branch = state.branch;
    const frame = await api(`${branchBase()}/timeline?minute=${minute}`);
    if (
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
    renderKpis($("sim-kpis"), frame.kpis);
    renderKpis($("report-kpis"), frame.kpis);
    $("scenario-name").textContent =
      state.displayName || state.data?.scenario_id || "保存シナリオ";
    $("scenario-summary").textContent =
      `${num(frame.kpis.population)}人 / ${shortBranch(state.branch)}\n${time(state.minute)}（経過${state.minute}分）`;
    $("map-location").textContent = frame.geometry
      ? "合成施設 / 記録された形状"
      : "物理座標なし";
    const missing = frame.agents.filter(
      (a) => !a.point || a.position_kind === "display_only",
    ).length;
    $("position-note").textContent =
      `人物位置 ${frame.agents.length - missing}/${frame.agents.length}人記録 · 同一座標は人数表示 · 感染症リスクは正式KPI未提供`;
    const issues = [];
    if (frame.kpis.toilet_queue_length > 0)
      issues.push([
        "トイレの待機列",
        `${frame.kpis.toilet_queue_length}人が待機。累積最大待機時間 ${num(frame.kpis.toilet_wait_max_minutes)}分。`,
      ]);
    if (frame.kpis.unresolved_support_request_count > 0)
      issues.push([
        "未解決の支援要請",
        `${frame.kpis.unresolved_support_request_count}件。資格・移動・スタッフ稼働などの制約は記録から確認してください。`,
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
      const node = el(clickable ? "button" : "div", undefined, "event-item");
      node.append(
        el(
          "b",
          `${eventNames[e.event_type] || e.event_type}${e.count > 1 ? ` ×${e.count}件` : ""}`,
        ),
        el(
          "span",
          `${time(e.minute)} · ${e.status === "applied" ? "適用済み" : e.status}`,
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
    return `${p.traits?.age_years ?? "年齢未記録"}歳 · ${activityNames[p.activity] || p.activity} · ストレス ${num(p.stress * 100)}/100`;
  }
  function renderPeople() {
    const f = state.frame;
    $("people-list").replaceChildren();
    for (const p of f.people.slice(0, 8)) {
      const button = el("button", `♟ ${p.id}`, "person-chip");
      button.addEventListener("click", () => selectPerson(p.id));
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
    }
  }
  function selectPerson(id) {
    state.selected = id;
    renderPeople();
    drawAll();
  }
  async function togglePlayback() {
    if (state.playing) {
      stopPlayback();
      return;
    }
    if (state.page === "comparison") {
      if (!state.comparison)
        throw new Error("比較結果を先に取得してください。");
      if (state.comparison.minute >= state.comparison.common_range[1])
        await loadComparison(state.comparison.common_range[0]);
    } else if (state.minute >= upper()) await seek(lower());
    state.playing = true;
    $("play").textContent = "Ⅱ 一時停止";
    $("timeline-play").textContent = "Ⅱ 一時停止";
    void playbackLoop();
  }
  async function playbackLoop() {
    if (!state.playing) return;
    try {
      const comparing = state.page === "comparison",
        minute = comparing ? state.comparison.minute : state.minute,
        hi = comparing ? state.comparison.common_range[1] : upper(),
        next = Math.min(hi, minute + Number($("speed").value));
      if (next === minute) {
        stopPlayback();
        return;
      }
      if (comparing) await loadComparison(next);
      else await seek(next);
      if (state.playing) setTimeout(playbackLoop, 700);
    } catch (e) {
      stopPlayback();
      note(e.message, "error");
    }
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
            `計算は ${data.status} で停止しました。保存済み範囲は保持されています。`,
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
    $("chat").replaceChildren();
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
  async function loadComparison(minute) {
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
    const result = await api(
      `${runBase()}/compare?branch_ids=${enc(ids.join(","))}&minute=${m}`,
    );
    if (scope !== state.scope || selected !== $("compare-branch").value) return;
    state.comparison = result;
    $("compare-seek").min = lo;
    $("compare-seek").max = hi;
    $("compare-seek").value = m;
    $("comparison-range").textContent = `共通計算済み範囲 ${lo}–${hi} 分`;
    $("compare-time").textContent = time(m);
    $("branch-a-name").textContent = `A · ${shortBranch(ids[0])}`;
    $("branch-b-name").textContent = `B · ${shortBranch(ids[1])}`;
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
        missing = frame.facilities.filter((f) => !f.point).length;
      $(`compare-conditions-${key}`).textContent =
        `稼働トイレ ${num(frame.kpis.toilet_working_units)}基 · 物理座標未記録の設備 ${missing}件（推測配置しません）`;
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
          `記録された分岐 ${ids[1]} / 起点 ${refs[1].fork_minute}分`;
        $("comparison-effect").replaceChildren(
          el(
            "p",
            "介入文のローカル表示記録はありません。Checkpointの分岐根拠を参照してください。",
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
    const last = rows.at(-1);
    $("history-note").textContent = rows.length
      ? `記録 ${rows.length}点 · ${step}分間隔の標本 · tick終了時のKPI（マップはtick直前境界）。取得範囲 ${lo}–${hi}分${last ? "" : ""}`
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
      `LLM: ${llm.mode || "記録なし"} / fallback ${llm.outcomes?.fallback_count ?? "集計を参照"}`;
    $("report-status").textContent =
      report.status === "not_generated"
        ? "計算途中 · レポート未生成"
        : report.status === "rule_fallback"
          ? "規則による代替レポート"
          : report.status === "unavailable"
            ? "レポート生成不可"
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
      row.append(el("span", "♟", "avatar"), body);
      $("report-people").append(row);
    }
    for (const s of state.frame.staff.slice(0, 2)) {
      const row = el("div", undefined, "person-row");
      row.append(
        el("span", "◆", "avatar"),
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
    bubble.append(
      el("small", `回答状態：${answer.mode || "記録を参照"} / ${time(minute)}`),
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
    for (const source of answer.sources || [])
      bubble.append(
        el(
          "div",
          `${source.alias} · ${source.label} · ${json(source.reference)}`,
          "source-fine",
        ),
      );
    for (const limitation of answer.limitations || [])
      bubble.append(el("p", limitation, "fine"));
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
    canvas.width = Math.round(r.width * dpr);
    canvas.height = Math.round(r.height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    return { ctx, w: r.width, h: r.height };
  }
  const hits = new Map();
  function drawMap(canvas, frame, layer = "normal", zoom = state.zoom) {
    const sf = surface(canvas);
    if (!sf) return;
    const { ctx, w, h } = sf;
    ctx.fillStyle = "#d4e2dc";
    ctx.fillRect(0, 0, w, h);
    hits.set(canvas, []);
    if (!frame?.geometry) {
      ctx.fillStyle = "#53738f";
      ctx.font = "12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(
        frame
          ? "物理座標が記録されていません"
          : "シナリオを作成すると表示されます",
        w / 2,
        h / 2,
      );
      return;
    }
    const g = frame.geometry,
      pad = Math.min(w, h) * 0.065,
      scale =
        Math.min((w - 2 * pad) / g.width_m, (h - 2 * pad) / g.height_m) * zoom,
      ox = (w - g.width_m * scale) / 2,
      oy = (h - g.height_m * scale) / 2,
      point = (p) => ({ x: ox + p.x * scale, y: oy + p.y * scale });
    ctx.fillStyle = "#c6d3c8";
    for (let x = 0; x < w; x += 22) {
      ctx.fillRect(x, 0, 1, h);
    }
    for (let y = 0; y < h; y += 22) {
      ctx.fillRect(0, y, w, 1);
    }
    ctx.shadowColor = "#17374045";
    ctx.shadowBlur = 14;
    ctx.shadowOffsetY = 6;
    ctx.fillStyle = "#a8b8ad";
    ctx.fillRect(
      ox - 6,
      oy - 6,
      g.width_m * scale + 12,
      g.height_m * scale + 12,
    );
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    const palettes = {
      sleeping: ["#e9c984", "#c5a66a"],
      quiet: ["#e3e4d7", "#9ea7a5"],
      medical: ["#dfedeb", "#98aca6"],
      food: ["#eee1c8", "#b9a77e"],
      toilet: ["#d8ebf2", "#8eabb8"],
      intake: ["#e5edf0", "#a6b7bb"],
      staff: ["#dce6e9", "#8b9ea4"],
      corridor: ["#d1d4cc", "#aeb5b1"],
    };
    const count = {};
    for (const a of frame.agents || [])
      count[a.zone_id] = (count[a.zone_id] || 0) + 1;
    const bounds = [];
    for (const z of g.zones) {
      const key = z.zone_id.split("_")[0],
        colors = palettes[key] || ["#e1e8df", "#acb5ac"];
      const poly = z.polygon.map(point);
      const xs = poly.map((p) => p.x),
        ys = poly.map((p) => p.y),
        x = Math.min(...xs),
        y = Math.min(...ys),
        zw = Math.max(...xs) - x,
        zh = Math.max(...ys) - y;
      bounds.push({ z, x, y, w: zw, h: zh });
      ctx.save();
      ctx.beginPath();
      poly.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      ctx.closePath();
      ctx.fillStyle =
        layer === "crowd"
          ? `hsl(${120 * (1 - Math.min(1, (count[z.zone_id] || 0) / Math.max(1, z.usable_area_m2)))},55%,80%)`
          : colors[0];
      ctx.fill();
      ctx.clip();
      if (key === "sleeping" && layer !== "crowd") {
        ctx.strokeStyle = "#fff4d744";
        ctx.lineWidth = 1;
        for (let yy = y; yy < y + zh; yy += 8) {
          ctx.beginPath();
          ctx.moveTo(x, yy);
          ctx.lineTo(x + zw, yy);
          ctx.stroke();
        }
        ctx.strokeStyle = "#caaa6a55";
        for (let yy = y; yy < y + zh; yy += 24)
          for (
            let xx = x + (Math.round(yy / 24) % 2) * 18;
            xx < x + zw;
            xx += 60
          ) {
            ctx.beginPath();
            ctx.moveTo(xx, yy);
            ctx.lineTo(xx, yy + 8);
            ctx.stroke();
          }
      } else {
        ctx.strokeStyle = "#ffffff55";
        ctx.lineWidth = 0.6;
        for (let xx = x; xx < x + zw; xx += 18) {
          ctx.beginPath();
          ctx.moveTo(xx, y);
          ctx.lineTo(xx, y + zh);
          ctx.stroke();
        }
        for (let yy = y; yy < y + zh; yy += 18) {
          ctx.beginPath();
          ctx.moveTo(x, yy);
          ctx.lineTo(x + zw, yy);
          ctx.stroke();
        }
      }
      ctx.restore();
      ctx.strokeStyle = colors[1];
      ctx.lineWidth = Math.max(2, scale * 0.16);
      ctx.stroke();
    }
    // Stored routes are paths, not invented agent journeys.
    if (layer === "operations")
      for (const route of g.routes || []) {
        ctx.beginPath();
        route.points
          .map(point)
          .forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
        ctx.strokeStyle = "#2589d54a";
        ctx.setLineDash([3, 4]);
        ctx.lineWidth = Math.max(1, scale * 0.06);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    for (const f of frame.facilities || []) {
      if (!f.point) continue;
      const p = point(f.point),
        size = Math.max(5, Math.min(16, scale * 0.52));
      ctx.fillStyle = "#aab9bf";
      ctx.fillRect(p.x - size / 2 + 1, p.y - size / 2 + 2, size, size);
      ctx.fillStyle =
        f.working_units === 0
          ? "#ffc1cf"
          : f.kind === "toilet"
            ? "#f3fcff"
            : f.kind === "medical_support"
              ? "#f9ffff"
              : "#c6a36a";
      ctx.fillRect(p.x - size / 2, p.y - size / 2, size, size);
      ctx.strokeStyle = "#627e90";
      ctx.lineWidth = 1;
      ctx.strokeRect(p.x - size / 2, p.y - size / 2, size, size);
      ctx.fillStyle = f.kind === "medical_support" ? "#ed4666" : "#397694";
      ctx.textAlign = "center";
      ctx.font = `bold ${Math.max(6, size * 0.7)}px sans-serif`;
      ctx.fillText(
        f.kind === "medical_support" ? "+" : f.kind === "toilet" ? "▣" : "▪",
        p.x,
        p.y + size * 0.26,
      );
      if (layer === "operations" && w > 600) {
        ctx.fillStyle = "#1e3d60";
        ctx.font = "9px sans-serif";
        ctx.fillText(
          `${f.working_units ?? f.capacity}/${f.capacity}`,
          p.x,
          p.y - size * 0.9,
        );
      }
    }
    const people = new Map((frame.people || []).map((p) => [p.id, p]));
    function actor(id, physical, person, staff = false) {
      if (!physical) return;
      const p = point(physical);
      if (p.x < 0 || p.x > w || p.y < 0 || p.y > h) return;
      const size = Math.max(2.8, Math.min(6.7, scale * 0.23));
      let color = staff
        ? "#2085dc"
        : person?.activity === "queued"
          ? "#eca72f"
          : "#05a788";
      if (!staff && (layer === "stress" || layer === "health")) {
        const value =
          layer === "stress"
            ? person?.stress
            : Math.max(
                person?.needs?.fatigue || 0,
                person?.needs?.hunger || 0,
                person?.needs?.hydration_need || 0,
              );
        color =
          typeof value === "number"
            ? `hsl(${120 * (1 - Math.max(0, Math.min(1, value)))},75%,44%)`
            : "#8796a9";
      }
      ctx.fillStyle = "#23393525";
      ctx.beginPath();
      ctx.ellipse(
        p.x,
        p.y + size * 1.1,
        size * 0.9,
        size * 0.38,
        0,
        0,
        Math.PI * 2,
      );
      ctx.fill();
      ctx.fillStyle = "#283f60";
      ctx.fillRect(
        p.x - size * 0.45,
        p.y + size * 0.3,
        size * 0.32,
        size * 0.7,
      );
      ctx.fillRect(
        p.x + size * 0.13,
        p.y + size * 0.3,
        size * 0.32,
        size * 0.7,
      );
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.roundRect(
        p.x - size * 0.65,
        p.y - size * 0.5,
        size * 1.3,
        size * 1.15,
        size * 0.3,
      );
      ctx.fill();
      ctx.fillStyle = "#f3ceac";
      ctx.beginPath();
      ctx.arc(p.x, p.y - size * 0.9, size * 0.48, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = staff ? "#e4f5ff" : "#3d4149";
      ctx.beginPath();
      ctx.ellipse(
        p.x,
        p.y - size * 1.17,
        size * 0.49,
        size * 0.24,
        0,
        Math.PI,
        Math.PI * 2,
      );
      ctx.fill();
      if (staff) {
        ctx.fillStyle = "#d5f1ff";
        ctx.fillRect(p.x - size * 0.5, p.y - size * 0.2, size, 1.6);
      }
      if (state.selected === id) {
        ctx.strokeStyle = "#0878ff";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.ellipse(p.x, p.y, size * 1.5, size * 2, 0, 0, Math.PI * 2);
        ctx.stroke();
      }
      hits.get(canvas).push({ id, x: p.x, y: p.y, r: Math.max(10, size * 2) });
    }
    const groups = new Map();
    for (const a of frame.agents || []) {
      if (!a.point || a.position_kind === "display_only") continue;
      const key = `${a.point.x.toFixed(3)},${a.point.y.toFixed(3)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(a);
    }
    for (const group of groups.values()) {
      const a = group[0];
      actor(a.agent_id, a.point, people.get(a.agent_id));
      if (group.length > 1) {
        const p = point(a.point),
          radius = Math.max(9, Math.min(16, scale * 0.5));
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
        const avg =
          group.reduce((sum, item) => {
            const p = people.get(item.agent_id);
            return (
              sum +
              (layer === "health"
                ? Math.max(
                    p?.needs?.fatigue || 0,
                    p?.needs?.hunger || 0,
                    p?.needs?.hydration_need || 0,
                  )
                : p?.stress || 0)
            );
          }, 0) / group.length;
        ctx.fillStyle =
          layer === "stress" || layer === "health"
            ? `hsl(${120 * (1 - avg)},65%,43%)`
            : "#16897d";
        ctx.fill();
        ctx.strokeStyle = "white";
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.fillStyle = "white";
        ctx.font = `bold ${radius * 0.8}px sans-serif`;
        ctx.textAlign = "center";
        ctx.fillText(String(group.length), p.x, p.y + radius * 0.3);
        hits.get(canvas).push({ id: a.agent_id, x: p.x, y: p.y, r: radius });
      }
    }
    for (const s of frame.staff || []) {
      const p = s.position?.point || s.position;
      if (typeof p?.x === "number" && typeof p?.y === "number")
        actor(s.id, p, null, true);
    }
    // Labels stay inside recorded zone bounds; no additional rooms are illustrated.
    for (const b of bounds) {
      const { z, x, y, w: zw, h: zh } = b;
      if (zw < 12 || zh < 12) continue;
      const key = z.zone_id.split("_")[0],
        name = zoneNames[key] || z.zone_id,
        font = Math.max(6, Math.min(12, (zw / Math.min(name.length, 8)) * 0.9));
      ctx.font = `bold ${font}px sans-serif`;
      ctx.textAlign = "center";
      const lines =
          name.length > 7 ? [name.slice(0, 6), name.slice(6)] : [name],
        ly = y + Math.min(zh * 0.18, 25);
      ctx.fillStyle = "#ffffffdf";
      ctx.fillRect(x + 2, ly - font - 3, zw - 4, lines.length * (font + 3) + 6);
      ctx.fillStyle = "#18386b";
      lines.forEach((t, i) =>
        ctx.fillText(t, x + zw / 2, ly + i * (font + 3), zw - 5),
      );
      if (layer === "crowd") {
        ctx.font = `${font}px sans-serif`;
        ctx.fillText(
          `${count[z.zone_id] || 0}人`,
          x + zw / 2,
          ly + lines.length * (font + 3) + font,
        );
      }
    }
    // Obstacles, walls and openings come from the geometry contract when present.
    for (const wall of g.walls || []) {
      if (!wall.start || !wall.end) continue;
      const a = point(wall.start),
        b = point(wall.end);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.strokeStyle = "#647681";
      ctx.lineWidth = 3;
      ctx.stroke();
    }
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
      d = metricDefs.find((d) => d.key === key),
      raw = state.history.map((row) => row.data || row.snapshot || row);
    const points = raw.filter(
      (r) => typeof r[key] === "number" && typeof r.minute_index === "number",
    );
    ctx.font = "10px sans-serif";
    ctx.fillStyle = "#6783a4";
    if (!points.length) {
      ctx.fillText("この指標の時系列記録はありません", 20, h / 2);
      return;
    }
    const l = 44,
      r = 18,
      t = 22,
      b = 34,
      maxX = Math.max(...points.map((p) => p.minute_index), 1),
      minX = Math.min(...points.map((p) => p.minute_index)),
      maxY = Math.max(...points.map((p) => p[key] * d.factor), 1) * 1.1;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = t + ((h - t - b) * i) / 4;
      ctx.strokeStyle = "#e6eef8";
      ctx.beginPath();
      ctx.moveTo(l, y);
      ctx.lineTo(w - r, y);
      ctx.stroke();
      ctx.fillStyle = "#6a80a5";
      ctx.textAlign = "right";
      ctx.fillText(num(maxY * (1 - i / 4)), l - 7, y + 4);
    }
    const pos = (p) => [
      l + ((p.minute_index - minX) / Math.max(1, maxX - minX)) * (w - l - r),
      h - b - ((p[key] * d.factor) / maxY) * (h - t - b),
    ];
    ctx.beginPath();
    points.forEach((p, i) => {
      const [x, y] = pos(p);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle =
      key === "stress_mean"
        ? "#ff365e"
        : key === "staff_load_ratio"
          ? "#f3a11d"
          : "#0878ff";
    ctx.lineWidth = 2;
    ctx.stroke();
    if (points.length === 1) {
      const [x, y] = pos(points[0]);
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fillStyle = "#0878ff";
      ctx.fill();
    }
    ctx.textAlign = "center";
    for (let i = 0; i <= 3; i++) {
      const x = l + ((w - l - r) * i) / 3,
        m = Math.round(minX + ((maxX - minX) * i) / 3);
      ctx.fillStyle = "#6a80a5";
      ctx.fillText(`${m}分`, x, h - 12);
    }
    ctx.textAlign = "left";
    ctx.fillText(`${d.label} (${d.unit})`, l, 12);
  }
  function drawAll() {
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
      drawMap($("simulation-map"), state.frame, $("layer").value);
    if (state.page === "comparison") {
      drawMap(
        $("compare-map-a"),
        state.comparison?.frames[0],
        $("compare-layer").value,
      );
      drawMap(
        $("compare-map-b"),
        state.comparison?.frames[1],
        $("compare-layer").value,
      );
    }
    if (state.page === "report") {
      drawMap($("report-map"), state.frame, "normal", 1);
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
  for (const id of ["layer", "compare-layer", "chart-metric"])
    $(id).addEventListener("change", drawAll);
  document.querySelectorAll("[data-zoom]").forEach((b) =>
    b.addEventListener("click", () => {
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
  $("simulation-map").addEventListener("click", (e) => {
    const rect = e.currentTarget.getBoundingClientRect(),
      x = e.clientX - rect.left,
      y = e.clientY - rect.top;
    const hit = (hits.get(e.currentTarget) || [])
      .map((p) => ({ ...p, d: Math.hypot(x - p.x, y - p.y) }))
      .filter((p) => p.d <= p.r)
      .sort((a, b) => a.d - b.d)[0];
    if (hit) selectPerson(hit.id);
  });
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
      state.history = [];
      $("chat").replaceChildren();
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
      state.report = null;
      invalidateIntervention();
      $("chat").replaceChildren();
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
        $("mode-badge").textContent = `LLM: ${llm.mode || "未記録"}`;
      }
    } else if (state.run) {
      state.run = null;
      note(
        "保存先に指定Runがありません。新しいシナリオを作成してください。",
        "error",
      );
    }
    show(location.hash.slice(1) || (state.run ? "simulation" : "scenario"));
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
