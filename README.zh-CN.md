# X MCP 中文文档

语言：[English](README.md) · 简体中文

让支持 MCP 的 Agent 以可控、可确认、可复验的方式读取 X、协同运维个人账号，并通过轻量控制台实时观察状态。

- **Agent 主导配置与执行**：Agent 通过 `tools/list` 动态发现全部 **65** 个去重后的工具，自主完成人设同步、目标发现、文案入队与波次编排；人类主人只需做轻量确认。
- **Obsidian-Zinc 轻量控制台**：本地 Web 面板（`ui/dashboard.html`）集中展示运行状态、待确认托管窗口、今日各动作安全配额、人设支柱与执行流水。
- **双通道与强审计**：每次外部写入均经过服务端确认短语、单次动作租约、目标/账号一致性校验、刷新后状态复验（状态不明标记 `uncertain`，绝不盲目重试）以及哈希链证据账本（`x_evidence_get`）。浏览器路径不读取 Cookie、localStorage 或密码。

---

## 核心能力与架构

1. **免登录公开读取**：`x_public_profile`、`x_public_timeline`、`x_public_post`，无需登录或 X API Key。
2. **专属浏览器执行路径**：通过持久化 Chrome/Chromium Profile 执行页面读取与写入（DOM / 可访问性定位优先，失效时启用受控截图坐标兜底），覆盖发帖、删帖、点赞、转发、回复与关注校验。
3. **Host 托管 OAuth / X API 路径**：支持 `x_oauth_start` / `x_oauth_status` / `x_post_create_api`，凭据由宿主环境托管，不进入 MCP 工具参数与仓库。
4. **个人账号自动化运维与安全风控**：
   - **健康度与养号阶段（`x_account_health`）**：按 `ghost -> light_engage -> content_build -> growth` 阶段动态计算每日安全配额与熔断状态。
   - **人设记忆与主人信息同步（`x_persona_memory`）**：内置可配置人设模板（含 `tech_casual` 开发者日常闲聊预设），支持 `sync_owner` 节流同步主页简介与近期帖子（默认浏览器检查间隔 `>= 6h` 保护，且保留人工微调字段）。
   - **目标发现与预采集池（`x_search_posts` / `x_visit_profile` / `x_notifications_read` / `x_target_pool`）**：先看对方主页近况与通知提及再互动，或由探路号预采集候选放入 `x_target_pool`，降低主号搜索频次。
   - **多策略互动编排、粉丝增长趋势与风控守护（`x_content_queue` / `x_daily_orchestrate_*` / `x_autopilot`）**：支持 `auto_fastest`（智能择优）、`reciprocal_first`（互关起号）、`niche_sniper_bundle`（精准三连）、`thread_reply_magnet`（干货抢评引流）、`reply_then_follow`（高粘深聊）与 `balanced`（稳健均衡）6 套编排策略，并支持热切换 `set_strategy`；浏览器档案读取与定时保活自动采集成长快照（`followers_count` / `following_count`）及互关入站时间戳，在控制台与 `ops_summary` 中呈现 **7 日粉丝增长趋势、策略赛马对比与粘性留存榜（`follower_trend` / `strategy_performance` / `fastest_strategy`）**。默认日配额偏向高点赞与深回复（点赞默认 `120/天`、上限 `150/天`，回复默认 `45/天`、上限 `50/天`），转发收紧为 `<=4/天` 且经 `score_technical_quality` 只转精品技术内容；`clean_post_body` 剥离页面 Noise 并严格过滤未识别短碎片，配合同端口 `Origin`、`X-Frame-Options: DENY` 与 `2h` 冷却熔断遵循 [`docs/orchestration-profile-safe.json`](docs/orchestration-profile-safe.json)。

---

## 快速开始

环境要求：**Python 3.11+**（推荐 `3.12`），浏览器路径需要 **Google Chrome**；可选 **Node.js 20+** 用于官方 MCP 一致性检查。

### 1. 一键启动浏览器路径与轻量控制台（Windows）

```powershell
dashboard.cmd
```

脚本会在独立 Profile（`.browser-main-profile`）与浏览器模式下启动 `http://127.0.0.1:8765/mcp`，并自动在浏览器打开 `http://127.0.0.1:8765/` 控制台。

也可以通过环境变量手动启动源码服务：

