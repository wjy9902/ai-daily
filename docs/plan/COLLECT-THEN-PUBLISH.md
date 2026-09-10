# 采集与出刊分离：全天采集，06:30 一次出刊

> 状态：v2 —— 已通过 Codex 独立评审一轮（17 条问题，全部消化，见 §13 评审记录），
> 2026-09-10 按 §9 实施完成（条目库 `item_store.py`、`collect` / `publish-artifact` 子命令、
> 入口与守卫、systemd 单元、部署脚本）。上线按 §9.6 分两步走。
> 目标读者：实施者（可能是全新的 session）。本文档不假设读者读过任何前期讨论，
> 现状证据、决策理由和已知的坑都写在这里。

## 1. 要解决的问题

现在 `ai-daily.timer` 有四个窗口（04:20 / 05:05 / 07:00 / 08:30），每个窗口跑的是同一个
`ai-daily daily`：先查今天有没有已上线的 L0 完整刊，有就零成本退出；没有就整套流程重跑一遍
（采集 → 新鲜度 → 聚类 → 判定 → 规划 → 起草 → 发布），再由 `guard_same_day_overwrite`
决定能不能替换已上线的刊。

设计初衷（`docs/plan/SELF-HOST-MIGRATION.md`）是"04:20 主跑，后面三个是重试和升级窗口，
刊已完整时不花钱"。这个前提在实际运行中不成立：**L0 几乎达不到**（头条要求第一方来源或两个
独立注册域佐证，最近三天全是 L1），于是每个窗口都是一次完整的花钱运行。

近三天实况（来源：`budget/<date>.json`、`artifacts/<date>/*/publication.json`、journal）：

| 日期 | 完整运行 | 上线 | 被守卫拒绝 | 花费 | 模型请求 |
|---|---|---|---|---|---|
| 09-08 | 4 | 2 | 2 | ¥4.57 | 70 |
| 09-09 | 4 | 3 | 1 | ¥4.90 | 80 |
| 09-10 | 5 | 3 | 2 | ¥5.99 | 101 |

三个后果：

1. **采集没有积累。** 每个窗口重新拉一遍 feed，跑完就扔；`artifacts/<run>/sources.json`
   虽然留着，下一轮不读。IT之家全站 RSS 只覆盖最近 4–7 小时，前一天下午的稿子到凌晨已经
   不在 feed 里，早上跑几个窗口都抓不到。2026-09-08/09 的 DeepSeek V4.1 Flash 内测与
   发布计划连漏两天，根因就是这个，不是选题。
2. **出刊次数是采集窗口数的副产品。** 读者早上看到的一期在 04:20 到 08:30 之间平均换两三次
   内容。为收拾这个局面才有了升级守卫；守卫先比"头条是否完好"再比条数，不看选题本身：
   09-10 收录了 DeepSeek 的新刊因为详报 9 对 10 被拒。
3. **成本按窗口数倍增。** 一轮模型花费约 ¥1.2–1.5，一天真正需要的只有一轮。

## 2. 目标

- 采集和出刊是两件事，各有自己的定时器。采集全天进行、不调模型、结果沉淀；出刊每天
  **06:30（Asia/Shanghai）一次**，读全天沉淀的条目，调一次模型，发一次。
- 一期内容上线后不再被同日重跑替换，除非级别升级到 L1 以上（见 §6）或运维显式要求。
- 采集覆盖面 ≥ 现在四个窗口之和：任何在 feed 里停留过 ≥ 3 小时的条目都会进库。
- 常态日只调一轮模型：花费 ¥1.5 上下、请求 25 左右。这是验收指标，不是预算测试能证明
  的东西（§8）。

不做的事：不改变新鲜度规则（36 小时窗口、可验证发布时间、社区条目的时间语义）、不改变
选题逻辑、不改变渲染和发布事务的顺序、不引入新依赖（条目库用标准库 `sqlite3`）。

## 3. 目标流程

