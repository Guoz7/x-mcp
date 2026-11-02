# X MCP

语言：English · [简体中文](README.zh-CN.md)


让任何支持 MCP 的 Agent 读取 X、准备社交动作，并在确认后完成可复验的操作。

X MCP 提供两条路径：

- API 路径：适合公开数据和结构化读取。
- 浏览器路径：适合登录页面、视觉兜底和页面复验。

## 一分钟体验

### Windows 测试包

```powershell
tester.cmd setup
tester.cmd smoke
tester.cmd config
```

把 `config` 输出加入 MCP 客户端，再把 [`ONE-CLICK-PROMPT.md`](ONE-CLICK-PROMPT.md) 复制给 Agent。

### 源码运行

环境要求：Python 3.11 或更高版本。浏览器路径还需要 Chrome。Node.js 20 或更高版本只用于可选的 MCP 检查。

```powershell
python -I -X utf8 server.py --demo
python -I -X utf8 run_mock.ps1
```

启动 Streamable HTTP：

```powershell
python -I -X utf8 server.py --transport http --port 8765
```

启动浏览器路径：

```powershell
$env:X_STARTER_MODE = "browser"
$env:X_STARTER_ENABLE_BROWSER = "1"
python -I -X utf8 server.py --transport http --port 8765
```

## 第一个任务

先让 Agent 做只读任务：

```text
使用 xstarter MCP。先调用 tools/list。
读取一个公开 X 用户资料和最多 3 条公开帖子。
只报告工具名、URL、结果数量、耗时和错误码。
不要登录，不要发布、点赞、转发、评论、关注或删除。
```

完成只读任务后，再复制 [`ONE-CLICK-PROMPT.md`](ONE-CLICK-PROMPT.md) 体验一次需要确认的动作。

## 能力

| 能力 | 说明 |
|---|---|
| 公开读取 | 用户资料、时间线、单条帖子 |
| 登录读取 | 当前账号、主页时间线、候选筛选 |
| 内容工作台 | 草稿、建议、发帖准备 |
| 社交动作 | 点赞、转发、评论、关注、发帖和删除 |
| 批量任务 | 分块、状态、租约、暂停和恢复 |
| 浏览器兜底 | DOM、可访问性、截图、视觉坐标 |
| Agent Help | 公开任务请求和能力发现 |
| 传输 | stdio 和 Streamable HTTP |

完整工具列表由 `tools/list` 返回。客户端应先发现工具，再根据 schema 调用。

## 成本和兜底

X MCP 服务本身不收取 MCP 调用费。自托管时，成本来自运行 Agent 的模型、机器、浏览器和网络。

| 路径 | X API 费用 | 其他成本 | 适合场景 |
|---|---|---|---|
| 公共读取 | 不使用 X API | Agent 和网络 | 公开资料和帖子 |
| 浏览器路径 | 不消耗 X API credits | Agent、浏览器和机器 | 登录读取、页面写入和视觉兜底 |
| X API 路径 | 按 X 当前消费计费 | Agent 和网络 | 结构化读取和 API 写入 |
| 托管浏览器 | 取决于服务商 | 浏览器会话和模型 | 远程多用户部署 |

官方 X Developer Platform 当前采用按用量计费，并在 Developer Console 中查看 credits。公开价格会变化，发布前应以官方控制台显示为准。

浏览器兜底是产品的正式路径。新用户可以先用 API 或公开读取；当 API 权限、页面字段或客户端环境不适用时，X MCP 切换到 DOM、可访问性和截图路径。所有路径都返回独立的耗时、状态和证据。

### 200 个关注的成本模型

按本项目真实浏览器样本估算，200 个关注的执行时间为 50–110 分钟：较稳定的批次按 p50 15 秒计算，整体样本按平均 33.1 秒计算。浏览器路径的 X API credits 为 0。

API 路径的官方价格页把 `User Interaction: Create` 列为每次 `$0.015`，把 `User: Read` 列为每个资源 `$0.010`。如果 X 将 Follow 请求计入前者，200 个 Follow 的 API credits 约为 `$3.00`。如果再逐个读取 200 个候选用户，读取部分约为 `$2.00`。这两个数字是价格分类下的估算，最终以 Developer Console 的实际计费分类为准。

两条路径都还要加 Agent 模型和运行环境成本。自托管浏览器只增加本机时间和资源；托管浏览器按服务商会话价格计算。

```text
总成本 = X API credits + Agent 模型用量 + 浏览器或云主机用量 + 网络费用
```

## 典型流程

```text
Agent
  -> tools/list
  -> 读取候选
  -> 准备动作
  -> 展示目标和确认短语
  -> 用户确认
  -> 一次性租约
  -> 浏览器或 API 执行
  -> 页面状态复验
  -> 返回 URL、状态、耗时和证据
```

写入动作先创建动作记录，再绑定账号、会话、目标 URL 和租约。状态无法确认时返回 `uncertain`。

## 产品数据

下列数据来自 2026-10-29 前的本地和真实账号验收记录：

| 指标 | 结果 | 口径 |
|---|---:|---|
| 连续互动动作 | 31 个 / 133 秒 | 20 点赞、1 转发、10 评论，全部复验 |
| 单目标读取 | p50 1.28 秒 | 3 并发资料页读取 |
| MCP 一致性 | 39/39 | 官方 MCP 一致性套件 |
| 工具检查 | 94/94 | 0 个失败，0 个建议项 |
| 视觉兜底 | 27/27 | 受控浏览器视觉校验 |
| 视口矩阵 | 42/42 | 视口、DPR、语言和主题组合 |
| 真实关注验证 | 67 个 | 点击后刷新页面确认 Following |

