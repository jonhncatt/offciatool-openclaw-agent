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

function refreshSession() {
  sessionIdView.textContent = state.sessionId || "(未创建)";
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
    addBubble("system", `上传完成，共 ${files.length} 个文件。`);
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
    max_output_tokens: Number(tokenInput.value || 1600),
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

    addBubble("assistant", data.text, data.tool_events || []);

    // 每次发送后清空附件，避免重复带入
    state.attachments = [];
    refreshFileList();
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
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
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

(async function boot() {
  try {
    const health = await fetch("/api/health").then((r) => r.json());
    addBubble("system", `服务已启动，默认模型：${health.model_default}`);
    modelInput.placeholder = health.model_default || "gpt-4.1";
  } catch {
    addBubble("system", "健康检查失败，请确认后端已运行。", null);
  }
})();