```
00:10 03:10 06:00 09:10 12:10 15:10 18:10 21:10   ai-daily collect   只采集，写条目库，不调模型
06:30                                              ai-daily daily     条目库 ∪ 实时采集（也写库）→ 现有流程 → 发布
07:30                                              ai-daily daily     仅当今天没有刊，或只有 L2A/L2B 快讯刊时重跑
07:45                                              ai-daily papers    从 06:10 移到日报之后（§7）
```

时间点理由：美国工作日的新闻在北京时间 05:00 前后落定，中国当天的早间新闻 08:30 之后才
开始；06:30 两头都不吃亏，且早于读者的早间阅读时段。06:00 这一轮保留，出刊是否执行
不能影响采集的连续性（评审 #2）。

## 4. 条目库（item store）

### 4.1 位置与形态

`<site_root>/items.sqlite`，WAL 模式，标准库 `sqlite3`，`busy_timeout` 5 秒。与 `published/`
同级；丢了只损失最近几天的沉淀，不损失刊期记录，`ops/backup.sh` 不带它。

### 4.2 表结构

```sql
CREATE TABLE items (
    source         TEXT NOT NULL,     -- SourceConfig.name
    source_item_id TEXT NOT NULL,     -- RawItem.source_item_id：采集器认为"是同一条"的键
    canonical_url  TEXT NOT NULL,     -- normalize.canonicalize_url(item.url)，只做索引
    payload        TEXT NOT NULL,     -- RawItem.model_dump_json()
    published_at   TEXT,              -- 与 payload.published_at 同值（写入时由 payload 派生）
    first_seen     TEXT NOT NULL,     -- 首次入库时间，ISO-8601 UTC
    last_seen      TEXT NOT NULL,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, source_item_id)
);
CREATE INDEX items_canonical_url ON items (canonical_url);
CREATE INDEX items_published_at ON items (published_at);
CREATE INDEX items_first_seen ON items (first_seen);

CREATE TABLE rounds (
    round_id    TEXT PRIMARY KEY,     -- <date>-<8 hex>
    kind        TEXT NOT NULL,        -- 'collect' | 'publish'
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    ok_sources  INTEGER NOT NULL,
    failed_sources INTEGER NOT NULL,
    health      TEXT NOT NULL         -- list[SourceHealth] JSON
);
```

主键是 `(source, source_item_id)` 而不是 URL（评审 #8）：Hugging Face change-watch 用
`模型ID@sha/时间` 做 `source_item_id` 区分同一仓库页面上的不同更新事件，URL 不变；按 URL
存会把新更新压进旧行。同一 URL 来自两个源（官方账号原帖与官方站点）在现有聚类里是两个
item，佐证计数和证据包都区分它们，这里也自然是两行。

### 4.3 写入规则（逐字段合并，评审 #4）

新 `(source, source_item_id)`：插入，`first_seen = last_seen = now`。已存在：`last_seen = now`，
`seen_count += 1`，payload 逐字段合并，**没有"谁优先"，只有"谁更完整"**：

| 字段 | 规则 |
|---|---|
| `published_at` | 旧值非空则保留旧值；旧值为空、新值非空则取新值。来源报告的发布时间不应漂移 |
| `summary` | 取更长的 |
| `title` | 取新值（标题纠错以最新为准） |
| `metrics` | 取新值（热度会变） |
| `discovered_at` | 取旧值。**它是来源自己的时间语义**，HN 写的是帖子提交时间（`sources.py` `_fetch_hackernews`），不是抓取时间，新鲜度规则对社区条目正是用它；改写会让三天前的帖子"变新"（评审 #3） |
| `source_tier/channel/region/ai_focused/label` | 存库时保留，**读出时用当前配置覆盖**（§4.4） |

SQL 列 `published_at` 每次都从合并后的 payload 重新派生，保证两者一致。实时采集与库合并
时用同一条规则，不存在"实时 payload 优先"。

### 4.4 读出规则（评审 #11、#15）

