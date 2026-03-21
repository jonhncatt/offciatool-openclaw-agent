const state = {
  sessionId: null,
  uploading: false,
  sendingSessionIds: new Set(),
  attachments: [],
  drilling: false,
  evaluating: false,
};
const SESSION_STORAGE_KEY = "officetool.session_id";

const chatList = document.getElementById("chatList");
const fileInput = document.getElementById("fileInput");
const fileList = document.getElementById("fileList");
const dropZone = document.getElementById("dropZone");
const messageInput = document.getElementById("messageInput");
const sendBtn = document.getElementById("sendBtn");
const newSessionBtn = document.getElementById("newSessionBtn");
const sandboxDrillBtn = document.getElementById("sandboxDrillBtn");
const evalHarnessBtn = document.getElementById("evalHarnessBtn");
const sessionIdView = document.getElementById("sessionIdView");
const sessionHistoryView = document.getElementById("sessionHistoryView");
const refreshSessionsBtn = document.getElementById("refreshSessionsBtn");
const deleteSessionBtn = document.getElementById("deleteSessionBtn");
const tokenStatsView = document.getElementById("tokenStatsView");
const clearStatsBtn = document.getElementById("clearStatsBtn");
const appVersionView = document.getElementById("appVersionView");
const productTitleView = document.getElementById("productTitle");
const productHintView = document.getElementById("productHint");

const modelInput = document.getElementById("modelInput");
const execModeInput = document.getElementById("execModeInput");
const tokenInput = document.getElementById("tokenInput");
const ctxInput = document.getElementById("ctxInput");
const styleInput = document.getElementById("styleInput");
const toolInput = document.getElementById("toolInput");
const panelDebugInput = document.getElementById("panelDebugInput");
const rawDebugInput = document.getElementById("rawDebugInput");
const presetGeneralBtn = document.getElementById("presetGeneralBtn");
const presetCodingBtn = document.getElementById("presetCodingBtn");
const modeStatus = document.getElementById("modeStatus");
const backendPolicyView = document.getElementById("backendPolicyView");
const runStageBadge = document.getElementById("runStageBadge");
const runStageText = document.getElementById("runStageText");
const runStepList = document.getElementById("runStepList");
const runPayloadView = document.getElementById("runPayloadView");
const runTraceView = document.getElementById("runTraceView");
const runAgentPanelsView = document.getElementById("runAgentPanelsView");
const runAnswerBundleView = document.getElementById("runAnswerBundleView");
const runLlmFlowView = document.getElementById("runLlmFlowView");
const runRoleBoard = document.getElementById("runRoleBoard");
const kernelLiveLabel = document.getElementById("kernelLiveLabel");
const kernelLiveMeta = document.getElementById("kernelLiveMeta");
const kernelCoreMetrics = document.getElementById("kernelCoreMetrics");
const shadowLabMetrics = document.getElementById("shadowLabMetrics");
const evolutionMetrics = document.getElementById("evolutionMetrics");
const moduleBay = document.getElementById("moduleBay");
const moduleBayMeta = document.getElementById("moduleBayMeta");
const evolutionFeed = document.getElementById("evolutionFeed");
const evolutionFeedMeta = document.getElementById("evolutionFeedMeta");
const kernelConsoleSection = document.getElementById("kernelConsoleSection");
const kernelConsoleTitle = document.getElementById("kernelConsoleTitle");
const kernelConsoleSubtitle = document.getElementById("kernelConsoleSubtitle");
const roleBoardSection = document.getElementById("roleBoardSection");
const roleBoardTitle = document.getElementById("roleBoardTitle");
const roleBoardLegend = document.getElementById("roleBoardLegend");
const roleLabRuntimeMeta = document.getElementById("roleLabRuntimeMeta");
const roleLabRuntimeMetrics = document.getElementById("roleLabRuntimeMetrics");
const roleLabRegistry = document.getElementById("roleLabRegistry");
const roleLabRunGraph = document.getElementById("roleLabRunGraph");
const roleLabRunFailures = document.getElementById("roleLabRunFailures");
const runtimeDebugSections = Array.from(document.querySelectorAll(".runtime-panel .debug-only"));

const RUN_FLOW_STEPS = [
  { id: "prepare", label: "1. 准备请求" },
  { id: "send", label: "2. 发送请求" },
  { id: "wait", label: "3. 模型处理中" },
  { id: "parse", label: "4. 整理结果" },
  { id: "done", label: "5. 完成" },
];
const PANEL_DEBUG_STORAGE_KEY = "officetool.panel_debug";
let currentRunStepId = null;
let currentRunTone = "idle";

const LLM_FLOW_STAGE_LABELS = {
  frontend_prepare: "前端组包",
  frontend_error: "前端错误",
  backend_ingress: "后端接收输入",
  backend_router: "规则 Router 判定",
  backend_to_llm: "Processor -> Agent",
  llm_to_backend: "Agent -> Processor",
  backend_tool: "Coordinator 执行工具",
  backend_prefetch: "Coordinator 预取",
  backend_coordinator: "Coordinator 状态更新",
  llm_final: "Agent 输出",
  llm_error: "Agent 错误",
  backend_warning: "后端告警",
  backend_pricing: "计费处理",
  multi_agent_planner: "Planner",
  multi_agent_worker: "Worker",
  multi_agent_reviewer: "Reviewer",
  multi_agent_revision: "Revision",
  multi_agent_specialist: "Specialist",
};

const MODE_PRESETS = {
  general: {
    label: "通用模式",
    model: "gpt-5.1-chat",
    maxOutputTokens: 128000,
    maxContextTurns: 2000,
    responseStyle: "normal",
    enableTools: true,
  },
  coding: {
    label: "编码模式",
    model: "gpt-5.1-codex-mini",
    maxOutputTokens: 128000,
    maxContextTurns: 2000,
    responseStyle: "normal",
    enableTools: true,
  },
};

const ROLE_DEFS = [
  {
    id: "router",
    title: "Router",
    kindKey: "hybrid",
    kindLabel: "Agent + Processor",
    blurb: "为当前请求分诊，决定后续链路。",
    colors: { accent: "#4f7eff", accent2: "#98b7ff" },
  },
  {
    id: "coordinator",
    title: "Coordinator",
    kindKey: "processor",
    kindLabel: "Processor",
    blurb: "维护运行时状态，推动工具链与纠偏。",
    colors: { accent: "#c66c2d", accent2: "#f3b170" },
  },
  {
    id: "planner",
    title: "Planner",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "提炼目标、限制与执行计划。",
    colors: { accent: "#2d9f6f", accent2: "#8dd6b1" },
  },
  {
    id: "researcher",
    title: "Researcher",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "生成联网搜索与取证简报。",
    colors: { accent: "#2e77bb", accent2: "#89bde9" },
  },
  {
    id: "file_reader",
    title: "FileReader",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "为文档和附件生成阅读定位策略。",
    colors: { accent: "#7e5cff", accent2: "#c7b7ff" },
  },
  {
    id: "summarizer",
    title: "Summarizer",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "把大段内容压成高信息量摘要。",
    colors: { accent: "#20a2a5", accent2: "#8ad9da" },
  },
  {
    id: "fixer",
    title: "Fixer",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "聚焦修复动作与补丁方向。",
    colors: { accent: "#d98a1f", accent2: "#f5c06c" },
  },
  {
    id: "worker",
    title: "Worker",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "执行主任务，必要时调用工具链。",
    colors: { accent: "#137a58", accent2: "#60c79f" },
  },
  {
    id: "conflict_detector",
    title: "Conflict Detector",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "报警明显冲突与高风险确定性。",
    colors: { accent: "#c94a4a", accent2: "#f0a35c" },
  },
  {
    id: "reviewer",
    title: "Reviewer",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "审查覆盖度、证据链和交付风险。",
    colors: { accent: "#2c8b4b", accent2: "#8cd2a1" },
  },
  {
    id: "revision",
    title: "Revision",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "按审阅结论做最后修订。",
    colors: { accent: "#a45ad1", accent2: "#e3b8ff" },
  },
  {
    id: "structurer",
    title: "Structurer",
    kindKey: "agent",
    kindLabel: "Agent",
    blurb: "整理结构化证据包与 assertions（关键结论）。",
    colors: { accent: "#3f7f9b", accent2: "#9fcbe0" },
  },
];

const ROLE_DEF_MAP = new Map(ROLE_DEFS.map((item) => [item.id, item]));
const ROLE_KIND_LABELS = {
  agent: "Agent",
  processor: "Processor",
  hybrid: "Agent + Processor",
};
const ROLE_TOKEN_MAP = new Map([
  ["router", "router"],
  ["coordinator", "coordinator"],
  ["planner", "planner"],
  ["researcher", "researcher"],
  ["file_reader", "file_reader"],
  ["file reader", "file_reader"],
  ["summarizer", "summarizer"],
  ["fixer", "fixer"],
  ["worker", "worker"],
  ["reviewer", "reviewer"],
  ["revision", "revision"],
  ["structurer", "structurer"],
  ["conflict_detector", "conflict_detector"],
  ["conflict detector", "conflict_detector"],
]);

function normalizeRoleId(value) {
  const raw = String(value || "").trim().toLowerCase();
  return ROLE_TOKEN_MAP.get(raw) || raw.replace(/\s+/g, "_");
}

function normalizeRoleKind(value, fallback = "agent") {
  const raw = String(value || "").trim().toLowerCase();
  if (raw === "agent" || raw === "processor" || raw === "hybrid") return raw;
  return fallback;
}

function normalizeRoleSet(value) {
  const roles = new Set();
  (Array.isArray(value) ? value : []).forEach((item) => {
    const roleId = normalizeRoleId(item);
    if (roleId) roles.add(roleId);
  });
  return roles;
}

function normalizeRoleStateMap(value) {
  const states = new Map();
  (Array.isArray(value) ? value : []).forEach((item) => {
    const roleId = normalizeRoleId(item?.role);
    if (!roleId) return;
    states.set(roleId, {
      role: roleId,
      status: String(item?.status || "").trim().toLowerCase(),
      phase: String(item?.phase || "").trim(),
      detail: String(item?.detail || "").trim(),
    });
  });
  return states;
}

function detectRolesFromText(text) {
  const lower = String(text || "").toLowerCase();
  const roles = new Set();
  ROLE_TOKEN_MAP.forEach((roleId, token) => {
    if (lower.includes(token)) roles.add(roleId);
  });
  if (lower.includes("specialist")) {
    ["researcher", "file_reader", "summarizer", "fixer"].forEach((roleId) => {
      if (lower.includes(roleId.replace("_", " ")) || lower.includes(roleId)) roles.add(roleId);
    });
  }
  return roles;
}

function inferActiveRolesFromDebugItem(item) {
  const roles = detectRolesFromText(`${String(item?.title || "")}\n${String(item?.detail || "")}`);
  const stage = String(item?.stage || "").trim().toLowerCase();
  if (stage === "backend_router") {
    roles.add("router");
    roles.add("coordinator");
  }
  if (stage === "backend_tool" || stage === "backend_prefetch" || stage === "backend_coordinator") {
    roles.add("coordinator");
  }
  if (stage === "backend_to_llm" || stage === "llm_to_backend" || stage === "llm_final" || stage === "llm_error") {
    if (roles.size) roles.add("coordinator");
  }
  return roles;
}

function svgRect(x, y, color, size = 4) {
  return `<rect x="${x * size}" y="${y * size}" width="${size}" height="${size}" fill="${color}" />`;
}

function buildRoleSprite(roleId) {
  const meta = ROLE_DEF_MAP.get(roleId) || ROLE_DEFS[0];
  const outline = "#1f2f27";
  const shell = "#dcefe2";
  const shadow = "#a3c1b1";
  const eye = meta.kindKey === "processor" ? "#fff1c9" : "#f6fff6";
  const accent = meta.colors.accent;
  const accent2 = meta.colors.accent2;
  const px = [];
  const add = (cells, color) => {
    cells.forEach(([x, y]) => px.push(svgRect(x, y, color)));
  };

  add(
    [
      [3, 1], [4, 1], [5, 1], [6, 1], [7, 1], [8, 1],
      [2, 2], [9, 2], [2, 3], [9, 3], [1, 3], [10, 3],
      [2, 4], [9, 4], [1, 4], [10, 4],
      [2, 5], [9, 5],
      [2, 6], [9, 6],
      [3, 7], [4, 7], [5, 7], [6, 7], [7, 7], [8, 7],
      [3, 8], [8, 8], [3, 9], [8, 9],
      [4, 10], [5, 10], [6, 10], [7, 10],
      [4, 11], [7, 11],
    ],
    outline
  );
  add(
    [
      [3, 2], [4, 2], [5, 2], [6, 2], [7, 2], [8, 2],
      [3, 3], [4, 3], [5, 3], [6, 3], [7, 3], [8, 3],
      [3, 4], [4, 4], [5, 4], [6, 4], [7, 4], [8, 4],
      [3, 5], [4, 5], [5, 5], [6, 5], [7, 5], [8, 5],
      [3, 6], [4, 6], [5, 6], [6, 6], [7, 6], [8, 6],
      [4, 8], [5, 8], [6, 8], [7, 8],
      [4, 9], [5, 9], [6, 9], [7, 9],
      [5, 11], [6, 11],
    ],
    shell
  );
  add(
    [
      [4, 6], [5, 6], [6, 6], [7, 6],
      [4, 9], [5, 9], [6, 9], [7, 9],
    ],
    shadow
  );
  add(
    roleId === "conflict_detector" ? [[4, 4]] : [[4, 4], [7, 4]],
    roleId === "conflict_detector" ? "#ffef78" : eye
  );
  if (roleId === "conflict_detector") {
    add([[7, 4]], "#ff8872");
  }

  switch (roleId) {
    case "router":
      add([[2, 0], [3, 0], [8, 0], [9, 0], [5, 8], [6, 9]], accent);
      add([[4, 0], [7, 0], [5, 9], [6, 8]], accent2);
      break;
    case "coordinator":
      add([[4, 0], [5, 0], [6, 0], [7, 0], [5, 3], [6, 3], [5, 8], [6, 8]], accent);
      add([[5, 1], [6, 1], [4, 8], [7, 8]], accent2);
      break;
    case "planner":
      add([[2, 0], [3, 0], [4, 0], [5, 0], [6, 0], [7, 0], [8, 0], [9, 0]], accent);
      add([[4, 8], [5, 8], [6, 8], [7, 8], [4, 9], [7, 9]], accent2);
      break;
    case "researcher":
      add([[6, 0], [6, 1], [10, 2], [10, 3], [8, 8], [8, 9]], accent);
      add([[7, 1], [9, 2], [9, 3], [5, 8], [6, 8]], accent2);
      break;
    case "file_reader":
      add([[4, 8], [4, 9], [5, 8], [5, 9]], accent);
      add([[6, 8], [6, 9], [7, 8], [7, 9]], accent2);
      add([[5, 9], [6, 9]], outline);
      break;
    case "summarizer":
      add([[4, 5], [5, 5], [6, 5], [7, 5], [3, 9], [5, 8], [7, 9]], accent);
      add([[4, 8], [6, 8], [8, 9]], accent2);
      break;
    case "fixer":
      add([[0, 8], [1, 8], [2, 8], [8, 9], [9, 8], [10, 7]], accent);
      add([[1, 7], [2, 9], [9, 7], [10, 8]], accent2);
      break;
    case "worker":
      add([[3, 3], [4, 3], [5, 3], [6, 3], [7, 3], [8, 3], [4, 9], [5, 9], [6, 9], [7, 9]], accent);
      add([[3, 4], [8, 4], [4, 8], [7, 8]], accent2);
      break;
    case "conflict_detector":
      add([[5, 0], [6, 0], [5, 8], [6, 8]], accent);
      add([[4, 0], [7, 0], [4, 8], [7, 8]], accent2);
      break;
    case "reviewer":
      add([[5, 8], [6, 8], [4, 9], [5, 9], [6, 9], [7, 9], [5, 10], [6, 10]], accent);
      add([[5, 1], [6, 1], [5, 2], [6, 2]], accent2);
      break;
    case "revision":
      add([[3, 0], [4, 1], [5, 2], [6, 3], [7, 4], [8, 5], [4, 9], [5, 8], [6, 9], [7, 8]], accent);
      add([[4, 0], [5, 1], [6, 2], [7, 3], [8, 4]], accent2);
      break;
    case "structurer":
      add([[4, 8], [5, 8], [6, 8], [7, 8], [4, 9], [7, 9], [4, 10], [5, 10], [6, 10], [7, 10]], accent);
      add([[5, 9], [6, 9]], accent2);
      break;
    default:
      add([[5, 8], [6, 8], [5, 9], [6, 9]], accent);
      break;
  }

  return `<svg class="role-sprite" viewBox="0 0 48 48" aria-hidden="true">${px.join("")}</svg>`;
}

