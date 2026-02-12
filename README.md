# Offciatool (Office Agent)

一个可在办公室高频使用的本地 Agent 工具，核心能力：

- 对话 + 上传图片/文档（支持拖拽）
- 可选本地工具执行（白名单命令）
- 会话自动摘要压缩，避免上下文无限增长
- 页面显示“执行计划 + 执行轨迹”，可见每次调用实际做了什么
- 可控输出长度（short/normal/long）和 token 上限
- LLM 驱动层使用 `langchain_openai`（支持 OpenAI 兼容网关）
- 附件链路带“未找到附件”显式告警，避免只看到“上传成功”但上下文没带上

## 1. 快速启动

```bash
cd /Users/dalizhou/Desktop/offciatool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY
# 如需接公司网关，再填 OFFCIATOOL_OPENAI_BASE_URL=https://<YOUR_COMPANY_API_BASE>/v1
# 如需内部根证书，再填 OFFCIATOOL_CA_CERT_PATH=/absolute/path/to/your-root-ca.cer
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

打开浏览器：

- [http://127.0.0.1:8080](http://127.0.0.1:8080)

### Windows 启动（PowerShell）

```powershell
cd $HOME\Desktop
git clone https://github.com/jonhncatt/offciatool.git
cd .\offciatool
git checkout codex/office-agent

py -3.11 -m venv .venv
# 如果激活报策略限制，先执行：Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt

$env:OPENAI_API_KEY = "<YOUR_API_KEY>"
$env:OFFCIATOOL_OPENAI_BASE_URL = "https://<YOUR_COMPANY_API_BASE>/v1"
$env:OFFCIATOOL_CA_CERT_PATH = "C:\path\to\KIOXIAInternalRootCA.cer"
$env:OFFCIATOOL_USE_RESPONSES_API = "false"

python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

### Windows 启动（CMD）

```bat
cd %USERPROFILE%\Desktop
git clone https://github.com/jonhncatt/offciatool.git
cd offciatool
git checkout codex/office-agent

py -3.11 -m venv .venv
call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
pip install -r requirements.txt

set OPENAI_API_KEY=<YOUR_API_KEY>
set OFFCIATOOL_OPENAI_BASE_URL=https://<YOUR_COMPANY_API_BASE>/v1
set OFFCIATOOL_CA_CERT_PATH=C:\path\to\KIOXIAInternalRootCA.cer
set OFFCIATOOL_USE_RESPONSES_API=false

python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

### Windows 日常启动（后续每次）

首次配置完成后，后续每天只需要下面几步：

```powershell
cd $HOME\Desktop\offciatool
git pull
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

建议把以下环境变量做成长期生效（仅需一次）：

```powershell
setx OPENAI_API_KEY "<YOUR_API_KEY>"
setx OFFCIATOOL_OPENAI_BASE_URL "https://<YOUR_COMPANY_API_BASE>/v1"
setx OFFCIATOOL_CA_CERT_PATH "C:\path\to\KIOXIAInternalRootCA.cer"
setx OFFCIATOOL_USE_RESPONSES_API "false"
```

## 2. 功能说明

### 图片/文档

- 支持图片：png/jpg/jpeg/webp/gif/heic/heif
- 支持文档：txt/md/csv/json/pdf/docx 及常见代码文本
- 图片直接送入多模态输入；文档会先抽取文本后送入模型
- HEIC 优先本地转码为 JPEG；若环境缺少转码依赖会回退为原始 HEIC 并给出提示

### Agent 工具调用

默认开放 3 个工具：

- `run_shell`: 在工作目录下执行单条命令（禁用管道/链式操作）
- `list_directory`: 列目录
- `read_text_file`: 读文本文件

安全约束：

- 命令白名单（`OFFCIATOOL_ALLOWED_COMMANDS`）
- 路径只能在 workspace 根目录内

### 上下文控制

- 每次请求只带最近 `max_context_turns` 条历史消息（不是“思考轮数”）
- 当历史轮数超过阈值时自动摘要，保留长期记忆但压缩 tokens

### 参数解释（页面左侧）

- `最大输出 tokens`：单次回复的 token 上限。默认 `3200`，可按任务调到 `4000~8000`（网关若有限制会报错）。
- `上下文消息条数`：每次请求带入最近多少条历史消息。默认 `16`（约等于最近 8 轮问答）。
- `回答长度`：输出风格开关（短/中/长），用于控制回答详细程度，不等于固定 token 数。

### API 地址配置

- 默认直接访问 OpenAI 官方地址
- 如需脱敏并改走公司代理，请在 `.env` 设置：`OFFCIATOOL_OPENAI_BASE_URL`
- 如需公司 CA 证书，请设置：`OFFCIATOOL_CA_CERT_PATH`（等价 `curl --cacert`）
- 如需强制走 Chat Completions/tool calling 语义，请设置：`OFFCIATOOL_USE_RESPONSES_API=false`

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