出刊运行用 `collection_window()` 算出的同一对 `(cutoff, run_time)` 查询：
`published_at >= cutoff`，或 `published_at IS NULL AND discovered_at >= cutoff`
（`discovered_at` 存在 payload 里，按 JSON 取；行数在千级，全表扫描可接受）。读出后：

- 丢弃 `source` 不在当前配置启用列表里的行：配置撤销的信任不能从缓存里回来。
- 用当前 `SourceConfig` 覆盖 `source_tier / source_channel / source_region /
  source_ai_focused / source_label`（`sources._source_fields`），旧行不得带旧身份。
- `metrics["first_seen"]` 写入读出的条目，供事后追溯。

之后与实时采集结果按 §4.3 规则合并，进入 `filter_fresh_items` 及所有后续阶段，一行不改。

### 4.5 保留

每次 `collect` 结束删除 `last_seen` 早于 7 天且 `published_at`（空则看 `discovered_at`）早于
7 天的行；`rounds` 保留 30 天。库文件预期 10 MB 量级。

### 4.6 故障（评审 #7）

不做自动重建。sqlite 抛任何错误：`collect` 整轮失败，非零退出，journal 报错，
`status/collect.json` 不更新（于是 `age` 会变大，见 §5.4）；`daily` 记录
`store_error` 到 status 与 `sources.json`，退化为只用实时采集继续出刊，不因此停刊。
重建是人工动作：停 collect timer，改名 `items.sqlite*`（含 `-wal`、`-shm`），下一轮
`collect` 建空库。

## 5. 命令与运行

### 5.1 `ai-daily collect`

- 持 `.collect.lock`（新锁，`ops/deploy.sh` 也要拿它，评审 #6），`Collector.collect(sources)`
  → §4.3 upsert → 写 `rounds` → 写 `status/collect.json`（`finished_at`、`ok_sources`、
  `failed_sources`、库内条目数、窗内条目数）→ journal 一行 JSON。
- **部分结果也提交**：`Collector.collect` 是逐源 `gather`，单源失败已被 `_collect_one` 吞成
  `failed` health，不会拖累其它源；只有整轮抛错（sqlite、超时被杀）才无提交。
  **全部源失败**（`ok_sources == 0`）非零退出，让 systemd 记为 failed（评审 #10）。
- 不调模型、不读预算台账、不碰 `.daily-run.lock`。
- 单独的 `ai-daily-collect.service` + `.timer`，复制 `ai-daily.service` 的全部加固段，
  `TimeoutStartSec=900`（源超时上限 60 秒 × 并发受限，实测一轮 1–2 分钟）。
- 频率 3 小时，8 轮/天（§3）。依据：IT之家 feed 深度 4–7 小时，是所有源里最浅的；X 源经
  自建 RSSHub（缓存 600 秒）带小号 token，8 轮 × 31 个 X 源 = 248 次/天，比四窗口的 124 次
  多一倍，仍远低于风控线；公众号 feed 来自本机 we-mp-rss，无外部请求。

### 5.2 `ai-daily daily`（出刊）

采集这一步：先做现有的实时采集，把结果按 §4.3 写库（`rounds.kind = 'publish'`），再按 §4.4
读库合并；之后所有阶段不变。`artifacts/<run>/sources.json` 增加 `store` 段：读出条数、
合并后条数、库最近一轮 `finished_at`、`store_error`。

**源健康判定分成两件事**（评审 #12）：`_check_source_health` 现在只看本轮实时请求，
库里有完整证据时一次实时故障也会记 `SOURCE_COVERAGE_LOW`。改为：连接健康仍按本轮
health 记录进 `sources.json`，但 `SOURCE_COVERAGE_LOW` 按**合并后条目**里 Tier A 源的
出现比例计算。

`_daily` 入口的语义（评审 #1、#5）：