```powershell
$env:X_STARTER_MODE = "browser"
$env:X_STARTER_ENABLE_BROWSER = "1"
python -I -X utf8 server.py --transport http --port 8765
```

首次使用自有账号时，先以普通 Chrome 打开专用 Profile 手动登录，关闭窗口后再交由 MCP 接管：

```powershell
$env:X_STARTER_OWNER_PROFILE = "$env:LOCALAPPDATA\x-starter\owner-profiles\owner-x"
python -I -X utf8 scripts/owner_login_open.py
python -I -X utf8 scripts/owner_login_check.py
```

### 2. 测试包安装与客户端配置（Windows）

```powershell
tester.cmd setup
tester.cmd smoke
tester.cmd doctor
tester.cmd login
tester.cmd check-login
tester.cmd config
```

把生成的 `mcp-client.json` 或 `mcp-codex.toml` 条目合并到 MCP 客户端配置中，然后把 [`skill.md`](skill.md) 与 [`docs/ONE-CLICK-PROMPT.md`](docs/ONE-CLICK-PROMPT.md) 交给 Agent。

### 3. 本地离线 Demo 与 Mock 模式

```powershell
python -I -X utf8 server.py --demo
pwsh -NoProfile -File .\scripts\run_mock.ps1
```

Mock 模式使用内置合成账号 `mock_creator`（返回 `meta.simulated: true`），不会发起任何真实 X 请求。

---

## Agent 协作工作流

遵循 [`skill.md`](skill.md) 的核心分工：**Agent 负责配置、规划与执行，人类只需确认与复核**。

| 模块 | 代表工具 | 职责说明 |
|---|---|---|
| 公开只读 | `x_public_profile`, `x_public_timeline`, `x_public_post` | 免登录读取公开用户、时间线和单帖 |
| 登录读取与探索 | `x_owner_read`, `x_account_get`, `x_timeline_observe`, `x_search_posts`, `x_visit_profile`, `x_notifications_read` | 主人时间线、利基/跨语言搜索、先访问主页再互动、通知/提及监控 |
| 账号健康与人设记忆 | `x_account_health`, `x_persona_memory`, `x_target_pool`, `x_content_queue` | 养号阶段与安全预算、人设支柱与 `tech_casual` 预设、`sync_owner` 同步、预采集池、内容队列 |
| 每日编排与有界托管 | `x_daily_orchestrate_plan`, `x_daily_orchestrate_prepare`, `x_daily_orchestrate_execute`, `x_autopilot` | 权重规划、7 天托管制备/激活、后台调度器、状态与 CSV/JSON 报告导出 |
| 单动作与批量执行 | `x_post_prepare`, `x_post_publish`, `x_post_delete`, `x_interaction_batch_*`, `x_follow_candidates_read`, `x_follow_batch_*` | 服务端确认短语、分块租约、刷新核验（`Following` / `unlike` / `unretweet`） |
| 浏览器与外部执行桥 | `browser_open`, `browser_observe`, `browser_screenshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_close`, `x_browser_next_action`, `x_browser_complete_action`, `x_browser_action_get`, `x_evidence_get` | DOM 优先、受控截图坐标兜底、外部 Computer Use 领取契约与哈希链证据 |
| OAuth / API | `x_oauth_start`, `x_oauth_status`, `x_oauth_disconnect`, `x_post_prepare_api`, `x_post_create_api` | 宿主托管 OAuth 2.0 PKCE 授权与 API 发帖 |
| 网关与 Agent Help | `connector_list`, `connector_negotiate`, `connector_plan`, `x_compose_workbench`, `x_compose_check`, `x_interaction_link`, `agent_help_request_*`, `ahan_handoff` | 能力协商、MCP Apps 创作工作台、Web Intent 与 AHAN 边界请求 |

---

## 第一条验证任务

建议先让 Agent 执行只读任务：

```text
使用 xstarter MCP。先调用 tools/list。
调用 x_public_profile 读取 screen_name="NASA"，并调用 x_public_timeline 读取最多 3 条公开帖子。
只报告工具名、URL、结果数量、耗时和错误码。不要登录或执行写入。
```

接入登录 Profile 后，可直接复用 [`docs/ONE-CLICK-PROMPT.md`](docs/ONE-CLICK-PROMPT.md) 中的日常托管模板，或查阅 [`docs/PROMPTS.md`](docs/PROMPTS.md)。

---

## 本地自检、实测口径与安全边界

