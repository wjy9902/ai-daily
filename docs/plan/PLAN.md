# Plan

构建一个独立、可审计、低运维的“甲鱼 AI 日报”流水线：以官方 RSS/API 为主采集来源，完成来源健康检查、规范化、跨源事件聚类、受证据约束的编辑和幂等发布；保留现有 GitHub Issues → GitHub Pages/RSS 发布层，不恢复 OpenClaw、RagFlow 或通用 Agent 平台。

> 状态：规划完成，尚未开始实现。
> 项目目录：`/Users/jessew/projects/ai-daily`
> 采集项目调研：[research/collection-methods.md](research/collection-methods.md)
> 多云模型调研：[research/model-provider-layer.md](research/model-provider-layer.md)

## 1. 成功标准

新系统成功，不是“每天能生成一篇文字”，而是同时满足以下条件：

- 每条入选信息都能追溯到明确来源，来源失败不会被伪装成空结果。
- 同一事件的多家报道被合并，而不是在日报里重复出现。
- 重要事实、数字和版本号有原始链接支撑；深度条目必须有第一方证据。
- 同一天重复运行只更新同一份日报，不生成重复 Issue。
- 不依赖某台 Mac 上的绝对路径、OpenClaw 会话、浏览器 Cookie、代理或常驻容器。
- 一次来源故障不会拖垮整份日报；系统性故障又会明确失败，不能发布一份看似正常的残缺日报。
- 本地可以 dry-run，GitHub Actions 可以定时运行，运行产物足以复盘“收到了什么、为什么选这些”。
- 每天北京时间 06:00 前，当日日报已经能从网站打开，并出现在 RSS 的最新条目中。
- 正式流程从采集到发布全自动完成，不等待人工审核；质量门禁失败时自动恢复或明确失败，不把人工确认变成日常依赖。
- 唯一公开渠道是网站和 RSS，不维护 Telegram、微信、邮件或其他推送分支。

## 2. 已确定的设计决策

### 2.1 系统边界