1. 今天没有刊期记录 → 完整运行并发布。
2. 有记录（**任何级别**）→ 先校验页面可见（`verify_publication`）。不可见 → `rebuild-site`
   后再校验一次，仍不可见则非零退出并写 status。这修复的是 `publish_site` 先写记录再切
   `current` 之间被杀的情况；现在只对 L0 做这件事，L1/L2 被杀后会重跑花钱再被拒，站点
   永远不恢复。
3. 可见且级别为 L1 或 L0 → noop。**L1 不重跑**：L1 是编辑环节成功、证据不足的刊，重跑大概率
   还是 L1 然后被拒，只是白花一轮。
4. 可见且级别为 L2A / L2B → 完整运行；发布时只允许升到 L1 或 L0（§6）。

于是常态日只有 06:30 一轮模型调用；07:30 只在 06:30 崩溃、超时或模型环节整体失败时
才花钱。

### 5.3 运维显式重出（评审 #14）

`ai-daily publish-artifact <artifacts/<date>/<run>/publication.json> [--replace]`：发布一个已
建好的刊，不重新采集和起草，与论文页的 `--publish-artifact` 对等。没有 `--replace` 时走
正常守卫；带 `--replace` 时跳过同日守卫，在发布锁内先把旧记录复制到
`published/<date>.replaced-<ts>.json`（`published_dates()` 只认 `YYYY-MM-DD.json`，不会当
刊期），备份失败则中止。journal 记 `{"action": "replace", "previous_marker": …}`。
`daily`（timer 入口）不接受这些参数。09-10 想重出新刊而不得，这就是当时缺的口子。

### 5.4 状态与监控（评审 #16）

- `_run` 开始时先写一次 `status.json`：`action = "running"`、`run_id`、`started_at`；被杀掉
  时留下的是"running 且很旧"，而不是上一轮的成功。
- `status/collect.json` 带 `finished_at`；`daily` 把最近一轮的 `age_hours` 写进 status，
  超过 4 小时记 `collect_stale = true`。
- 第 2 步的校验失败路径（HTTP 错误、网络异常）统一按不可见处理，不只捕获
  `PublicationNotVisible`。

## 6. 升级守卫收窄（评审 #13）

`guard_same_day_overwrite` 只保留级别比较：`is_upgrade(existing.level, new.level)` 才放行。
删除 `_carries_more` 与 `_coverage`。

明确接受的损失：`_coverage` 先比"头条是否因缺佐证被降级"再比条数，它能让 09-04 那种
"04:20 头条降级、07:00 头条补齐、级别都是 L1"的情况自动替换。一天一次出刊后，这种情况
只会出现在 07:30 的重试里，而 §5.2 已经规定 L1 不重试，所以这个能力没有触发点，删除是
一致的。三个原回归测试（引用 2026-09-01 与 09-04）改写成新策略的断言：同级别 L1 重跑
被拒；L2A → L1 放行；头条补齐但级别相同也被拒（写明这是有意为之）。

## 7. 论文页（评审 #9）

`ai-daily-papers.timer` 从 06:10 移到 07:45。理由：论文选片用日报**最近一次 run** 的
`sources.json` 做交叉信号（`PAPERS.md` §3），06:10 起跑读到的会是昨天的；07:45 在 06:30
出刊和 07:30 重试之后，读到的是当天的。论文运行最长 65 分钟，08:50 前结束，不影响任何
定时。

已知但本方案不修的并发缺陷：`publish_papers` 在拿发布锁**之前**读最新日报，等锁期间日报
若发布成功，论文仍用旧日报渲染首页（`site_publisher.py` `publish_papers`）。新时间表下
07:45 起跑时日报早已发完，这个交错不会发生；仍应作为独立问题记入 `ops/DEPLOYMENT.md`
待办。

## 8. 预算与测试契约（评审 #17）

- `tests/test_budget_staging.py::test_every_publication_window_can_judge_a_full_pool`
  从 timer 读窗口数，改后按 2 个窗口计算，通过。它证明的是"两个窗口的判定份额够"，
  不证明每日一轮；每日一轮由 §5.2 的入口规则保证，用 CLI 测试覆盖：今天已有 L1 →
  `daily` 不创建 `DailyPipeline`。
