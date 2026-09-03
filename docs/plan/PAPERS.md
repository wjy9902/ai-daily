# 论文页（/papers/）设计与实施计划

> 状态：v2 —— 已通过 Codex 独立评审（22 条问题，全部消化，见文末评审记录）。
> 目标读者：实施者（可能是全新的 Claude session）。本文档假设读者未读过任何前期讨论，
> 所有决策、理由和已知的坑都写在这里。

## 1. 目标

为个人使用建一个每日更新的论文页 `daily.jiayutool.cn/papers/`，收集 agent、模型、
推理对齐方向的高质量最新论文，每篇给出**专家级中文深读**（基于 arXiv 全文，不是摘要复述）。

三条硬原则：

1. **LLM 不当质量裁判。** 摘要看不出论文水不水。质量靠外部硬信号：HF Daily Papers
   人工策展 + 社区投票、机构、代码、交叉讨论。LLM 只做主题相关性分类、少量"漏网之鱼"
   补选和深读生成。这本质是**声誉与热度代理**，不是论文质量的直接度量——接受这个局限，
   它仍是新论文可得的最强信号；补选通道就是为冷门好论文留的口子。
2. **宁缺毋滥，且有明确阈值。** 见 §3.4 的数值门槛。够格的不足就少放，最低 3 篇，
   不足 3 篇当天不更新（保留上一期）。
3. **深读基于全文，实验数字必须带可校验的原文引用。** 沿用日报的 quote-substring
   引用纪律（`content.py`），程序校验。明确其边界：**引用属实 ≠ 论断成立**（原文引用
   可能被安到错误的对象上），这与日报对该机制的定位一致——收窄幻觉，不消除幻觉。
   结构化的 claim+quote 配对（§5.3）把可校验面压到最大。

## 2. 每篇深读卡的内容模板

设计依据：Keshav 三遍读论文法、李沐三遍精读法、NeurIPS 审稿维度
（soundness / novelty / significance）。每篇 7 段：

| 段 | 内容 | 要求 |
|---|---|---|
| ① 一句话定位 | 解决什么问题 + 相对已有工作的 delta | 默认展开 |
| ② 背景与动机 | 此前主流方法卡在哪、为什么现在出现这篇 | 折叠 |
| ③ 方法机制 | 核心设计怎么运作、关键公式/算法的直觉、训练与数据细节 | 篇幅最重，折叠。目标：读完能向别人复述机制 |
| ④ 实验与证据 | 基线、主结果数字、最关键 1–2 个消融、设置公平性 | 折叠。**结构化 claim+quote 列表**（§5.3），非自由散文 |
| ⑤ 审稿人视角 | novelty / soundness / significance 各一句判断 | 默认展开 |
| ⑥ 局限与保留意见 | 作者承认的 + 文中能看出但没明说的 | 折叠 |
| ⑦ 值得跟进 | 可复用的想法、开放问题、适合对比读的论文 | 折叠 |

卡片头部徽章：机构 · HF ↑票数 · GitHub star · X/HN 提及 · 链接（arXiv / HF 讨论页 / alphaXiv）。

全文获取失败或深读失败的论文降级为**简读卡**（基于摘要，只有 ①⑤ 弱化版 + "未深读"标注）。

## 3. 质量漏斗

```
候选池（每天 ~40-60 篇）
├─ HF Daily Papers 全量（limit 50）
├─ arXiv cs.AI / cs.CL / cs.LG / cs.MA 直采
└─ 交叉信号：日报最近一次已完成 run 的 sources.json（X 账号 + HN）
        ↓ 硬信号打分（纯规则）
        ↓ LLM 相关性分类 + 漏网之鱼补选（有硬信号地板）
        ↓ 阈值筛选，top 3–8 进全文深读
```

### 3.1 硬信号评分（初版公式，里程碑 1 校准）

