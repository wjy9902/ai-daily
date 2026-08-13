# AI Daily 运维手册

## 安全前提

本项目只读取 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`DEEPSEEK_API_KEY`、`GITHUB_TOKEN`。密钥只能进入本机环境变量或 GitHub Actions Secrets，禁止写入 `.env.example`、YAML、测试 fixture、日志和 artifact。

生产密钥配置为 GitHub Actions Secrets，不进入仓库、Issue、日志或 artifact。若密钥曾进入提交、构建日志或公开页面，立即在供应商控制台轮换。百炼正式环境使用华北 2 业务空间专属兼容地址。

## 来源与选题门禁

- 来源分为官方、新闻、社区、研究和版本发布；官方与高质量媒体承担主要召回，研究和版本发布只作有限补充。
- 只有来源首次发布时间可验证、且落在 36 小时窗口内的内容才能进入候选；更新时间和发现时间都不能替代新闻发布时间。显式配置的官方版本/模型仓库 change-watch 可以把提交或仓库变更时间作为“更新事件”的时间，但标题和正文不得把它改写成首次发布。社区提交时间永远不能充当新闻时间，只能在精确 URL 已由另一条合格来源验证时作为热度旁证。订阅源缺失发布时间时仅回源读取文章元数据，仍无法确认就淘汰并记录在 `freshness.json`。
- 混合主题订阅源先做确定性 AI 相关性过滤，再进入聚类和模型判断。
- 同一事件在 48 小时内跨来源聚类，并按 URL 与标题对历史 45 天去重；同一安全公告的多个维护分支合并为一条。
- 最终选题由一次全局编辑调用在最多 80 个候选中完成，不比较不同批次的模型分数。
- 初筛后的高相关候选优先使用 feed/API 自带全文；内容不足时只回源抓取与条目自身来源配置同源的正文。全局编辑纠正初筛并提升为重点或关注的候选会在成稿前补抓一次。抓取结果记录在 `article-enrichment.json`，重点与关注稿至少要有一份 800 字符以上的正文证据，并记录在 `evidence-quality.json`，否则整期停止发布。
- 每期必须有 4–5 条今日重点、5–7 条值得关注、8–12 条快讯；详细报道最多 2 条研究。同一来源通常不超过 2 条，但互相独立的重大新闻可以例外，不能为了来源均衡漏掉当天的重要变化。
- 模型只能引用当次证据包中的 ID 和 URL；来源健康低于门槛、证据不完整或结构校验失败时不发布。

## 本地验证

```bash
uv sync --all-groups
uv run ruff check .
uv run mypy src
uv run pyright
uv run pytest
uv run ai-daily run --date 2026-08-12 --mode dry-run
```

最后一条命令会调用云模型，但不会发布。无新密钥时只运行前三类离线检查，以及 `Collector` 的公开来源 smoke。

四模型评测：

```bash
uv run ai-daily benchmark-models --dataset tests/evals/judge-golden.json
```

评测会真实调用四个配置模型。所有 20 条必须通过 schema 和证据 ID 门禁；出现 fallback 的候选不具备晋级资格。第一名至少领先第二名 5 分才建议修改 `config/models.yaml`。

不调用模型的编辑样式预览：

```bash
uv run python scripts/render_fixture.py \
  --fixture tests/fixtures/editorial-preview.json \
  --output /tmp/ai-daily-preview.md
```

该 fixture 固定包含 17 条真实新闻，覆盖重点、关注、快讯、中文动态、研究和编辑观点；测试会检查关键新闻召回、栏目配额、论文上限、证据引用和 Issue 正文大小。

## GitHub 配置

仓库 Secrets：

- `DASHSCOPE_API_KEY`
- `DASHSCOPE_BASE_URL`
- `DEEPSEEK_API_KEY`

`GITHUB_TOKEN` 使用 Actions 自动提供的短期 token。日报 job 只有 `contents: read` 与 `issues: write`；Pages 权限隔离在可复用站点工作流。

首次启用前，先在 staging 仓库通过 `workflow_dispatch` 选择 `dry-run`，再选择 `publish`。确认同一日期重复运行仍只有一个带 `Daily` 标签的 Issue，且页面和 RSS 最新条目一致。

## 正式调度

- 04:20 Asia/Shanghai：完整生成、发布、构建和验证。
- 05:05：若当日页面已验证则退出，否则幂等恢复。
- 05:45：只验证；失败时重建站点，不调用模型。
- 三段任务共享 `ai-daily-publication-lock`，禁止并发写入。

## 失败处理

| 现象 | 自动行为 | 人工检查 |
|---|---|---|
| 单个普通来源失败 | 记录健康状态并继续 | 检查来源是否永久迁移 |
| Tier A 覆盖低于 60% | 停止发布 | 网络、解析器、来源变更 |
| 429、连接失败、可恢复 5xx | 按显式跨 Provider 链切换 | 配额和供应商状态页 |
| 401/403、schema、参数错误 | 立即失败，不切模型 | Secret、模型名、配置版本 |
| Issue 已存在、站点未更新 | 只重建 Pages | Pages job 与 isite/Zola 日志 |
| 06:00 后仍不可见 | Actions 标红 | 若 30 天出现两次 runner 排队，迁移 runner |

## 回滚

先在 Actions 中禁用 `Generate AI Daily` schedule，保留 `generate_site.yml`。回滚代码只恢复上一版 workflow 和生成器，不删除历史 Issue、Pages 或 RSS。任何提交、推送、Secret 创建和正式启用都需要用户明确授权。
