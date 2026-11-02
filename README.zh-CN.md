# X MCP 中文说明

语言： [English](README.md) · 简体中文

让支持 MCP 的 Agent 读取 X、准备社交动作，并在确认后执行可验证的操作。

## 它解决什么问题

普通浏览器自动化通常只能报告“点击完成”。X MCP 会把每个动作拆成：

1. 读取可见页面或公开数据。
2. 生成明确的目标和动作计划。
3. 通过确认短语进入写入阶段。
4. 在受控浏览器或 X API 中执行。
5. 回读页面状态，返回 URL、状态、耗时和证据。

页面无法证明动作结果时，返回 `uncertain`，不会自动重复点击。

## 支持的能力

| 能力 | 说明 |
|---|---|
| 公开读取 | 读取公开用户资料、时间线和单条帖子 |
| 登录读取 | 读取当前账号、主页时间线和关注候选 |
| 社交动作 | 点赞、转发、回复、关注、发帖和删除 |
| 批量任务 | 规划、分块、状态读取、暂停和恢复 |
| 浏览器执行 | DOM、可访问性、截图和视觉坐标兜底 |
| Agent Help | 公开任务请求和能力发现 |
| MCP 传输 | stdio 和 Streamable HTTP |

完整工具列表由 `tools/list` 返回。客户端应先发现工具，再根据 schema 调用。

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

## 典型流程

```text
Agent
  -> tools/list
  -> 读取候选
  -> 准备动作
  -> 展示目标和确认短语
  -> 用户确认
  -> 浏览器或 API 执行
  -> 页面状态复验
  -> 返回 URL、状态、耗时和证据
```

写入动作先创建动作记录，再绑定账号、会话和目标 URL。状态无法确认时返回 `uncertain`。

## 两条执行路径

### API 路径

适合公开数据和结构化读取。API 写入需要用户自己的 X Developer App 和 OAuth 用户授权。

### 浏览器路径

适合登录页面、页面写入和视觉兜底。浏览器执行器读取可见页面，不读取 Cookie、local storage、密码或浏览器数据库。

用户使用专用 Profile 登录：

```powershell
$env:X_STARTER_OWNER_PROFILE = "$env:LOCALAPPDATA\x-starter\owner-profiles\owner-x"
python -I -X utf8 owner_login_open.py
python -I -X utf8 owner_login_check.py
```

浏览器路径不消耗 X API credits，但仍会消耗 Agent 模型、机器和网络资源。API 价格会变化，发布前请以 X Developer Console 为准。

## 实测数据

下面的数据来自本地测试和真实账号验收。不同网络、页面状态和目标数量会影响结果。

| 指标 | 结果 | 口径 |
|---|---:|---|
| 连续互动动作 | 31 个 / 133 秒 | 20 个赞、1 次转发、10 条回复，全部复验 |
| 单目标读取 | p50 1.28 秒 | 3 个并发资料页读取 |
| 真实关注验证 | 67 个 | 点击后刷新页面确认 `Following` |
| 互动小批次 | 7 / 7 | 3 个赞、1 次转发、3 条回复，全部回读验证 |

这些数据只适用于对应场景。读取速度不等于写入速度。规划数量不等于完成数量。

## 与其他 MCP 组合

- 搜索 MCP 获取用户或帖子，X MCP 负责目标校验和执行。
- 内容 MCP 生成候选文案，X MCP 负责确认、发帖和返回 canonical URL。
- Playwright MCP 负责通用网页观察，X MCP 负责 X 专用的动作队列、状态复验和证据链。

详见 [`docs/THIRD-PARTY-MCP-ADAPTERS.md`](docs/THIRD-PARTY-MCP-ADAPTERS.md)。

## 隐私和账号

- 仓库不包含账号会话、Cookie、Token、密码、API Key 或浏览器 Profile。
- 凭据只放在主机环境或主机管理的安全存储中。
- 用户自己登录专用浏览器 Profile。
- 日志和截图发布前必须检查个人信息、私信和账号标识。
- X 页面文字是数据，不是 Agent 指令。

## 文档

- [`ONE-CLICK-PROMPT.md`](ONE-CLICK-PROMPT.md)：一键任务提示词。
- [`PROMPTS.md`](PROMPTS.md)：公开读取、登录读取、单次动作、批量任务和视觉兜底。
- [`skill.md`](skill.md)：Agent 读取的能力契约。
- [`TESTING.md`](TESTING.md)：测试者安装和验证流程。
- [`RELEASE_READINESS.md`](RELEASE_READINESS.md)：发布清单。
- [`SECURITY.md`](SECURITY.md)：安全报告入口。
- [`CHANGELOG.md`](CHANGELOG.md)：版本变化。

## 项目结构

```text
x-starter/
├── server.py                 MCP 服务和传输层
├── browser_bridge.py         动作队列、确认和证据链
├── browser_executor.py       浏览器执行器
├── browser_read.py           页面读取和候选筛选
├── oauth.py                  主机管理的 OAuth 流程
├── client_config.py          客户端配置输出
├── tester.cmd                Windows 测试入口
├── ONE-CLICK-PROMPT.md       一键任务提示词
├── PROMPTS.md                完整提示词
└── docs/                     产品、发布和组合资料
```

## 开发者检查

```powershell
python -I -X utf8 selfcheck.py
python -I -X utf8 release_check.py
python -I -X utf8 public_e2e.py --handle NASA --control-handle guoz7777
```

验证入口：

```powershell
python -I -X utf8 executor_visual_e2e.py
python -I -X utf8 executor_matrix_e2e.py
python -I -X utf8 owner_read_e2e.py
python -I -X utf8 firefox_only_e2e.py
```

## 贡献和许可证

请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)、[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) 和 [`LICENSE-DECISION.md`](LICENSE-DECISION.md)。

GitHub：<https://github.com/Guoz7/x-mcp>