```
score = 3.0 * hf_listed
      + 1.5 * log2(1 + upvotes)
      + 2.0 * org_tier          # 一线=1.0，其他有机构=0.4，无=0
      + 1.0 * has_code + 0.5 * log2(1 + github_stars)
      + 1.0 * min(cross_mentions, 2)
```

一线机构名单维护在 `config/papers.yaml`（初版 20–30 个，按 HF `organization.name`
子串匹配，全小写比较）。已知偏差：HF 上榜与名校机构相关，存在声誉叠加——接受，
理由见 §1 原则 1；如里程碑 1 校准发现名单外好论文被系统性压制，下调 `org_tier` 权重。

**字段防御**：HF 当前 payload 的 `upvotes` 位于 `paper` 内层、`numComments` 位于外层，
两者都兼容旧响应的另一层位置及 `numUpvotes` 旧字段名；`githubRepo`/`githubStars`/
`organization` 取 `paper` 内层。字段均用 `.get()` 取值、缺省为 0/None，任何字段缺失
只影响该项得分，不抛错。不做独立的 GitHub API 查询（star 数只用 HF 给的，可能滞后，接受）。

### 3.2 交叉信号的取数与匹配规则

- 取数：`{artifacts_dir}/{target_date}/` 下**有 `run.json` 的最新 run 目录**（run.json
  存在 = run 已完成）；当天没有则取前一天的。取不到则 cross_mentions = 0，不阻塞。
- 匹配：① 从条目 url + summary 里正则抽 arXiv ID（`\d{4}\.\d{4,5}`，含 abs/pdf/html
  链接形态），命中即计；② 标题归一化（小写、去空白与标点）后完全相等且长度 ≥ 30 字符。
- 计数：按**不同 source name** 数去重（同一账号转发三次算 1 次）。已知失真并明确
  接受：同一机构的多个官方账号会被计成多次，而 HN 整体只算 1 个来源——因此该信号
  权重仅 1.0、封顶降到 2，是打分微调项而非门槛项，失真不值得引入账号→机构映射表。

### 3.3 LLM 的两个任务（各一次批量调用，judge 档位）

1. **相关性分类**：候选 → `agent | 模型架构与训练 | 推理与对齐 | 其他`，agent 类
   score +1.0。输入标题 + 摘要，pydantic 结构化输出。**分批调用，每批 20 篇**
   （仿日报 `JUDGE_BATCH_SIZE` 模式），单批失败只丢该批（这些候选按"其他"处理、
   不加分），validator 校验返回 ID 集合与该批输入完全一致（无缺失/重复/未知 ID）。
2. **漏网之鱼补选**：对未上 HF 榜的 arXiv 候选，最多补 2 篇，须给理由。**硬信号地板**：
   补选对象必须满足（有机构可辨 或 有代码链接 或 cross_mentions ≥ 1）之一，
   由代码在候选列表送入 LLM 前过滤，LLM 只能从过滤后的池子里选。

### 3.4 阈值与数量（宁缺毋滥的实现）

- **主通道**：score 降序、score ≥ 4.0（初值，里程碑 1 校准后写进 papers.yaml）。
- **补选通道**：豁免 score 阈值（补选存在的意义就是硬信号尚未起来），但必须过
  §3.3 的硬信号地板，最多 2 篇。两通道合计上限 8 篇，补选排在主通道之后。
- **发布前置门（选择阶段）**：主通道合格篇数 < 3 → 当天不发布，保留上一期
  （补选论文不计入这个最低数）。
- **发布后置门（深读阶段）**：深读成功篇数 < 2，或简读卡多于深读卡 → 当天不发布，
  保留上一期。深读是本产品的核心承诺，一整期简读卡没有意义。两道门任一触发都只
  写日志、不写 record。

### 3.5 新鲜度与去重