- `STAGE_REQUEST_SHARE` / `STAGE_COST_SHARE` 不改。省下的预算这一版不重新分配。
- 新增契约测试：`ai-daily-collect.timer` 的 `OnCalendar` 有 8 个且都在 `:00`/`:10`；
  `ai-daily.timer` 恰好两个窗口且第一个是 06:30；`ai-daily-papers.timer` 晚于第二个窗口。

## 9. 实施里程碑

1. **条目库**：`src/ai_daily/item_store.py`（open / merge_many / read_window / prune /
   record_round），`ai-daily collect` 子命令，`.collect.lock`。测试：§4.3 每条合并规则、
   `first_seen` 不可变、`discovered_at` 不被改写、主键按 `source_item_id` 区分 HF 更新事件、
   读出时按当前配置过滤并覆盖身份字段、保留策略、sqlite 错误让 collect 非零退出、
   全源失败非零退出。
2. **出刊读库**：`DailyPipeline.run` 写库 + 读库合并、`sources.json` 的 `store` 段、
   `SOURCE_COVERAGE_LOW` 按合并后条目计算。测试：只在库里的条目进入候选；同键合并遵守
   §4.3；库为空或报错时行为与今天完全一致且 status 带 `store_error`；实时全失败但库有
   Tier A 证据时不记覆盖不足。
3. **入口、守卫、重出**：`_daily` 四步语义、`publish-artifact --replace`、守卫收窄、
   status `running` 标记、`collect_stale`。测试：L1 存在时 noop 且不构造 pipeline；记录存在
   页面不可见时 rebuild 并再校验；L2A 重跑升到 L1 放行；同级被拒；`--replace` 备份成功才
   覆盖、`replaced-*.json` 不被当作刊期。
4. **部署链路**（评审 #6）：`ops/deploy.sh` 增加第三把锁 `.collect.lock`，并在同步依赖后
   把 `ops/systemd/*.service|*.timer` 复制到 `/etc/systemd/system/` 并 `daemon-reload`
   （需要 sudo 的部分拆成 `ops/install-units.sh`，deploy 脚本检测到 unit 内容与已安装的
   不一致时以非零退出提示运行它）。回滚步骤写进 `docs/OPERATIONS.md`：
   `deploy.sh --rollback` → `install-units.sh`（旧仓库里的四窗口 timer 会被装回去）→
   `systemctl disable --now ai-daily-collect.timer`。
5. **文档**：`ops/DEPLOYMENT.md`、`docs/OPERATIONS.md`（第 75–77 行的窗口说明）、
   `README.md` 第 47 行、`ops/DEPLOYMENT.md` 待办里加论文并发缺陷。
6. **上线顺序**（评审 #17）：先只部署代码和 collect timer，`ai-daily.timer` **保持四窗口**
   （新入口规则对四窗口同样成立：04:20 出 L1 后其余三个窗口 noop，本身就是省钱）；
   连续采集 ≥ 36 小时确认库覆盖了一个完整窗口后，再把 timer 切成 06:30 / 07:30，论文
   切 07:45；之后用橘鸦当天一期做逐条覆盖对比，观察一周花费。

## 10. 风险与取舍

- **一天只有一次机会。** 06:30 失败只剩 07:30 一次重试，两次都失败就是 L3 保留昨天的刊。
  现在四个窗口也是同一天预算内的重试，区别不大；换来的是刊期稳定和花费降 70%。
- **条目库让"新鲜"变宽的误解。** 一条 09-08 下午的 IT之家稿，09-10 06:30 出刊时距发布
  38 小时，仍会被 36 小时窗口拒绝——窗口规则不变。库只是补上 feed 深度不够的那部分。
