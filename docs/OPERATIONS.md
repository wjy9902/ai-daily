# AI Daily 运维手册

## 安全前提

本项目只读取 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL`、`DEEPSEEK_API_KEY`、`GITHUB_TOKEN`。密钥只能进入本机环境变量或 GitHub Actions Secrets，禁止写入 `.env.example`、YAML、测试 fixture、日志和 artifact。

对话中曾暴露的密钥必须先在两个供应商控制台吊销；之后生成的新密钥不要粘贴到聊天、Issue 或终端历史中。百炼正式环境使用华北 2 业务空间专属兼容地址。

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