- **新鲜度语义**：HF 通道取"当天榜单"（API 返回的当前 daily 列表）；arXiv 通道按
  `submittedDate` 近 7 天。**papers 子命令没有 `--date` 参数**——它只做"今天"
  （北京时区），记录落盘日期恒为运行日，杜绝"用今天的候选伪造历史刊期"。同日重跑
  被同日护栏拒绝（record 已存在）或自然继续（record 不存在）。论文修订版（v2/v3）
  不因修订重新入选。
- **去重**：主键为**去版本号的 arXiv ID**（`2401.12345v2` → `2401.12345`）。扫描
  `published-papers/` 全部历史记录（每天一个小 JSON，一年也只有几百个文件，无需窗口），
  已出现过的 ID 永久排除。无 arXiv ID 的候选（罕见）用归一化标题做键。

### 3.6 Semantic Scholar（可选增强，非阻塞）

作者 h-index 信号。公共接口无 key 会 429（已实测）。有 `S2_API_KEY` 环境变量时启用，
无 key 时该信号整体跳过，不重试、不报错。**默认不实现，留接口位**——HF organization
已覆盖大部分需求，这是唯一一处"预留"，其余一律按需实现。

## 4. 架构决策

### 4.1 独立子管线

- 新模块 `src/ai_daily/papers.py`（超 800 行则拆包）。
- 新 CLI 子命令 `ai-daily papers`（仅 `--mode {dry-run,publish}`，**没有 `--date`**，
  理由见 §3.5；全局 `--config-dir`/`--site-root` 照旧、必须在子命令之前）。
- **独立配置加载**：`config/papers.yaml` 由 papers 模块**单独的 `load_papers_config()`
  加载，绝不接进 `load_config`/`AppConfig`**——那条路径被 daily/verify/rebuild/probe
  全部命令共用且 extra=forbid，接进去等于让论文配置故障放倒日报。papers.yaml 缺失或
  非法只使 `ai-daily papers` 失败。
- 独立 systemd 单元 `ai-daily-papers.service` + `.timer`，安全加固块从
  `ai-daily.service` 原样照抄。**`TimeoutStartSec=3600`**（见 §4.5 时长核算，
  不照抄日报的 1800）。运行时间：北京 06:10（错开日报 04:20/05:05/07:00/08:30 四窗）。

### 4.2 发布事务与锁（关键修正）

事实：日报的 `_run` 在**整个采集→模型→发布流程期间**持有 `.publish.lock`，且锁是
非阻塞的（拿不到直接失败）。因此：

- papers 管线自己的流程（采集/深读）**不碰** `.publish.lock`；
- 只在最终发布一步需要它，拿不到时**重试：每 60 秒一次，最多 10 次**，仍失败则本次
  不发布（本次深读结果只存 artifacts，下次运行重做，去重保证幂等）；
- **事务顺序：完整 release 目录先建好，record 后落盘，最后激活**。具体：内存中构建
  `PapersPublication` → 取锁 → 调用扩展后的 `render_release` 变体，用
  `published/` + `published-papers/` 的磁盘记录**加上这份内存中的新 record**，把
  完整 release 目录（news + papers 全部页面）构建到 staging 路径——此步任何失败都
  中止，磁盘状态零变化 → 原子写 `published-papers/<date>.json` → 激活 symlink
  （原子 `os.replace`，照抄 `activate_release`）→ 放锁。record 写入和激活之间只剩
  一次 symlink 交换，若它仍失败：当场重试一次，再失败则退出非零让 systemd 记失败，
  次日 papers 运行全量重建自愈（明确接受这一最多一天的窗口，个人页可容忍）。
- **papers 触发的 release 重建需要一份 `DailyPublication`**（现有 `render_release`
  以它渲染站点首页并断言 marker）：取 `published/` 中最新一期；`published/` 为空
  （全新站点）时拒绝 papers 发布并明确报错——实践中不会发生。
- 同日重发布护栏：同一天已有 papers record 时拒绝覆盖（个人页无升级语义，比日报简单）。

