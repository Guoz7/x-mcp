# X MCP 推广动画与文章交付说明 - 2026-10-27

## 产物

- `x-starter/docs/promotion/x-mcp-200-follow.mp4`
- `x-starter/docs/promotion/x-mcp-200-follow-story.json`
- `x-starter/docs/promotion/x-mcp-200-follow-deck.html`
- `x-starter/docs/promotion/x-mcp-200-follow.srt`
- `x-starter/docs/promotion/x-mcp-200-follow-narration.md`
- `x-starter/docs/promotion/x-mcp-200-follow-article.md`

## 视频验证

- Windows SAPI voice: `Microsoft Huihui Desktop`
- 6 页
- 1920x1080
- H.264 + AAC
- 30 fps
- 实际时长约 194.96 s
- 动画源会展示“10 分钟关注 200 人？”作为问题，不展示为已完成事实

## 数据口径

- follow click cycle: p50 1.16 s, n=5
- historical independent server confirmation: 8/18, 44.4%
- local HTTP mock: 994.06 RPS at 512 clients, 0 errors
- local HTTP extreme: 533.08 RPS at 2048 clients, 0 errors
- public X profile cold read: 1.31-1.59 s
- rapid public timeline pressure: HTTP 429
- large batch contract: 200 likes, 10 reposts, 200 replies, chunked
- actual 200-follow write: not completed

## 不能使用的标题

- “10 分钟保证关注 200 人”
- “200 个关注全部成功”
- “500 条评论每天稳定完成”

## 推荐标题

> 10 分钟关注 200 人？我们用真实 X 数据拆解 Agent 自动社交的速度与成功率

## 推荐说明

> 小样本里，关注点击周期 p50 约 1.16 秒；但历史 18 次连续关注的服务端有效率只有 44.4%。所以 X MCP 不把按钮变色当成功，而是把动作拆成有确认、有租约、有后置验证的工作单元。真实 200 关注仍在工程验收中。

## 生成命令

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File x-mcp-200-follow-tts.ps1
node x-mcp-200-follow-render.mjs
```

Edge TTS 在本机返回 403，因此视频使用 Windows SAPI 作为本地无 Key 保底。