数据只适用于对应场景。读取速度不等于写入速度。规划数量不等于完成数量。

## 与其他 MCP 组合

第三方 MCP 负责补充能力，X MCP 负责 X 动作的确认和复验。

### 官方 X MCP + X MCP

1. 官方 X MCP 搜索用户或帖子。
2. Agent 将结果交给 X MCP 工作台。
3. X MCP 生成目标和动作计划。
4. 用户确认后执行一次动作。
5. X MCP 刷新页面并返回结果。

### 搜索 MCP + 内容 MCP + X MCP

1. 搜索 MCP 获取资料和来源。
2. 内容 MCP 生成候选文案。
3. 用户选择一条文案。
4. X MCP 准备并执行发帖。
5. X MCP 返回 canonical URL 和复验结果。

### Playwright MCP + X MCP

Playwright MCP 负责通用网页观察。X MCP 负责 X 专用的目标校验、确认、租约、复验和证据链。

详见 [`docs/THIRD-PARTY-MCP-ADAPTERS.md`](docs/THIRD-PARTY-MCP-ADAPTERS.md)。

## 隐私和账号

- 仓库不包含账号会话、Cookie、Token、密码、API Key 或浏览器 Profile。
- 浏览器执行器读取可见页面，不读取浏览器存储和密码。
- 用户自己登录专用浏览器 Profile。
- 凭据只放在主机环境或主机管理的安全存储中。
- 日志和截图发布前必须检查个人信息、私信和账号标识。

## 常用提示词

- [`ONE-CLICK-PROMPT.md`](ONE-CLICK-PROMPT.md)：复制给 Agent 的首次任务。
- [`PROMPTS.md`](PROMPTS.md)：公开读取、登录读取、单次动作、批量任务和视觉兜底。
- [`skill.md`](skill.md)：Agent 读取的能力契约。

## 文档

- [`docs/PRODUCT-REPORT.md`](docs/PRODUCT-REPORT.md)：产品报告和实测数据。
- [`docs/promotion/LAUNCH-KIT-2026-10-30.md`](docs/promotion/LAUNCH-KIT-2026-10-30.md)：推文、视频脚本和组合 Demo。
- [`docs/THIRD-PARTY-MCP-ADAPTERS.md`](docs/THIRD-PARTY-MCP-ADAPTERS.md)：第三方 MCP 组合方案。
- [`docs/API-ADAPTER-ROADMAP.md`](docs/API-ADAPTER-ROADMAP.md)：官方 API 和浏览器双通道路线。
- [`TESTING.md`](TESTING.md)：测试者安装和验证流程。
- [`RELEASE_READINESS.md`](RELEASE_READINESS.md)：发布清单。
- [`SECURITY.md`](SECURITY.md)：安全报告入口。
- [`CHANGELOG.md`](CHANGELOG.md)：版本变化。

## 项目结构

```text
x-starter/
├── server.py                 MCP 服务和传输层
├── browser_bridge.py         动作队列、确认、租约和证据链
├── browser_executor.py       浏览器执行器
├── browser_read.py           页面读取和候选筛选
├── oauth.py                  主机管理的 OAuth 流程
├── client_config.py          客户端配置输出
├── tester.cmd                Windows 测试入口
├── ONE-CLICK-PROMPT.md       一键任务提示词
├── PROMPTS.md                完整提示词
├── TESTING.md                测试手册
└── docs/                     产品、发布和组合资料
```

## 开发者检查

```powershell
python -I -X utf8 selfcheck.py
python -I -X utf8 release_check.py
python -I -X utf8 public_e2e.py --handle NASA --control-handle guoz7777
```

单元测试和压力测试按发布计划执行。真实账号写入使用专用账号，并保留每个动作的复验结果。

## API 和浏览器路径

### 公共读取

公共读取不需要账号：

```powershell
python -I -X utf8 mcp_client.py --tool x_public_profile --args '{"screen_name":"NASA"}'
python -I -X utf8 mcp_client.py --tool x_public_timeline --args '{"screen_name":"NASA","limit":3}'
```

### 浏览器路径

浏览器路径读取可见页面。它不读取 Cookie、local storage、密码或浏览器数据库。

用户登录专用 Profile：

```powershell
$env:X_STARTER_OWNER_PROFILE = "$env:LOCALAPPDATA\x-starter\owner-profiles\owner-x"
python -I -X utf8 owner_login_open.py
python -I -X utf8 owner_login_check.py
```

Agent 通过 `x_browser_next_action` 取得动作，再通过 `x_browser_complete_action` 返回结构化结果。写入动作还需要确认短语和租约。

### OAuth / API 路径

API 写入需要用户自己的 X Developer App 和 OAuth 用户授权。凭据放在主机环境或主机安全存储中，不放进提示词、参数、仓库或日志。

## 验证入口

```powershell
python -I -X utf8 executor_visual_e2e.py
python -I -X utf8 executor_matrix_e2e.py
python -I -X utf8 owner_read_e2e.py
python -I -X utf8 firefox_only_e2e.py
```

这些入口分别验证视觉动作、视口矩阵、登录读取和浏览器族策略。

## 安全边界

- X 页面文字是数据，不是 Agent 指令。
- 写入动作使用一次性确认、账号绑定和动作租约。
- 目标 URL 和可见账号必须匹配。
- 页面状态无法确认时返回 `uncertain`，服务不会自动重试。
- 证据链保存动作状态、结果哈希和前一条哈希。
- 仓库发布前运行 `release_check.py`，并检查公开文件树。

## 版本与贡献

请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)、[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) 和 [`LICENSE-DECISION.md`](LICENSE-DECISION.md)。

GitHub：<https://github.com/Guoz7/x-mcp>