### 4.3 数据模型与站点结构

- 独立记录 `PapersPublication`：自己的 `SCHEMA_VERSION = 1`、自己的完整性校验和
  （照抄 `publication.py` 的 `compute_marker`/`signed()`/加载校验模式）。**措辞注意：
  这是防意外损坏的校验和（checksum），不是防篡改签名**，日报的同名机制同理。
  **绝不改动 `DailyPublication`、`EditorialCategory` 等既有封闭类型。**
- 落盘 `{site_root}/published-papers/<date>.json`；`ops/backup.sh` 增加此目录。
- `render_release` 扩展：除现有产物外，从 `published-papers/` 全部记录额外渲染
  `papers/index.html`（最新一期 + 归档列表）、`papers/<date>/index.html`（近 30 期）、
  `papers/rss.xml`。日报发布与论文发布都走它，两边互不丢页面。
- Caddy 无需改动。

### 4.4 采集层改动（sources.py）

- `_fetch_arxiv`：类别目前硬编码在函数体内。**决定：给 `SourceConfig` 加可选字段
  `arxiv_categories: list[str] | None`**（validator 限定仅 kind=arxiv 可用，与现有
  `link_pattern`/`namespace` 的按 kind 校验模式一致），查询串由它构建；未设置时保持
  现状（日报的 arxiv-ai 源零改动）。papers 用自己的一条 source 条目（四个类别一条查询，
  arXiv API 支持 OR），`limit: 100`（kind 上限）。
- `_fetch_huggingface`：按 §3.1 的防御式取值把 `organization.name`、`githubRepo`、
  `githubStars`、`numComments`、作者名列表（拼逗号串）补进 `RawItem.metrics`。
  兼容外层/内层两种 payload 形态（现有代码已处理 `value.get("paper") or value`）。
- **papers 源的空结果语义**：现有 Collector 把"0 条"视为源失败——papers 复用此语义
  没问题（HF daily 榜单和 4 类 arXiv 每天不可能为 0，为 0 就是真故障）。
- 全文抓取：**版本解析规则是确定性的**——对入选论文批量调一次 arXiv API
  （`id_list=<id1>,<id2>,...`），从返回 entry 的 id 尾缀 `vN` 取最新版本号，抓
  `https://arxiv.org/html/<id>v<N>`（已实测可用）。该 API 调用同时补齐作者列表。
  papers 管线**自建 Collector/client**（日报的 collector 在模型阶段后已被 aclose，
  且两管线独立运行），复用 `PublicAsyncHTTPTransport`。
- **全文清洗（明确规格）**：
  - 保留章节文本与表格；表格逐行线性化为 `单元格A | 单元格B | ...` 文本，保住数字
    与表头的邻接关系，使 claim+quote 能引用到表格行；
  - 剥离导航、参考文献列表、作者页脚；
  - 超长截断（>60k 字符）按**节优先级从低到高砍**，优先级从高到低：
    experiments > method > limitation > abstract/intro > appendix > related work。
    即先砍 related work，再砍 appendix（从尾部起），依次向上，砍到 ≤60k 为止；
    若只剩 experiments+method 仍超限，从 method 尾部砍（experiments 永不砍）。
    无小节结构可辨时退化为"保头 50k + 保含最多数字的 10k"；
  - 无 HTML 版（2023 年底前的旧论文等）→ 降级简读卡。**不做 PDF 解析**（明确不做，
    复杂度不成比例；简读卡如实标注）。

### 4.5 LLM、预算与时长