1. **不使用 OpenClaw 作为运行时。** Prompt 只负责有限的内容判断和写作，不负责调度、抓取和发布控制。
2. **不恢复 RagFlow、Redis、向量数据库或浏览器自动化集群。** 日报首版不需要 RAG 平台。
3. **采用可切换的进程内模型层。** 使用 `pydantic-ai-slim` 的 typed model/provider 能力，通过 `provider:model` 配置切换直连云模型、OpenRouter 或本机 Ollama；不运行独立模型网关。Ollama 不会在生产故障时静默接管。
4. **保留现有发布仓库和历史。** `/Users/jessew/projects/ai-daily` 后续应成为现有 [wjy9902/ai-daily](https://github.com/wjy9902/ai-daily) 的正式本地工作目录，在同一仓库中加入生成器，避免新建第二个远程仓库和跨仓库 Token。
5. **先做单次完整采集 + 截止前恢复。** 04:20 执行完整流水线，05:05 只在当日结果缺失或未验证时做幂等恢复，05:45 做轻量最终校验/站点重建；不恢复旧系统的跨运行原始候选池。
6. **首版没有后台 UI。** 来源由版本化配置文件管理，运行状态由 CLI、测试、GitHub Actions Summary 和审计产物查看。
7. **渠道固定为网站和 RSS。** 不恢复 Telegram，也不把微信、邮件或其他通知列入首版后续事项。
8. **生产发布完全自动。** 不创建待人工确认的草稿；实现阶段先在测试仓库完成端到端验证，启用正式调度后由自动质量门禁决定发布或失败。

### 2.2 技术选型

- Python 3.12；依赖和命令由 `uv` 管理。
- `httpx` 负责异步 HTTP；`feedparser` 解析 RSS/Atom；Pydantic 负责配置和模型校验。
- SQLite 只作为一次运行内的事务性工作库；不把二进制数据库提交到 Git。
- shortlist 正文抽取优先评估 `trafilatura`，只对少量候选执行；不默认启用浏览器。
- 模型调用使用 `pydantic-ai-slim` 和严格 Pydantic output type，只安装实际使用的 provider extras；项目内保留一个小型 `ModelGateway` 边界，不使用工具、MCP、图、Web UI、Logfire 或多 Agent。
- `config/models.yaml` 分别声明 `judge` 与 `editor` 的 primary、显式跨 provider fallback、timeout 和预算；API key 仅来自环境变量/Secrets。
- 测试使用 `pytest`、固定 fixtures 和 HTTP mock；代码质量使用 Ruff 和类型检查。
- 发布使用 GitHub REST API；站点继续使用当前 PyGithub/isite/Zola/GitHub Pages 链路，第一阶段只做必要的信任规则与可复用工作流改造。

具体依赖必须在实现阶段按“仓库搜索 → 成熟包 → 现有模式 → 才自行实现”的顺序确认，计划中的包名不是提前锁死的依赖清单。

### 2.3 模型 Provider 与切换策略

调研比较了 Pydantic AI、Instructor、LiteLLM、aisuite、OpenRouter、Portkey 和 BAML。最终选择 `pydantic-ai-slim`，原因是它同时提供主流 provider、Pydantic 结构化输出、model capability profile、测试模型、用量限制和显式 `FallbackModel`，又可以只安装需要的 extras，不必部署 proxy/Redis/控制台。详细证据见[多云模型调研](research/model-provider-layer.md)。

配置按任务角色而不是在代码里写死模型名：

```yaml
profiles:
  judge:
    primary: "<provider>:<model>"
    fallbacks: ["<different-provider>:<model>"]
    timeout_seconds: 90
    output_retries: 1
  editor:
    primary: "<provider>:<model>"
    fallbacks: ["<different-provider>:<model>"]
    timeout_seconds: 120
    output_retries: 1
```

执行规则：

- provider/model 切换只修改配置和 Secrets，再运行 capability check、固定评测集及 live smoke，不修改采集/编辑业务代码；
- 只在 timeout、连接失败、429 和可恢复 5xx 时进入显式配置的下一家 provider；认证、参数、上下文和 schema 配置错误立即失败；
- 输出校验失败在同一模型内重试一次，仍失败则该阶段失败，不把未验证内容交给另一个模型掩盖；
- 每次调用记录 profile、请求/实际 provider 与 model、fallback 原因、延迟、tokens 和可获得的成本数据；fallback 不得静默；
- Ollama 只作为本机明确选择的 profile；生产 fallback 必须是另一家云 provider；
- OpenRouter 可以是一个可选 provider，但需固定 model slug，并设置 `require_parameters=true`、`allow_fallbacks=false`、`data_collection=deny`，让故障链仍由本项目控制；
- LiteLLM/Portkey 只有在至少三个应用需要共享密钥、预算和路由时才重新评估，首版不部署模型网关。

初始 `judge`/`editor` 不在计划里硬编码易过期的模型名。实现时用最近 20–30 期旧日报构建评测集，对至少两家独立云 provider 按证据忠实度 35%、选题质量 20%、结构化输出 15%、中文编辑 15%、延迟稳定性 10%、成本 5% 评分，再把胜出者写入配置。

## 3. Scope

### 3.1 包含

- 声明式来源注册表和严格配置校验；
- RSS/Atom、JSON/REST API、少量官方 HTML change-watch 三类 adapter；
- 异步并发、超时、有限重试、按 host 限流和条件 HTTP；
- 标准数据模型、来源血缘、运行健康状态和审计产物；
- URL 规范化、精确去重、标题近似去重和同事件聚类；
- shortlist 后正文抽取与证据包；
- 确定性初筛、结构化模型判断、日报选题和中文编辑；
- 可配置的多云模型 profile、显式 fallback、模型用量/实际路由审计；
- Markdown/链接/证据/日期/重复项验证；
- 当日 GitHub Issue 幂等创建或更新；
- 现有 Pages/RSS 构建的显式触发和发布后验证；
- 本地 dry-run、历史日期 backfill 和 GitHub Actions 定时运行；
- 单元、契约、集成、端到端、幂等与故障测试。

### 3.2 不包含

- OpenClaw、RagFlow、Redis、消息队列、向量数据库；
- Telegram、微信公众号、邮件和其他内容发布/推送渠道；
- X、Reddit、微信公众号 Cookie 抓取或登录浏览器；
- 通用 web search 作为定时主来源；
- 全网爬虫、永久全文归档、付费墙绕过；
- 后台管理界面、多人权限、插件市场、MCP Server；
- 投资/交易型宏观日报；
- 首版的小时级实时雷达。

除“只保留网站和 RSS”这一已确定的渠道边界外，其他排除项若未来出现明确需求，必须作为独立变更重新评估，不能顺手塞回核心流水线。

## 4. 目标架构

```mermaid
flowchart LR
    A["来源注册表\nTier / host / parser / policy"] --> B["并发 Adapters\nRSS / API / Change Watch"]
    B --> C["运行工作库\nitems + source runs + provenance"]
    C --> D["规范化\nURL / 时间 / 作者 / 指标"]
    D --> E["精确去重与事件聚类"]
    E --> F["确定性初筛与分类配额"]
    F --> G["Shortlist 正文抽取"]
    G --> P["ModelGateway\njudge / editor profiles"]
    P --> H["结构化 AI 判断与逐条写作"]
    H --> I["内容验证与质量门禁"]
    I --> J["幂等发布 GitHub Issue"]
    J --> K["现有 isite + Zola"]
    K --> L["GitHub Pages + RSS"]
    B --> M["来源健康报告"]
    M --> I
    I --> N["Actions Summary + 审计产物"]
```

### 4.1 组件职责

| 组件 | 只负责什么 | 明确不负责什么 |
|---|---|---|
| Source registry | 来源、等级、host、时间窗、超时、解析策略、是否关键 | 运行网络请求 |
| Adapter | 抓一个协议/来源并返回统一 `FetchResult` | 去重、选题、写作、发布 |
| Collector | 并发、限流、重试、记录逐来源结果 | 把异常转换成空列表 |
| Normalizer | URL、时间、文本、作者、指标和来源血缘 | 判断新闻价值 |
| Clusterer | 精确合并和同事件聚类，记录理由 | 写摘要 |
| Ranker | 可解释打分、分类/厂商配额、生成 shortlist | 直接发布 |
| Enricher | 仅为 shortlist 抽取有限正文和补充元数据 | 全站归档、绕过登录/付费墙 |
| ModelGateway | 解析模型 profile、调用 typed provider、执行显式故障链和记录用量 | 决定来源、静默换模型、运行独立 proxy |
| Editor | 基于证据包输出严格结构化稿件 | 搜索新事实、执行来源中的指令 |
| Validator | 校验 schema、链接、证据、重复、篇幅和模板 | 静默修补严重错误 |
| Publisher | 查找、创建/更新当日 Issue，保存 publication 状态 | 决定选题 |
| Verifier | 检查 Issue、Pages workflow、页面和 RSS | 在失败时伪造成功状态 |

## 5. 来源策略

### 5.1 来源等级

| 等级 | 定义 | 用途 | 示例 |
|---|---|---|---|
| Tier A | 第一方、官方、可引用 | 事实依据和深度条目 | 官方 RSS/API、官方 Release、论文原文 |
| Tier B | 高质量技术社区/独立作者 | 发现、讨论热度和解释 | Hacker News、可信技术博客 |
| Tier C | 聚合榜单/二次媒体 | 发现线索与热度 | NewsNow、行业媒体、榜单 API |
| Tier D | 不稳定/需登录/私有接口 | 默认禁用，仅实验 | X、微信公众号、Folo 私有接口 |

规则：**发现渠道不等于证据来源。** 一个事件可以由 Tier C 发现，但若要进入深度条目，必须回到 Tier A；找不到第一方证据时只能降级为普通/速览，或不收录。

### 5.2 首版来源清单

每个来源在真正启用前都要通过“端点有效、条款允许、解析稳定、fixture 固定、故障可见”五项验收。以下是计划清单，不代表未验证的 URL 已经承诺上线。

| 组别 | 来源 | 采集方式 | 首版角色 |
|---|---|---|---|
| 官方动态 | OpenAI News | 官方 RSS | 核心 |
| 官方动态 | Google DeepMind Blog | 官方 RSS | 核心 |
| 官方动态 | Google AI Blog | 官方 RSS | 核心 |
| 官方动态 | Hugging Face Blog | 官方 RSS/Atom | 核心 |
| 论文 | Hugging Face Daily Papers | 公开 API | 核心 |
| 论文 | arXiv `cs.AI/cs.CL/cs.LG` | 官方 Atom API，关键词与类别过滤 | 核心 |
| 开发者生态 | GitHub Changelog / GitHub Blog AI | 官方 feed | 核心 |
| 项目更新 | 维护的 GitHub watchlist | Releases REST/Atom | 核心 |
| 官方动态 | Anthropic News | 官方网页 change-watch；若官方 feed 可用则改用 feed | 核心但允许单源失败 |
| 社区 | Hacker News | Firebase + Algolia，取分数、评论数和有限评论上下文 | 信号 |
| 独立分析 | Simon Willison、Latent Space 等 3–5 个精选 feed | RSS/Atom | 信号/解释 |
| 国内官方 | 2–4 家中国模型厂商的官方发布页 | 官方 feed/API 优先，否则受限 change-watch | 首版逐一验收 |

### 5.3 第二阶段候选

- OSS Insight star gain 或 GitHub Search：只做开源项目热度，不抓 GitHub Trending HTML。
- NewsNow/TrendRadar：只输入热榜排名和轨迹，不直接成为事实证据。
- YouTube 官方 channel Atom feed：仅跟踪白名单官方频道，不用 yt-dlp 做全站搜索。
- Bilibili 公开热门/API：只做国内热度信号，遇到风控即标记 blocked。
- RSSHub：只有不可替代且持续稳定的路由才接入。
- changedetection.io：当轻量 change-watch 规则超过维护阈值时作为独立旁路服务。

### 5.4 首版明确禁用

- X/Twitter：官方 API 成本和第三方桥接稳定性不足；
- Reddit：公开端点限流/403 历史较多，且不是本日报不可替代来源；
- 微信公众号：没有稳定的公开批量协议，Cookie/浏览器路径维护和合规成本高；
- Product Hunt：在没有稳定、授权 API 方案前不抓 HTML；
- 通用搜索 API：只允许人工调研或一次性来源发现，不进入定时主链路；
- Folo、SocialData、TikHub 等内部/付费桥接：没有单独预算和风险决定前不接入。

## 6. Adapter 与失败契约

每个 adapter 返回同一种结果，而不是“成功返回数组、失败也返回空数组”。概念契约如下：

```text
fetch(source, context) -> FetchResult

FetchResult:
  source_id
  status
  started_at / finished_at
  http_status
  item_count
  etag / last_modified
  retry_count
  items[]
  error_code / error_message
```

`status` 必须是以下枚举之一：

- `success`：请求和解析成功且有条目；
- `success_empty`：成功，但时间窗内确实没有条目；
- `not_modified`：条件请求确认无变化；
- `failed_fetch`：DNS、连接、超时、TLS 或非预期 HTTP；
- `failed_parse`：响应成功但内容不符合 schema；
- `rate_limited`：明确的 429/限额；
- `blocked`：风控、登录或条款阻断；
- `disabled`：配置中禁用。

实现规则：

- Adapter 只捕获已知网络/解析异常并转换为带类型的 source error；未知异常向上抛出并让该来源失败。
- 默认连接/读取超时分别配置，单源总时限不超过 20 秒；最多两次带 jitter 的指数退避，并尊重 `Retry-After`。
- 同一 host 有并发上限；RSS/API 支持 ETag/Last-Modified 时发条件请求。
- 注册表是静态 allowlist，只允许 HTTPS；重定向后的 host 也必须通过校验，防止 SSRF。
- 日志不输出 Authorization、Cookie、URL credentials、模型密钥或完整原文。
- 所有来源文本都视为不可信数据，不能作为系统指令，也不能触发代码、工具或 shell。

## 7. 数据模型与审计产物

### 7.1 核心实体

| 实体 | 关键字段 | 目的 |
|---|---|---|
| `SourceDefinition` | id、kind、tier、region、URL/host、critical、timeout、parser、policy | 来源的版本化声明 |
| `SourceRun` | run_id、source_id、status、时间、HTTP 状态、重试、数量、错误 | 区分空、失败和降级 |
| `RawItem` | source item ID、URL、标题、摘要、发布时间、发现时间、作者、指标、来源 | 保留入站血缘 |
| `NormalizedItem` | canonical URL、规范时间、清洗文本、content hash、语言 | 稳定比较 |
| `EventCluster` | event ID、规范标题、primary item、members、cluster reason/confidence | 合并同一事件 |
| `EvidenceBundle` | 事件、第一方链接、辅助链接、有限摘录、提取状态 | 约束模型写作 |
| `ModelRun` | profile、requested/actual provider/model、attempt、fallback reason、usage、cost、latency、status | 支持模型切换与故障审计 |
| `DigestItem` | event ID、rank、level、category、tags、score breakdown、正文 | 日报条目 |
| `Publication` | date、issue number、digest hash、status、attempts、verified_at | 幂等与验证 |

### 7.2 每次运行的文件

```text
var/runs/YYYY-MM-DD/<run-id>/
  manifest.json
  source-runs.json
  raw-items.jsonl
  normalized-items.jsonl
  event-clusters.jsonl
  selection.json
  model-runs.json
  digest.md
  validation.json
  health.json
  run.sqlite
```

- `var/` 默认 Git ignore，本地保留用于调试。
- GitHub Actions 上传经过脱敏的 JSON/Markdown 产物，建议 `retention-days: 90`；不上传密钥、Cookie、完整网页或付费内容。
- 完整正文只存在于运行期 SQLite/临时目录；审计产物保存有限证据摘录、URL、hash 和抽取状态。
- 发布后的长期去重不依赖 Actions artifact，而是读取最近 45 天日报 Issue 中的机器标记和来源 URL。因此 artifact 丢失不会改变发布正确性；若读取历史失败则明确终止去重阶段，不静默忽略。

## 8. 规范化、去重与事件聚类

### 8.1 URL 规范化

- host 小写、删除 fragment、标准化默认端口；
- 删除 `utm_*`、`fbclid`、`gclid` 等跟踪参数；
- 对已知域名执行可测试的 canonical 规则，不写通用猜测；
- 只在 host allowlist 内跟随重定向；
- 同时保留原 URL 和 canonical URL，不能丢失血缘。

### 8.2 三阶段合并

1. **精确层**：canonical URL、官方对象 ID、论文 ID、GitHub repo+release tag 完全相同。
2. **确定性近似层**：在 48 小时窗内比较规范标题、关键实体、厂商、模型名和版本；只有高阈值且实体不冲突才合并。
3. **歧义裁决层**：仅把边界候选交给模型，模型返回 `same_event / different_event / uncertain`、置信度和理由；`uncertain` 默认不合并。

每次合并都要保存成员、主来源和理由。禁止仅凭“标题都提到 GPT”就合并，也不能把模型不同版本、论文修订和产品更新错误折叠。

### 8.3 跨日报去重

- 每个 Event 生成稳定 `event_id`；日报 Markdown 在每条下写隐藏 marker。
- 发布前读取最近 45 天 Issue 的 `event_id` 和 canonical URL。
- 同一事件默认 14 天内不重复；若确有重大后续，使用 `follow_up_of` 明确关联，并在标题中说明“更新”。
- 论文/项目的版本发布按版本 ID 区分，不因 repo/论文主 URL 相同就全部过滤。

## 9. 筛选、全文与编辑策略

### 9.1 内容定位与优先顺序

日报面向“既关心 AI 技术变化，又需要把信息转化为产品、工程或业务行动”的中文读者。它不是论文榜、融资简报或全网热搜合集，核心判断顺序是：

1. **今天能否改变判断或行动**：新能力、成本变化、可直接使用的工具和生态迁移优先；
2. **是否有可信的一手证据**：官方发布、代码/Release、论文原文优先于二次解读；
3. **是否真正新增**：旧闻改标题、营销合集、无新信息的跟风讨论不收；
4. **是否覆盖中外关键变化**：国内信息必须有稳定官方来源，不为“国内栏目”硬凑条目。

每天 8–12 条的软配额：

| 内容类型 | 目标数量 | 选择标准 |
|---|---:|---|
| 模型、平台和官方能力更新 | 2–3 | 能力、价格、API、许可或可用性发生实质变化 |
| 开发者工具与开源项目 | 2–3 | 可实际试用，有代码/Release/文档，不只看 Star |
| 重要研究 | 1–2 | 方法或结果对模型/产品有明确意义，不做论文堆砌 |
| 国内 AI 与重大行业变化 | 1–2 | 影响产品、成本、监管或竞争格局，有可靠证据 |
| 其他速览 | 0–2 | 有价值但不需要长篇分析 |

“产品机会”和“今日行动项”由当天入选事件推导，不另外抓一批低质量创业资讯。融资、人事、传闻和泛政策只在确实改变行业格局且证据充分时收录；没有合格内容的类型可以为零。

### 9.2 确定性初筛

模型调用前先完成：

- 时间窗、语言、来源等级、AI 相关关键词/排除词；
- 官方来源保底与单来源上限；
- 垃圾标题、活动广告、职位、重复周报和纯营销过滤；
- 事件聚类；
- 可解释初始分数。

初始 100 分建议权重：

| 维度 | 权重 | 说明 |
|---|---:|---|
| AI 相关性 | 20 | 与模型、工具、研究、开发者生态或产业变化的直接程度 |
| 证据质量 | 20 | 第一方/原文、来源等级、信息完整性 |
| 技术或产品影响 | 20 | 能否改变能力、成本、工作流或市场格局 |
| 实用价值 | 15 | 读者今天是否能使用、验证或做决定 |
| 新颖性与时效 | 15 | 是否真正新增、是否处于有效时间窗 |
| 多源佐证与热度 | 10 | 独立来源、讨论/排名信号；不把重复转载当佐证 |

厂商过度集中、同主题过量、纯二手信息和证据缺失使用显式 penalty，不暗改原始分数。

### 9.3 Shortlist 后再抓正文

- 初筛后只保留约 20–30 个事件进入 enrichment。
- 优先使用 feed/API 自带内容；确有必要才请求原始网页并用轻量正文抽取。
- 不登录、不绕付费墙、不执行页面 JavaScript、不下载无关资产。
- 正文抽取失败与“feed 摘要可用”分别记录；快速条目可使用可靠摘要，深度条目必须有足够正文或结构化第一方数据。
- 模型只收到与当前事件有关的有限证据包，降低成本和 Prompt injection 面。

### 9.4 模型职责

模型分两步使用，不让一个长 Prompt 一次包办整份日报：

1. **结构化评审**：为每个 Event 返回相关性、影响、建议级别、分类、标签、推荐理由、风险和应引用来源；输出必须通过 schema。
2. **逐条写作**：只基于该 Event 的 EvidenceBundle 写中文条目；没有证据的数字、比较和因果关系不得生成。

最后由确定性 assembler 组合栏目、目录、来源链接和编辑观察。编辑观察可以由模型起草，但必须只引用已选 Event。

模型失败时：

- API/网络/限流类故障只按 `models.yaml` 中的显式跨 provider 顺序切换，并记录实际模型和原因；
- 认证、参数、schema、上下文和输出质量错误不通过换模型掩盖；
- Ollama 不自动接管生产任务；
- 不把候选标题直接拼成“正常日报”；
- 保留候选和健康产物，任务明确失败；
- 05:05 恢复任务可用同一 run-id 重跑 compose；本地也可手工重跑，但正式发布不等待人工批准。

### 9.5 日报产品规则

- 通常 8–12 条；安静日可以少于 8 条，绝不为了凑数降低门槛。
- 2–3 条深度、4–5 条标准、2–4 条速览只是目标分布，不是硬凑配额。
- 栏目保留：模型与平台、前沿研究、值得试的项目、行业动态、国内 AI 动态、产品机会、今日行动项；当天无合格内容的栏目可以省略。
- 同一厂商默认不超过 2 条，除非当日确有多个独立重大事件，并在选择报告中解释。
- 每条至少一个可点击原始来源；深度条目至少一个 Tier A，重要争议最好有第二个独立来源。
- 正文以转述和分析为主，不长篇复制来源；保留现有“AI 辅助、以原始信息为准”的声明。

## 10. 质量门禁与故障策略

### 10.1 发布前门禁

以下条件全部满足才允许正常发布：

- 配置和所有结构化产物通过 schema；
- 至少 60% 的启用 Tier A 来源成功或 `not_modified`，且不存在共同网络/解析系统性故障；阈值先用历史回放和测试仓库 canary 校准，上线后按数据显式调整；
- 每个入选 Event 有 canonical URL、来源血缘、发布时间或明确的发现时间；
- 所有深度条目有 Tier A 证据；
- 没有重复 event ID、重复 canonical URL、空链接、模板占位符或未闭合 Markdown；
- 文内事实检查结果无 blocker；
- 模型、发布 API 和验证阶段都返回明确成功；
- 每次模型调用都有实际 provider/model 记录；发生 fallback 时仍通过相同 schema 和事实门禁；
- 当天 Issue 的目标状态可判定，不能出现“不知道是否创建成功就再创建一次”。

如果来源健康但当天只有少量真正重要事件，允许发布“短版”。如果来源健康门禁未通过，则不发布伪正常日报；Actions 失败并保留 health report。

### 10.2 部分失败

- 非关键来源失败：继续，但在 health report 中保留错误；若影响明显，在日报末尾加简短“采集说明”。
- 一个关键来源失败：是否发布由整体 Tier A 覆盖率决定，不做隐式替换。
- 多个来源同类失败：视为系统性问题，停止发布。
- 429：尊重 `Retry-After`，本次超时后标记 rate-limited，不用代理绕过。
- HTML 规则失效：标记 failed-parse，不能把整页导航文本当内容。
- 发布后页面/RSS 验证失败：Issue 不重复创建，publication 标记为 `issue_published_site_unverified`，重试部署/验证。

## 11. 发布与幂等设计

### 11.1 当日 Issue 身份

- 标题固定为 `甲鱼 AI 日报 · YYYY-MM-DD`。
- 使用标签 `daily` 和 `generated`。
- body 顶部加入机器 marker，例如：

```html
<!-- ai-daily:v2 date=2026-08-12 digest_sha256=<hash> run_id=<id> -->
```

- 每个条目加入独立 `event_id` marker，供跨日去重和审计。

### 11.2 发布算法

1. 按 marker、日期和标签查询当天 Issue。
2. 没有时创建；只有一个且作者/marker 可信时更新；出现多个或身份不明时立即失败并要求人工处理。
3. digest hash 相同则不改 body，只进入验证。
4. 更新前保存原 issue number/hash 到 publication 记录。
5. API 请求超时后先重新查询远端状态，再决定是否重试，避免 at-least-once 导致重复 Issue。

### 11.3 与现有站点的兼容

现有 `main.py` 只接受仓库 owner 创建的 Issue；GitHub Actions 使用 `GITHUB_TOKEN` 创建时作者会是 bot。需要做一个小而明确的改造：

- 历史 owner Issue 继续被接受；
- 新 bot Issue 只有同时具有 `daily` 标签和合法 `ai-daily:v2` marker 时才被接受；
- 其他用户或无 marker 的 bot 内容仍被忽略。

另外，`GITHUB_TOKEN` 创建的 Issue 通常不会再次触发新的 workflow run。避免使用个人 PAT 的方案是：

- 把站点构建 workflow 抽成可 `workflow_call` 的复用 job；
- 日报 workflow 发布 Issue 后直接调用站点构建；
- 手工编辑 Issue 仍保留现有 `issues` 触发器；
- 发布 workflow 使用最小权限：`contents: read`、`issues: write`、站点 job 所需的 `pages/id-token` 权限分开配置。

### 11.4 发布后验证

- GitHub Issue 可读、标题/marker/hash 正确；
- Pages deploy job 成功；
- 当日页面返回 200，并包含当日标题和至少一个 event marker 对应内容；
- `rss.xml` 返回 200，最新 item 指向当日页面；
- 验证结果写入 `publication` 和 Actions Summary。

## 12. 调度方案

### 12.1 首版

- **可见性 SLO：每天 06:00 Asia/Shanghai 前，当日网页返回 200，RSS 最新条目指向当日网页。** “Issue 已创建但网页/RSS 未更新”不算达标。
- 主触发：每天 04:20 Asia/Shanghai（GitHub cron 使用 UTC `20 20 * * *`），执行完整采集、编辑、发布、站点构建和验证，目标 05:00 前结束。
- 恢复触发：每天 05:05 Asia/Shanghai（UTC `5 21 * * *`）。若网页和 RSS 已验证则立即退出；若 Issue 已存在但站点未更新，只重建并验证站点；若当日 Issue 不存在，则完整幂等重跑，目标 05:40 前结束。
- 最终校验：每天 05:45 Asia/Shanghai（UTC `45 21 * * *`），只检查 Issue、页面和 RSS；可恢复的站点构建问题自动重建，不重新调用模型。目标 05:55 前结束。
- 所有触发都从 `Asia/Shanghai` 计算 `target_date`，不能直接使用 UTC 日历日期；三个触发复用同一 workflow/命令和 publication lock，不复制三套逻辑。
- GitHub hosted runner 的 schedule 可能延迟，因此预留了 100 分钟缓冲。记录 `visible_at_cst`；若滚动 30 天内有 2 次因 runner 排队而晚于 06:00，调度/runner 必须迁移到有明确时间保证的云端执行环境，而不是继续提前 cron 碰运气。
- 采集窗口默认 `[目标日期前一天 00:00, 运行时刻]` 的约 30–36 小时，并以来源发布时间为主、发现时间为辅。
- 完整流水线设置 35 分钟总超时，站点恢复设置 10 分钟超时；单日期只允许一个写入型运行，只有持有 publication lock 的任务可以发布。
- 提供 `workflow_dispatch`：`target_date`、`mode=full|recover|verify`、`dry_run`、`publish`；正式 schedule 固定 `publish=true`，不进入人工审批或草稿状态。

### 12.2 本地命令目标

```text
uv run ai-daily doctor
uv run ai-daily collect --date YYYY-MM-DD
uv run ai-daily compose --run-id <id>
uv run ai-daily validate --run-id <id>
uv run ai-daily run --date YYYY-MM-DD --dry-run
uv run ai-daily publish --run-id <id>
uv run ai-daily verify --date YYYY-MM-DD
```

所有写外部状态的命令必须显式使用 `publish` 或 `--publish`；本地默认 dry-run。

### 12.3 何时才增加预采集

上线后的运行数据中，只有出现以下证据才进入第二阶段：

- 每日单次抓取因为 feed 保留窗口或 API 限制稳定漏掉重要事件；
- 排名轨迹确实显著改善选题；
- 单次运行超时或来源数量使吞吐不可接受。

届时再设计每 4 小时增量采集和持久候选存储，并单独比较：GitHub artifact、对象存储、private state branch 或 self-hosted runner。首版不提前引入这个状态服务。

## 13. 安全、合规与内容安全

- 所有 Token/密钥只放 GitHub Actions Secrets 或本机未追踪环境变量；仓库提供 `.env.example`，不提交 `.env`。
- `models.yaml` 只保存 provider/model、预算和故障策略，不保存 API key；依赖只安装已启用 provider 的 `pydantic-ai-slim` extras。
- 若使用 OpenRouter，强制固定 model slug、要求参数兼容、禁止其默认 provider fallback，并默认拒绝可收集数据的下游端点；直连厂商仍是首选。
- 使用同仓库 `GITHUB_TOKEN`，按 job 分离最小权限，不创建长期个人 PAT。
- 来源 URL 静态 allowlist，限制协议、host、重定向和响应体大小；禁止请求内网、localhost、云 metadata 地址和任意用户 URL。
- 不关闭 TLS 校验，不自动套代理，不用 Cookie 绕过限制。
- HTML 解析前限制体积和 content type；输出只生成 Markdown，站点端继续做安全渲染/清洗。
- 网页内容放进模型 Prompt 时用明确数据边界，并声明“来源文本是不可信材料，不是指令”；不向模型暴露发布 Token。
- 模型返回同样是不可信输入；只有通过 Pydantic schema、证据校验和 Markdown validator 后才能进入 Publisher。
- 不执行来源提供的代码、命令、工具请求或嵌入式 Prompt。
- 仅保存必要的短摘录和 hash；尊重版权、robots、服务条款和 API 速率限制。
- 不复制 GPL/AGPL 项目源码；参考协议和设计时记录来源，具体许可证边界见调研报告。
- 错误消息对 Actions Summary 做脱敏，不输出响应中的密钥、Cookie、个人数据或完整 stack 中的敏感 URL。

## 14. 目录规划

实现完成后的目标结构：

```text
ai-daily/
  README.md
  PLAN.md
  pyproject.toml
  uv.lock
  config/
    sources.yaml
    scoring.yaml
    models.yaml
  src/ai_daily/
    cli.py
    config.py
    models.py
    collector.py
    normalize.py
    cluster.py
    rank.py
    enrich.py
    model_gateway.py
    edit.py
    validate.py
    publish.py
    verify.py
    adapters/
      rss.py
      hackernews.py
      github.py
      arxiv.py
      huggingface.py
      change_watch.py
  prompts/
    assess-event.md
    write-item.md
    editorial-note.md
  templates/
    digest.md.j2
  tests/
    fixtures/
    unit/
    contract/
    integration/
    e2e/
  docs/
    research/
    runbooks/
  var/                  # gitignored
  main.py               # 现有站点发布代码，后续再小步整理
  .github/workflows/
```

不预建没有明确职责的 `services/`、`managers/`、`common/` 或插件层。单文件接近 400 行时就按职责拆分，任何文件不得超过项目约定的 800 行上限。

## 15. 测试计划

### 15.1 单元与契约测试

- 每个 adapter 至少一组成功、空、超时、429、非预期 schema fixture；
- RSS 测试 Atom/RSS、缺 GUID、错误日期、相对 URL、重复 item、ETag/304；
- URL canonicalization 对每条域名规则有正反例；
- 事件聚类覆盖同事件、相似但不同版本、同厂商不同产品、跨语言标题；
- scoring 每一项可解释且总分稳定；
- model profile 覆盖直连 provider、OpenRouter 可选路径、Ollama 本地路径、API fallback、不可 fallback 配置错误和实际模型审计；
- 模型 schema 对缺字段、额外字段、非法 URL、unsupported claim 明确失败；
- Markdown validator 检查链接、marker、重复、模板残留和栏目规则；
- publisher 覆盖创建、相同 hash 无操作、更新、远端超时后查重、冲突 Issue；
- verifier 覆盖 Issue 成功但 Pages/RSS 未更新的中间状态。

### 15.2 集成与端到端

- 无网络 E2E：用固定来源 fixtures 生成一份 golden digest；
- live smoke：只抓少量官方端点，不发布；
- GitHub 测试 Issue 或专用测试仓库验证幂等，不能在正式日报里试错；
- 现有站点构建基线测试：历史 Issues、README、30 条 RSS 和 Top/TODO 规则不回归；
- 一次完整 dry-run 输出 source health、clusters、selection、digest 和 validation；
- 故障注入：半数来源失败、模型超时、GitHub 500、重复 runner、Pages 延迟。
- 调度回放：UTC cron 能正确计算北京时间目标日期，04:20 主任务、05:05 恢复和 05:45 校验共享同一 publication，不重复发布。

### 15.3 每次宣称完成前的证据

- `uv run ruff check .` 通过；
- 类型检查通过；
- `uv run pytest` 通过；
- 构建 workflow 在测试环境成功；
- 同一日期连续执行两次只存在一个目标 Issue；
- 实际页面和 RSS 验证通过。
- 在测试仓库的计时 E2E 中，模拟正常运行和一次可恢复故障都能在北京时间 06:00 截止预算内完成。

没有这些新鲜证据时，只能说明已实现但未验证，不能说“完成”或“应该可用”。

## 16. 验收指标

先在测试仓库完成验收；正式启用后自动发布，前 7 天作为增强观察期做发布后抽查，但不设置人工审批门槛。至少记录以下指标：

| 指标 | 首版门槛 |
|---|---:|
| 北京时间 06:00 前网页与 RSS 可见 | 100% |
| 入选条目来源链接覆盖率 | 100% |
| 深度条目第一方证据覆盖率 | 100% |
| 同日重复 canonical URL | 0 |
| 明显同事件重复条目 | 0 |
| 逐来源运行状态覆盖率 | 100% |
| 实际 provider/model/fallback 审计覆盖率 | 100% |
| 发布幂等测试 | 同日两次运行仍为 1 个 Issue |
| 配置/模型/内容 schema 通过率 | 100% 才发布 |
| 需要发布后紧急更正的日报 | 首月目标 ≤ 5% |
| 正式发布人工审批步骤 | 0 |

不能用“条目越多越好”做指标。漏掉一条普通新闻通常比发布一条错误、重复或无来源信息更可接受。

## 17. 实施阶段与退出条件

### Phase 0：接管现有仓库与建立基线（0.5–1 天）

- 把当前目录安全接到现有 `wjy9902/ai-daily` 历史，不覆盖本次文档；
- 在隔离 worktree 中工作，记录主分支状态；
- 运行现有 `main.py`/站点 workflow 的本地或等价基线测试；
- 保存当前网站、RSS 和最新 Issue 的验证结果。

退出条件：历史完整、主分支未被污染、现有发布链路有可重复基线。

### Phase 1：骨架、配置和运行模型（1–2 天）

- 建立 `uv` 项目、CLI、配置 schema、核心实体、run 目录和 SQLite schema；
- 接入 `pydantic-ai-slim`，建立 `models.yaml`、`ModelGateway`、测试模型和实际模型审计；
- 实现 source registry 校验、结构化日志、doctor 和 dry-run；
- 先用两个本地 fixture adapter 跑通全流程空壳。

退出条件：没有真实网络也能生成完整审计产物，所有异常状态可区分。

### Phase 2：第一方采集器（2–3 天）

- 先实现通用 RSS/Atom；
- 再实现 HN、GitHub、arXiv、Hugging Face API；
- 最后实现一个受限 change-watch；
- 为每个启用来源建立 fixture、contract test 和 live smoke。

退出条件：十余个核心来源能稳定收集，单源故障不会变成空成功。

### Phase 3：规范化、聚类和来源健康（2 天）

- URL/time/text normalization；
- 精确、近似和边界聚类；
- 45 天历史去重 marker；
- source health、质量门禁和 Actions Summary。

退出条件：固定测试集无重复、无明显误合并，失败门禁符合预期。

### Phase 4：正文、评分与编辑（2–3 天）

- shortlist 正文抽取；
- 可解释 scoring 和分类/厂商配额；
- 用旧日报评测集比较至少两家云 provider，确定 `judge`、`editor` primary 与跨云 fallback；
- 结构化模型评审、逐条写作、模板装配；
- 事实/链接/marker/Markdown validator。

退出条件：golden digest 可重复生成，所有深度条目有第一方证据，模型异常明确失败。

### Phase 5：发布集成（1–2 天）

- 实现 Issue 查找、创建、更新和 hash 幂等；
- 修改现有发布端的 trusted bot 规则；
- 把站点构建抽成可复用 workflow；
- 实现页面/RSS verifier。

退出条件：测试环境同日重复运行不产生重复 Issue，页面与 RSS 可验证。

### Phase 6：全自动调度、切换与增强观察（1–2 天实施 + 7 天观察）

- 在专用测试仓库完成全链路、幂等、恢复和 06:00 截止预算验证；
- 正式启用 04:20 主任务、05:05 恢复和 05:45 最终校验，三个 schedule 从第一天就自动发布，不生成待审草稿；
- 前 7 个正式发布日增强记录候选、漏报、误报、重复、fallback、成本、可见时间和发布后更正；
- 发现 blocker 时自动停止错误发布并修复流水线，不恢复旧 OpenClaw 任务，也不改为日常人工审批；
- 冻结首版来源、模型 profile、阈值和模板，保留幂等手动 rerun 作为故障处理工具。

退出条件：测试仓库验收通过后即完成切换；随后连续 7 个生产日记录增强观察指标，至少一次自动恢复演练通过。

预计工程量约 9–13 个有效开发日，另有 7 个自然日生产增强观察，但观察期不阻塞全自动发布；来源端点和模型质量可能影响实际时间。

## 18. Action items

- [ ] 将 `/Users/jessew/projects/ai-daily` 接入现有 `wjy9902/ai-daily` Git 历史，并先验证旧站点基线；任何 commit 前单独征得用户确认。
- [ ] 在隔离 worktree 创建 Python 3.12 + `uv` 骨架、CLI、Ruff、类型检查和 pytest。
- [ ] 定义 `SourceDefinition`、`FetchResult`、Item/Event/Digest/Publication schema 与 SQLite migration。
- [ ] 安装最小 `pydantic-ai-slim` provider extras，定义 `models.yaml`、`ModelGateway`、显式 fallback 和 `ModelRun` 审计。
- [ ] 建立 `sources.yaml`，逐个验证首版来源端点、许可、时间语义、速率限制和 fixture。
- [ ] 实现异步 HTTP 基础设施：timeout、重试、per-host limit、条件请求、响应大小和 host allowlist。
- [ ] 实现通用 RSS/Atom adapter，并完成 OpenAI、DeepMind、Google AI、Hugging Face、GitHub feeds 的契约测试。
- [ ] 实现 HN、GitHub Releases、arXiv、Hugging Face Papers API adapters。
- [ ] 实现一个只允许官方白名单页面的 change-watch adapter，并先接一个来源验证维护成本。
- [ ] 实现规范化、canonical URL、精确去重、近似事件聚类和 45 天跨日报去重。
- [ ] 实现逐来源 health report、整体质量门禁和脱敏 Actions Summary。
- [ ] 实现 deterministic scoring、分类/厂商配额与 shortlist。
- [ ] 实现 shortlist 正文抽取，记录 excerpt/full-text/error 的显式状态。
- [ ] 用旧日报固定评测集比较至少两家云 provider，确定 `judge`/`editor` primary 与跨云 fallback，并完成严格 schema 和 prompt-injection 防护。
- [ ] 实现日报模板、证据/链接/重复/Markdown validator 和 golden E2E fixture。
- [ ] 实现 GitHub Issue marker、可信身份、create/update/no-op 幂等与未知状态查重。
- [ ] 小步修改现有 site generator 以接受受信 bot Issue，并把 Pages 构建抽为复用 workflow。
- [ ] 实现 Issue、页面、RSS 发布后验证以及 04:20/05:05/05:45 三段幂等恢复逻辑。
- [ ] 在测试仓库验证全自动发布和 06:00 截止预算，再直接启用正式 schedule。
- [ ] 完成失败注入、并发发布和回滚演练，形成短 runbook。
- [ ] 正式发布前 7 天记录增强观察指标；后续来源和高频预采集全部作为独立变更评审。

## 19. 已确认的产品决定

1. **模型层**：采用 `pydantic-ai-slim` 作为可替换的进程内 provider 层；实际模型由固定评测选出并放入配置，不在业务代码中硬编码。OpenRouter 是可选 provider，LiteLLM/Portkey 首版不部署。
2. **时间**：每天北京时间 06:00 前网站和 RSS 均可见；04:20 主运行、05:05 自动恢复、05:45 最终校验。
3. **内容**：优先能改变产品/工程行动的官方模型能力、工具和开源项目；重要研究与国内/行业变化保持有限配额，传闻、泛融资和论文堆砌降权。
4. **渠道**：只保留网站和 RSS，不恢复 Telegram、微信、邮件或其他内容推送。
5. **发布方式**：正式环境全自动发布，没有人工审批/草稿确认环节；自动门禁、幂等恢复和发布后审计承担质量控制。