function renderRoleBoard(panels = [], activeRoles = new Set(), currentRole = null, roleStates = new Map()) {
  if (!runRoleBoard) return;
  const panelMap = new Map();
  (Array.isArray(panels) ? panels : []).forEach((panel) => {
    const roleId = normalizeRoleId(panel?.role);
    if (roleId) panelMap.set(roleId, panel);
  });

  runRoleBoard.innerHTML = "";
  ROLE_DEFS.forEach((meta) => {
    const panel = panelMap.get(meta.id);
    const roleState = roleStates instanceof Map ? roleStates.get(meta.id) : null;
    const isActive = activeRoles instanceof Set ? activeRoles.has(meta.id) : false;
    const isCurrent = normalizeRoleId(currentRole) === meta.id;
    const isSeen = Boolean(panel) || isActive;
    const card = document.createElement("article");
    card.className = `role-card${isSeen ? " is-seen" : ""}${isActive ? " is-active" : ""}${isCurrent ? " is-current" : ""}`;
    const kindKey = normalizeRoleKind(panel?.kind, meta.kindKey);
    const kindLabel = ROLE_KIND_LABELS[kindKey] || meta.kindLabel;

    const head = document.createElement("div");
    head.className = "role-card-head";

    const spriteWrap = document.createElement("div");
    spriteWrap.className = "role-sprite-wrap";
    spriteWrap.innerHTML = buildRoleSprite(meta.id);
    head.appendChild(spriteWrap);

    const metaNode = document.createElement("div");
    metaNode.className = "role-meta";

    const nameNode = document.createElement("div");
    nameNode.className = "role-name";
    nameNode.textContent = meta.title;
    metaNode.appendChild(nameNode);

    const kindRow = document.createElement("div");
    kindRow.className = "role-kind-row";

    const kindNode = document.createElement("span");
    kindNode.className = `role-kind ${kindKey}`;
    kindNode.textContent = kindLabel;
    kindRow.appendChild(kindNode);

    const stateNode = document.createElement("span");
    stateNode.className = `role-state ${isCurrent ? "current" : isActive ? "active" : isSeen ? "seen" : "idle"}`;
    stateNode.textContent = isCurrent ? "主工作中" : isActive ? "协同中" : isSeen ? "已参与" : "待命";
    kindRow.appendChild(stateNode);

    metaNode.appendChild(kindRow);
    head.appendChild(metaNode);
    card.appendChild(head);

    const summaryNode = document.createElement("div");
    summaryNode.className = "role-summary";
    const baseSummary = String(meta?.blurb || "").trim();
    const liveSummary = String(panel?.summary || "").trim();
    summaryNode.textContent = isPanelDebugEnabled() ? (liveSummary || baseSummary) : (baseSummary || liveSummary);
    card.appendChild(summaryNode);

    const phaseText = String(roleState?.phase || "").trim();
    const detailText = String(roleState?.detail || "").trim();
    if (phaseText || detailText) {
      const phaseNode = document.createElement("div");
      phaseNode.className = "role-phase";
      phaseNode.textContent = phaseText ? `${phaseText}${detailText ? ` · ${detailText}` : ""}` : detailText;
      card.appendChild(phaseNode);
    }

    const bullets = Array.isArray(panel?.bullets) ? panel.bullets.slice(0, 2) : [];
    if (bullets.length) {
      const list = document.createElement("ul");
      list.className = "role-bullets";
      bullets.forEach((item) => {
        const li = document.createElement("li");
        li.textContent = String(item || "");
        list.appendChild(li);
      });
      card.appendChild(list);
    }

    runRoleBoard.appendChild(card);
  });
}

function applyModePreset(mode, announce = true) {
  const preset = MODE_PRESETS[mode];
  if (!preset) return;

  modelInput.value = preset.model;
  tokenInput.value = String(preset.maxOutputTokens);
  ctxInput.value = String(preset.maxContextTurns);
  styleInput.value = preset.responseStyle;
  toolInput.checked = Boolean(preset.enableTools);

  if (modeStatus) {
    modeStatus.textContent = `当前模式：${preset.label}`;
  }
  if (presetGeneralBtn) {
    presetGeneralBtn.classList.toggle("preset-active", mode === "general");
  }
  if (presetCodingBtn) {
    presetCodingBtn.classList.toggle("preset-active", mode === "coding");
  }
  if (announce) {
    addBubble(
      "system",
      `已切换到${preset.label}：model=${preset.model}，max_tokens=${preset.maxOutputTokens}，context=${preset.maxContextTurns}`
    );
  }
}

function isPanelDebugEnabled() {
  return Boolean(panelDebugInput?.checked);
}

function applyPanelDebugMode(enabled, { persist = true } = {}) {
  const value = Boolean(enabled);
  if (panelDebugInput) {
    panelDebugInput.checked = value;
  }
  document.body.classList.toggle("panel-debug-on", value);
  document.body.classList.toggle("panel-debug-off", !value);
  runtimeDebugSections.forEach((node) => {
    node.hidden = !value;
    if (!value && "open" in node) node.open = false;
  });
  if (persist) {
    try {
      window.localStorage.setItem(PANEL_DEBUG_STORAGE_KEY, value ? "1" : "0");
    } catch {}
  }
  renderRunSteps(currentRunStepId, currentRunTone === "error");
}

function restorePanelDebugMode() {
  let enabled = false;
  try {
    const raw = String(window.localStorage.getItem(PANEL_DEBUG_STORAGE_KEY) || "").trim().toLowerCase();
    enabled = raw === "1" || raw === "true" || raw === "yes" || raw === "on";
  } catch {}
  applyPanelDebugMode(enabled, { persist: false });
}

function hasAnswerBundleContent(bundle) {
  if (!bundle || typeof bundle !== "object") return false;
  return Boolean(
    String(bundle.summary || "").trim() ||
      (Array.isArray(bundle.claims) && bundle.claims.length) ||
      (Array.isArray(bundle.citations) && bundle.citations.length) ||
      (Array.isArray(bundle.warnings) && bundle.warnings.length)
  );
}

function normalizeCitationIds(rawIds) {
  const seen = new Set();
  const out = [];
  (Array.isArray(rawIds) ? rawIds : []).forEach((item) => {
    const id = String(item || "").trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    out.push(id);
  });
  return out;
}

function normalizeMatchText(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^a-z0-9\u3400-\u9fff\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function collectLatinTokens(text) {
  const matches = normalizeMatchText(text).match(/[a-z0-9_]{2,}/g) || [];
  return Array.from(new Set(matches));
}

function collectCjkBigrams(text) {
  const cjkChars = (String(text || "").match(/[\u3400-\u9fff]/g) || []).join("");
  if (cjkChars.length < 2) return [];
  const out = [];
  for (let i = 0; i < cjkChars.length - 1; i += 1) {
    out.push(cjkChars.slice(i, i + 2));
  }
  return Array.from(new Set(out));
}

function overlapRatio(sourceTokens, targetTokenSet) {
  const tokens = Array.isArray(sourceTokens) ? sourceTokens : [];
  if (!tokens.length) return 0;
  let hit = 0;
  tokens.forEach((token) => {
    if (targetTokenSet.has(token)) hit += 1;
  });
  return hit / tokens.length;
}

function scoreClaimToNode(claimText, nodeText) {
  const claimJoined = normalizeMatchText(claimText).replace(/\s+/g, "");
  const nodeJoined = normalizeMatchText(nodeText).replace(/\s+/g, "");
  if (!claimJoined || !nodeJoined) return { score: 0, matched: false };

  let containmentScore = 0;
  if (claimJoined.length >= 12 && nodeJoined.includes(claimJoined)) {
    containmentScore = 1;
  } else if (nodeJoined.length >= 12 && claimJoined.includes(nodeJoined)) {
    containmentScore = 0.82;
  }

  const claimLatin = collectLatinTokens(claimText);
  const claimCjk = collectCjkBigrams(claimText);
  const nodeLatinSet = new Set(collectLatinTokens(nodeText));
  const nodeCjkSet = new Set(collectCjkBigrams(nodeText));
  const latinScore = overlapRatio(claimLatin, nodeLatinSet);
  const cjkScore = overlapRatio(claimCjk, nodeCjkSet);

  const lexicalScore =
    claimLatin.length && claimCjk.length
      ? latinScore * 0.55 + cjkScore * 0.45
      : claimLatin.length
      ? latinScore
      : cjkScore;
  const score = Math.max(containmentScore, lexicalScore);
  const matched =
    containmentScore >= 0.82 ||
    latinScore >= 0.5 ||
    cjkScore >= 0.45 ||
    (score >= 0.42 && (claimLatin.length >= 2 || claimCjk.length >= 3));
  return { score, matched };
}

function annotateAssistantInlineCitations(contentNode, bundle) {
  if (!contentNode || !bundle || typeof bundle !== "object") return;
  const claims = Array.isArray(bundle.claims) ? bundle.claims : [];
  if (!claims.length) return;

  let targetNodes = Array.from(
    contentNode.querySelectorAll("p, li, blockquote, h1, h2, h3, h4, h5, h6")
  ).filter((node) => {
    const text = String(node?.textContent || "").trim();
    return text.length >= 8;
  });
  if (!targetNodes.length) {
    const fallbackText = String(contentNode.textContent || "").trim();
    if (fallbackText.length >= 12) targetNodes = [contentNode];
  }
  if (!targetNodes.length) return;

  const nodeCitations = new Map();
  claims.forEach((claim) => {
    const ids = normalizeCitationIds(claim?.citation_ids);
    const statement = String(claim?.statement || "").trim();
    if (!ids.length || !statement) return;

    let bestNode = null;
    let bestScore = 0;
    targetNodes.forEach((node) => {
      const text = String(node?.textContent || "").trim();
      if (!text) return;
      const match = scoreClaimToNode(statement, text);
      if (!match.matched) return;
      if (match.score > bestScore) {
        bestScore = match.score;
        bestNode = node;
      }
    });

    if (!bestNode || bestScore < 0.42) return;
    if (!nodeCitations.has(bestNode)) nodeCitations.set(bestNode, new Set());
    const holder = nodeCitations.get(bestNode);
    ids.forEach((id) => holder.add(id));
  });

  nodeCitations.forEach((idSet, node) => {
    const merged = Array.from(idSet).filter(Boolean);
    if (!merged.length) return;
    const oldMarker = node.querySelector(".inline-citation-tag[data-generated='1']");
    if (oldMarker) oldMarker.remove();

    const marker = document.createElement("span");
    marker.className = "inline-citation-tag";
    marker.setAttribute("data-generated", "1");
    marker.textContent = ` [${merged.join(", ")}]`;
    node.appendChild(marker);
  });
}

function partitionAnswerCitations(citations) {
  const evidence = [];
  const candidates = [];
  (Array.isArray(citations) ? citations : []).forEach((citation) => {
    const kind = String(citation?.kind || "").trim().toLowerCase();
    if (kind === "candidate") {
      candidates.push(citation);
    } else {
      evidence.push(citation);
    }
  });
  return { evidence, candidates };
}

function appendCitationSection(wrap, titleText, citations, noteText = "") {
  if (!Array.isArray(citations) || !citations.length) return;
  const section = document.createElement("div");
  section.className = "answer-bundle-section";
  const title = document.createElement("div");
  title.className = "answer-bundle-title";
  title.textContent = titleText;
  section.appendChild(title);

  if (noteText) {
    const note = document.createElement("div");
    note.className = "answer-bundle-meta";
    note.textContent = noteText;
    section.appendChild(note);
  }

  citations.slice(0, 8).forEach((citation) => {
    const item = document.createElement("div");
    item.className = "answer-bundle-item";
    const heading = document.createElement("div");
    heading.className = "answer-bundle-statement";
    const label = String(citation?.label || citation?.title || citation?.url || citation?.path || citation?.id || "source").trim();
    heading.textContent = `${String(citation?.id || "").trim() || "-"} · ${label}`;
    item.appendChild(heading);

    const meta = [];
    if (citation?.tool) meta.push(`tool: ${citation.tool}`);
    if (citation?.domain) meta.push(`domain: ${citation.domain}`);
    if (citation?.locator) meta.push(`locator: ${citation.locator}`);
    if (citation?.published_at) meta.push(`published: ${citation.published_at}`);
    if (meta.length) {
      const metaNode = document.createElement("div");
      metaNode.className = "answer-bundle-meta";
      metaNode.textContent = meta.join(" | ");
      item.appendChild(metaNode);
    }

    const excerpt = String(citation?.excerpt || "").trim();
    if (excerpt) {
      const excerptNode = document.createElement("div");
      excerptNode.className = "answer-bundle-excerpt";
      excerptNode.textContent = excerpt;
      item.appendChild(excerptNode);
    }

    const link = String(citation?.url || "").trim();
    if (link) {
      const linkNode = document.createElement("a");
      linkNode.className = "answer-bundle-link";
      linkNode.href = link;
      linkNode.target = "_blank";
      linkNode.rel = "noreferrer noopener";
      linkNode.textContent = link;
      item.appendChild(linkNode);
    } else if (citation?.path) {
      const pathNode = document.createElement("div");
      pathNode.className = "answer-bundle-meta";
      pathNode.textContent = `path: ${citation.path}`;
      item.appendChild(pathNode);
    }

    const warning = String(citation?.warning || "").trim();
    if (warning) {
      const warningNode = document.createElement("div");
      warningNode.className = "answer-bundle-warning";
      warningNode.textContent = `warning（风险提示）: ${warning}`;
      item.appendChild(warningNode);
    }
    section.appendChild(item);
  });

  wrap.appendChild(section);
}

function buildAnswerBundleNode(bundle, options = {}) {
  const showSummary = Boolean(options?.showSummary);
  const showAssertions = Boolean(options?.showAssertions);
  const wrap = document.createElement("div");
  wrap.className = "answer-bundle";

  const summary = String(bundle?.summary || "").trim();
  if (showSummary && summary) {
    const summaryNode = document.createElement("div");
    summaryNode.className = "answer-bundle-summary";
    summaryNode.textContent = summary;
    wrap.appendChild(summaryNode);
  }

  const claims = Array.isArray(bundle?.claims) ? bundle.claims : [];
  if (showAssertions && claims.length) {
    const section = document.createElement("div");
    section.className = "answer-bundle-section";
    const title = document.createElement("div");
    title.className = "answer-bundle-title";
    title.textContent = "Assertions（关键结论）";
    section.appendChild(title);
    claims.slice(0, 5).forEach((claim, idx) => {
      const item = document.createElement("div");
      item.className = "answer-bundle-item";
      const statement = document.createElement("div");
      statement.className = "answer-bundle-statement";
      statement.textContent = `${idx + 1}. ${String(claim?.statement || "").trim()}`;
      item.appendChild(statement);

      const meta = [];
      const citationIds = Array.isArray(claim?.citation_ids) ? claim.citation_ids.filter(Boolean) : [];
      if (citationIds.length) meta.push(`sources: ${citationIds.join(", ")}`);
      if (claim?.status) meta.push(`status: ${claim.status}`);
      if (claim?.confidence) meta.push(`confidence: ${claim.confidence}`);
      if (meta.length) {
        const metaNode = document.createElement("div");
        metaNode.className = "answer-bundle-meta";
        metaNode.textContent = meta.join(" | ");
        item.appendChild(metaNode);
      }
      section.appendChild(item);
    });
    wrap.appendChild(section);
  }

  const citations = Array.isArray(bundle?.citations) ? bundle.citations : [];
  const { evidence, candidates } = partitionAnswerCitations(citations);
  appendCitationSection(wrap, "Citations（证据来源）", evidence);
  appendCitationSection(wrap, "Search Candidates（候选来源）", candidates, "这些链接仅是搜索候选，尚未抓取正文。");

  const warnings = Array.isArray(bundle?.warnings) ? bundle.warnings : [];
  if (warnings.length) {
    const section = document.createElement("div");
    section.className = "answer-bundle-section";
    const title = document.createElement("div");
    title.className = "answer-bundle-title";
    title.textContent = "Warnings（风险提示）";
    section.appendChild(title);
    warnings.slice(0, 5).forEach((warning) => {
      const item = document.createElement("div");
      item.className = "answer-bundle-warning";
      item.textContent = String(warning || "");
      section.appendChild(item);
    });
    wrap.appendChild(section);
  }

  return wrap.childElementCount ? wrap : null;
}

function addBubble(role, text, answerBundle = null) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  const value = typeof text === "string" ? text : String(text ?? "");
  if (role === "assistant") {
    const content = document.createElement("div");
    content.innerHTML = renderAssistantMarkdown(value);
    annotateAssistantInlineCitations(content, answerBundle);
    bubble.appendChild(content);
    if (hasAnswerBundleContent(answerBundle)) {
      const bundleNode = buildAnswerBundleNode(answerBundle, { showSummary: false, showAssertions: true });
      if (bundleNode) {
        bubble.appendChild(bundleNode);
      }
    }
  } else {
    bubble.textContent = value;
  }

  chatList.appendChild(bubble);
  chatList.scrollTop = chatList.scrollHeight;
}