- 深读用 **editor 档位**、分类用 **judge 档位**，不新增 `ModelRole`。
- **papers 自己的预算上限写在 papers.yaml**（不复用 models.yaml 的全局 ¥40）：
  初值 60 requests / 3M input / 0.6M output / **¥8 每天**。papers 管线自建
  `BudgetLedger`（独立文件 `{site_root}/budget/papers-<date>.json`）。
  **份额算术修正**：`STAGE_REQUEST_SHARE`/`STAGE_COST_SHARE` 目前是模块常量，
  papers 若沿用，深读（计 DRAFT）只能花到 ¥8×0.35=¥2.8。改法：给 `BudgetLedger`
  构造器加可选 `request_shares`/`cost_shares` 参数，缺省取现有常量（日报路径零改动、
  零行为变化），papers 传自己的分配 `{JUDGE: 0.2, PLAN: 0.0, DRAFT: 0.8}`
  （papers 不用 PLAN；两表各自 sum=1.00 的不变量保留）。**不改 `BudgetStage` 枚举。**
- **深读串行执行**（gateway 用默认并发 1）。原因：`ModelGateway` 的预检不做预算
  预留，并发调用会同时通过 `check_stage` 再各自记账，可能双双越过 ¥8 上限。
  预留机制（`BudgetLedger.reserve/settle`）存在但接进 gateway 是额外改动——串行
  更简单且时长可控，不值得。
- **时长控制不靠估算，靠硬截止**（TimeoutStartSec=3600 的真正依据）：
  - 单篇深读包 `asyncio.timeout(600)`（10 分钟，覆盖最坏的端点重试 × 语义重试链），
    超时该篇降简读卡；
  - 深读阶段全局 deadline 40 分钟：到点未完成的论文全部降简读卡（仍受 §3.4 后置门
    约束——降太多就不发布）；
  - 发布锁重试最多 10 次 × 60s = 10 分钟；
  - 采集 + 分类 + 渲染经验值 <8 分钟，且均有各自的 HTTP/模型超时兜底。
  由构造保证总时长 < 60 分钟，不依赖对重试组合的枚举。
- 成本估算：正常日 ¥1–2；最坏情况（全员重试 + fallback 模型接手）由 ¥8 熔断兜底。

### 4.6 引用校验（防幻觉核心）

- 复用 `normalize_quote_text`/`quote_supports`（≥12 字符、归一化 substring）。
  论文只有一份证据（清洗后全文），直接对它校验。
- **④ 实验段的输出 schema 是结构化列表**：`list[{claim: str, quote: str}]`，
  外加一段不含具体数字的简短总评。校验器：每个 quote 必须是全文子串；总评文本里
  出现 3 位以上连续数字或百分号即拒绝（数字只允许出现在 claim+quote 对里）。
  校验失败经 `gateway.generate(validator=...)` 触发 ModelRetry。
- ①②③⑤⑥⑦ 为解读性内容，不逐句强制引用（强制会把深读写成摘抄）。它们的错误风险
  由"模型只见全文、prompt 要求所有断言以全文为据、反注入句式照抄日报"约束。
  **明确承认：引用校验只覆盖④，其余段落的幻觉风险靠模型质量与人工抽查兜底。**
- prompt 保留日报的反注入句式（论文全文是不可信文本）。

### 4.7 渲染

- 新增 `src/ai_daily/render/papers.py`，复用转义器（`_t`/`_url`/`_x`）与整体风格。
- **页面外壳不复用 `_page`**——它硬编码了日报的 RSS 路径与站头。新写
  `_papers_page(...)`（或把 `_page` 参数化，二选一，倾向前者：少动共享代码），
  RSS alternate 指向 `papers/rss.xml`，站头加"日报 ⇄ 论文"互链；日报站头是否加
  论文入口链接由实施时顺手加上（一行改动，`_header` 加一个 `<a>`）。
- prefix：`/papers/` 用 `"../"`、`/papers/<date>/` 用 `"../../"`（file:// 可预览）。
- 折叠用原生 `<details>`，零 JS。CSS 追加组件段到 `static/site.css`。
- `papers/rss.xml` 照 `render_rss` 纯函数模式，guid = `{base}/papers/{date}/`。

## 5. 已知的坑（实施前必读）

