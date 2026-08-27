# AI 日报与信息采集项目调研

> 调研日期：2026-08-12（Asia/Shanghai）  
> 调研方式：阅读官方仓库的 README、工作流、配置和实际采集/存储源码；没有只根据项目宣传页下结论。Star 数仅用于说明社区热度，不代表架构质量。

## 1. 调研范围与判断标准

本次逐项检查了四件事：

1. **数据究竟从哪里来**：官方 RSS/API、聚合 API、网页抓取、浏览器模拟，还是需要 Cookie/付费桥接。
2. **如何保存和去重**：是否有持久状态、来源血缘、跨来源事件聚类和历史窗口。
3. **如何调度和暴露失败**：并发、超时、重试、限流、空结果与失败是否能区分，定时任务是否真的启用。
4. **适不适合我们的日报**：可借鉴什么、不能继承什么、部署和许可证成本多大。

### 1.1 仓库快照

| 项目 | 本次检查提交 | Star 快照 | 许可证 | 项目本质 |
|---|---:|---:|---|---|
| [Horizon](https://github.com/Thysrael/Horizon) | `5064bb9` | 8,798 | MIT | 端到端个性化资讯摘要 |
| [TrendRadar](https://github.com/sansan0/TrendRadar) | `8ee2602` | 61,395 | GPL-3.0 | 中文热榜聚合、排名轨迹与推送 |
| [AI News Radar](https://github.com/LearnPrompt/ai-news-radar) | `b5708e8` | 1,658 | MIT | 高频 AI 信号采集与静态雷达页 |
| [DailyBrief](https://github.com/leiting-eric/DailyBrief) | `c9cebf9` | 317 | MIT | 多源抓取后一次性生成日报 |
| [CloudFlare AI Insight Daily](https://github.com/justlovemaki/CloudFlare-AI-Insight-Daily) | `287cde1` | 1,769 | GPL-3.0 | 已迁移的旧版 Cloudflare 日报系统 |
| [PrismFlowAgent](https://github.com/justlovemaki/PrismFlowAgent) | `6421449` | 81 | GPL-3.0 | 插件式 Agent/数据工作流平台 |
| [RSSHub](https://github.com/DIYgod/RSSHub) | `e086c17` | 45,710 | AGPL-3.0 | 把网站/API 转换成 RSS 的路由服务 |
| [changedetection.io](https://github.com/dgtlmoon/changedetection.io) | `aac6fcf` | 33,084 | Apache-2.0 | 网页变化监控服务 |
| [Karakeep](https://github.com/karakeep-app/karakeep) | `a8df483` | 28,264 | AGPL-3.0 | 收藏、抓全文、归档和检索系统 |
| [Huginn](https://github.com/huginn/huginn) | `580d708` | 49,776 | MIT | 自托管 IFTTT/Zapier 式 Agent 平台 |
| [FreshRSS](https://github.com/FreshRSS/FreshRSS) | `d677efb` | 15,752 | AGPL-3.0 | 多用户 RSS 阅读器/聚合器 |
| [Miniflux](https://github.com/miniflux/v2) | `106cdd0` | 9,572 | Apache-2.0 | 极简 RSS 阅读器/聚合器 |

## 2. 先复盘：原日报到底是怎么做出来的

旧日报不是一个普通 Python 应用，而是 **OpenClaw Agent + 定时 Prompt + 若干本机脚本 + GitHub 发布仓库** 拼出来的工作流。恢复出的 v7.1 系统文档显示，它大致按下面四段运行。

### 2.1 调度

| 时间 | 任务 | 作用 |
|---|---|---|
| 22:00 | 晚间预采集 | 提前收集欧美白天出现的信息 |
| 04:00 | 凌晨补采集 | 补充晚间新信息和国内来源 |
| 04:30 | 成稿并发布 | 选题、写作、创建 GitHub Issue |
| 04:45 | 发布兜底 | 检查是否已发布，失败时重试 |

这些任务由隔离的 `ribao` Agent 执行，历史配置使用 Claude Sonnet 4.6。调度思想是合理的，但运行依赖散落在 OpenClaw、Prompt、本机路径和外部工具中。

### 2.2 采集

主路径是 9 组通用 `web_search` 查询，覆盖 Hugging Face Daily Papers、Hacker News、GitHub Trending、OpenAI/Anthropic/DeepMind、arXiv、Product Hunt、Reddit、Bilibili 和中文科技媒体。

补充脚本 `collect-extra-sources.py` 还负责：

- YouTube：`yt-dlp` 搜索/取元数据；
- RSS：`feedparser`；
- Bilibili：公开 API；
- 微信公众号：miku_ai/Sogou，加 Camoufox 浏览器模拟；
- Exa：通过外部 MCP 调用。

结果追加到 `raw_items.jsonl`。已发布项目放入 `reported_items.jsonl`，用最近 7 天的标题或 URL 精确匹配来排重。

### 2.3 选题与写作

旧规则每天选择 8–12 条，按以下权重打分：技术影响 35%、实用价值 25%、行业影响 20%、社区热度 10%、时效性 10%。成稿分为：

- 2–3 条深度解读；
- 4–5 条标准资讯；
- 2–4 条快速浏览；
- 技术、产品、商业标签；
- 一段编辑观察或行动建议。

这套产品形态值得保留：它不是无穷信息流，而是一份有取舍、可在几分钟内读完的编辑型日报。

### 2.4 发布

成稿先保存本地 Markdown，再发送 Telegram，并通过 `gh issue create` 创建到 [wjy9902/ai-daily](https://github.com/wjy9902/ai-daily)。发布仓库中的 GitHub Actions 在 Issue 变更后运行：

1. `main.py` 使用 PyGithub 读取 Issues，生成首页备份和 RSS；
2. `isite` 把 Issues 转换为 Zola 内容；
3. Zola 构建静态站点；
4. GitHub Pages 部署公开网站。

这个发布边界是旧系统中最干净的部分：**Issue 是内容接口，网站/RSS 独立构建**。第一阶段应继续使用它。

### 2.5 旧系统为什么越改越乱

- 搜索结果而不是第一方接口承担了主采集职责，结果不稳定且可追溯性差。
- 一个 Agent 同时负责采集、判断、写作、发布和异常兜底，失败边界不清楚。
- 本机绝对路径、代理、Cookie、CLI、MCP 和浏览器自动化形成隐性运行环境。
- 发布成功后清空原始池，缺少可复盘的候选集和逐来源运行报告。
- 只做精确标题/URL 去重，没有把“多家报道同一事件”合并成一个事件。
- 微信、Reddit、Bilibili 等受登录、风控或接口变化影响的来源被放在关键路径中。
- 多处采用“失败就继续/换方法”，但没有把降级结果明确写进运行状态。

结论不是恢复旧 OpenClaw，而是保留它的编辑格式与 Issues 发布接口，重写中间的可靠流水线。

## 3. 项目逐项调研

### 3.1 Horizon：最接近我们目标的架构参照

核心证据：[scrapers 目录](https://github.com/Thysrael/Horizon/tree/5064bb9/src/scrapers)、[RSS 实现](https://github.com/Thysrael/Horizon/blob/5064bb9/src/scrapers/rss.py)、[去重 Prompt](https://github.com/Thysrael/Horizon/blob/5064bb9/src/ai/prompting/deduplication.py)。

**采集方式**

- 每种来源实现统一的异步 `fetch(since)` 接口，主流程用 `asyncio.gather` 并发执行。
- Hacker News 直接使用 Firebase API，先取 top story ID，再取 item 和前几条评论。
- RSS/Atom 使用 `feedparser`；单个 feed 可选 `trafilatura` 抽取原文正文。
- GitHub 使用 REST API 读取用户公开事件和仓库 Release，Token 只用于提升限额。
- Reddit 默认尝试公开网页/JSON/RSS，遇到 429 做有限重试。
- Telegram 读取公开 `t.me/s/<channel>` 预览页，不要求机器人 Token。
- X/Twitter 默认依赖 Apify actor，轮询任务后读取 Dataset；这是有费用和外部依赖的路径。
- “GitHub 趋势”实际使用 OSS Insight 的 star gain API，不是抓 GitHub Trending 页面。
- 另外包含 GDELT、Google News RSS 和 OpenBB 自选列表。

**状态、去重与失败**

- 规范化 URL 时删除 UTM 等跟踪参数，以规范 URL 合并相同条目，并保留内容最丰富的版本及合并后的元数据/评论。
- 还可让模型根据标题、标签和摘要做主题级去重；模型失败时保留原列表。
- 每个来源有 `success / empty / failure` 结果，局部失败不阻塞其余来源；全部来源失败才终止。
- 没有成熟的长期原始数据仓或逐来源增量游标，主要按时间窗口重新抓取。
- RSS 全文抽取失败会退回 feed 摘要，但这个退回在内容层面不够显式。

**调度事实**

README 给人“开箱即用定时任务”的印象，但实际文件是 [`daily-summary.yml.disabled`](https://github.com/Thysrael/Horizon/blob/5064bb9/.github/workflows/daily-summary.yml.disabled)。这是典型的文档与真实运行状态漂移，说明任何项目都必须检查工作流源码。

**我们的取舍**

- 借鉴：统一 adapter 契约、并发采集、逐来源报告、规范 URL 合并、主题级聚类、分类配额。
- 不继承：整套 Jekyll/多渠道发布、Apify X 依赖、过宽的 AI 配置面、静默正文退回。
- 结论：**最佳概念基线，但不整仓 Fork。**

### 3.2 TrendRadar：强项是热榜轨迹，不是原始事实采集

核心证据：[热榜 fetcher](https://github.com/sansan0/TrendRadar/blob/8ee2602/trendradar/crawler/fetcher.py)、[RSS fetcher](https://github.com/sansan0/TrendRadar/blob/8ee2602/trendradar/crawler/rss/fetcher.py)、[SQLite schema](https://github.com/sansan0/TrendRadar/blob/8ee2602/trendradar/storage/schema.sql)。

**采集方式**

- 中文热榜不是逐站抓取，而是请求 NewsNow API，默认形如 `.../api/s?id=<platform>&latest`。
- 默认平台包括今日头条、百度、华尔街见闻、澎湃、Bilibili 热搜、财联社、凤凰、贴吧、微博、抖音和知乎。
- 来源按顺序请求，加入随机间隔，失败重试两次；同时校验 HTTPS 和预期域名，域名不符时整源丢弃。
- RSS/Atom/JSON Feed 使用 `requests + feedparser`，保存标题、URL、GUID、时间、作者及最多 500 字摘要；主采集器不抓文章全文。
- 可以把 NewsNow 自托管，但这等于新增一个长期维护服务。

**状态、去重与失败**

- SQLite 持久化新闻、标题变化、每次排名、采集批次和逐来源状态。
- 热榜按 `URL + platform` 唯一；RSS 按 `guid + feed` 或 `URL + feed` 唯一。
- 支持本地 SQLite，也支持 S3/R2 远程状态。
- 报告模式有当天、当前、增量；可做关键词或 AI 标题分类，并根据排名历史分析趋势。
- AI 过滤失败时会回落到关键词过滤，若不额外记录容易隐藏模型故障。

**调度事实**

GitHub Actions 每小时第 33 分钟运行，但默认有 7 天“签到试用”自停逻辑；长期运行官方更推荐 Docker。Actions 的本地 SQLite 不会天然跨 runner 保留，必须配置远程存储。

**我们的取舍**

- 借鉴：排名轨迹、逐来源状态、域名安全校验。
- 可选接入：把 NewsNow/TrendRadar 结果作为“中国热度信号”，不能把热榜标题直接当作事实依据。
- 不继承：完整推送/MCP/AI 配置系统、7 天自停模型、GPL 源码。
- 结论：**第二阶段可选信号源，不是主采集器。**

### 3.3 AI News Radar：来源治理最好，但实现已经单体化

核心证据：[主采集脚本](https://github.com/LearnPrompt/ai-news-radar/blob/b5708e8/scripts/update_news.py)、[来源覆盖文档](https://github.com/LearnPrompt/ai-news-radar/blob/b5708e8/docs/SOURCE_COVERAGE.md)、[工作流](https://github.com/LearnPrompt/ai-news-radar/blob/b5708e8/.github/workflows/update-news.yml)。

**采集方式**

- 第一方来源包括 OpenAI、DeepMind、Google AI、Hugging Face Blog、GitHub AI/Changelog 等 RSS，以及 GitHub commit Atom。
- Hacker News 用 Algolia 关键词搜索，并按评论数或分数过滤。
- 支持 OPML 导入和大量媒体 RSS，按来源设置上限、关键词和质量先验。
- 也接入 AI HOT、NewsNow、BestBlogs、Zeli、AIbase、Follow Builders 等聚合服务，部分页面使用 `requests + BeautifulSoup` 解析。
- 被阻挡或需要更多上下文的页面有 Jina Reader 路径。
- AgentMail、X 官方 API、SocialData、TikHub 等付费/私有来源被单独配置预算和状态。
- 项目明确默认跳过不稳定的 RSSHub Telegram、即刻、Bilibili、知乎、播客和微信公众号桥接路由，有官方 feed 时优先替换。

**状态、去重与编辑**

- `archive.json` 保存 21 天，条目 ID 由站点、来源、标题和 URL 哈希生成，并记录首次/最后出现时间。
- 独立输出 `source-status.json` 和付费来源状态，来源血缘与审计性较强。
- 先用可解释的关键词、来源先验和 AI 相关性规则筛选；LLM 主要用于标题改善、翻译、推荐理由和人群说明。
- 精确标题/URL 去重之外，还会按 canonical URL 或标题相似度合并 6 小时内的同一故事，并用厂商/模型实体保护降低误合并。
- 重要性综合编辑权重、来源等级、AI 相关性、时效和多源热度；多来源或高分事件才进入重点摘要。

**主要问题**

- 绝大多数采集逻辑堆在约 6,500 行的 `update_news.py` 中，来源选择器和聚合服务硬编码很多。
- 多层聚合会让“原始出处”和“发现渠道”混在一起。
- README 称每 30 分钟更新，实际工作流是每小时第 17 分钟。
- 工作流每小时把整个 `data/` 提交回仓库，历史审计直接，但会造成提交噪声与状态耦合。

**我们的取舍**

- 借鉴：来源等级、健康报告、确定性预过滤、事件合并记录、宁缺毋滥。
- 不继承：巨型单文件、多层下游聚合、每小时提交数据快照、复杂付费桥接。
- 结论：**最佳来源治理参考，不能直接成为代码底座。**

### 3.4 DailyBrief：来源注册表和幂等调度值得借鉴

核心证据：[来源配置](https://github.com/leiting-eric/DailyBrief/blob/c9cebf9/sources.config.json)、[来源分发](https://github.com/leiting-eric/DailyBrief/blob/c9cebf9/lib/sources/dispatch.ts)、[定时工作流](https://github.com/leiting-eric/DailyBrief/blob/c9cebf9/.github/workflows/daily.yml)。

**采集方式**

- 配置中共有 53 个来源、默认启用 26 个：21 个 RSS、4 个 API、1 个 HTML 抓取。
- AI 来源包括 OpenAI、DeepMind、Hugging Face Blog、Hugging Face Daily Papers API、TLDR AI、Latent Space、GitHub Trending HTML、Hacker News Firebase 等。
- RSS 用 `rss-parser`，15 秒超时、最多 30 条、截取约 300 字摘要；部分 TLS 问题用 `curl` 子进程绕过。
- GitHub Trending 用 Cheerio 抓 HTML；Hugging Face Papers 用 API 并按关键词/赞数过滤；HN 只取分数和评论数，不取评论正文。
- 还包含 V2EX、LinuxDo、金融和全球新闻，以及一个反向分析的第三方 X 排行 API。

**处理方式**

- 各来源顺序抓取，单源异常后继续；只有总结果为 0 才终止。
- 以分类轮转方式限制候选，保留 14 天最大窗口。
- 主流程没有清晰的跨来源事件聚类；LLM 基本只拿标题、URL 和短摘要一次性生成整份 JSON/HTML。
- Actions 每小时两次触发，再由时段 gate 和“当天是否已有报告”判断是否执行，支持错过时间后的 catch-up。

**我们的取舍**

- 借鉴：声明式来源注册表、启动时配置校验、时段 gate、按日期幂等和 catch-up。
- 不继承：过宽的金融/交易范围、HTML Trending、第三方 X 接口、只用短摘要的一次性整稿。
- 结论：**适合借来源注册和发布幂等，不适合直接 Fork。**

### 3.5 CloudFlare AI Insight Daily：已经是遗留实现

核心证据：[旧数据源目录](https://github.com/justlovemaki/CloudFlare-AI-Insight-Daily/tree/287cde1/src/dataSources)、[构建工作流](https://github.com/justlovemaki/CloudFlare-AI-Insight-Daily/blob/287cde1/.github/workflows/build-daily-book.yml)。

**采集方式**

- 旧实现运行在 Cloudflare Worker/KV，包含 GitHub 项目、Hugging Face Papers、中文 AI 媒体等 adapter。
- Reddit/X 依赖 Folo 的内部接口、`FOLO_COOKIE`、列表 ID 和分页详情，不是稳定的官方公开协议。
- GitHub、Folo、模型、登录和 Worker URL 等环境变量很多。
- 日报 book 工作流中的 schedule 已注释，主要靠手动触发和外部 Worker URL，再把结果提交到分支。

README 已明确后端迁往 PrismFlowAgent，因此这个仓库不是仍在演进的采集核心。

**我们的取舍**

- 不复制任何实现，也不恢复 Folo Cookie/私有接口。
- 仅把它作为“为什么不要让 Workers、Cookie、模型和 GitHub commit 混成一个任务”的反例。
- 结论：**不采用。**

### 3.6 PrismFlowAgent：插件平台能力多，但边界过大

核心证据：[BaseAdapter](https://github.com/justlovemaki/PrismFlowAgent/blob/6421449/src/plugins/base/BaseAdapter.ts)、[内置 adapters](https://github.com/justlovemaki/PrismFlowAgent/tree/6421449/src/plugins/builtin/adapters)、[SchedulerService](https://github.com/justlovemaki/PrismFlowAgent/blob/6421449/src/services/SchedulerService.ts)。

**采集方式**

- Fastify + React + SQLite 的完整应用，通过扫描内置/自定义目录注册 adapter。
- Adapter 先 `fetch` 原始数据，再 `transform` 为统一数据。
- RSS adapter 使用 `rss-parser`；GitHub Trending 用 HTML 正则；Follow adapter 仍依赖 Folo 内部 API 与 Cookie。
- AI Search adapter 让 Agent 自己搜索并返回 JSON，这更像不确定的发现工具，不是可审计的一手采集。

**状态与失败**

- SQLite 保存 `source_data`、全文索引、状态和日志；调度时区为 Asia/Shanghai。
- 来源 adapter 默认顺序执行。
- `BaseAdapter` 捕获异常后返回空数组，后续任务仍可能被标为成功；这会把“来源失败”伪装成“今天没有内容”。
- 主键主要是 adapter 产生的 `id`，缺少成熟的规范 URL 唯一性和跨来源事件聚类。

**我们的取舍**

- 只借鉴 `fetch → transform` 的接口思想；Horizon 的契约更小、更适合本项目。
- 不引入后台、前端、插件注册、Agent 编排、Docker 镜像和 SSH 部署面。
- 结论：**对一个人维护的日报明显过重，不采用。**

### 3.7 RSSHub：是协议转换层，不是日报采集系统

核心证据：[路由源码](https://github.com/DIYgod/RSSHub/tree/e086c17/lib/routes)。

**采集方式**

- 每个 HTTP 路由在请求到来时调用上游 API 或解析网页，再即时输出 RSS item。
- 例如 Bilibili 热门路由直接调用 Bilibili 公开 API，把标题、描述、发布时间、链接和作者映射成 feed。
- 数千条路由中，有些只需公开 API，有些需要 Cookie、代理、配置或 Puppeteer；路由元数据会声明反爬和配置要求。
- 服务层提供缓存和访问控制。

**它没有做的事**

- 不主动轮询全部来源；
- 不持久化我们的候选池；
- 不做跨 feed 去重、事件聚类、重要性排序或编辑；
- 路由是否稳定仍取决于上游页面/API。

**我们的取舍**

- 当某个必要来源确实没有官方 RSS/API 时，可以把一条经过固定和监控的 RSSHub 路由当作普通 feed。
- 首版不自托管 RSSHub；只有至少 3–5 条不可替代路由长期证明有价值后才评估。
- 不复制 AGPL 路由源码到本项目。
- 结论：**可选的末级转换器，不进入核心。**

### 3.8 changedetection.io：适合盯官方更新页，不适合发现全网新闻

核心证据：[fetch backends](https://github.com/dgtlmoon/changedetection.io/tree/aac6fcf/changedetectionio/content_fetchers)、[RSS 输出](https://github.com/dgtlmoon/changedetection.io/tree/aac6fcf/changedetectionio/blueprint/rss)。

**采集方式**

- 每个 watch 指定一个 URL，可用快速 HTTP requests，也可用 Playwright、Puppeteer 或 Selenium 浏览器。
- 支持 headers、Cookie、代理、GET/POST、浏览器步骤和登录流程。
- CSS/XPath、JSONPath/jq 和文本过滤器只比较页面的有效区域。
- 保存历史快照，按 checksum/diff 判断变化；还支持库存、图片变化、截图和通知。
- 可按 watch 设置时区/周期，并把变化输出为 RSS。

**我们的取舍**

- 很适合监视 5–15 个没有 feed 的**官方发布页或 Changelog**，例如 Anthropic News。
- 页面变化只是“可能有新信息”，还必须经过提取、规范化和编辑验证，不能直接变成日报条目。
- 首版优先写一个受限的轻量 change-watch adapter；如果页面规则越来越多，再把 changedetection.io 作为旁路服务接入。
- 结论：**明确的补充能力，不作为核心平台。**

### 3.9 Karakeep：先收藏再深抓，适合人工资料库

核心证据：[RSS 文档](https://github.com/karakeep-app/karakeep/blob/a8df483/docs/docs/05-integrations/06-rss-feeds.md)、[FeedWorker](https://github.com/karakeep-app/karakeep/blob/a8df483/apps/workers/workers/feedWorker.ts)、[Crawler](https://github.com/karakeep-app/karakeep/tree/a8df483/apps/workers/workers/crawler)。

**采集方式**

- 每小时扫描启用的 RSS feed，并按 feed ID 哈希把任务均匀分布在一小时内。
- 每次请求 5 秒超时，要求 HTTP 200 和 XML content type。
- `rss-parser` 只规范 `id/link/guid/title/categories`，GUID 依次退回到 id 或 link。
- `rss_feed_imports` 记录 `feed + entryId`；新条目创建为 bookmark，再进入后续 crawler。
- Crawler 可用浏览器抓页面、解析正文和元数据、截图、PDF、完整页面归档；视频还可使用 yt-dlp。
- 后续支持 LLM 标签/摘要、OCR、全文和向量检索，也支持本地 Ollama。

**代价与定位**

- 它需要 Web、数据库、队列、worker、浏览器和资产存储，是完整的个人知识库，不是一个小型 collector。
- Feed 到 bookmark 的链路非常适合“我以后要读”，但没有为日报设计事件聚类、来源证据和编辑配额。

**我们的取舍**

- 借鉴：先收候选、只对 shortlist 抓全文；失败状态与原始摘要分开保存。
- 如果未来需要个人复核箱，可以把候选单向导入 Karakeep；不能让它成为日报运行前提。
- 结论：**可选人工阅读/归档旁路，不安装为核心。**

### 3.10 Huginn：通用事件 Agent 很灵活，也会重新制造复杂度

核心证据：[RSS Agent](https://github.com/huginn/huginn/blob/580d708/app/models/agents/rss_agent.rb)、[Website Agent](https://github.com/huginn/huginn/blob/580d708/app/models/agents/website_agent.rb)、[De-duplication Agent](https://github.com/huginn/huginn/blob/580d708/app/models/agents/de_duplication_agent.rb)。

**采集方式**

- RSS Agent 基于 Feedjira，默认每天执行；支持多 feed、headers、Basic Auth、清洗和每次条目上限。
- 它在 agent memory 中记住最近 ID，默认 500 个；多 feed 中相同 GUID 被视为重复。
- Website Agent 支持 HTML/XML 的 CSS/XPath、JSONPath、文本正则，以及 `all / on_change / merge` 模式。
- Agent 之间通过数据库事件图传播，可再串接去重、格式转换、Digest、邮件、Webhook 等 Agent。
- Scheduler 用 Rufus/后台任务支持分钟、小时、每天和自定义 cron。

**状态与失败**

- 网站抓取异常写 Agent log，不发事件；working 状态结合预期更新周期和近期错误。
- 通用去重 Agent 对配置属性做有限 lookback，长属性使用 CRC32；这不等同于 URL 规范化或语义事件聚类。
- 部署需要 Rails、数据库、队列/worker 和大量通用 Agent 配置。

**我们的取舍**

- 它能复刻旧 OpenClaw 的“拖 Agent 流程”，但会再次把简单日报变成一个通用自动化平台。
- 本项目需要显式代码、类型和测试，不需要可视化 Agent 图。
- 结论：**不采用。**

### 3.11 FreshRSS：成熟的订阅阅读器，不是编辑流水线

核心证据：[README](https://github.com/FreshRSS/FreshRSS/blob/d677efb/README.md)、[Feed 更新文档](https://github.com/FreshRSS/FreshRSS/blob/d677efb/docs/en/admins/08_FeedUpdates.md)、[网站抓取文档](https://github.com/FreshRSS/FreshRSS/blob/d677efb/docs/en/users/11_website_scraping.md)。

**采集方式**

- 支持 RSS、Atom、JSON Feed、OPML，以及兼容来源的 WebSub 实时推送。
- 内置网页抓取可用 XPath 1.0 从 HTML 生成 feed，也能用 dotted path 解析 JSON。
- `actualize_script.php` 由 Docker cron、系统 cron 或 systemd 触发；单 feed 最快约每 20 分钟更新一次。
- 基于 SimplePie/cURL 请求，默认 10 秒级超时，处理重定向和 429/503 `Retry-After`。
- 条目和 feed 状态保存在 SQLite、PostgreSQL、MariaDB/MySQL 中，适合长期未读/收藏管理。

**我们的取舍**

- 强项是订阅管理、阅读状态、多用户 UI 和客户端 API；它不做 AI 选题、跨 feed 事件聚类或证据验证。
- 如果未来想要一个人工 RSS 收件箱，可以单独使用；新日报不应为此增加 PHP Web 服务和数据库。
- 结论：**不作为运行依赖。**

### 3.12 Miniflux：最值得参考的可靠 feed 拉取实现

核心证据：[README](https://github.com/miniflux/v2/blob/106cdd0/README.md)、[调度器](https://github.com/miniflux/v2/blob/106cdd0/internal/cli/scheduler.go)、[RefreshFeed](https://github.com/miniflux/v2/blob/106cdd0/internal/reader/handler/handler.go)。

**采集方式**

- 支持 Atom、RSS、JSON Feed、OPML，默认约一小时轮询。
- 持久保存 ETag 和 Last-Modified，发出条件请求；未修改时不重复解析正文。
- 参考 feed TTL、Cache-Control、Expires、历史更新频率和 `Retry-After` 安排下次检查。
- Scheduler 按 batch、解析错误上限和每 host 并发上限生成任务，worker pool 并行刷新。
- 保存 feed error counter，解析失败、数据库失败和限流都有明确状态。
- 可按 CSS 规则抓原文，也有本地 Readability 全文提取、过滤规则和 REST API。

**代价与定位**

- 单 Go binary 很轻，但强制 PostgreSQL；一旦采用就新增常驻服务和数据库。
- 它解决的是可靠读 feed，不解决多来源事件、选题和日报写作。

**我们的取舍**

- 借鉴：条件 HTTP、按 host 限流、`success/not-modified/failure` 分离、错误计数、动态刷新时间。
- 首版直接在 Python adapter 中实现所需的小子集，不部署 Miniflux。
- 结论：**优秀实现参考，不作为服务依赖。**

## 4. 横向对比

| 项目 | 第一方 RSS/API | 热榜/社交 | 网页变化 | 持久采集历史 | 跨源事件合并 | 编辑型日报 | 部署负担 | 对本项目的角色 |
|---|---|---|---|---|---|---|---|---|
| Horizon | 强 | 中 | 弱 | 弱 | 强 | 强 | 中 | 核心架构参照 |
| TrendRadar | RSS 中、热榜依赖聚合 API | 强 | 无 | 强 | 弱 | 中 | 中 | 中文热度旁路 |
| AI News Radar | 强 | 强 | 中 | 中 | 强 | 中 | 中 | 来源治理参照 |
| DailyBrief | 中 | 中 | 弱 | 弱 | 弱 | 中 | 低 | 注册表/幂等参照 |
| CloudFlare AI Insight | 中 | 私有桥接多 | 弱 | 中 | 弱 | 中 | 高 | 不采用 |
| PrismFlowAgent | 中 | 中 | 弱 | 中 | 弱 | 中 | 高 | 不采用 |
| RSSHub | 路由转换 | 强 | 路由级 | 缓存而非历史 | 无 | 无 | 中至高 | 末级 feed 转换器 |
| changedetection.io | 无 | 无 | 强 | 强 | 无 | 无 | 中 | 官方页面补充监控 |
| Karakeep | RSS 中 | 无 | 抓全文 | 强 | URL 级 | 无 | 高 | 可选人工归档箱 |
| Huginn | 中 | 取决于 Agent | 强 | 事件级 | 简单属性去重 | Digest 而非编辑 | 高 | 不采用 |
| FreshRSS | 强 | 无 | XPath/JSON | 强 | feed 条目级 | 无 | 中 | 可选个人阅读器 |
| Miniflux | 强 | 无 | CSS/全文 | 强 | feed 条目级 | 无 | 中 | feed 可靠性参照 |

## 5. 能力怎么组合，而不是项目怎么堆叠

研究后的合理组合是“借能力、少依赖”：

| 我们需要的能力 | 主要借鉴 | 实际采用方式 |
|---|---|---|
| 小而统一的来源接口 | Horizon | 自己实现 `fetch → normalize` adapter 契约 |
| 来源等级、血缘和健康报告 | AI News Radar | 写入本项目的数据模型和运行产物 |
| 排名变化/热度轨迹 | TrendRadar | 第二阶段可选数据字段或外部信号 adapter |
| 声明式来源配置和日期幂等 | DailyBrief | YAML 注册表 + 发布标记 |
| 条件请求、限流、错误计数 | Miniflux | 在 RSS/API 基础设施中实现必要子集 |
| 无 RSS 官方页面变化 | changedetection.io | 少量受限 change-watch adapter，必要时外接服务 |
| 只为 shortlist 抓全文 | Karakeep/Horizon | 先筛选，再用轻量正文抽取，不全网归档 |
| 网站和 RSS 发布 | 现有 ai-daily 仓库 | 继续以 GitHub Issue 为内容接口 |

明确不组合的东西：

- 不同时部署 TrendRadar、RSSHub、Karakeep、Huginn、FreshRSS、Miniflux 和 PrismFlowAgent；
- 不把 Cookie、Folo 私有接口、X 抓取或浏览器登录放进首版；
- 不把通用搜索/Agent 输出当作事实来源；
- 不把采集、写作和发布塞进一个大脚本；
- 不因为某项目 Star 多就接受它的运行边界。

## 6. 许可证边界

- MIT 项目（Horizon、AI News Radar、DailyBrief、Huginn）允许在保留许可和版权说明的条件下复用，但本项目优先重写所需的小接口，避免携带不必要结构。
- Apache-2.0 项目（changedetection.io、Miniflux）可作为算法和工程参考，若复制具体代码需保留通知并检查专利条款。
- GPL/AGPL 项目（TrendRadar、CloudFlare AI Insight、PrismFlowAgent、RSSHub、Karakeep、FreshRSS）不复制或 vendoring 源码到本项目。通过公开 HTTP/RSS 协议消费一个独立服务，与把其代码嵌入项目是两件事。
- 以上是降低许可证耦合的工程策略，不是法律意见。

## 7. 最终判断

没有一个现成项目能同时满足“来源可靠、中文 AI 聚焦、跨源合并、编辑质量高、运行简单、沿用现有站点”这六点。直接安装其中任何一个，都只是在另一处制造新的耦合。

最合理的路线是：

1. 用 Horizon 的 adapter/并发/来源报告作为结构参考；
2. 用 AI News Radar 的来源等级、血缘和事件合并作为质量参考；
3. 用 Miniflux 的条件请求和失败状态作为采集可靠性参考；
4. 用 DailyBrief 的注册表与幂等调度作为运维参考；
5. 继续使用现有 GitHub Issues → Pages/RSS 发布层；
6. 从一个每日单次、十余个高质量来源的小系统开始，质量稳定后再增加热榜和高频采集。

具体落地步骤见项目根目录的 [PLAN.md](../../PLAN.md)。