function escapeHtml(raw) {
  return String(raw)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function normalizeTableLikeTextToMarkdown(rawText) {
  const source = String(rawText ?? "");
  if (!source.trim()) return source;

  const codeBlocks = [];
  const tokenized = source.replace(/```[\s\S]*?```/g, (block) => {
    const token = `__MD_CODE_PRESERVE_${codeBlocks.length}__`;
    codeBlocks.push({ token, block: String(block) });
    return token;
  });

  const isMdTableSeparator = (line) =>
    /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(String(line || ""));

  const lines = tokenized.replace(/\r\n/g, "\n").split("\n");
  const codeTokenSet = new Set(codeBlocks.map((item) => item.token));
  const output = [];

  const shouldSkipTableNormalization = (blockLines) => {
    const codeHints = (
      /\b(function|class|def|import|const|let|var|return|public|private)\b/i
    );
    return blockLines.some((line) => {
      const t = String(line || "").trim();
      if (!t) return false;
      if (/^[-*+]\s+/.test(t) || /^\d+\.\s+/.test(t) || /^>\s*/.test(t) || /^#{1,6}\s+/.test(t)) return true;
      if (/[{};]/.test(t)) return true;
      return codeHints.test(t);
    });
  };

  const splitColumns = (line) => {
    const text = String(line || "").trim();
    if (!text) return null;
    if (text.includes("\t")) {
      const parts = text.split(/\t+/).map((item) => item.trim()).filter(Boolean);
      if (parts.length >= 2) return { mode: "tab", parts };
    }
    if (!/\s{2,}/.test(text)) return null;
    const parts = text.split(/\s{2,}/).map((item) => item.trim()).filter(Boolean);
    if (parts.length >= 2) return { mode: "space", parts };
    return null;
  };

  const normalizeBlockToTable = (blockLines) => {
    if (!Array.isArray(blockLines) || blockLines.length < 2) return null;
    if (shouldSkipTableNormalization(blockLines)) return null;
    for (let idx = 0; idx < blockLines.length - 1; idx += 1) {
      const cur = String(blockLines[idx] || "").trim();
      const next = String(blockLines[idx + 1] || "").trim();
      if (cur.includes("|") && isMdTableSeparator(next)) {
        return null;
      }
    }

    const rows = [];
    const modes = [];
    for (const line of blockLines) {
      const parsed = splitColumns(line);
      if (!parsed) return null;
      rows.push(parsed.parts);
      modes.push(parsed.mode);
    }
    const colCount = rows[0]?.length || 0;
    if (colCount < 2 || colCount > 12) return null;
    if (rows.some((row) => row.length !== colCount)) return null;

    const hasTabRow = modes.includes("tab");
    if (!hasTabRow && rows.length < 3) return null;

    const esc = (cell) => String(cell ?? "").replace(/\|/g, "\\|");
    const out = [];
    out.push(`| ${rows[0].map(esc).join(" | ")} |`);
    out.push(`| ${rows[0].map(() => "---").join(" | ")} |`);
    rows.slice(1).forEach((row) => out.push(`| ${row.map(esc).join(" | ")} |`));
    return out;
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = String(line || "").trim();
    if (!trimmed) {
      output.push(line);
      i += 1;
      continue;
    }
    if (codeTokenSet.has(trimmed)) {
      output.push(line);
      i += 1;
      continue;
    }

    let j = i;
    while (j < lines.length) {
      const t = String(lines[j] || "").trim();
      if (!t || codeTokenSet.has(t)) break;
      j += 1;
    }
    const block = lines.slice(i, j);
    const normalized = normalizeBlockToTable(block);
    if (normalized) {
      output.push(...normalized);
    } else {
      output.push(...block);
    }
    i = j;
  }

  let result = output.join("\n");
  codeBlocks.forEach((item) => {
    result = result.replace(item.token, item.block);
  });
  return result;
}

function renderAssistantMarkdown(text) {
  const source = normalizeTableLikeTextToMarkdown(text);
  const markedApi = window.marked;
  const purifyApi = window.DOMPurify;

  if (markedApi && purifyApi && typeof markedApi.parse === "function") {
    try {
      const html = markedApi.parse(source, {
        gfm: true,
        breaks: true,
      });
      const sanitized = purifyApi.sanitize(html, { USE_PROFILES: { html: true } });
      const sourceCompactLen = source.replace(/\s+/g, "").length;
      if (sourceCompactLen >= 24) {
        const probe = document.createElement("div");
        probe.innerHTML = sanitized;
        const renderedCompactLen = String(probe.textContent || "").replace(/\s+/g, "").length;
        if (!renderedCompactLen || renderedCompactLen < Math.max(8, Math.floor(sourceCompactLen * 0.45))) {
          return renderMarkdownLite(source);
        }
      }
      return sanitized;
    } catch {}
  }

  return renderMarkdownLite(source);
}

function renderMarkdownLite(text) {
  const source = String(text ?? "");
  const codeBlocks = [];
  const withCodeTokens = source.replace(/```([\s\S]*?)```/g, (_, code) => {
    const token = `__MD_CODE_BLOCK_${codeBlocks.length}__`;
    const codeHtml = `<pre><code>${escapeHtml(String(code).replace(/^\n+|\n+$/g, ""))}</code></pre>`;
    codeBlocks.push({ token, html: codeHtml });
    return token;
  });

  const renderInlineMarkdownLite = (raw) => {
    let html = escapeHtml(String(raw ?? ""));
    html = html.replace(
      /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    return html;
  };

  const lines = withCodeTokens.replace(/\r\n/g, "\n").split("\n");
  const chunks = [];
  let i = 0;
  const codeTokenSet = new Set(codeBlocks.map((item) => item.token));
  const isCodeTokenLine = (line) => codeTokenSet.has(String(line || "").trim());
  const parseTableRow = (line) => {
    const raw = String(line || "").trim().replace(/^\|/, "").replace(/\|$/, "");
    return raw.split("|").map((cell) => cell.trim());
  };
  const isTableSeparator = (line) =>
    /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(String(line || ""));

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = String(line || "").trim();
    if (!trimmed) {
      i += 1;
      continue;
    }

    if (isCodeTokenLine(trimmed)) {
      chunks.push(trimmed);
      i += 1;
      continue;
    }

    if (trimmed.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const headerCells = parseTableRow(lines[i]);
      i += 2;
      const bodyRows = [];
      while (i < lines.length) {
        const rowLine = String(lines[i] || "").trim();
        if (!rowLine || !rowLine.includes("|") || isCodeTokenLine(rowLine)) break;
        bodyRows.push(parseTableRow(rowLine));
        i += 1;
      }
      const thead = `<thead><tr>${headerCells
        .map((cell) => `<th>${renderInlineMarkdownLite(cell)}</th>`)
        .join("")}</tr></thead>`;
      const tbody = bodyRows.length
        ? `<tbody>${bodyRows
            .map((row) => {
              const normalized = row.slice(0, headerCells.length);
              while (normalized.length < headerCells.length) normalized.push("");
              return `<tr>${normalized
                .map((cell) => `<td>${renderInlineMarkdownLite(cell)}</td>`)
                .join("")}</tr>`;
            })
            .join("")}</tbody>`
        : "";
      chunks.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    const orderedMatch = trimmed.match(/^\d+\.\s+(.+)$/);
    if (orderedMatch) {
      const items = [];
      while (i < lines.length) {
        const row = String(lines[i] || "").trim();
        const matched = row.match(/^\d+\.\s+(.+)$/);
        if (!matched) break;
        items.push(matched[1]);
        i += 1;
      }
      chunks.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdownLite(item)}</li>`).join("")}</ol>`);
      continue;
    }

    const unorderedMatch = trimmed.match(/^[-*+]\s+(.+)$/);
    if (unorderedMatch) {
      const items = [];
      while (i < lines.length) {
        const row = String(lines[i] || "").trim();
        const matched = row.match(/^[-*+]\s+(.+)$/);
        if (!matched) break;
        items.push(matched[1]);
        i += 1;
      }
      chunks.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdownLite(item)}</li>`).join("")}</ul>`);
      continue;
    }

    const paraLines = [];
    while (i < lines.length) {
      const row = String(lines[i] || "");
      const rowTrimmed = row.trim();
      if (!rowTrimmed) break;
      if (isCodeTokenLine(rowTrimmed)) break;
      if (rowTrimmed.includes("|") && i + 1 < lines.length && isTableSeparator(lines[i + 1])) break;
      if (/^\d+\.\s+/.test(rowTrimmed)) break;
      if (/^[-*+]\s+/.test(rowTrimmed)) break;
      paraLines.push(rowTrimmed);
      i += 1;
    }
    if (paraLines.length) {
      const paraHtml = renderInlineMarkdownLite(paraLines.join("\n")).replace(/\n/g, "<br>");
      chunks.push(`<p>${paraHtml}</p>`);
      continue;
    }

    i += 1;
  }

  let html = chunks.join("");
  if (!html) {
    html = renderInlineMarkdownLite(withCodeTokens).replace(/\n/g, "<br>");
  }

  codeBlocks.forEach((item) => {
    html = html.replace(item.token, item.html);
  });
  return html;
}

function formatNumberedLines(title, items) {
  if (!Array.isArray(items) || !items.length) return null;
  const lines = items.map((item, idx) => `${idx + 1}. ${item}`);
  return `${title}\n${lines.join("\n")}`;
}

