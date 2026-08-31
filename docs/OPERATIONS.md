# AI Daily 运维手册

## 安全前提

> **本文档描述内容与质量策略。** 部署现状见 [`../ops/DEPLOYMENT.md`](../ops/DEPLOYMENT.md)，
> 灾难恢复见 [`../ops/RESTORE.md`](../ops/RESTORE.md)。2026-08-13 起发布不再经由
> GitHub Issue 与 Pages，改为自有服务器上的 systemd timer，本文中涉及 Actions 的部分已相应更新。

本项目只读取模型密钥（当前为 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`）与站点地址配置。密钥只能进入
服务器上的 `/etc/ai-daily/env`（属主 `ai-daily`，权限 `0600`），禁止写入 `.env.example`、YAML、
测试 fixture、日志和 artifact。发布不再需要任何 GitHub 写权限。

若密钥曾进入提交、构建日志、聊天记录或公开页面，立即在供应商控制台轮换——曾经暴露过的密钥，
即使随后删除，也必须视为已泄露。

## 来源与选题门禁

- 来源分为官方、新闻、社区、研究和版本发布；官方与高质量媒体承担主要召回，研究和版本发布只作有限补充。
- 只有来源首次发布时间可验证、且落在 36 小时窗口内的内容才能进入候选；更新时间和发现时间都不能替代新闻发布时间。显式配置的官方版本/模型仓库 change-watch 可以把提交或仓库变更时间作为“更新事件”的时间，但标题和正文不得把它改写成首次发布。社区提交时间永远不能充当新闻时间，只能在精确 URL 已由另一条合格来源验证时作为热度旁证。订阅源缺失发布时间时仅回源读取文章元数据，仍无法确认就淘汰并记录在 `freshness.json`。
- 混合主题订阅源先做确定性 AI 相关性过滤，再进入聚类和模型判断。
- 同一事件在 48 小时内跨来源聚类，并按 URL 与标题对历史 45 天去重；同一安全公告的多个维护分支合并为一条。
- 最终选题由一次全局编辑调用在最多 100 个候选中完成，不比较不同批次的模型分数。
- 初筛后的高相关候选优先使用 feed/API 自带全文；内容不足时只回源抓取与条目自身来源配置同源的正文。全局编辑纠正初筛并提升为重点或关注的候选会在成稿前补抓一次。抓取结果记录在 `article-enrichment.json`，重点与关注稿至少要有一份 800 字符以上的正文证据，并记录在 `evidence-quality.json`；证据不足的稿件先尝试与快讯互换，换不动就降为快讯并降低刊期等级，不会为此停刊。
- 每期必须有 4–5 条今日重点、5–8 条值得关注、8–16 条快讯；详细报道最多 2 条研究、最多 2 条前瞻与传闻。同一来源通常不超过 2 条，但互相独立的重大新闻可以例外，不能为了来源均衡漏掉当天的重要变化。
- 前瞻与传闻是唯一允许收录未证实消息的类目：标题、摘要和详细稿必须写明消息出处（据报道、爆料称、消息称等），不得写成已确认事实，也不得进入今日重点。
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

服务器不持有任何 GitHub 写权限用于发布。它有两把 deploy key：一把只读，用于拉取本仓库；
一把可写，只绑定备份仓库 `ai-daily-site-backup`。发布本身完全是本机文件操作。

首次启用前先跑 `ai-daily run --mode dry-run`，确认产出级别与条目数合理；再 `--mode publish`。
同一日期重复运行由升级守卫保护：只有更高级别的刊期才能覆盖当天已发布的内容，同级和降级一律拒绝。

## 正式调度

- 04:20 Asia/Shanghai：完整生成、发布、构建和验证。
- 05:05：若当日页面已验证则退出，否则幂等恢复。
- 07:00：只验证；失败时重建站点，不调用模型。
- 三段任务共享 `ai-daily-publication-lock`，禁止并发写入。

### 甲鱼主编版

基础日报成功提交并激活 marker-keyed 上游快照后，`ai-daily-persona.timer` 在
07:10 / 07:40 / 08:10（北京时间）运行。默认 unit 使用 `--mode site`，只更新统一站点的
`/jiayu/`，不会调用公众号写接口。三次窗口先等待 marker 稳定 30 秒；相同 marker 的成稿可复用，
旧日期补跑只增加主编版归档，不会把首页回滚到旧日报。

主编版运行状态独立写入 `/www/wwwroot/ai-daily/status/persona.json`：

- `ready`：稿件通过确定性证据、风格和长度门禁。
- `held`：上游级别、记忆冲突、证据或模型结构校验不合格，没有发布。

微信草稿状态另写入 `/www/wwwroot/ai-daily/status/wechat.json`，不会覆盖主编版状态。关键状态包括：

- `draft_verified`：微信草稿创建后又通过 `draft/get` 元数据与规范化 HTML 回读。
- `unknown`：发送后响应不确定，禁止重试，只能对账。

人工启用草稿前，先在 `/etc/ai-daily/env` 配置 `WECHAT_*` 与两把不同的 HMAC key，然后：

`persona-run --mode draft` 只是保留的命令入口；它直接读取 `published/<date>.json`，并复用网站
基础日报的正文渲染结果，不调用主编模型、不读取主编版成稿，也不重写正文。公众号标题固定为
`AI 日报 YYYY-MM-DD`；摘要使用基础日报已有的 `highlight`，仅在超过微信 120 字限制时截断；
原始网站刊期作为来源链接。为满足微信正文 2 万字符限制，发送版只压缩网站专用 HTML 包装，
并在生成时校验可见文字逐字一致；回读草稿时，来源链接的文字边界、顺序和地址也必须逐一一致。

```bash
# 分别运行两次；每次输出作为一把 key，禁止复用
openssl rand -hex 32
```

```bash
uv run ai-daily authorize-wechat \
  --issuer jesse --column-id jiayu-editorial --valid-days 90 \
  --output /etc/ai-daily/jiayu-draft-authorization.json

