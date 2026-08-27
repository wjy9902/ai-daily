# 甲鱼AI日报

> 每日 AI 前沿技术情报，自动生成。内容由 AI 辅助创作，可能存在错误，请以原始信息为准。

🔗 **网站**: https://daily.jiayutool.cn/
📡 **RSS**: https://daily.jiayutool.cn/rss.xml
📊 **运行状态**: https://daily.jiayutool.cn/status.json

旧站 `wjy9902.github.io/ai-daily` 已于 2026-08-13 停止更新，作为归档保留，所有历史链接继续有效。
旧 RSS 不再更新，订阅请换用上面的新地址。

## 内容板块

- **今日重点（4–5 条）** — 当天最重要的模型、产品、公司与产业变化
- **值得关注（5–7 条）** — 影响明确、值得继续跟踪的进展
- **快讯（8–12 条）** — 用紧凑格式补齐工具、融资、研究和社区动态
- **编辑观点** — 只基于本期证据提炼跨新闻趋势，不做无来源推断
- **甲鱼主编版** — `/jiayu/`：结合已批准的公开经历与写作原则，挑选真正影响
  AI 使用者和 AI 产品团队的变化，并给出产品判断、反面条件与观察信号

采集覆盖 36 个公开来源：AI 实验室与平台官方站点、中英文科技媒体、研究源和少量高信号项目发布。
研究论文与版本更新设有篇幅上限，避免挤占重大产品和行业新闻。

## 出刊级别

系统不会因为某个环节失败就停刊。每种失败都对应它仍然允许的最好刊期：

| 级别 | 含义 |
|---|---|
| L0 | 完整刊：详报 + 快讯 + 编辑观点 |
| L1 | 缩减刊：证据不足的详报降为快讯 |
| L2A | 快讯刊：编辑环节失败，条目来自已完成的相关性筛选 |
| L2B | 自动快讯刊：模型环节完全不可用，条目由排序直接产生 |
| L3 | 未出刊：新鲜候选过少或渲染失败，站点保留上一期 |

L3 不算产出，是被最小化的失败态。降级刊期在页面上有明确横幅，不会伪装成正常刊期。

## 内容可信度

- 每条事实和 TL;DR 都必须附上所引证据中的**原文引用**，引用只在它所声明的那一条证据里查找，不跨证据搜索。
  这收窄幻觉但不消除幻觉——引用属实不代表由它推出的结论成立，所以既有的推测性表述过滤全部保留。
- 今日重点必须来自第一方来源，或有 **2 个不同可注册域**的独立佐证；同稿转载和聚合站转帖不算独立。
- 所有条目必须有可验证的发布时间，且落在 36 小时窗口内。社区提交时间不能当作发布时间。

## 技术栈

- Python 3.13 + uv，采集/聚类/评分/LLM/发布全部自建
- 渲染层为纯 Python 字符串构建，无模板引擎、无前端框架、无外部资源
- 自有服务器 + Caddy + systemd timer，每日 04:20 / 05:05 / 07:00（北京时间）

## 本地开发

```bash
uv sync --frozen --all-groups
uv run pytest
uv run ai-daily --site-root /tmp/ai-daily-site run --date 2026-08-13 --mode dry-run
```

```bash
# 从 fixture 渲染整站预览，不调模型
uv run python scripts/render_fixture.py --fixture tests/fixtures/editorial-preview.json --site /tmp/preview
```

```bash
# 诊断每个源：抓到多少、多少条通过新鲜度、被拒的主因是什么
uv run ai-daily probe-sources
```

甲鱼主编版在基础日报的不可变快照激活后运行：

```bash
# 生成并合入统一网站；不触碰微信公众号
uv run ai-daily --site-root /tmp/ai-daily-site \
  persona-run --date 2026-08-27 --mode site

# 微信只读能力探测，不创建草稿
uv run ai-daily wechat-probe
```

公众号路径严格限制为“创建草稿”：自动群发、自动公开发布和评论管理都没有实现。
真实执行还要求独立的授权/发布 HMAC 密钥、签名授权文件和永久封面素材；网络超时进入
`unknown`，必须先执行 `wechat-reconcile`，系统不会盲目重试。

## 运维

- 部署现状与常用命令：[`ops/DEPLOYMENT.md`](ops/DEPLOYMENT.md)
- 灾难恢复（从零到重新出刊）：[`ops/RESTORE.md`](ops/RESTORE.md)
- 迁移设计与评审记录：[`docs/plan/SELF-HOST-MIGRATION.md`](docs/plan/SELF-HOST-MIGRATION.md)

---

Powered by 🍗 鸡胸肉