1. `SourceConfig.kind` 封闭 Literal，分发靠 `getattr(self, f"_fetch_{kind}")`
   （sources.py:517）；`limit` 上限 100。
2. 源返回 0 条视为失败（`SourceCollectionError`）；非 community 源全部条目缺
   `published_at` 也视为失败。
3. `config/papers.yaml` 走独立加载器（§4.1），**不要**接 `load_config`/`AppConfig`。
4. `verifier.py` 不是引用校验器（它验证线上站点可见性）；引用纪律在
   `content.py:107-175`。
5. `render/site.py` 的 `_url()` 对非 http(s) raise；`_time_label` 对只有 community
   时间戳的条目 raise。论文记录 URL 一律 https。
6. `static/site.css` 按 `parents[2]` 解析后逐 release 拷贝，papers 页自动获得样式。
7. `.publish.lock` 被日报**全程**持有且非阻塞（cli.py:102 起）——papers 发布须按
   §4.2 重试。`_exclusive_lock`（site_publisher.py:108）已对路径泛化，可直接用于
   `papers.lock`。
8. `_daily` 在已有 L0 时短路返回——papers 绝不能挂在 `daily` 子命令里。
9. `gateway.ledger` 会被日报的 `_bind_daily_budget` 中途重绑（仅当 pipeline 自建
   gateway 时）。papers 自建 gateway + 显式传 ledger，测试注入假 gateway。
10. mypy strict + pyright + ruff（行宽 100）全过；`tests/factories.py` 以顶层
    `import factories` 导入（无 conftest.py）；asyncio_mode=auto。
11. `tests/test_workflows.py` 现状**没有**对 timer 单元的断言、
    `tests/test_budget_staging.py` 只核算日报窗口与日报账本——**不要**把 papers
    塞进它们的现有断言；为 papers 单元新增独立断言（见 §6）。
12. deepseek editor 档位的 reasoning token 计入 `max_output_tokens`（48000 因此而来）；
    深读输出预算按此理解。

## 6. 测试要求

沿用仓库惯例（MockTransport 注入、factories 最小构造、tmp_path 隔离、XSS 常量断言）。
必须覆盖：

- 漏斗：评分公式、前置门（主通道 <3 篇不发布）、后置门（深读成功 <2 或简读多于
  深读不发布）、补选豁免阈值但过地板、分类分批与单批失败隔离、交叉信号匹配规则。
- 采集：HF payload 两种形态与字段缺失、arXiv 类别配置化（含日报源不受影响）、
  全文清洗的表格线性化与节优先级截断、无 HTML 版降级。
- 引用校验：claim+quote 通过/拒绝、总评含数字被拒、quote 为表格行子串。
- 发布事务：渲染失败不落盘、锁被占时的重试与放弃、record 写入后 release 重建失败
  的下次自愈、同日重发布拒绝、去版本号 arXiv ID 去重、损坏的历史 record 报错不吞。
- 渲染：papers 页 RSS alternate 指向 papers/rss.xml、XSS 断言、prefix 正确性、
  简读卡标注。
- systemd：新增对 `ai-daily-papers.timer`/`.service` 的文本断言（仿现有风格）；
  papers 预算核算独立成测试，不动 `test_budget_staging.py` 的日报断言。

## 7. 实施里程碑（每步有验证门）

1. **数据通路 + 漏斗 + 记录模型**：papers.yaml 与独立加载器、arXiv 类别配置化、
   HF 字段增补、交叉信号、评分与阈值、LLM 相关性分类与补选（judge 调用，便宜）、
   `PapersPublication` 模型。**dry-run 的产物只写 papers 自己的 artifacts 目录
   （`{artifacts_dir}/{date}/papers-{run_id}/`），绝不写 `published-papers/`**——
   否则去重集合被 dry-run 污染、正式发布撞同日护栏。验证门：连跑 2–3 天 dry-run，
   输出当日 top 8 名单（含各信号分解）给用户对口味，校准阈值与机构名单。**此时的
   名单即最终系统的名单**（分类与补选已在环内），校准即真校准。
