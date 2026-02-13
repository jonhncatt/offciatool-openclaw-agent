# Officetool (Office Agent)

一个可在办公室高频使用的本地 Agent 工具，核心能力：

- 对话 + 上传图片/文档（支持拖拽）
- 可选本地工具执行（读写文件、白名单命令、联网抓取）
- 会话自动摘要压缩，避免上下文无限增长
- 页面显示“执行轨迹”，可见每次调用实际做了什么
- 页面展示 Token 统计（本轮/会话累计/全局累计），除非手动清除会一直累积
- 可控输出长度（short/normal/long）和 token 上限
- LLM 驱动层使用 `langchain_openai`（支持 OpenAI 兼容网关）
- 附件链路带“未找到附件”显式告警，避免只看到“上传成功”但上下文没带上
- 输入框支持 `Enter` 直接发送，`Shift+Enter` 换行

## 1. 快速启动

```bash
cd /Users/dalizhou/Desktop/officetool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY
# 如需接公司网关，再填 OFFICETOOL_OPENAI_BASE_URL=https://<YOUR_COMPANY_API_BASE>/v1
# 如需内部根证书，再填 OFFICETOOL_CA_CERT_PATH=/absolute/path/to/your-root-ca.cer
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

打开浏览器：

- [http://127.0.0.1:8080](http://127.0.0.1:8080)
- 说明：应用会自动读取项目根目录 `.env`，无需再手动 `export` 或 `setx`

### Windows 启动（PowerShell）

```powershell
cd $HOME\Desktop
git clone https://github.com/jonhncatt/offciatool.git officetool
cd .\officetool
git checkout codex/office-agent

py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 编辑 .env（填 OPENAI_API_KEY；需要的话再填公司网关和 CA）

.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

### Windows 启动（CMD）

```bat
cd %USERPROFILE%\Desktop
git clone https://github.com/jonhncatt/offciatool.git officetool
cd officetool
git checkout codex/office-agent

py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
rem 编辑 .env（填 OPENAI_API_KEY；需要的话再填公司网关和 CA）

.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

### Windows 日常启动（后续每次）

首次配置完成后，后续每天只需要下面几步：

```powershell
cd $HOME\Desktop\officetool
git pull
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

检查 `OFFICETOOL_EXTRA_ALLOWED_ROOTS` 是否生效：

```powershell
cd $HOME\Desktop\officetool
.\.venv\Scripts\python.exe -c "from app.config import load_config; c=load_config(); print(c.allowed_roots)"
```

如果助手仍说“只能看当前目录”，先确认：

- 左侧 `启用本地工具执行` 已勾选
- 提问时带绝对路径（示例：`请列出 C:/Users/<YOU>/Desktop/workbench`）
- 看“执行轨迹”是否出现 `执行工具: list_directory` / `read_text_file`

## 2. 功能说明

### 图片/文档

- 支持图片：png/jpg/jpeg/webp/gif/heic/heif
- 支持文档：txt/md/csv/json/pdf/docx/msg 及常见代码文本
- 图片直接送入多模态输入；文档会先抽取文本后送入模型
- `.msg`（Outlook 邮件）会抽取主题/发件人/收件人/时间/正文/附件列表
- HEIC 优先本地转码为 JPEG；若环境缺少转码依赖会回退为原始 HEIC 并给出提示
- 对无法结构化解析的二进制/未知类型文件，会自动附带十六进制预览，确保模型“看得到文件内容”

### Agent 工具调用

默认开放 9 个工具：

- `run_shell`: 在工作目录下执行单条命令（禁用管道/链式操作）
- `list_directory`: 列目录
- `read_text_file`: 读文本文件（支持 `start_char + max_chars` 分块读取大文件）
- `copy_file`: 二进制安全复制文件（推荐用于“复制整个文件”）
- `write_text_file`: 新建/覆盖写文本文件
- `replace_in_file`: 按目标文本做替换（支持一次或多次）
- `search_web`: 关键词联网搜索，返回候选链接与摘要（优先用于“先找链接”）
- `fetch_web`: 联网抓取网页/JSON 文本
- `download_web_file`: 二进制安全下载网页文件并落盘（PDF/ZIP/图片等）

安全约束：