function renderBackendPolicy(health = {}) {
  if (!backendPolicyView) return;

  const allowAnyPath = Boolean(health.allow_any_path);
  const platformName = String(health.platform_name || "Unknown").trim();
  const workspaceRoot = String(health.workspace_root || "").trim() || "(unknown)";
  const allowedRoots = Array.isArray(health.allowed_roots) ? health.allowed_roots : [];
  const defaultExtraRoots = Array.isArray(health.default_extra_allowed_roots) ? health.default_extra_allowed_roots : [];
  const source = String(health.extra_allowed_roots_source || "platform_default").trim().toLowerCase();
  const sourceLabel = source === "env_override" ? "环境变量覆盖" : "平台默认";

  const lines = [
    `平台: ${platformName}`,
    `路径策略: ${allowAnyPath ? "不限制（ALLOW_ANY_PATH）" : "只允许已配置根目录"}`,
    `额外根目录来源: ${sourceLabel}`,
    `工作区根目录: ${workspaceRoot}`,
    "当前允许读取根目录:",
  ];

  if (allowedRoots.length) {
    allowedRoots.forEach((item, idx) => lines.push(`${idx + 1}. ${String(item || "")}`));
  } else {
    lines.push("(空)");
  }

  lines.push("");
  lines.push("平台默认额外根目录:");
  if (defaultExtraRoots.length) {
    defaultExtraRoots.forEach((item, idx) => lines.push(`${idx + 1}. ${String(item || "")}`));
  } else {
    lines.push("(空)");
  }

  backendPolicyView.textContent = lines.join("\n");
}

function renderAppVersion(health = {}) {
  if (!appVersionView) return;
  const buildVersion = String(health.build_version || "").trim();
  const appVersion = String(health.app_version || "").trim();
  appVersionView.textContent = buildVersion || (appVersion ? `v${appVersion}` : "版本未知");
  appVersionView.title = buildVersion || appVersion || "版本未知";
}

function renderProductProfile(health = {}) {
  const profile = String(health.product_profile || "kernel_robot").trim() || "kernel_robot";
  const productTitle = String(health.product_title || "Officetool Console").trim() || "Officetool Console";
  const productTagline = String(health.product_tagline || "").trim();
  const kernelTitle = String(health.product_kernel_title || "主核 / 模块舱 / 影子实验台").trim();
  const kernelSubtitle = String(health.product_kernel_subtitle || "").trim();
  const roleTitleText = String(health.product_role_title || "Role 视图").trim();
  const roleLegendText = String(health.product_role_legend || "").trim();
  const showKernelConsole = health.show_kernel_console !== false;
  const showRoleBoard = health.show_role_board !== false;
  const buildVersion = String(health.build_version || "").trim();

  document.body.dataset.productProfile = profile;
  document.title = buildVersion ? `${productTitle} · ${buildVersion}` : productTitle || "Officetool Console";

  if (productTitleView) productTitleView.textContent = productTitle;
  if (productHintView) {
    productHintView.textContent = productTagline || "共享 runtime-core，按产品画像切换不同入口。";
  }
  if (kernelConsoleTitle) kernelConsoleTitle.textContent = kernelTitle || "主核 / 模块舱 / 影子实验台";
  if (kernelConsoleSubtitle) kernelConsoleSubtitle.textContent = kernelSubtitle || "";
  if (roleBoardTitle) roleBoardTitle.textContent = roleTitleText || "Role 视图";
  if (roleBoardLegend) roleBoardLegend.textContent = roleLegendText || "";
  if (kernelConsoleSection) {
    kernelConsoleSection.hidden = !showKernelConsole;
    kernelConsoleSection.style.display = showKernelConsole ? "" : "none";
  }
  if (roleBoardSection) {
    roleBoardSection.hidden = !showRoleBoard;
    roleBoardSection.style.display = showRoleBoard ? "" : "none";
  }
}

const MODULE_LABELS = {
  router: { title: "Router", desc: "全局语义分诊与最小链路选择。" },
  policy: { title: "Policy", desc: "执行策略与 gate（闸门）配置。" },
  attachment_context: { title: "Attachment", desc: "附件上下文、自动关联与 scoped route state。" },
  finalizer: { title: "Finalizer", desc: "最终输出整理、表格/邮件/证据包收口。" },
  tool_registry: { title: "Tools", desc: "工具注册表与执行能力描述。" },
  "provider:api_key": { title: "Provider / API", desc: "公司 API 或标准 OpenAI API 通道。" },
  "provider:codex_auth": { title: "Provider / Codex", desc: "本地 Codex auth 调试通道。" },
};

function formatRelativeTime(raw) {
  const value = String(raw || "").trim();
  if (!value) return "未知";
  try {
    const ts = new Date(value);
    if (Number.isNaN(ts.getTime())) return value;
    const diffSec = Math.max(0, Math.floor((Date.now() - ts.getTime()) / 1000));
    if (diffSec < 60) return `${diffSec}s 前`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m 前`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h 前`;
    return `${Math.floor(diffSec / 86400)}d 前`;
  } catch {
    return value;
  }
}

function normalizeCounterItems(items) {
  return (Array.isArray(items) ? items : [])
    .map((item) => ({
      name: String(item?.name || "").trim(),
      count: Number(item?.count || 0),
    }))
    .filter((item) => item.name);
}

function pickTopCounterName(items, fallback = "none") {
  const normalized = normalizeCounterItems(items);
  return normalized.length ? `${normalized[0].name} · ${normalized[0].count}` : fallback;
}

function renderKernelStatGrid(container, items) {
  if (!container) return;
  container.innerHTML = "";
  (Array.isArray(items) ? items : []).forEach((item) => {
    const cell = document.createElement("div");
    cell.className = "kernel-stat";

    const label = document.createElement("div");
    label.className = "kernel-stat-label";
    label.textContent = String(item?.label || "");
    cell.appendChild(label);

    const value = document.createElement("div");
    value.className = "kernel-stat-value";
    value.textContent = String(item?.value || "-");
    cell.appendChild(value);

    const meta = String(item?.meta || "").trim();
    if (meta) {
      const metaNode = document.createElement("div");
      metaNode.className = "kernel-stat-meta";
      metaNode.textContent = meta;
      cell.appendChild(metaNode);
    }

    container.appendChild(cell);
  });
}

function renderModuleBay(health = {}) {
  if (!moduleBay) return;
  const selected = health?.kernel_selected_modules || {};
  const moduleHealth = health?.kernel_module_health || {};
  const overlay = health?.assistant_overlay_profile || {};
  const moduleAffinity = overlay?.module_affinity || {};
  const entries = Object.entries(selected);

  moduleBay.innerHTML = "";
  if (!entries.length) {
    moduleBay.textContent = "模块舱为空。";
    if (moduleBayMeta) moduleBayMeta.textContent = "0 modules";
    return;
  }

  if (moduleBayMeta) {
    moduleBayMeta.textContent = `${entries.length} 个模块在线`;
  }

  entries.forEach(([key, ref]) => {
    const meta = MODULE_LABELS[key] || { title: key, desc: "未命名模块。" };
    const healthItem = moduleHealth?.[key] || {};
    const status = String(healthItem?.status || "active").trim().toLowerCase() || "active";
    const failureCount = Number(healthItem?.failure_count || 0);
    const overlayItems = normalizeCounterItems(moduleAffinity?.[key.replace("provider:", "")] || moduleAffinity?.[key] || []);

    const card = document.createElement("article");
    card.className = `module-card status-${status}`;

    const head = document.createElement("div");
    head.className = "module-card-head";
    head.innerHTML = `
      <div>
        <div class="module-card-title">${meta.title}</div>
        <div class="module-card-ref">${String(ref || "-")}</div>
      </div>
      <span class="module-status-badge">${status}</span>
    `;
    card.appendChild(head);

    const desc = document.createElement("div");
    desc.className = "module-card-desc";
    desc.textContent = meta.desc;
    card.appendChild(desc);

    const stats = document.createElement("div");
    stats.className = "module-card-stats";
    stats.innerHTML = `
      <span>failure=${failureCount}</span>
      <span>selected=${String(healthItem?.selected_ref || ref || "-")}</span>
    `;
    card.appendChild(stats);

    if (overlayItems.length) {
      const signals = document.createElement("div");
      signals.className = "module-card-signals";
      overlayItems.slice(0, 3).forEach((item) => {
        const chip = document.createElement("span");
        chip.className = "signal-chip";
        chip.textContent = `${item.name} · ${item.count}`;
        signals.appendChild(chip);
      });
      card.appendChild(signals);
    }

    const errorText = String(healthItem?.last_error || "").trim();
    if (errorText) {
      const errorNode = document.createElement("div");
      errorNode.className = "module-card-error";
      errorNode.textContent = `last_error: ${errorText}`;
      card.appendChild(errorNode);
    }

    moduleBay.appendChild(card);
  });
}

function renderEvolutionFeed(events = []) {
  if (!evolutionFeed) return;
  const items = Array.isArray(events) ? events : [];
  evolutionFeed.innerHTML = "";
  if (!items.length) {
    evolutionFeed.textContent = "还没有进化记录。";
    if (evolutionFeedMeta) evolutionFeedMeta.textContent = "0 events";
    return;
  }
  if (evolutionFeedMeta) {
    evolutionFeedMeta.textContent = `${items.length} 条最近记录`;
  }
  items.slice(0, 8).forEach((item) => {
    const node = document.createElement("article");
    node.className = "evolution-event";
    const terms = Array.isArray(item?.domain_terms) ? item.domain_terms.filter(Boolean).slice(0, 4) : [];
    node.innerHTML = `
      <div class="evolution-event-head">
        <span class="evolution-event-title">${String(item?.primary_intent || "standard")}</span>
        <span class="evolution-event-time">${formatRelativeTime(item?.created_at)}</span>
      </div>
      <div class="evolution-event-summary">${String(item?.summary || "").trim() || "本轮记录了新的适应信号。"}</div>
      <div class="evolution-event-meta">profile=${String(item?.runtime_profile || "-")} · task=${String(item?.task_type || "-")}</div>
      <div class="module-card-signals">${terms.map((term) => `<span class="signal-chip">${term}</span>`).join("")}</div>
    `;
    evolutionFeed.appendChild(node);
  });
}

function renderRoleLabRunGraph(lastRun = {}) {
  if (!roleLabRunGraph || !roleLabRunFailures) return;
  const nodes = Array.isArray(lastRun?.nodes) ? lastRun.nodes : [];
  const instances = Array.isArray(lastRun?.instances) ? lastRun.instances : [];
  const events = Array.isArray(lastRun?.events) ? lastRun.events : [];
  roleLabRunGraph.innerHTML = "";
  roleLabRunFailures.innerHTML = "";

  if (!nodes.length) {
    roleLabRunGraph.textContent = "最近还没有运行图。";
    roleLabRunFailures.textContent = "最近还没有局部失败。";
    return;
  }

  const nodeMap = new Map();
  const children = new Map();
  nodes.forEach((node) => {
    const id = String(node?.node_id || "").trim();
    if (!id) return;
    nodeMap.set(id, node);
    const parentId = String(node?.parent_node_id || "").trim();
    if (!children.has(parentId)) children.set(parentId, []);
    children.get(parentId).push(node);
  });

  const instanceMap = new Map();
  instances.forEach((item) => {
    const nodeId = String(item?.node_id || "").trim();
    if (!nodeId) return;
    const list = instanceMap.get(nodeId) || [];
    list.push(item);
    instanceMap.set(nodeId, list);
  });

  const preferredRoots = nodes.filter((node) => {
    const parentId = String(node?.parent_node_id || "").trim();
    return !parentId || !nodeMap.has(parentId);
  });
  const roots = preferredRoots.length ? preferredRoots : nodes.slice(0, 1);

  const labelForNode = (node) => {
    const nodeType = String(node?.node_type || "role").trim();
    const meta = node?.meta || {};
    if (nodeType === "join") return "join";
    if (nodeType === "branch") {
      const toolName = String(meta?.tool_name || "").trim();
      return toolName ? `tool:${toolName}` : String(node?.phase || "branch");
    }
    return String(node?.role || "-");
  };

  const renderNode = (node, depth = 0) => {
    const nodeType = String(node?.node_type || "role").trim();
    const status = String(node?.status || "pending").trim();
    const attempts = Number(node?.attempts || 0);
    const meta = node?.meta || {};
    const item = document.createElement("article");
    item.className = `runtime-graph-node ${nodeType} ${status}`;
    item.style.setProperty("--depth", String(depth));
    const tags = [];
    if (attempts > 1) tags.push(`retry=${attempts - 1}`);
    if (meta?.synthetic) tags.push("synthetic");
    if (meta?.batch_index) tags.push(`slot=${meta.batch_index}`);
    if (meta?.failed_branches > 0) tags.push(`failed=${meta.failed_branches}`);
    const relatedInstances = instanceMap.get(String(node?.node_id || "").trim()) || [];
    if (relatedInstances.length) {
      const lastInstance = relatedInstances[relatedInstances.length - 1] || {};
      tags.push(String(lastInstance?.instance_id || "").trim());
    }
    item.innerHTML = `
      <div class="runtime-graph-line">
        <span class="runtime-graph-dot ${status}"></span>
        <span class="runtime-graph-label">${labelForNode(node)}</span>
        <span class="runtime-graph-phase">${String(node?.phase || "-")}</span>
      </div>
      <div class="runtime-graph-meta">
        <span>${status}</span>
        ${tags.map((tag) => `<span>${tag}</span>`).join("")}
      </div>
      ${
        String(node?.summary || "").trim()
          ? `<div class="runtime-graph-summary">${String(node.summary).trim()}</div>`
          : ""
      }
      ${
        String(node?.error || "").trim()
          ? `<div class="runtime-graph-error">${String(node.error).trim()}</div>`
          : ""
      }
    `;
    roleLabRunGraph.appendChild(item);
    (children.get(String(node?.node_id || "").trim()) || []).forEach((child) => renderNode(child, depth + 1));
  };

  roots.forEach((node) => renderNode(node, 0));

  const failureEvents = events.filter((item) => {
    const kind = String(item?.kind || "").trim();
    return kind === "worker_tool_retry_scheduled" || kind === "node_failed" || kind === "role_failed";
  });
  if (!failureEvents.length) {
    roleLabRunFailures.textContent = "最近还没有局部失败。";
    return;
  }
  failureEvents.slice(-8).reverse().forEach((event) => {
    const row = document.createElement("div");
    row.className = "runtime-failure-row";
    const kind = String(event?.kind || "").trim();
    const label =
      kind === "worker_tool_retry_scheduled"
        ? `retry · ${String(event?.tool_name || "-")}`
        : `${kind} · ${String(event?.role || event?.tool_name || "-")}`;
    row.innerHTML = `
      <div class="runtime-failure-head">
        <span>${label}</span>
        <span>${formatRelativeTime(event?.ts)}</span>
      </div>
      <div class="runtime-failure-detail">${String(event?.error || event?.summary || event?.branch_group || "-")}</div>
    `;
    roleLabRunFailures.appendChild(row);
  });
}