请严格区分本地离线测试、协议压测与真实 X 账号有限验收，不将测试数字外推为托管 SaaS SLA。

```powershell
python -I -X utf8 scripts/selfcheck.py
python -I -X utf8 scripts/release_check.py
python -m unittest discover -s tests -p "test_*.py"
```

历史已记录验证快照（详见证据文档）：
- MCP 协议一致性：`39/39`（`2026-10-22`）；`checkmcp` 合规扫描：`94/94`。
- 本地 HTTP Mock 压测：`512` 并发 `994.06 RPS` / `2048` 并发 `533.08 RPS`、`0` 错误（纯本地 Mock，无 X 网络开销）。
- External Agent 受控验收：`31` 个互动动作 / `133 秒`（`20` 赞、`1` 转、`10` 评）；External Agent 分窗累计 `202` 个唯一已验证关注（其余 `100` 条 `uncertain` 按规则中止不重试）。
- 来自 [`2026-11-14` 封号复盘](docs/reports/POSTMORTEM-SUSPENSION-2026-11-14.md) 的硬约束：禁止机器级高频短间隔连刷；遇到 `283`、`161`、`141`、`37` 等拒绝信号或安全拦截页立即全线停止并冷却。
- 自托管浏览器路径不消耗 X API credits，但仍消耗模型、本机算力与网络资源。未选中开源许可证前，请勿公开分发为托管产品（见 [`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md)）。

---

## 目录结构

```text
x-starter/
├── server.py                     MCP 服务主入口（stdio + Streamable HTTP）与控制台宿主
├── dashboard.cmd / tester.cmd    Windows 一键控制台与测试包入口
├── skill.md / public_skill.md    Agent 操作契约与公开连接器说明
├── connector-manifest.json       连接器能力清单与契约定义
├── requirements-browser.txt      浏览器执行器依赖声明
├── src/                          核心运行时模块
│   ├── account_orchestrator.py   账号健康、养号阶段、人设记忆、候选池、内容队列与每日编排
│   ├── autopilot_ops.py          托管后台调度器、主人同步、微波次执行与报告导出
│   ├── daily_action_budget.py    分动作每日安全配额与速率闸门
│   ├── browser_bridge.py         确认短语、单次租约、状态账本与哈希链证据
│   ├── browser_executor.py       Chromium DOM/可访问性/视觉兜底执行器
│   ├── browser_read.py           资料、时间线、搜索、通知与互粉候选读取
│   ├── follow_batch.py / follow_orchestration.py / interaction_batch.py
│   ├── oauth.py / x_api.py / x_public.py / write_store.py / ledger_lock.py
│   └── agent_help_board.py / connector_runtime.py / composer.py / client_config.py
├── ui/
│   ├── dashboard.html            Obsidian-Zinc 轻量观察与确认控制台
│   └── composer.html             MCP Apps 交互式创作工作台
├── scripts/                      环境自检、扫码登录、发布校验、压测与测试包构建脚本
├── e2e/                          端到端验收、视觉/矩阵夹具与协议测试脚本
├── tools/                        安全节奏校验、窗口许可、过期租约清理与审计工具
├── examples/                     Codex、Claude Desktop、OpenClaw 与 HTTP 调用示例
├── tests/                        离线单测与契约测试集
└── docs/                         指南、提示词、发布边界、实测报告与宣传物料
    ├── ONE-CLICK-PROMPT.md       日常托管一键提示词
    ├── PROMPTS.md                提示词与测试模板
    ├── TESTING.md                Windows 测试包使用说明
    ├── RELEASE_READINESS.md      发布就绪检查与阻塞项
    ├── PERFORMANCE.md            性能与成本口径
    ├── PRODUCT-REPORT.md         产品说明
    ├── API-ADAPTER-ROADMAP.md    API 与浏览器双通道路线
    ├── orchestration-profile-safe.json
    ├── reports/                  2026 年 11 月历史实验、审计与封号复盘报告
    └── promotion/                演示脚本与宣传材料
```

---

## 贡献与许可证

- 贡献流程与安全反馈：[`CONTRIBUTING.md`](CONTRIBUTING.md)、[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)、[`SECURITY.md`](SECURITY.md)。
- 许可证状态：尚未选定开源协议，详见 [`docs/LICENSE-DECISION.md`](docs/LICENSE-DECISION.md)。
- GitHub 仓库：<https://github.com/Guoz7/x-mcp>