# 只准备不可变目标与签名，不请求微信
uv run ai-daily persona-run --mode draft \
  --authorization /etc/ai-daily/jiayu-draft-authorization.json

# 明确执行草稿创建；每个账号/栏目/日期只允许一个 slot
uv run ai-daily persona-run --mode draft --execute \
  --authorization /etc/ai-daily/jiayu-draft-authorization.json
```

如果返回 `draft_unknown_reconcile_required`：

```bash
uv run ai-daily wechat-reconcile \
  --authorization /etc/ai-daily/jiayu-draft-authorization.json
```

对账使用 `wechat-targets/<date>.json` 中的原始 HTML、元数据和请求哈希，不会用后来重生成的稿件。
`wechat-slots.sqlite3` 为 WAL 模式幂等台账。不要删除 unknown slot 或 target 来“重试”。

2026-08-27 对真实账号的只读探测结果：access token 正常、草稿箱可用（`errcode=0`）、
`freepublish` 不可用（`errcode=48001`）；群发按设计禁用。因此当前自动化终点只能是草稿箱。

## 失败处理

| 现象 | 自动行为 | 人工检查 |
|---|---|---|
| 单个普通来源失败 | 记录健康状态并继续 | 检查来源是否永久迁移 |
| Tier A 覆盖低于 60% | 停止发布 | 网络、解析器、来源变更 |
| 429、连接失败、可恢复 5xx | 按显式跨 Provider 链切换 | 配额和供应商状态页 |
| 401/403、schema、参数错误 | 立即失败，不切模型 | Secret、模型名、配置版本 |
| 记录已提交、站点未更新 | `ai-daily rebuild-site`（不花钱，从 `published/` 重渲染） | `journalctl -u ai-daily` |
| 微信创建后读/写超时 | 标记 `unknown`，保留不可变请求目标 | 只运行 `wechat-reconcile`，禁止再次 create |
| 主编版 held | 不更新 `/jiayu/`，基础日报不受影响 | `status/persona.json` 与 `journalctl -u ai-daily-persona` |
| 06:00 后仍不可见 | Actions 标红 | 若 30 天出现两次 runner 排队，迁移 runner |

## 回滚

停刊：`systemctl disable --now ai-daily.timer`。展示层回滚：把 `current` 软链指回上一个 release。
代码回滚：`ops/deploy.sh --rollback`。历史 Issue、旧 Pages 站点和旧 RSS 一律不删除——
旧链接已被外部引用和收录，保留它们的成本远低于断链的代价。