2. **深读生成 + 渲染 + 本地预览**：全文抓取清洗、深读 prompt、claim+quote 校验、
   渲染层、fixture 预览脚本（仿 render_fixture.py）。验证门：拿 2–3 篇用户熟悉的
   论文生成深读人工评估；整站预览。
3. **发布 + 定时器 + 部署**：发布事务（§4.2）、render_release 扩展、systemd 单元、
   backup.sh、DEPLOYMENT.md（目录树 + 定时表）。验证门：服务器真实出一期，curl 验证
   /papers/ 与 rss.xml，确认下一个日报窗口正常出刊。

## 8. 明确不做的事（防范围蔓延）

- 不做收藏/已读/个性化（V3；届时 localStorage，不上后端）。
- 不做日报 L0–L3 降级阶梯（papers 只有"出/不出/单篇简读卡"三态，见 §3.4 与 §4.2）。
- 不做周榜（数据积累两周后再设计）。
- 不做 PDF 解析、不做 HF 历史榜单回溯、不做独立 GitHub star 查询、不做 S2 集成
  （只留环境变量开关位）。
- 不改动日报的任何选择逻辑、预算份额、schema、现有测试断言。

## 附：Codex 评审记录（2026-09-01）

**第一轮 22 条**处置摘要：#1 发布事务顺序 → §4.2；#2 锁语义 → §4.2 重试 + 错峰；
#3 配置耦合 → §4.1 独立加载器；#4 无阈值 → §3.4；#5/#6 引用校验边界 → §4.6 结构化
claim+quote + 承认覆盖范围；#7/#8 全文截断与表格 → §4.4；#9 arXiv 配置 →
arxiv_categories 字段；#10 HF 字段 → 防御式取值（字段已实测存在）；#11 声誉偏差 →
§1 承认 + 权重可调；#12 交叉信号取数 → §3.2；#13 去重 → 全历史 + 去版本号；#14
新鲜度 → §3.5；#15 预算 → 独立上限 ¥8；#16 时长 → 3600s + 并发 2；#17 整体质量门；
#18 页面外壳 → _papers_page；#19 里程碑顺序 → M1 前移；#20/#21 测试 → §6；#22 措辞。

**第二轮 12 条**处置摘要：#1 事务原子性 → §4.2 改为"staging 全建好 → 写 record →
激活"，激活失败当场重试 + 非零退出 + 次日自愈（明确接受）；#2 dry-run 落盘位置 →
里程碑 1 限定 artifacts 目录；#3 补选阈值矛盾 → §3.4 双通道规则（补选豁免分数、
过地板、不计入最低篇数）；#4 深读后置门 → §3.4（成功 <2 或简读过半不发布）；#5
--date 伪造历史 → 删除该参数，papers 只做今天；#6 分类批次 → 每批 20 + ID 集合
校验；#7 时长含锁等待与重试 → §4.5 重算（~42 分钟）；#8 arXiv 版本 → API id_list
确定性解析；#9 截断矛盾 → 严格优先级序列 + 无结构退化规则；#10 render_release 的
DailyPublication 输入 → 取最新日报记录，为空拒绝发布；#11 预算份额 → BudgetLedger
构造器加可选 shares 参数；#12 交叉提及失真 → 封顶降为 2 + 明确接受为微调项。
**第三轮（终审）3 条阻塞项**处置：#1 §4.1 残留 `--date` 与 §3.5 矛盾 → 已删除；
#2 并发深读可绕过预算预检 → 改为串行执行（§4.5）；#3 最坏时长可能超 3600s →
改为硬截止设计（单篇 600s、全局 40 分钟 deadline，超时降简读卡，§4.5）。
三条均为机械性修正，已闭环。计划判定为可实施。