- 命令白名单（`OFFICETOOL_ALLOWED_COMMANDS`）
- 路径默认只能在 workspace 根目录内；可用 `OFFICETOOL_EXTRA_ALLOWED_ROOTS` 扩展
- 可用 `OFFICETOOL_ALLOW_ANY_PATH=true` 完全放开（仅建议内网可信环境；兼容旧名 `OFFCIATOOL_ALLOW_ANY_PATH`）
- 联网抓取可用 `OFFICETOOL_WEB_ALLOWED_DOMAINS` 限定域名白名单（为空则不限制）
- 网页抓取会自动从 HTML 提取正文文本；若目标站点是 JS 动态渲染/反爬页面，仍可能信息较少
- `fetch_web` 遇到 PDF 会尽量抽取正文文本；需要原始文件时请用 `download_web_file`
- 联网任务建议先 `search_web` 再 `fetch_web`，可减少“先问网址”的来回交互
- 对“新闻/实时”类问题，后端会自动做一次 `search_web` 预搜索并把候选链接注入上下文，减少反复追问
- 对“棒球新闻”等体育场景，`search_web` 会优先尝试 MLB/ESPN/Yahoo 等 RSS 源；搜索页被反爬时也会回退到可访问入口
- 如遇证书链异常，可设置 `OFFICETOOL_WEB_CA_CERT_PATH` 指定 CA；若仍失败可临时用 `OFFICETOOL_WEB_SKIP_TLS_VERIFY=true`（仅建议内网）
- 若未配置上述参数且遇到证书校验失败，`fetch_web` 也会自动降级重试一次（返回 `warning` 提示）
- 若要“完整复制一个文件”，请让助手使用 `copy_file`，不要用 `read_text_file + write_text_file`（前者是全量复制，后者可能按 `max_chars` 截断）
- 大文件建议让助手按块读取（例如每次 `max_chars=200000`，再用下一块 `start_char=end_char` 继续）

### 上下文控制

- 每次请求只带最近 `max_context_turns` 条历史消息（不是“思考轮数”）
- 当历史轮数超过阈值时自动摘要，保留长期记忆但压缩 tokens
- 会话历史持久化在 `app/data/sessions/*.json`；页面刷新后会自动尝试恢复上次会话（基于浏览器本地保存的 session_id）
- 左侧新增“历史会话”列表，可直接切换旧会话（不只保留当前会话）
- 历史会话支持删除（左侧“刷新”旁边点“删除”）；删除当前会话后会自动切换到下一条

### 参数解释（页面左侧）

- `通用模式 / 编码模式`：一键切换模型与参数预设（编码模式默认 `gpt-5.1-codex-mini`）。
- `最大输出 tokens`：单次回复的 token 上限。默认 `128000`（网关若有限制会报错）。
- `上下文消息条数`：每次请求带入最近多少条历史消息。默认 `2000`，页面最高可设置到 `2000`。
- `回答长度`：输出风格开关（短/中/长），用于控制回答详细程度，不等于固定 token 数。
- `Token 统计`：
  - 输入 tokens = 你发给模型的内容（system + history + 当前问题 + 工具结果等）
  - 输出 tokens = 模型回给你的内容
  - 费用 = 按 `输入/输出` 分别计价后相加（页面显示本轮/会话/全局累计 USD）

### API 地址配置

- 默认直接访问 OpenAI 官方地址
- 如需脱敏并改走公司代理，请在 `.env` 设置：`OFFICETOOL_OPENAI_BASE_URL`
- 如需公司 CA 证书，请设置：`OFFICETOOL_CA_CERT_PATH`（等价 `curl --cacert`）
- 如需强制走 Chat Completions/tool calling 语义，请设置：`OFFICETOOL_USE_RESPONSES_API=false`
- 摘要模型变量为 `OFFICETOOL_SUMMARY_MODEL`（兼容别名 `OFFICETOOL_SUMMARY_MODE`）
- 温度默认不强制传参；如需指定可设置 `OFFICETOOL_TEMPERATURE`（例如 `0` 或 `1`）
- 如网页抓取报 `CERTIFICATE_VERIFY_FAILED`（如 basic constraints not marked critical），请设置：`OFFICETOOL_WEB_SKIP_TLS_VERIFY=true`
- 模型价格来源：OpenAI 官方定价页（[openai.com/api/pricing](https://openai.com/api/pricing/)），当前内置表按 2026-02-12 的公开价格写入 `app/pricing.py`

## 3. 目录结构

```text
app/
  agent.py         # 模型调用 + 工具循环 + 会话摘要
  attachments.py   # 文档抽取、图片读取
  config.py        # 配置项
  local_tools.py   # 本地工具执行安全层
  main.py          # FastAPI 路由
  models.py        # API 数据模型
  storage.py       # 会话与上传持久化
  static/
    index.html
    styles.css
    app.js
  data/
    sessions/      # 会话存储（运行后生成）
    uploads/       # 上传文件与索引（运行后生成）
```

## 4. 办公场景建议

- 写日报：上传文档 + 指令“给我简版/长版日报”
- 看图提要：把会议白板或截图拖进去，要求模型提炼行动项
- 项目助手：打开工具执行，直接让它检查仓库状态并总结
- 降本控长：可手动改成 `short + 600~1000 tokens + context 8~12`

## 5. 注意事项

- 公司内网网页登录失败不会影响本地运行。
- 如果需要接企业 SSO，可后续在 `/api/chat` 前加鉴权中间件。
- HEIC 支持依赖 `pillow-heif`；如果缺依赖会提示解析失败。
- 如页面按钮点了无反应，先强制刷新浏览器缓存（Windows: `Ctrl+F5`）。新版已对 `/static/*` 设置 no-cache。
