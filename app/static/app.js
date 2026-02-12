const state = {
  sessionId: null,
  uploading: false,
  sending: false,
  attachments: [],
};

const chatList = document.getElementById("chatList");
const fileInput = document.getElementById("fileInput");
const fileList = document.getElementById("fileList");
const dropZone = document.getElementById("dropZone");
const messageInput = document.getElementById("messageInput");
const sendBtn = document.getElementById("sendBtn");
const newSessionBtn = document.getElementById("newSessionBtn");
const sessionIdView = document.getElementById("sessionIdView");
const tokenStatsView = document.getElementById("tokenStatsView");
const clearStatsBtn = document.getElementById("clearStatsBtn");

const modelInput = document.getElementById("modelInput");
const tokenInput = document.getElementById("tokenInput");
const ctxInput = document.getElementById("ctxInput");
const styleInput = document.getElementById("styleInput");
const toolInput = document.getElementById("toolInput");

function addBubble(role, text, tools = null) {
  const bubble = document.createElement("div");
  bubble.className = `bubble ${role}`;
  bubble.textContent = text;

  if (tools && Array.isArray(tools) && tools.length) {
    const toolBox = document.createElement("div");
    toolBox.className = "tool-box";

    tools.forEach((item, idx) => {
      const row = document.createElement("div");
      row.className = "tool-item";
      const inputTxt = item.input ? JSON.stringify(item.input) : "{}";
      row.textContent = `#${idx + 1} ${item.name}(${inputTxt}) -> ${item.output_preview}`;
      toolBox.appendChild(row);
    });

    bubble.appendChild(toolBox);
  }

  chatList.appendChild(bubble);
  chatList.scrollTop = chatList.scrollHeight;
}

function formatNumberedLines(title, items) {
  if (!Array.isArray(items) || !items.length) return null;
  const lines = items.map((item, idx) => `${idx + 1}. ${item}`);
  return `${title}\n${lines.join("\n")}`;
}

function refreshSession() {
  sessionIdView.textContent = state.sessionId || "(未创建)";
}

function renderTokenStats(payload) {
  if (!tokenStatsView) return;
  const last = payload?.last || {};
  const session = payload?.session || {};
  const global = payload?.global || {};
  tokenStatsView.textContent =
    `请求: ${global.requests || 0}\n` +
    `本轮: in ${last.input_tokens || 0} / out ${last.output_tokens || 0} / total ${last.total_tokens || 0}\n` +
    `本会话累计: req ${session.requests || 0} / total ${session.total_tokens || 0}\n` +
    `全局累计: req ${global.requests || 0} / total ${global.total_tokens || 0}`;
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

async function createSession() {
  const res = await fetch("/api/session/new", { method: "POST" });
  if (!res.ok) throw new Error(`create session failed: ${res.status}`);
  const data = await res.json();
  state.sessionId = data.session_id;
  refreshSession();
  await refreshTokenStatsFromServer();
}

async function uploadSingle(file) {
  const form = new FormData();
  form.append("file", file);

  const res = await fetch("/api/upload", {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`上传失败 ${file.name}: ${res.status} ${text}`);
  }
  return res.json();
}

async function handleFiles(files) {
  if (!files || !files.length) return;

  state.uploading = true;
  sendBtn.disabled = true;
  addBubble("system", `正在上传 ${files.length} 个文件...`);

  try {
    for (const file of files) {
      const uploaded = await uploadSingle(file);
      state.attachments.push(uploaded);
    }
    refreshFileList();
    const names = state.attachments.map((x) => x.name).join("，");
    addBubble("system", `上传完成，共 ${files.length} 个文件。\n当前附件：${names}`);
  } catch (err) {
    addBubble("system", String(err));
  } finally {
    state.uploading = false;
    sendBtn.disabled = false;
    fileInput.value = "";
  }
}

function getSettings() {
  return {
    model: modelInput.value.trim() || null,
    max_output_tokens: Number(tokenInput.value || 3200),
    max_context_turns: Number(ctxInput.value || 16),
    enable_tools: toolInput.checked,
    response_style: styleInput.value,
  };
}

async function sendMessage() {
  const message = messageInput.value.trim();
  if (!message || state.sending) return;

  if (!state.sessionId) {
    await createSession();
  }

  addBubble("user", message);
  if (state.attachments.length) {
    const names = state.attachments.map((x) => x.name).join("，");
    addBubble("system", `本轮将携带 ${state.attachments.length} 个附件：${names}`);
  }
  messageInput.value = "";
  sendBtn.disabled = true;
  state.sending = true;

  try {
    const body = {
      session_id: state.sessionId,
      message,
      attachment_ids: state.attachments.map((x) => x.id),
      settings: getSettings(),
    };

    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    const data = await res.json();
    if (!res.ok) {
      const msg = data?.detail || JSON.stringify(data);
      throw new Error(msg);
    }

    state.sessionId = data.session_id;
    refreshSession();

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

    const traceText = formatNumberedLines("执行轨迹", data.execution_trace || []);
    if (traceText) {
      addBubble("system", traceText, null);
    }

    addBubble("assistant", data.text, data.tool_events || []);

    addBubble("system", "附件已保留，可继续追问；不需要时点附件上的 × 删除。");
    renderTokenStats({
      last: data.token_usage || {},
      session: data.session_token_totals || {},
      global: data.global_token_totals || {},
    });
  } catch (err) {
    addBubble("system", `请求失败: ${String(err)}`);
  } finally {
    state.sending = false;
    sendBtn.disabled = false;
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

messageInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

newSessionBtn.addEventListener("click", async () => {
  await createSession();
  state.attachments = [];
  refreshFileList();
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

(async function boot() {
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    addBubble("system", `服务已启动，默认模型：${health.model_default}`);
    modelInput.placeholder = health.model_default || "gpt-4.1";
    await refreshTokenStatsFromServer();
  } catch {
    addBubble("system", "健康检查失败，请确认后端已运行。", null);
  }
})();