function renderRoleLabRuntime(health = {}) {
  if (!roleLabRuntimeMetrics || !roleLabRegistry) return;
  const runtime = health?.role_lab_runtime || {};
  const registry = runtime?.registry || {};
  const readiness = runtime?.stage4_readiness || {};
  const lastRun = runtime?.last_run || {};
  const runMeta = lastRun?.run || {};
  const registryRoles = Array.isArray(registry?.roles) ? registry.roles : [];

  if (roleLabRuntimeMeta) {
    roleLabRuntimeMeta.textContent = `${Number(registry?.registered_roles || 0)} registered · ${Number(readiness?.controller_backed_role_count || 0)} controller-backed`;
  }

  renderKernelStatGrid(roleLabRuntimeMetrics, [
    { label: "Stage 4 Trial", value: readiness?.ready_for_stage4_trial ? "READY" : "PREP", meta: String(readiness?.next_focus || "runtime abstraction") },
    { label: "Coverage", value: `${Number(readiness?.controller_backed_role_count || 0)}/${Number(registry?.registered_roles || 0)}`, meta: "controller-backed / registered" },
    { label: "Multi Instance", value: String(readiness?.multi_instance_role_count || 0), meta: "roles can spawn same-type instances" },
    { label: "Parent/Child", value: String(readiness?.parent_child_role_count || 0), meta: "roles support task parent-child graph" },
    { label: "Last Run", value: String(runMeta?.status || "-").toUpperCase(), meta: `instances=${Number(runMeta?.instance_count || 0)} · nodes=${Number(runMeta?.node_count || 0)}` },
    { label: "Current Role", value: String(lastRun?.current_role || "-"), meta: String((lastRun?.active_roles || []).join(", ") || "idle") },
  ]);
  renderRoleLabRunGraph(lastRun);

  roleLabRegistry.innerHTML = "";
  if (!registryRoles.length) {
    roleLabRegistry.textContent = "role registry 为空。";
    return;
  }
  registryRoles.forEach((item) => {
    const role = String(item?.role || "").trim();
    const title = String(item?.title || role).trim();
    const kind = String(item?.kind || "agent").trim();
    const executable = Boolean(item?.executable);
    const multiInstance = Boolean(item?.multi_instance_ready);
    const parentChild = Boolean(item?.supports_parent_child);
    const card = document.createElement("article");
    card.className = `module-card ${executable ? "status-active" : "status-idle"}`;
    card.innerHTML = `
      <div class="module-card-head">
        <div>
          <div class="module-card-title">${title}</div>
          <div class="module-card-ref">${role}</div>
        </div>
        <span class="module-status-badge">${executable ? "ACTIVE" : "META"}</span>
      </div>
      <div class="module-card-desc">${String(item?.description || "").trim() || "未填写描述。"}</div>
      <div class="module-card-stats">
        <span>kind=${kind}</span>
        <span>multi=${multiInstance ? "yes" : "no"}</span>
      </div>
      <div class="module-card-signals">
        <span class="signal-chip">${parentChild ? "parent-child ready" : "flat only"}</span>
        ${(Array.isArray(item?.runtime_profiles) ? item.runtime_profiles : []).slice(0, 3).map((profile) => `<span class="signal-chip">${String(profile)}</span>`).join("")}
      </div>
    `;
    roleLabRegistry.appendChild(card);
  });
}

function renderKernelConsole(health = {}) {
  const selected = health?.kernel_selected_modules || {};
  const overlay = health?.assistant_overlay_profile || {};
  const recentEvents = health?.assistant_evolution_recent || [];
  const validation = health?.kernel_shadow_validation || {};
  const promoteCheck = health?.kernel_shadow_promote_check || {};
  const lastUpgrade = health?.kernel_last_upgrade_run || {};
  const lastRepair = health?.kernel_last_repair_run || {};
  const lastPatch = health?.kernel_last_patch_worker_run || {};
  const lastPackage = health?.kernel_last_package_run || {};
  const toolRegistry = health?.kernel_tool_registry || {};
  const authMode = String(health?.auth_mode || "").trim() || "unknown";
  const turnCount = Number(overlay?.turns_observed || 0);

  if (kernelLiveLabel) {
    kernelLiveLabel.textContent = Object.keys(selected).length ? "主核在线" : "主核待机";
  }
  if (kernelLiveMeta) {
    kernelLiveMeta.textContent =
      `modules=${Object.keys(selected).length} · auth=${authMode} · overlay_turns=${turnCount}`;
  }

  renderKernelStatGrid(kernelCoreMetrics, [
    { label: "Active Manifest", value: String(health?.build_version || "runtime"), meta: `${Object.keys(selected).length} modules live` },
    { label: "Provider", value: authMode, meta: `${Number(toolRegistry?.tool_count || 0)} tools registered` },
    { label: "Router Bias", value: pickTopCounterName(overlay?.module_affinity?.router || [], "none"), meta: `top intent=${pickTopCounterName(overlay?.intent_counts || [], "none")}` },
    { label: "Explainer Bias", value: pickTopCounterName(overlay?.module_affinity?.explainer || [], "none"), meta: `profile=${pickTopCounterName(overlay?.runtime_profile_counts || [], "none")}` },
  ]);

  renderKernelStatGrid(shadowLabMetrics, [
    { label: "Validate", value: validation?.ok ? "PASS" : "CHECK", meta: String(validation?.reason || validation?.detail || "shadow validation") },
    { label: "Promote Gate", value: promoteCheck?.ok ? "OPEN" : "HOLD", meta: String(promoteCheck?.reason || "compatibility gate") },
    { label: "Last Upgrade", value: String(lastUpgrade?.run_id || "-").slice(0, 12) || "-", meta: formatRelativeTime(lastUpgrade?.finished_at || lastUpgrade?.started_at) },
    { label: "Patch Worker", value: String(lastPatch?.stop_reason || (lastPatch?.ok ? "pipeline_ok" : "-")), meta: `rounds=${Number(lastPatch?.round_count || 0)}` },
    { label: "Repair", value: String(lastRepair?.strategy || "-"), meta: formatRelativeTime(lastRepair?.finished_at) },
    { label: "Package", value: String(lastPackage?.run_id || "-").slice(0, 12) || "-", meta: formatRelativeTime(lastPackage?.finished_at) },
  ]);

  renderKernelStatGrid(evolutionMetrics, [
    { label: "Turns Observed", value: String(turnCount), meta: `updated=${formatRelativeTime(overlay?.updated_at)}` },
    { label: "Top Terms", value: pickTopCounterName(overlay?.domain_terms || [], "none"), meta: "长期对话累积的领域词" },
    { label: "Finalizer Bias", value: pickTopCounterName(overlay?.module_affinity?.finalizer || [], "none"), meta: `style=${pickTopCounterName(overlay?.response_style_counts || [], "normal")}` },
    { label: "Last Signal", value: String(overlay?.last_signal?.primary_intent || "-"), meta: String(overlay?.last_signal?.summary || "暂无最近信号") },
  ]);

  renderModuleBay(health);
  renderEvolutionFeed(recentEvents);
}

async function refreshSystemDashboard() {
  const health = await fetch("/api/health").then((r) => r.json());
  renderProductProfile(health);
  renderAppVersion(health);
  renderBackendPolicy(health);
  renderKernelConsole(health);
  renderRoleLabRuntime(health);
  return health;
}

function currentSessionKey() {
  return String(state.sessionId || "").trim();
}

function isSessionSending(sessionId) {
  const key = String(sessionId || "").trim();
  if (!key) return false;
  return state.sendingSessionIds.has(key);
}

function updateSendAvailability() {
  if (!sendBtn) return;
  const sid = currentSessionKey();
  const disabled = Boolean(state.uploading || (sid && isSessionSending(sid)));
  sendBtn.disabled = disabled;
}

function updateDrillAvailability() {
  if (!sandboxDrillBtn) return;
  sandboxDrillBtn.disabled = Boolean(state.drilling);
}

function updateEvalAvailability() {
  if (!evalHarnessBtn) return;
  evalHarnessBtn.disabled = Boolean(state.evaluating);
}

function refreshSession() {
  sessionIdView.textContent = state.sessionId || "(未创建)";
  if (deleteSessionBtn) {
    deleteSessionBtn.disabled = !state.sessionId;
  }
  try {
    if (state.sessionId) {
      window.localStorage.setItem(SESSION_STORAGE_KEY, state.sessionId);
    } else {
      window.localStorage.removeItem(SESSION_STORAGE_KEY);
    }
  } catch {}
  updateSendAvailability();
}

function getStoredSessionId() {
  try {
    const raw = window.localStorage.getItem(SESSION_STORAGE_KEY);
    const val = (raw || "").trim();
    return val || null;
  } catch {
    return null;
  }
}

function clearChat() {
  if (!chatList) return;
  chatList.innerHTML = "";
}

function formatSessionTime(raw) {
  const s = String(raw || "").trim();
  if (!s) return "-";
  try {
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleString();
  } catch {
    return s;
  }
}

async function refreshSessionHistory() {
  if (!sessionHistoryView) return [];
  sessionHistoryView.textContent = "加载中...";

  try {
    const res = await fetch("/api/sessions?limit=80");
    if (!res.ok) {
      sessionHistoryView.textContent = "历史会话加载失败";
      return [];
    }
    const data = await res.json();
    const sessions = Array.isArray(data?.sessions) ? data.sessions : [];
    sessionHistoryView.innerHTML = "";
    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "session-history-empty";
      empty.textContent = "暂无历史会话";
      sessionHistoryView.appendChild(empty);
      return [];
    }

    sessions.forEach((item) => {
      const sid = String(item?.session_id || "");
      if (!sid) return;
      const isCustomTitle = Boolean(item?.has_custom_title);
      const sessionTitle = String(item?.title || "新会话");

      const row = document.createElement("div");
      row.className = "session-history-row";

      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "session-history-item";
      if (sid === state.sessionId) openBtn.classList.add("active");

      const title = document.createElement("div");
      title.className = "session-history-title";
      title.textContent = sessionTitle;
      if (isCustomTitle) {
        title.title = "已使用自定义会话名";
      }
      openBtn.appendChild(title);

      const meta = document.createElement("div");
      meta.className = "session-history-meta";
      meta.textContent = `turns ${item?.turn_count || 0} · ${formatSessionTime(item?.updated_at)}`;
      openBtn.appendChild(meta);

      const preview = String(item?.preview || "").trim();
      if (preview) {
        const previewNode = document.createElement("div");
        previewNode.className = "session-history-preview";
        previewNode.textContent = preview;
        openBtn.appendChild(previewNode);
      }

      openBtn.addEventListener("click", async () => {
        await loadSessionById(sid, { announceMode: "switch" });
      });

      const renameBtn = document.createElement("button");
      renameBtn.type = "button";
      renameBtn.className = "session-history-rename";
      renameBtn.textContent = "改名";
      renameBtn.title = "自定义会话标题（留空恢复自动标题）";
      renameBtn.addEventListener("click", async (event) => {
        event.preventDefault();
        event.stopPropagation();
        await renameSessionById(sid, sessionTitle);
      });

      row.appendChild(openBtn);
      row.appendChild(renameBtn);
      sessionHistoryView.appendChild(row);
    });
    return sessions;
  } catch {
    sessionHistoryView.textContent = "历史会话加载失败";
    return [];
  }
}

