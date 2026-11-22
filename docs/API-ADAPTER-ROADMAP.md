# X API 适配路线

## 结论

自研 MCP 目前没有覆盖官方 X MCP 的全部能力。推荐采用双通道架构：

- API 适配器负责结构化读取和接口级能力。
- 浏览器适配器负责登录页面、视觉兜底和页面复验。
- X MCP 核心负责统一任务、确认、租约、状态和证据。

## 当前覆盖矩阵

| 能力 | 自研 MCP | 官方 X MCP / API | 下一步 |
|---|---|---|---|
| 公开用户和帖子读取 | 已有 | 已有 | 保持统一结果格式 |
| 登录账号和主页时间线 | 浏览器已覆盖 | API 可适配 | 增加 API adapter |
| 搜索用户和帖子 | 部分 | 官方能力较完整 | 接入 API 搜索 |
| 发帖、回复、转发、点赞 | 浏览器确认路径已覆盖部分动作 | API 可适配 | 共用确认和复验层 |
| 关注和取关 | 浏览器批次已实现 | API 端点待按当前权限核验 | 先做单动作 adapter |
| 书签和书签文件夹 | 未接入 | 官方 X MCP 已声明 | 加只读和写入 adapter |
| Likers、reposters、quoters | 未接入 | 官方 X MCP 已声明 | 加公开读取 adapter |
| 趋势和新闻 | 未接入 | 官方 X MCP 已声明 | 加只读 adapter |
| Articles | 未接入 | 官方 X MCP 已声明 | 加草稿和发布 adapter |
| 私信 | 未接入 | API 端点存在 | 单独权限和确认模型 |
| 视觉兜底 | 已有 | 通用浏览器 MCP 可补充 | 保持 X 专用安全闸门 |

## 适配器接口

所有 API 和浏览器实现都进入同一个路由层。上层 Agent 不直接选择底层实现。

```text
Agent
  -> X MCP tool contract
  -> capability router
      -> X API adapter
      -> browser adapter
      -> third-party browser adapter
  -> confirmation / lease / evidence
  -> normalized result
```

每个 adapter 返回：

```json
{
  "capability": "user_follow",
  "route": "x_api|browser|third_party_browser",
  "status": "completed|pending|uncertain|failed",
  "target": "canonical URL or user id",
  "elapsed_ms": 0,
  "request_count": 0,
  "evidence_ref": "local evidence id"
}
```

## 路由规则

1. 先调用 `tools/list` 和 capability discovery。
2. 只读结构化数据优先使用 X API adapter。
3. API 缺少权限、字段或当前客户端不能使用时，切换到 browser adapter。
4. DOM 或可访问性定位失败时，使用截图视觉兜底。
5. 写入动作在所有路径上共用确认短语、账号绑定、动作租约和复验。
6. 底层路径切换后，结果必须标记 `route`，不能混淆性能数据。
7. 状态不确定时停止，不自动换路重试写入。

## 为什么不能直接把官方能力全部复制进来

官方 X MCP 是 X API 的 MCP 包装。它覆盖 API endpoint，但不等于浏览器页面操作、视觉定位、截图证据和本地账号 Profile 管理。

自研 MCP 的核心优势是任务工作流和验证层。把官方 API 全部手工重写一遍会增加维护成本，也会让 API 变更与浏览器变更同时落在我们身上。

推荐复用官方 API 或官方 X MCP 做数据面，再由自研 MCP 统一 Agent 体验和写入闸门。

## 第一个适配批次

按收益和风险排序：

1. `user_search`：补足候选发现。
2. `post_search`：补足主题和互粉候选来源。
3. `user_lookup`：把 handle 解析为稳定 user id。
4. `bookmark_list`：补足内容工作台输入。
5. `user_follow`：先做单动作 API adapter，再接批量。
6. `trends` 和 `news`：用于发布前研究 Demo。

## 验收口径

API adapter 和 browser adapter 分开测：

- 能力发现是否准确。
- 结果格式是否一致。
- 单动作耗时和请求数。
- 认证失败、限流和权限不足的分类。
- 写入后的服务端或页面复验。
- route、evidence_ref 和 request_count 是否完整。

API 读取的速度不能作为浏览器写入速度。两条路径必须分别报告。

## 参考

- 官方 X MCP 文档：<https://github.com/xdevplatform/docs/blob/main/tools/mcp.mdx>
- 官方 X MCP 仓库：<https://github.com/xdevplatform/xmcp>
- X Developer Platform：<https://developer.x.com/>
- Microsoft Playwright MCP：<https://github.com/microsoft/playwright-mcp>
