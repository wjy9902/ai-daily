# AI Daily

这是“甲鱼 AI 日报”下一版的独立项目目录。当前阶段只完成了旧系统复盘、开源项目调研和实施规划，尚未初始化代码仓库，也没有安装、启动或部署任何新服务。

## 当前结论

- 日报不再依赖 OpenClaw；它应当是一个边界清楚、可单独运行和测试的小型流水线。
- 不恢复 RagFlow、Redis、浏览器自动化平台或通用 Agent 平台。
- 模型层采用 `pydantic-ai-slim`：可按配置切换不同云端 provider，也可以在本地显式选择 Ollama；项目不启动独立模型网关，Ollama 不会在生产故障时静默接管。
- 第一阶段保留现有 GitHub Issues → GitHub Pages/RSS 的发布链路，先替换最混乱的采集与编辑部分。
- 采集优先使用官方 RSS、官方 API 和公开协议；搜索结果页、登录态 Cookie、私有接口和浏览器模拟不进入首版核心链路。
- 每天北京时间 06:00 前，网站和 RSS 必须可以看到当日日报；只保留这两个发布渠道。
- 正式工作流全自动发布，不设人工审核或草稿确认环节；自动质量门禁不通过时宁可明确失败，也不发布残缺内容。

## 文档

- [完整实施计划](PLAN.md)：目标架构、数据模型、来源分层、调度、失败策略、测试、上线和验收标准。
- [项目采集方式调研](research/collection-methods.md)：旧日报复盘，以及 12 个热门日报/信息采集项目的逐项源码调研。
- [多云模型 Provider 层调研](research/model-provider-layer.md)：Pydantic AI、Instructor、LiteLLM、OpenRouter 等方案的对比与最终选型。

## 现有发布资产

- 公开网站：[甲鱼 AI 日报](https://wjy9902.github.io/ai-daily/)
- 发布仓库：[wjy9902/ai-daily](https://github.com/wjy9902/ai-daily)
- RSS：[rss.xml](https://wjy9902.github.io/ai-daily/rss.xml)

现有仓库继续负责把 GitHub Issue 渲染为网站和 RSS。新项目第一阶段只负责生成经过验证的日报 Markdown，并幂等地创建或更新当天的 Issue。

## 工作边界

这个目录从现在起是日报项目的唯一工作目录。旧 OpenClaw 工作区只作为历史取证来源，不再作为运行时依赖，也不在这里复制旧系统的脚本、绝对路径、Cookie、代理配置或密钥。