async function renameSessionById(sessionId, currentTitle = "") {
  const sid = String(sessionId || "").trim();
  if (!sid) return;

  const raw = window.prompt("请输入新的会话名称（留空恢复自动标题）", String(currentTitle || ""));
  if (raw === null) return;

  const title = String(raw || "").trim().slice(0, 120);
  try {
    const res = await fetch(`/api/session/${encodeURIComponent(sid)}/title`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (!res.ok) {
      let detail = `改名失败: ${res.status}`;
      try {
        const data = await res.json();
        if (data?.detail) detail = `改名失败: ${data.detail}`;
      } catch {}
      throw new Error(detail);
    }

    await refreshSessionHistory();
    addBubble("system", title ? `会话已改名：${title}` : "已清除自定义会话名，恢复自动标题。");
  } catch (err) {
    addBubble("system", String(err));
  }
}

async function deleteSessionById(sessionId) {
  const sid = String(sessionId || "").trim();
  if (!sid) return;
  const yes = window.confirm(`确认删除这个会话吗？\n${sid}\n删除后无法恢复。`);
  if (!yes) return;

  try {
    const res = await fetch(`/api/session/${encodeURIComponent(sid)}`, { method: "DELETE" });
    if (!res.ok) {
      let detail = `删除失败: ${res.status}`;
      try {
        const data = await res.json();
        if (data?.detail) detail = `删除失败: ${data.detail}`;
      } catch {}
      throw new Error(detail);
    }

    const deletingCurrent = sid === state.sessionId;
    if (deletingCurrent) {
      state.sessionId = null;
      refreshSession();
      clearChat();
    }

    const sessions = await refreshSessionHistory();
    if (deletingCurrent) {
      if (Array.isArray(sessions) && sessions.length) {
        await loadSessionById(String(sessions[0].session_id || ""), { announceMode: "switch" });
      } else {
        addBubble("system", `会话已删除：${sid}。当前无历史会话。`);
      }
    } else {
      addBubble("system", `会话已删除：${sid}`);
    }
  } catch (err) {
    addBubble("system", String(err));
  }
}

async function loadSessionById(sessionId, { announceMode = "none" } = {}) {
  const sid = String(sessionId || "").trim();
  if (!sid) return false;

  state.sessionId = sid;
  refreshSession();

  try {
    const res = await fetch(`/api/session/${encodeURIComponent(sid)}?max_turns=120`);
    if (!res.ok) {
      if (res.status === 404) {
        state.sessionId = null;
        refreshSession();
      }
      await refreshSessionHistory();
      return false;
    }
    const data = await res.json();
    const turns = Array.isArray(data?.turns) ? data.turns : [];
    clearChat();
    if (announceMode === "restore") {
      addBubble("system", `已恢复会话：${sid}（历史 ${data?.turn_count || turns.length} 条）`);
    } else if (announceMode === "switch") {
      addBubble("system", `已切换会话：${sid}（历史 ${data?.turn_count || turns.length} 条）`);
    }
    turns.forEach((turn) => {
      const role = turn?.role === "assistant" ? "assistant" : "user";
      const text = String(turn?.text || "").trim();
      if (text) addBubble(role, text, role === "assistant" ? turn?.answer_bundle || null : null);
    });
    await refreshTokenStatsFromServer();
    await refreshSessionHistory();
    return true;
  } catch {
    await refreshSessionHistory();
    return false;
  }
}

async function restoreSessionIfPossible() {
  const cached = getStoredSessionId();
  if (!cached) return false;
  return loadSessionById(cached, { announceMode: "restore" });
}

function renderRunSteps(activeStepId, isError = false) {
  if (!runStepList) return;

  const activeIndex = RUN_FLOW_STEPS.findIndex((step) => step.id === String(activeStepId || "").trim());
  runStepList.innerHTML = "";

  RUN_FLOW_STEPS.forEach((step, index) => {
    const node = document.createElement("div");
    node.className = "runtime-step";
    if (activeIndex >= 0 && index < activeIndex) {
      node.classList.add("is-done");
    }
    if (activeIndex === index) {
      node.classList.add(isError ? "is-error" : "is-active");
    }
    node.textContent = step.label;
    runStepList.appendChild(node);
  });
}

function setRunStage(stageLabel, text, stepId = null, tone = "idle") {
  currentRunStepId = String(stepId || "").trim() || null;
  currentRunTone = String(tone || "idle").trim() || "idle";
  if (runStageBadge) {
    runStageBadge.textContent = stageLabel;
    runStageBadge.className = `stage-badge stage-${tone}`;
  }
  if (runStageText) {
    runStageText.textContent = text;
  }
  renderRunSteps(currentRunStepId, currentRunTone === "error");
}

function formatJsonPreview(value, maxChars = 10000) {
  const raw = JSON.stringify(value, null, 2);
  if (raw.length <= maxChars) return raw;
  return `${raw.slice(0, maxChars)}\n\n...[truncated ${raw.length - maxChars} chars]`;
}

function formatBytes(value) {
  const num = Number(value || 0);
  if (!Number.isFinite(num) || num <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB"];
  let idx = 0;
  let cur = num;
  while (cur >= 1024 && idx < units.length - 1) {
    cur /= 1024;
    idx += 1;
  }
  if (idx === 0) return `${Math.round(cur)} ${units[idx]}`;
  return `${cur.toFixed(2)} ${units[idx]}`;
}

function startWaitStageTicker(totalAttachmentBytes = 0) {
  const startedAt = Date.now();
  const sizeHint =
    totalAttachmentBytes > 0 ? `（附件总大小 ${formatBytes(totalAttachmentBytes)}）` : "";
  let notifiedSlow = false;

  const update = () => {
    const elapsedSec = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    let text = `模型处理中，已等待 ${elapsedSec}s${sizeHint}`;
    if (elapsedSec >= 60) {
      text = `仍在处理中，已等待 ${elapsedSec}s${sizeHint}。大文件分析通常会更久。`;
    } else if (elapsedSec >= 20) {
      text = `处理中（可能在读取附件/执行工具），已等待 ${elapsedSec}s${sizeHint}`;
    }
    setRunStage("进行中", text, "wait", "working");
    if (!notifiedSlow && elapsedSec >= 45) {
      notifiedSlow = true;
      renderRunTrace(
        [
          "请求已发送，后端仍在处理中。",
          "如果本轮包含大文件，模型会分段读取并分析，耗时会明显增加。",
        ],
        []
      );
    }
  };

  update();
  const timer = window.setInterval(update, 1000);
  return () => window.clearInterval(timer);
}

function parseSseEventBlock(rawBlock) {
  const block = String(rawBlock || "").trim();
  if (!block) return null;
  const lines = block.split("\n");
  let event = "message";
  const dataLines = [];
  lines.forEach((line) => {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  });
  if (!dataLines.length) return null;
  const rawData = dataLines.join("\n");
  let data = rawData;
  try {
    data = JSON.parse(rawData);
  } catch {}
  return { event, data };
}

async function streamChatRequest(body, handlers = {}) {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data?.detail) detail = String(data.detail);
    } catch {}
    throw new Error(detail);
  }

  const contentType = String(res.headers.get("content-type") || "").toLowerCase();
  if (!contentType.includes("text/event-stream") || !res.body) {
    return await res.json();
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalResponse = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    while (true) {
      const splitAt = buffer.indexOf("\n\n");
      if (splitAt < 0) break;
      const block = buffer.slice(0, splitAt);
      buffer = buffer.slice(splitAt + 2);
      const parsed = parseSseEventBlock(block);
      if (!parsed) continue;

      const event = parsed.event;
      const payload = parsed.data && typeof parsed.data === "object" ? parsed.data : { detail: String(parsed.data || "") };

      if (event === "stage") {
        handlers.onStage?.(payload);
        continue;
      }
      if (event === "trace") {
        handlers.onTrace?.(payload);
        continue;
      }
      if (event === "debug") {
        handlers.onDebug?.(payload);
        continue;
      }
      if (event === "tool_event") {
        handlers.onToolEvent?.(payload);
        continue;
      }
      if (event === "agent_state") {
        handlers.onAgentState?.(payload);
        continue;
      }
      if (event === "heartbeat") {
        handlers.onHeartbeat?.(payload);
        continue;
      }
      if (event === "error") {
        throw new Error(String(payload?.detail || "stream error"));
      }
      if (event === "final") {
        finalResponse = payload?.response || null;
        handlers.onFinal?.(finalResponse);
        continue;
      }
      if (event === "done") {
        return finalResponse;
      }
    }
  }

  if (finalResponse) return finalResponse;
  throw new Error("流式响应中断：未收到最终结果。");
}

function applyBackendStage(payload) {
  const code = String(payload?.code || "").trim();
  const detail = String(payload?.detail || "").trim();
  if (!code) return;

  if (code === "backend_start" || code === "session_ready" || code === "attachments_ready") {
    setRunStage("进行中", detail || "后端处理中", "prepare", "working");
    return;
  }
  if (code === "agent_run_start") {
    setRunStage("进行中", detail || "模型推理中", "wait", "working");
    return;
  }
  if (code === "agent_run_done" || code === "session_saved" || code === "stats_saved" || code === "ready") {
    setRunStage("进行中", detail || "后处理中", "parse", "working");
  }
}

function renderRunPayload(body, attachmentNames) {
  if (!runPayloadView) return;
  const settings = body?.settings || {};
  const header = [
    `session_id: ${body?.session_id || "(new session)"}`,
    `message_chars: ${String(body?.message || "").length}`,
    `attachments: ${attachmentNames.length ? attachmentNames.join("，") : "(none)"}`,
    `model: ${settings.model || "(default)"}`,
    `max_output_tokens: ${settings.max_output_tokens}`,
    `max_context_turns: ${settings.max_context_turns}`,
    `enable_tools: ${settings.enable_tools}`,
    `execution_mode: ${settings.execution_mode || "(backend default)"}`,
    `debug_raw: ${settings.debug_raw}`,
    `response_style: ${settings.response_style}`,
    "",
    "payload json:",
  ];
  runPayloadView.textContent = `${header.join("\n")}\n${formatJsonPreview(body)}`;
}

function renderRunTrace(traceItems = [], toolEvents = []) {
  if (!runTraceView) return;

  const lines = [];
  if (Array.isArray(traceItems) && traceItems.length) {
    traceItems.forEach((item, idx) => lines.push(`${idx + 1}. ${item}`));
  } else {
    lines.push("暂无执行轨迹");
  }

  if (Array.isArray(toolEvents) && toolEvents.length) {
    lines.push("");
    lines.push("工具调用:");
    toolEvents.forEach((tool, idx) => {
      const args = tool?.input ? JSON.stringify(tool.input) : "{}";
      let modeSuffix = "";
      try {
        const raw = String(tool?.output_preview || "").trim();
        if (raw.startsWith("{")) {
          const parsed = JSON.parse(raw);
          const mode = String(parsed?.execution_mode || "").trim().toLowerCase();
          if (mode === "host" || mode === "docker") {
            modeSuffix = ` [${mode}]`;
          }
        }
      } catch {}
      lines.push(`${idx + 1}. ${tool?.name || "unknown"}(${args})${modeSuffix}`);
    });
  }

  runTraceView.textContent = lines.join("\n");
}

function renderAgentPanels(panels = [], plan = [], activeRoles = new Set(), currentRole = null, roleStates = new Map()) {
  renderRoleBoard(panels, activeRoles, currentRole, roleStates);
  if (!runAgentPanelsView) return;

  const lines = [];
  const specialistRoles = new Set(["researcher", "file_reader", "summarizer", "fixer"]);
  if (Array.isArray(plan) && plan.length) {
    lines.push("Execution Plan:");
    plan.forEach((item, idx) => {
      lines.push(`${idx + 1}. ${String(item || "")}`);
    });
    lines.push("");
  }

  if (Array.isArray(panels) && panels.length) {
    const fixedPanels = [];
    const dynamicPanels = [];
    panels.forEach((panel, idx) => {
      const role = String(panel?.role || `agent_${idx + 1}`);
      if (specialistRoles.has(role)) {
        dynamicPanels.push({ panel, idx });
      } else {
        fixedPanels.push({ panel, idx });
      }
    });

    lines.push("Core Roles:");
    if (!fixedPanels.length) {
      lines.push("(none)");
    }
    fixedPanels.forEach(({ panel, idx }) => {
      const role = String(panel?.role || `agent_${idx + 1}`);
      const title = String(panel?.title || role);
      const kind = normalizeRoleKind(panel?.kind, "agent");
      const summary = String(panel?.summary || "").trim();
      const bullets = Array.isArray(panel?.bullets) ? panel.bullets : [];
      lines.push(`[${idx + 1}] ${title} (${role}, ${kind})`);
      if (summary) lines.push(summary);
      bullets.forEach((item) => lines.push(`- ${String(item || "")}`));
      lines.push("");
    });

    lines.push("Specialist Roles:");
    if (!dynamicPanels.length) {
      lines.push("(none this run)");
      lines.push("");
    } else {
      dynamicPanels.forEach(({ panel, idx }) => {
        const role = String(panel?.role || `agent_${idx + 1}`);
        const title = String(panel?.title || role);
        const kind = normalizeRoleKind(panel?.kind, "agent");
        const summary = String(panel?.summary || "").trim();
        const bullets = Array.isArray(panel?.bullets) ? panel.bullets : [];
        lines.push(`[${idx + 1}] ${title} (${role}, ${kind})`);
        if (summary) lines.push(summary);
        bullets.forEach((item) => lines.push(`- ${String(item || "")}`));
        lines.push("");
      });
    }
  }

  if (!lines.length) {
    runAgentPanelsView.textContent = "暂无多 Role 摘要";
    return;
  }
  runAgentPanelsView.textContent = lines.join("\n").trim();
}

function renderAnswerBundle(bundle = {}) {
  if (!runAnswerBundleView) return;
  if (!hasAnswerBundleContent(bundle)) {
    runAnswerBundleView.textContent = "暂无结构化证据包";
    return;
  }

  const lines = [];
  const summary = String(bundle?.summary || "").trim();
  if (summary) {
    lines.push(`summary: ${summary}`);
    lines.push("");
  }

  const claims = Array.isArray(bundle?.claims) ? bundle.claims : [];
  if (claims.length) {
    lines.push("assertions（关键结论）:");
    claims.slice(0, 5).forEach((claim, idx) => {
      const ids = Array.isArray(claim?.citation_ids) ? claim.citation_ids.join(", ") : "";
      lines.push(`${idx + 1}. ${String(claim?.statement || "").trim()}`);
      lines.push(
        `   status=${String(claim?.status || "supported")} confidence=${String(claim?.confidence || "medium")} citations（证据来源）=${ids || "(none)"}`
      );
    });
    lines.push("");
  }

  const citations = Array.isArray(bundle?.citations) ? bundle.citations : [];
  const { evidence, candidates } = partitionAnswerCitations(citations);
  const appendCitationLines = (title, items, note = "") => {
    if (!items.length) return;
    lines.push(`${title}:`);
    if (note) lines.push(`- note: ${note}`);
    items.slice(0, 8).forEach((citation) => {
      lines.push(`- ${String(citation?.id || "-")} | ${String(citation?.tool || "")} | ${String(citation?.label || citation?.title || citation?.url || citation?.path || "")}`);
      if (citation?.locator) lines.push(`  locator: ${citation.locator}`);
      if (citation?.domain) lines.push(`  domain: ${citation.domain}`);
      if (citation?.published_at) lines.push(`  published_at: ${citation.published_at}`);
      if (citation?.url) lines.push(`  url: ${citation.url}`);
      if (citation?.path) lines.push(`  path: ${citation.path}`);
      if (citation?.excerpt) lines.push(`  excerpt: ${String(citation.excerpt).trim()}`);
      if (citation?.warning) lines.push(`  warning（风险提示）: ${citation.warning}`);
    });
    lines.push("");
  };
  appendCitationLines("citations（证据来源）", evidence);
  appendCitationLines("search_candidates（候选来源）", candidates, "候选链接，尚未抓取正文");

  const warnings = Array.isArray(bundle?.warnings) ? bundle.warnings : [];
  if (warnings.length) {
    lines.push("warnings（风险提示）:");
    warnings.slice(0, 5).forEach((warning, idx) => lines.push(`${idx + 1}. ${String(warning || "")}`));
  }

  runAnswerBundleView.textContent = lines.join("\n").trim();
}

function renderLlmFlow(items = []) {
  if (!runLlmFlowView) return;
  if (!Array.isArray(items) || !items.length) {
    runLlmFlowView.textContent = "暂无交换记录";
    return;
  }

  const lines = [];
  items.forEach((item, idx) => {
    const step = item?.step ?? idx + 1;
    const stage = item?.stage || "unknown";
    const stageLabel = LLM_FLOW_STAGE_LABELS[stage] || stage;
    const title = item?.title || "未命名步骤";
    const detail = item?.detail || "";
    lines.push(`[${step}] ${title} (${stageLabel})`);
    lines.push(detail);
    lines.push("");
  });

  runLlmFlowView.textContent = lines.join("\n").trim();
}

