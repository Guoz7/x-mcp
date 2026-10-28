# 第三方 MCP 组合方案

这份文档说明哪些第三方 MCP 适合接入产品 Demo，以及它们和 X MCP 的职责边界。

## 结论

第三方 MCP 应该作为输入和执行器插件接入。X MCP 保留 X 任务的规划、确认、租约、复验和证据链。

这样做有两个好处：

- Demo 能展示多个 MCP 协作完成一个真实工作流。
- X 的写入结果仍由 X MCP 自己验证，不把外部工具的返回值当成成功。

## 推荐组合

| MCP | 适合的职责 | 接入方式 | 发布 Demo |
|---|---|---|---|
| 官方 X MCP / `xmcp` | X API 搜索、用户、帖子和书签 | 作为 API 读取适配器 | 搜索结果进入 X MCP 工作台 |
| Microsoft Playwright MCP | 通用浏览器观察和 DOM 操作 | 作为浏览器能力参考或替换执行器 | 页面结构变化时的浏览器兜底 |
| 内容 MCP | 读取选题、生成候选文案 | stdio 或 Streamable HTTP | 内容工作台 |
| 文件或表格 MCP | 保存候选、结果和复盘 | 本地路径或受控远程端点 | 发布后复盘 |
| 搜索 MCP | 主题研究和资料来源 | 只读搜索连接 | 研究到发布 |

## 官方 X MCP 的定位

官方 X MCP 面向 X API。官方文档列出的能力包括搜索帖子、查找用户、读取时间线、书签、趋势、新闻和 Articles。它适合快速读取和 API 级数据访问。

我们的 X MCP 适合 Agent 需要浏览器状态、确认写入、截图兜底和页面复验的场景。两者可以并行：官方 X MCP 提供数据，我们的 X MCP 负责工作台和可复验动作。

不要在 Demo 中把两个服务的结果混成一个服务的成绩。画面上标出每个 MCP 的名称和调用阶段。

## Microsoft Playwright MCP 的定位

Microsoft Playwright MCP 使用可访问性快照和结构化浏览器工具。它适合通用网页探索和 DOM 优先操作。

我们的浏览器执行器额外负责 X 专用的动作确认、单次租约、目标账号校验、状态复验和证据记录。Playwright MCP 可以作为通用浏览器层，也可以作为对照组，但 X 写入仍要经过 X MCP 的动作门。

## Demo A：官方 X MCP + 我们的 X MCP

```text
1. 官方 X MCP 搜索主题、用户或帖子。
2. Agent 把搜索结果整理成候选表。
3. 我们的 X MCP 读取目标页面并生成动作计划。
4. Agent 展示目标 URL、动作类型、文本和确认短语。
5. 用户确认后，我们的 X MCP 执行一次动作。
6. 我们的 X MCP 刷新页面并返回结果和证据哈希。
```

这条链路适合发布文章和视频，因为每个 MCP 的价值都能在画面中看见。

## Demo B：搜索 MCP + 内容 MCP + 我们的 X MCP

```text
1. 搜索 MCP 找到主题资料和来源。
2. 内容 MCP 生成三条候选文案。
3. 用户选择一条文案。
4. 我们的 X MCP 准备发帖动作。
5. 用户确认后发布。
6. X MCP 返回 canonical URL 和复验结果。
```

## Demo C：Playwright MCP + 我们的 X MCP

```text
1. Playwright MCP 打开一个公开页面并返回结构化观察。
2. Agent 判断页面是否适合进入 X 工作流。
3. X MCP 读取 X 目标并生成动作计划。
4. DOM 定位失败时，X MCP 使用自己的截图兜底路径。
5. X MCP 完成动作并复验。
```

## 通用配置示例

下面是结构示例。密钥必须放在运行环境变量中，不能写入仓库或提示词。

```json
{
  "mcpServers": {
    "xstarter": {
      "command": "python",
      "args": ["server.py", "--transport", "stdio"],
      "env": {
        "X_STARTER_MODE": "browser",
        "X_STARTER_ENABLE_BROWSER": "1"
      }
    },
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
    }
  }
}
```

## Agent 编排提示词

```text
你正在使用 xstarter 和其他 MCP。

先调用 tools/list，并给每个工具标注所属 MCP。搜索 MCP 或官方 X MCP 只负责读取数据。内容 MCP 只负责生成候选文案。xstarter 负责 X 任务计划、确认、租约、执行、页面复验和证据。

在展示目标 URL、完整文案、动作类型、confirmation phrase 和预计耗时前，不执行 X 写入。用户确认后只执行一个动作。优先 DOM 或可访问性定位，失败时使用截图兜底。状态无法证明时报告 uncertain 并停止，不自动重试。最终按 MCP 分别报告工具名、耗时、结果、canonical URL 和证据哈希。不要读取 Cookie、密码、Token、私信或无关文件。
```

## 资料来源

- 官方 X MCP 文档：<https://github.com/xdevplatform/docs/blob/main/tools/mcp.mdx>
- 官方 X MCP 仓库：<https://github.com/xdevplatform/xmcp>
- Microsoft Playwright MCP：<https://github.com/microsoft/playwright-mcp>
- MCP Registry 文档：<https://registry.modelcontextprotocol.io/docs>