- **采集频率与源风控。** 若 RSSHub X 源开始限流（表现是 `probe-sources` 报 ok 但
  `in_window` 归零，见 `ops/FEED-INFRA.md`），先把 collect 降到 4 小时。
- **旧刊不再被更好的同级刊替换。** 这是 §6 明确接受的损失；需要时用 `publish-artifact
  --replace` 人工处理。

## 11. 未决问题

- 采集器要不要也跑 `_enrich_rss_dates` 的回源取日期？现在 `Collector.collect` 已包含，
  沿用即可；每轮多一些同源文章请求。
- `ops/install-units.sh` 需要 root；是否接受部署分成两步（deploy.sh 由 ai-daily 用户跑，
  units 由 root 装），还是给 ai-daily 用户一条 sudoers 白名单。倾向前者。

## 12. 与 v1 的差异

v1 认为 `discovered_at` 应改写为 `first_seen`、实时 payload 优先、L1 也重跑、sqlite 损坏
自动重建、论文定时不动、部署只需 `daemon-reload`。这些在评审里都被证明与源码或目标
矛盾，v2 已改。

## 13. 评审记录

Codex（gpt-6-astra，2026-09-10）对 v1 的 17 条问题及处置：

| # | 问题 | 处置 |
|---|---|---|
| 1 | L1 常态下 07:30 仍完整重跑再被拒，日成本是两轮 | 采纳：L1 不重跑，只有无刊或 L2 才重跑（§5.2） |
| 2 | 03:10→09:10 六小时采集缺口；出刊实时结果不落库 | 采纳：保留 06:00 轮；出刊运行也写库（§3、§5.2） |
| 3 | `discovered_at = first_seen` 改写 HN 提交时间语义 | 采纳：`discovered_at` 不改写，`first_seen` 另存（§4.3） |
| 4 | upsert 丢失有效字段；"实时优先"绕过保全 | 采纳：逐字段合并，无优先级（§4.3） |
| 5 | 守卫收窄后 L1/L2 发布中断无法恢复 | 采纳：任何级别都先校验可见，不可见即 rebuild（§5.2） |
| 6 | deploy.sh 不装 unit、回滚不还原 timer、采集不拿部署锁 | 采纳：`.collect.lock`、`install-units.sh`、回滚步骤（§9.4） |
| 7 | sqlite 故障协议不完整，读路径自行清库 | 采纳：不自动重建，collect 失败即退出，daily 退化（§4.6） |
| 8 | 主键按 URL 会压掉 HF 更新事件 | 采纳：主键 `(source, source_item_id)`（§4.2） |
| 9 | 论文"没有差别"判断错误；存在锁前读日报的竞争 | 采纳：论文移到 07:45；竞争记为独立待办（§7） |
| 10 | 采集整轮超时丢结果；全失败仍像成功 | 采纳：说明逐源隔离；全失败非零退出；超时放宽（§5.1） |
| 11 | 库绕开来源配置变更 | 采纳：读出时按启用列表过滤并覆盖身份字段（§4.4） |
| 12 | 源健康只看实时，库有证据也记覆盖不足 | 采纳：覆盖按合并后条目算（§5.2） |
| 13 | "只数条数"与源码不符；删守卫恢复 09-04 行为 | 采纳：改正描述，明确接受损失，测试改写而非删除（§6） |
| 14 | `--replace` 仍要重跑花钱；缺日报的 publish-artifact | 采纳：改为 `publish-artifact --replace`（§5.3） |
| 15 | 库查询窗口与 `collection_window` 不同源 | 采纳：复用同一对 cutoff/run_time（§4.4） |
| 16 | status 在结束时才写，被杀留下旧成功状态 | 采纳：开始时写 `running`；`collect_stale`（§5.4） |
| 17 | 验收避开核心风险；首刊不能代表积累效果 | 采纳：先采集满一个窗口再切 timer；补测试（§9.6） |

Codex 给出的严重度前五：#5、#3、#4、#2、#6，v2 全部覆盖。