function formatUsd(value) {
  const num = Number(value || 0);
  if (!Number.isFinite(num) || num <= 0) return "0.000000";
  return num.toFixed(6);
}

function formatPrice(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(2);
}

function renderTokenStats(payload) {
  if (!tokenStatsView) return;
  const last = payload?.last || {};
  const session = payload?.session || {};
  const global = payload?.global || {};
  const pricingLine = last.pricing_known
    ? `计费模型: ${last.pricing_model || "-"} (in $${formatPrice(last.input_price_per_1m)}/1M, out $${formatPrice(last.output_price_per_1m)}/1M)`
    : `计费模型: ${last.pricing_model || "-"} (未匹配价格表，仅统计 token)`;
  tokenStatsView.textContent =
    `请求: ${global.requests || 0}\n` +
    `说明: 输入=你发给模型的 tokens，输出=模型回复的 tokens\n` +
    `本轮: in ${last.input_tokens || 0} / out ${last.output_tokens || 0} / total ${last.total_tokens || 0}\n` +
    `本轮费用(USD): ${formatUsd(last.estimated_cost_usd)}\n` +
    `${pricingLine}\n` +
    `本会话累计: req ${session.requests || 0} / total ${session.total_tokens || 0}\n` +
    `本会话累计费用(USD): ${formatUsd(session.estimated_cost_usd)}\n` +
    `全局累计: req ${global.requests || 0} / total ${global.total_tokens || 0}\n` +
    `全局累计费用(USD): ${formatUsd(global.estimated_cost_usd)}`;
}

async function refreshTokenStatsFromServer() {
  if (!tokenStatsView) return;
  try {
    const res = await fetch("/api/stats");
    if (!res.ok) return;
    const data = await res.json();
    const sessionTotals = state.sessionId ? (data.sessions?.[state.sessionId] || {}) : {};
    renderTokenStats({
      last: {},
      session: sessionTotals,
      global: data.totals || {},
    });
  } catch {}
}

function refreshFileList() {
  fileList.innerHTML = "";
  state.attachments.forEach((att, idx) => {
    const chip = document.createElement("div");
    chip.className = "file-chip";
    chip.innerHTML = `<span>${att.name}</span>`;

    const removeBtn = document.createElement("button");
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => {
      state.attachments.splice(idx, 1);
      refreshFileList();
    });
    chip.appendChild(removeBtn);

    fileList.appendChild(chip);
  });
}

function inferFileExtensionFromMime(mime) {
  const raw = String(mime || "").toLowerCase();
  if (raw.includes("png")) return "png";
  if (raw.includes("jpeg") || raw.includes("jpg")) return "jpg";
  if (raw.includes("webp")) return "webp";
  if (raw.includes("gif")) return "gif";
  if (raw.includes("bmp")) return "bmp";
  if (raw.includes("heic")) return "heic";
  return "png";
}

function buildClipboardImageName(file, index = 0) {
  const now = new Date();
  const stamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
    String(now.getHours()).padStart(2, "0"),
    String(now.getMinutes()).padStart(2, "0"),
    String(now.getSeconds()).padStart(2, "0"),
  ].join("");
  const ext = inferFileExtensionFromMime(file?.type);
  const suffix = index > 0 ? `-${index + 1}` : "";
  return `clipboard-image-${stamp}${suffix}.${ext}`;
}

function resolveUploadFileName(file, index = 0) {
  const raw = String(file?.name || "").trim();
  if (raw) return raw;
  return buildClipboardImageName(file, index);
}

function extractPastedImageFiles(event) {
  const clipboard = event?.clipboardData;
  if (!clipboard) return [];

  const files = Array.from(clipboard.files || []).filter((file) =>
    String(file?.type || "").toLowerCase().startsWith("image/")
  );
  if (files.length) return files;

  const items = Array.from(clipboard.items || []);
  const fromItems = [];
  items.forEach((item) => {
    if (String(item?.kind || "").toLowerCase() !== "file") return;
    if (!String(item?.type || "").toLowerCase().startsWith("image/")) return;
    const file = item.getAsFile();
    if (file) fromItems.push(file);
  });
  return fromItems;
}

function clipboardLooksLikeTableText(clipboard) {
  if (!clipboard || typeof clipboard.getData !== "function") return false;
  const plain = String(clipboard.getData("text/plain") || "");
  const html = String(clipboard.getData("text/html") || "").toLowerCase();
  if (html && (html.includes("<table") || html.includes("mso-"))) return true;
  if (!plain.trim()) return false;
  const lines = plain.split(/\r?\n/).filter((line) => String(line || "").trim());
  return plain.includes("\t") && lines.length >= 1;
}

async function createSession() {
  const res = await fetch("/api/session/new", { method: "POST" });
  if (!res.ok) throw new Error(`create session failed: ${res.status}`);
  const data = await res.json();
  state.sessionId = data.session_id;
  refreshSession();
  await refreshTokenStatsFromServer();
  await refreshSessionHistory();
}

async function uploadSingle(file, fileName = "") {
  const form = new FormData();
  const safeName = String(fileName || file?.name || "upload.bin").trim() || "upload.bin";
  form.append("file", file, safeName);

  const res = await fetch("/api/upload", {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`上传失败 ${safeName}: ${res.status} ${text}`);
  }
  return res.json();
}

async function handleFiles(files) {
  if (!files || !files.length) return;
  const preparedFiles = Array.from(files)
    .map((file, idx) => ({
      file,
      name: resolveUploadFileName(file, idx),
    }))
    .filter((item) => item.file instanceof Blob);
  if (!preparedFiles.length) return;

  state.uploading = true;
  updateSendAvailability();
  addBubble("system", `正在上传 ${preparedFiles.length} 个文件...`);

  try {
    for (const item of preparedFiles) {
      const uploaded = await uploadSingle(item.file, item.name);
      state.attachments.push(uploaded);
    }
    refreshFileList();
    const names = state.attachments.map((x) => x.name).join("，");
    addBubble("system", `上传完成，共 ${preparedFiles.length} 个文件。\n当前附件：${names}`);
  } catch (err) {
    addBubble("system", String(err));
  } finally {
    state.uploading = false;
    updateSendAvailability();
    fileInput.value = "";
  }
}

function getSettings() {
  const mode = String(execModeInput?.value || "").trim().toLowerCase();
  return {
    model: modelInput.value.trim() || null,
    max_output_tokens: Number(tokenInput.value || 128000),
    max_context_turns: Number(ctxInput.value || 2000),
    enable_tools: toolInput.checked,
    execution_mode: mode === "host" || mode === "docker" ? mode : null,
    debug_raw: Boolean(rawDebugInput?.checked),
    response_style: styleInput.value,
  };
}

async function runSandboxDrill() {
  if (state.drilling) return;

  const settings = getSettings();
  const payload = {
    execution_mode: settings.execution_mode || null,
  };
  const modeLabel = payload.execution_mode || "(backend default)";

  state.drilling = true;
  updateDrillAvailability();
  setRunStage("进行中", `开始沙盒演练，执行环境 ${modeLabel}`, "prepare", "working");
  if (runPayloadView) {
    runPayloadView.textContent = `sandbox drill payload:\n${formatJsonPreview(payload)}`;
  }
  renderRunTrace(["沙盒演练请求已发送。"], []);
  renderAgentPanels([], [], new Set(), null, new Map());
  renderAnswerBundle({});
  renderLlmFlow([
    {
      step: 1,
      stage: "frontend_prepare",
      title: "前端发起沙盒演练",
      detail: `POST /api/sandbox/drill\nexecution_mode=${modeLabel}`,
    },
  ]);

  try {
    const res = await fetch("/api/sandbox/drill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data?.detail) detail = String(data.detail);
      } catch {}
      throw new Error(detail);
    }

    const data = await res.json();
    const steps = Array.isArray(data?.steps) ? data.steps : [];
    const trace = [
      `run_id: ${data?.run_id || "-"}`,
      `execution_mode: ${data?.execution_mode || "-"}`,
      `summary: ${data?.summary || "-"}`,
      "",
      "steps:",
    ];
    steps.forEach((step, idx) => {
      const okText = step?.ok ? "OK" : "FAIL";
      const ms = Number(step?.duration_ms || 0);
      trace.push(
        `${idx + 1}. [${okText}] ${step?.name || "unnamed"} (${ms} ms) - ${String(step?.detail || "")}`
      );
    });
    renderRunTrace(trace, []);
    renderAgentPanels([], [], new Set(), null, new Map());
    renderAnswerBundle({});
    renderLlmFlow([
      {
        step: 1,
        stage: data?.ok ? "backend_tool" : "backend_warning",
        title: "沙盒演练结果",
        detail: formatJsonPreview(data),
      },
    ]);

    if (data?.ok) {
      setRunStage("完成", data?.summary || "沙盒演练通过", "done", "done");
      addBubble("system", `沙盒演练通过。\n${data?.summary || ""}`);
    } else {
      const failedNames = steps
        .filter((step) => !step?.ok)
        .map((step) => String(step?.name || "").trim())
        .filter(Boolean);
      setRunStage("失败", data?.summary || "沙盒演练失败", "parse", "error");
      addBubble(
        "system",
        `沙盒演练失败。\n${data?.summary || ""}${
          failedNames.length ? `\n失败步骤：${failedNames.join("，")}` : ""
        }`
      );
    }
  } catch (err) {
    const msg = `沙盒演练请求失败: ${String(err)}`;
    renderRunTrace([msg], []);
    renderAgentPanels([], [], new Set(), null, new Map());
    renderAnswerBundle({});
    renderLlmFlow([
      {
        step: 1,
        stage: "frontend_error",
        title: "沙盒演练失败",
        detail: msg,
      },
    ]);
    setRunStage("失败", "沙盒演练失败，请检查错误信息", "parse", "error");
    addBubble("system", msg);
  } finally {
    state.drilling = false;
    updateDrillAvailability();
  }
}

function summarizeEvalResult(item) {
  const name = String(item?.name || "unnamed");
  const kind = String(item?.kind || "tool");
  const status = String(item?.status || "unknown").toUpperCase();
  const elapsed = Number(item?.payload?.elapsed_sec || 0);
  const suffix = elapsed > 0 ? ` (${elapsed.toFixed(3)}s)` : "";
  if (item?.status === "failed") {
    const errors = Array.isArray(item?.errors) ? item.errors : [];
    return `[${status}] ${name} [${kind}]${suffix} - ${errors.join("; ") || "unknown error"}`;
  }
  if (item?.status === "skipped") {
    return `[${status}] ${name} [${kind}] - ${String(item?.reason || "")}`;
  }
  return `[${status}] ${name} [${kind}]${suffix}`;
}

async function runEvalHarness() {
  if (state.evaluating) return;

  const payload = {
    include_optional: false,
    name_filter: "",
  };

  state.evaluating = true;
  updateEvalAvailability();
  setRunStage("进行中", "开始回归测试（默认非 optional 用例）", "prepare", "working");
  if (runPayloadView) {
    runPayloadView.textContent = `eval harness payload:\n${formatJsonPreview(payload)}`;
  }
  renderRunTrace(["回归测试请求已发送。"], []);
  renderAgentPanels([], [], new Set(), null, new Map());
  renderAnswerBundle({});
  renderLlmFlow([
    {
      step: 1,
      stage: "frontend_prepare",
      title: "前端发起回归测试",
      detail: "POST /api/evals/run\ninclude_optional=false",
    },
  ]);

  try {
    const res = await fetch("/api/evals/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data?.detail) detail = String(data.detail);
      } catch {}
      throw new Error(detail);
    }

    const data = await res.json();
    const results = Array.isArray(data?.results) ? data.results : [];
    const failed = results.filter((item) => item?.status === "failed");
    const skipped = results.filter((item) => item?.status === "skipped");
    const trace = [
      `run_id: ${data?.run_id || "-"}`,
      `summary: ${data?.summary || "-"}`,
      `duration_ms: ${Number(data?.duration_ms || 0)}`,
      `cases_path: ${data?.cases_path || "-"}`,
      "",
      "results:",
      ...results.map((item) => summarizeEvalResult(item)),
    ];
    renderRunTrace(trace, []);

    const panels = [
      {
        role: "eval_harness",
        title: "Regression Evals",
        summary: data?.summary || "回归测试已完成。",
        bullets: [
          `passed=${Number(data?.passed || 0)}`,
          `failed=${Number(data?.failed || 0)}`,
          `skipped=${Number(data?.skipped || 0)}`,
          `total=${Number(data?.total || 0)}`,
        ],
      },
    ];
    if (failed.length) {
      panels.push({
        role: "eval_failures",
        title: "Failed Cases",
        summary: `失败用例 ${failed.length} 个。`,
        bullets: failed.slice(0, 6).map((item) => summarizeEvalResult(item)),
      });
    }
    if (skipped.length) {
      panels.push({
        role: "eval_skips",
        title: "Skipped Cases",
        summary: `跳过用例 ${skipped.length} 个。`,
        bullets: skipped.slice(0, 6).map((item) => summarizeEvalResult(item)),
      });
    }
    renderAgentPanels(panels, [], new Set(), null, new Map());
    renderAnswerBundle({});
    renderLlmFlow([
      {
        step: 1,
        stage: data?.ok ? "backend_tool" : "backend_warning",
        title: "回归测试结果",
        detail: formatJsonPreview(data),
      },
    ]);

    if (data?.ok) {
      setRunStage("完成", data?.summary || "回归测试通过", "done", "done");
      addBubble("system", `${data?.summary || "回归测试通过。"}\n可在运行面板查看逐条用例结果。`);
    } else {
      setRunStage("失败", data?.summary || "回归测试失败", "parse", "error");
      addBubble("system", `${data?.summary || "回归测试失败。"}\n请查看运行面板中的 Failed Cases。`);
    }
  } catch (err) {
    const msg = `回归测试请求失败: ${String(err)}`;
    renderRunTrace([msg], []);
    renderAgentPanels([], [], new Set(), null, new Map());
    renderAnswerBundle({});
    renderLlmFlow([
      {
        step: 1,
        stage: "frontend_error",
        title: "回归测试失败",
        detail: msg,
      },
    ]);
    setRunStage("失败", "回归测试失败，请检查错误信息", "parse", "error");
    addBubble("system", msg);
  } finally {
    state.evaluating = false;
    updateEvalAvailability();
  }
}

