# X MCP 一键任务提示词

把下面整段复制给支持 MCP 的 Agent。它会先自检，再执行一个可控的 X 任务。

```text
你正在使用 xstarter MCP。请严格按以下流程执行，不要猜测结果。

目标：读取我的 X 时间线，找出 1 条公开帖子，并在我确认后完成 1 次点赞；随后验证结果。

规则：
1. 先调用 tools/list，确认已连接 xstarter MCP。
2. 先调用 x_owner_read 或 x_timeline_observe，只读读取，不执行写入。
3. 选择 1 条 canonical post URL，向我展示作者、URL、动作和预计耗时。
4. 在我明确回复“确认执行”前，不得点赞、转发、评论、关注、发帖或删除。
5. 确认后，只执行这 1 个点赞动作。使用 MCP 的准备、确认、租约和复验流程。
6. 优先使用 DOM 或可访问性定位；只有定位失败时才使用截图视觉兜底。
7. 每次写操作都要重新读取页面状态。状态无法确认时报告 uncertain，停止，不自动重试。
8. 返回：目标 URL、action_id、执行耗时、验证方式、最终状态和 MCP 错误码。
9. 不读取 Cookie、密码、Token、本地浏览器数据库、私信或无关文件。
10. 不把计划数量、按钮瞬时变化或模拟结果当成成功。
```

## 批量任务模板

把目标和数量替换后再使用。先规划，确认后分块执行。

```text
使用 xstarter MCP 执行一个 X 批量任务。

任务：从已验证的公开候选中筛选目标，准备 {数量} 个 {动作}。
执行上限：{数量}。
分块大小：10。
失败策略：stop。

流程：
1. tools/list。
2. 读取并保存候选快照，去重并报告目标 URL。
3. 调用对应的 prepare 工具，展示 batch_id、确认短语、分块数和预计耗时。
4. 等待我回复“确认执行当前批次”。
5. 每次只执行一个分块，并在分块后调用 status。
6. 每个动作独立验证。遇到 uncertain、pending、rate_limited 或页面异常立即停止。
7. 最终分别报告 planned、completed、verified、uncertain、pending、skipped、elapsed。

禁止：自动扩大目标数量、自动重试不确定动作、读取凭据、使用通用点击绕过确认、把模拟结果写成真实结果。
```

## 安装

- GitHub：<https://github.com/Guoz7/x-mcp>
- Windows：下载仓库 ZIP，运行 `tester.cmd setup`，再运行 `tester.cmd smoke`。
- 生成配置：运行 `tester.cmd config`，把输出加入你的 MCP 客户端。
- 首次真实任务：先使用上面的单次点赞提示词。