async function sendMessage() {
  const message = messageInput.value.trim();
  if (!message) return;
  let requestSessionId = currentSessionKey();
  if (requestSessionId && isSessionSending(requestSessionId)) return;
  let stopWaitTicker = null;
  const isForegroundSession = () => currentSessionKey() === requestSessionId;

  setRunStage("进行中", "正在准备本轮请求参数", "prepare", "working");

  if (!requestSessionId) {
    try {
      setRunStage("进行中", "正在创建新会话", "prepare", "working");
      await createSession();
    } catch (err) {
      setRunStage("失败", "创建会话失败，请检查错误信息", "prepare", "error");
      addBubble("system", `创建会话失败: ${String(err)}`);
      return;
    }
    requestSessionId = currentSessionKey();
  }
  if (!requestSessionId) {
    setRunStage("失败", "创建会话失败，请重试", "prepare", "error");
    addBubble("system", "创建会话失败，请重试。");
    return;
  }
  if (isSessionSending(requestSessionId)) {
    return;
  }

  addBubble("user", message);
  if (state.attachments.length) {
    const names = state.attachments.map((x) => x.name).join("，");
    addBubble("system", `本轮将携带 ${state.attachments.length} 个附件：${names}`);
  }
  messageInput.value = "";
  state.sendingSessionIds.add(requestSessionId);
  updateSendAvailability();

  try {
    const body = {
      session_id: requestSessionId,
      message,
      attachment_ids: state.attachments.map((x) => x.id),
      settings: getSettings(),
    };
    renderRunPayload(
      body,
      state.attachments.map((x) => x.name)
    );
    const liveTrace = ["客户端已组装请求，等待发送。"];
    const liveToolEvents = [];
    let liveExecutionPlan = [];
    let liveAgentPanels = [];
    let liveActiveRoles = new Set();
    let liveRoleStates = new Map();
    const liveFlow = [
      {
        step: 1,
        stage: "frontend_prepare",
        title: "前端准备请求",
        detail: "已生成 payload，正在调用 /api/chat/stream",
      },
    ];
    let heartbeatCount = 0;
    renderRunTrace(liveTrace, liveToolEvents);
    renderAgentPanels([], [], liveActiveRoles, null, liveRoleStates);
    renderAnswerBundle({});
    renderLlmFlow(liveFlow);
    setRunStage("进行中", "请求已发往后端，等待模型处理", "send", "working");
    const totalAttachmentBytes = state.attachments.reduce((sum, item) => {
      const size = Number(item?.size || 0);
      return sum + (Number.isFinite(size) ? size : 0);
    }, 0);
    stopWaitTicker = startWaitStageTicker(totalAttachmentBytes);
    const data = await streamChatRequest(body, {
      onStage: (payload) => {
        if (!isForegroundSession()) return;
        const code = String(payload?.code || "");
        if (
          typeof stopWaitTicker === "function" &&
          (code === "agent_run_done" || code === "session_saved" || code === "stats_saved" || code === "ready")
        ) {
          stopWaitTicker();
          stopWaitTicker = null;
        }
        applyBackendStage(payload);
      },
      onTrace: (payload) => {
        if (!isForegroundSession()) return;
        const line = String(payload?.message || "").trim();
        if (!line) return;
        liveTrace.push(line);
        renderRunTrace(liveTrace, liveToolEvents);
      },
      onDebug: (payload) => {
        if (!isForegroundSession()) return;
        const item = payload?.item;
        if (!item || typeof item !== "object") return;
        liveFlow.push(item);
        if (!liveActiveRoles.size) {
          liveActiveRoles = inferActiveRolesFromDebugItem(item);
          renderRoleBoard(liveAgentPanels, liveActiveRoles, null, liveRoleStates);
        }
        renderLlmFlow(liveFlow);
      },
      onToolEvent: (payload) => {
        if (!isForegroundSession()) return;
        const item = payload?.item;
        if (!item || typeof item !== "object") return;
        liveToolEvents.push(item);
        renderRunTrace(liveTrace, liveToolEvents);
      },
      onAgentState: (payload) => {
        if (!isForegroundSession()) return;
        liveExecutionPlan = Array.isArray(payload?.execution_plan) ? payload.execution_plan : liveExecutionPlan;
        liveAgentPanels = Array.isArray(payload?.panels) ? payload.panels : liveAgentPanels;
        liveActiveRoles = normalizeRoleSet(payload?.active_roles);
        const liveCurrentRole = normalizeRoleId(payload?.current_role);
        liveRoleStates = normalizeRoleStateMap(payload?.role_states);
        if (liveCurrentRole) liveActiveRoles.add(liveCurrentRole);
        renderAgentPanels(liveAgentPanels, liveExecutionPlan, liveActiveRoles, liveCurrentRole || null, liveRoleStates);
      },
      onHeartbeat: () => {
        if (!isForegroundSession()) return;
        heartbeatCount += 1;
        if (heartbeatCount === 1 || heartbeatCount % 3 === 0) {
          liveTrace.push(
            `后端心跳：仍在处理中（约 ${heartbeatCount * 10}s 无新事件，连接正常）`
          );
          renderRunTrace(liveTrace, liveToolEvents);
        }
      },
    });
    if (typeof stopWaitTicker === "function") {
      stopWaitTicker();
      stopWaitTicker = null;
    }
    setRunStage("进行中", "收到最终结果，正在整理展示", "parse", "working");
    if (!data || typeof data !== "object") {
      throw new Error("流式响应异常：未收到最终结果。");
    }

    const responseSessionId = String(data.session_id || requestSessionId);
    if (isForegroundSession()) {
      state.sessionId = responseSessionId;
      refreshSession();
      const selectedModel = String(body?.settings?.model || "").trim();
      const effectiveModel = String(data?.effective_model || "").trim();
      const queueWaitMs = Number(data?.queue_wait_ms || 0);
      if (effectiveModel && (!selectedModel || selectedModel !== effectiveModel)) {
        addBubble("system", `本轮模型自动切换：${selectedModel || "(默认)"} -> ${effectiveModel}`);
      }
      if (queueWaitMs >= 1000) {
        addBubble("system", `本轮排队等待 ${queueWaitMs} ms 后开始执行。`);
      }

      if (data.summarized) {
        addBubble("system", "历史上下文已自动压缩摘要，避免窗口过长。", null);
      }
      if (Array.isArray(data.missing_attachment_ids) && data.missing_attachment_ids.length) {
        addBubble(
          "system",
          `有 ${data.missing_attachment_ids.length} 个附件未找到，请重新上传后重试。\nIDs: ${data.missing_attachment_ids.join(", ")}`,
          null
        );
        const missing = new Set(data.missing_attachment_ids);
        state.attachments = state.attachments.filter((x) => !missing.has(x.id));
        refreshFileList();
      }
      const autoLinkedNames = Array.isArray(data.auto_linked_attachment_names)
        ? data.auto_linked_attachment_names.filter((x) => String(x || "").trim())
        : [];
      if (autoLinkedNames.length) {
        addBubble("system", `已自动关联历史附件：${autoLinkedNames.join("，")}`, null);
      } else if (String(data.attachment_context_mode || "") === "cleared") {
        addBubble("system", "已按你的指令忽略历史附件。", null);
      }

      renderRunTrace(data.execution_trace || [], data.tool_events || []);
      liveActiveRoles = normalizeRoleSet(data.active_roles);
      const finalCurrentRole = normalizeRoleId(data.current_role);
      liveRoleStates = normalizeRoleStateMap(data.role_states);
      renderAgentPanels(data.agent_panels || [], data.execution_plan || [], liveActiveRoles, finalCurrentRole || null, liveRoleStates);
      renderAnswerBundle(data.answer_bundle || {});
      renderLlmFlow(data.debug_flow || []);
      addBubble("assistant", data.text, data.answer_bundle || null);
    }
    await refreshSessionHistory();
    if (isForegroundSession()) {
      renderTokenStats({
        last: data.token_usage || {},
        session: data.session_token_totals || {},
        global: data.global_token_totals || {},
      });
      await refreshSystemDashboard().catch(() => {});
      setRunStage("完成", "本轮已完成", "done", "done");
    }
  } catch (err) {
    if (typeof stopWaitTicker === "function") {
      stopWaitTicker();
      stopWaitTicker = null;
    }
    if (isForegroundSession()) {
      renderRunTrace([`请求失败: ${String(err)}`], []);
      renderAgentPanels([], [], new Set(), null, new Map());
      renderAnswerBundle({});
      renderLlmFlow([
        {
          step: 1,
          stage: "frontend_error",
          title: "前端请求失败",
          detail: String(err),
        },
      ]);
      setRunStage("失败", "请求失败，请检查错误信息", "parse", "error");
      addBubble("system", `请求失败: ${String(err)}`);
    }
  } finally {
    if (typeof stopWaitTicker === "function") {
      stopWaitTicker();
      stopWaitTicker = null;
    }
    state.sendingSessionIds.delete(requestSessionId);
    updateSendAvailability();
  }
}

fileInput.addEventListener("change", (e) => {
  const files = Array.from(e.target.files || []);
  handleFiles(files);
});

["dragenter", "dragover"].forEach((evt) => {
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropZone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach((evt) => {
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropZone.classList.remove("dragging");
  });
});

dropZone.addEventListener("drop", (e) => {
  const files = Array.from(e.dataTransfer?.files || []);
  handleFiles(files);
});

sendBtn.addEventListener("click", sendMessage);

if (panelDebugInput) {
  panelDebugInput.addEventListener("change", () => {
    applyPanelDebugMode(Boolean(panelDebugInput.checked));
  });
}

if (presetGeneralBtn) {
  presetGeneralBtn.addEventListener("click", () => applyModePreset("general"));
}

if (presetCodingBtn) {
  presetCodingBtn.addEventListener("click", () => applyModePreset("coding"));
}

if (sandboxDrillBtn) {
  sandboxDrillBtn.addEventListener("click", runSandboxDrill);
}

if (evalHarnessBtn) {
  evalHarnessBtn.addEventListener("click", runEvalHarness);
}

messageInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

document.addEventListener("paste", (e) => {
  const target = e.target;
  const isInputLike =
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    Boolean(target && typeof target === "object" && "isContentEditable" in target && target.isContentEditable);
  if (isInputLike && target !== messageInput) return;

  const clipboard = e.clipboardData;
  const imageFiles = extractPastedImageFiles(e);
  if (!imageFiles.length) return;
  if (clipboardLooksLikeTableText(clipboard)) return;

  e.preventDefault();
  handleFiles(imageFiles);
});

newSessionBtn.addEventListener("click", async () => {
  await createSession();
  state.attachments = [];
  refreshFileList();
  clearChat();
  addBubble("system", "已新建会话。", null);
});

if (clearStatsBtn) {
  clearStatsBtn.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/stats/clear", { method: "POST" });
      if (!res.ok) throw new Error(`clear failed: ${res.status}`);
      await refreshTokenStatsFromServer();
      addBubble("system", "Token 统计已清除。");
    } catch (err) {
      addBubble("system", `清除统计失败: ${String(err)}`);
    }
  });
}

if (refreshSessionsBtn) {
  refreshSessionsBtn.addEventListener("click", async () => {
    await refreshSessionHistory();
    addBubble("system", "历史会话列表已刷新。");
  });
}

if (deleteSessionBtn) {
  deleteSessionBtn.addEventListener("click", async () => {
    if (!state.sessionId) {
      addBubble("system", "当前没有可删除的会话。");
      return;
    }
    await deleteSessionById(state.sessionId);
  });
}

(async function boot() {
  applyModePreset("general", false);
  restorePanelDebugMode();
  setRunStage("空闲", "等待发送请求", null, "idle");
  updateDrillAvailability();
  updateEvalAvailability();
  renderRunPayload(
    {
      session_id: null,
      message: "",
      attachment_ids: [],
      settings: getSettings(),
    },
    []
  );
  renderRunTrace([], []);
  renderAgentPanels([], [], new Set(), null, new Map());
  renderLlmFlow([]);
  try {
    const health = await refreshSystemDashboard();
    modelInput.placeholder = health.model_default || MODE_PRESETS.general.model;
    if (!modelInput.value) {
      modelInput.value = health.model_default || MODE_PRESETS.general.model;
    }
    const backendExecMode = String(health.execution_mode_default || "host").toLowerCase();
    const dockerMsg = String(health.docker_message || "").trim();
    renderBackendPolicy(health);
    if (execModeInput) {
      execModeInput.value = "";
      const dockerOption = execModeInput.querySelector('option[value="docker"]');
      if (dockerOption) {
        const dockerReady = Boolean(health.docker_available);
        dockerOption.disabled = !dockerReady;
        dockerOption.textContent = dockerReady ? "Docker（沙盒）" : "Docker（未就绪）";
        dockerOption.title = dockerMsg || (dockerReady ? "Docker is available" : "Docker is not available");
      }
      execModeInput.title = `后端默认执行环境: ${backendExecMode}`;
    }
    await refreshSessionHistory();
    const restored = await restoreSessionIfPossible();
    if (!restored) {
      const dockerTip = health.docker_available ? "Docker 可用" : "Docker 未就绪";
      const allowAllWebDomains = Boolean(health.web_allow_all_domains);
      const webDomains = Array.isArray(health.web_allowed_domains) ? health.web_allowed_domains : [];
      const pathPolicyTip = Boolean(health.allow_any_path)
        ? "文件路径：不限制（ALLOW_ANY_PATH）"
        : `文件根目录：${Array.isArray(health.allowed_roots) && health.allowed_roots.length ? health.allowed_roots.join(", ") : "(空)"}`;
      const webPolicyTip = allowAllWebDomains
        ? "联网域名：不限制"
        : `联网域名白名单：${webDomains.length ? webDomains.join(", ") : "(空)"}`;
      addBubble(
        "system",
        `服务已启动，版本：${String(health.build_version || health.app_version || "unknown")}；默认模型：${health.model_default}；默认执行环境：${backendExecMode}（${dockerTip}）。\n${pathPolicyTip}\n${webPolicyTip}${dockerMsg ? `\nDocker: ${dockerMsg}` : ""}`
      );
    }
    await refreshTokenStatsFromServer();
    window.setInterval(() => {
      refreshSystemDashboard().catch(() => {});
    }, 15000);
  } catch {
    addBubble("system", "健康检查失败，请确认后端已运行。", null);
  }
})();
