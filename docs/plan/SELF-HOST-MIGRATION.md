# 甲鱼AI日报自建迁移实施计划（v3，已吸收 codex 审阅意见）

> 状态：codex 审阅完成（2026-08-13，43 条意见），本版已吸收；见文末审阅记录
> 北极星目标：**高质量、稳定的每日产出**——不过时、不编造、不停刊，并尽可能全面地收集 AI 信息。
> 对北极星的诚实注解：L3 兜底页**不算产出**，是被最小化的失败态；本计划的目标是把它的
> 发生率压到最低并保证它发生时可见、可恢复，而不是把「站点可访问」偷换成「有产出」。

## 0. 已确认决策与服务器事实

### 决策（用户已确认）

| 项 | 决定 |
|---|---|
| 子域名 | `daily.jiayutool.cn`（主域名不变） |
| 失败通知 | 不做推送渠道。明确后果：故障发现延迟 = 用户下次查看的间隔。以降级阶梯保「有产出」，以 `status.json` 提供自查；这是**接受的检测延迟**，不是可靠性手段 |
| 切换方式 | 不做长期双跑；保留 **2 天浸泡期**（新站正常自动跑、老站仍是权威版本），浸泡通过后一次性切换 |
| 备份仓库 | 新建私有仓库 `ai-daily-site-backup`。服务器持读写 deploy key（GitHub deploy key 无「只写」；密钥只绑该备份库，不触主仓库） |

### 服务器事实（2026-08-13 实测）

| 项 | 值 |
|---|---|
| 服务器 | 腾讯云新加坡 `101.32.114.8`，OpenCloudOS 9（RHEL 系，dnf），2C / 2G / 40G（余 30G） |
| SSH | `ssh -i ~/.ssh/singapore_rsa root@101.32.114.8`（已验证） |
| Caddy | `/usr/local/bin/caddy`，systemd 服务 active，`/etc/caddy/Caddyfile` 当前仅 `zb.jiayutool.cn` |
| 既有负载 | ResearchPilot LLM 网关（PM2）、燕麦账本反向隧道端点；内存已用 ~650Mi / 1.9Gi |
| Python | 系统 3.11.6 → uv 安装独立 3.13，不动系统 Python |
| 时区 | 服务器本地时区已是 CST（Asia/Shanghai），timer 直接用本地时间 |
| DNS | 阿里云万网；需新增 A 记录 `daily.jiayutool.cn → 101.32.114.8`（用户操作） |

### 目标架构

```
systemd timer（04:20 主跑；05:05 / 07:00 重试与升级窗口）
  → ai-daily run       # 采集/聚类/LLM/质量门 + 降级状态机（阶段 3）
  → 发布事务           # 见「发布事务与原子性」
  → Caddy file_server  # daily.jiayutool.cn，自动 HTTPS
发布成功后：published/ + artifacts 摘要 push 备份仓库（失败不阻塞发布，记录进 status）
```

砍掉：GitHub Issue 数据库、isite、Zola、even 主题、`sed` 注入、`main.py`、双部署竞态。
保留：`src/ai_daily` 流水线代码、**32 个源**的配置、预算控制、测试体系、签名 marker 机制（重定义见下）。

---

## 核心设计（v3 新增，回应 codex P0）

### 降级状态机（失败类别 → 级别的显式映射）

先明确健康信号：L3 的触发依据是**经过新鲜度过滤后的可用候选数**（post-freshness yield），
不是源覆盖率——覆盖率 100% 也可能全是旧闻。

| 失败类别 | 处置 |
|---|---|
| Tier-A 源覆盖不足（<0.6） | 不再直接停刊；记录进 status，继续流程 |
| 新鲜候选 < 17 但 ≥ 5 | L2b：确定性快讯刊（评分池 Top N：标题 + 来源 + 时间，零 LLM 文案） |
| 新鲜候选 < 5 | L3 |
| judge 部分批次失败 | 已完成批次结果**逐批持久化**（v3 改造），未判事件丢弃；产出走 L1/L2a |
| judge 全部失败 | L2b |
| 编辑规划（plan）失败 | L2a：judge 通过项按分数确定性排列成快讯刊 |
| 起草部分失败 | 该故事降为快讯，继续 → L1 |
| 详报证据审计不达标 | 先走既有 `_repair_detail_evidence` 交换；仍不达 → 降为快讯 → L1 |
| 预算耗尽 | 当前阶段截断，按已完成阶段就地降级（见预算模型） |
| 渲染失败 | L3（兜底页独立于渲染器，见下） |
| 发布/校验失败 | 本次退出，留待下一 timer 窗口重跑（发布事务可幂等重入） |

L1 使用**独立的宽松校验契约**：不复用 `validate_editorial_plan()` 的 lead/follow 配额下限，
`assemble` 对 L1 接受详报数少于计划数——不是对正常 plan 做变异后硬塞回严格校验器。

### L3 兜底（不依赖失败组件自身）

- 部署时（阶段 4）预构建一个静态兜底页 `fallback/index.html`（纯静态、不依赖流水线），连同首个占位 release 一起上线——**首次运行失败不会 404**。
- L3 的动作 = 把 `current` 指回上一个完好 release（若无则指 fallback）+ 更新横幅状态；这一步只有「写一个 JSON + 切软链」，不经过 Jinja2 渲染路径。
- `status.json` 写在 **releases 之外的固定路径**（`/www/wwwroot/ai-daily/status/status.json`），由 Caddy 单独路由供给，`Cache-Control: no-store`。渲染器挂掉不影响状态可见。

### 发布事务与原子性（固定顺序，幂等重入）

```
1. 流水线产出 digest + level（内存）
2. published/<date>.json 写 .tmp → fsync → rename        ← 提交点
3. 从 published/<date>.json 渲染 → releases/<date>-<ts>/
4. 本地校验：重算 sha256(canonical published JSON) 与页面 meta marker 一致
5. 软链原子切换（ln -sfn + rename 语义）
6. 公网校验（httpx 直连，无中间缓存；校验页面 200 + marker 重算一致 + RSS 首条为当日）
7. status.json 更新（含 level、源健康、预算消耗、备份状态）
8. 备份 push（失败不阻塞，记入 status）
```

- 任一步崩溃后重入：同日重跑从第 1 步开始，`published/<date>.json` 允许被**更高级别**覆盖（L2 → L0 升级重发），禁止降级覆盖。
- 校验返回 `{published, level}` 而非布尔值：05:05 / 07:00 窗口发现 level < L0 且预算尚余时，尝试升级重跑；level == L0 才零成本退出。
- marker 语义重定义：v4 marker = sha256(canonical `published/<date>.json`)，渲染进 HTML meta；校验方**重算**摘要比对，不是查字符串存在。
- RSS 校验保留现有强度：最新条目必须是当日、含当日 marker、链接正确（RSS 现在由我们自己从 `published/` 确定性生成，该校验成本低且保留了防错位保护）。

### 预算模型（¥5 是日上限，不是进程上限）

- 预算台账落盘：`budget/<date>.json`，同日**所有运行共享**（04:20 + 两个重试窗口累计不超 ¥5 / 40 请求）。
- 删除「每调用预留 2 槽」的预留机制（`MAX_MODEL_CONCURRENCY=1`，无并发竞争，预留无意义），改为调用前查剩余、超限即触发对应阶段降级。
- 分阶段子预算：judge 40% / plan 15% / draft 45%；某阶段耗尽 → 该阶段按状态机就地降级，不吞掉全天预算。

### 漏刊语义（服务器跨午夜宕机）

不做补刊：每次运行的目标日期 = 运行时刻的 Asia/Shanghai 当日。跨午夜宕机导致的缺期在
归档页显式标注「未出刊」，`status.json` 记录 gap。理由：新闻日报补刊价值低、引入回填
窗口漂移复杂度高（现有 `collection_window` 回填语义 bug 也一并废弃）。

---

## 阶段 0 — 前置盘点（1 天，先于一切实现，产出决定后续范围）

- [ ] **真实采集诊断**（不是 curl）：新增 `ai-daily probe-sources` 命令，用真实 Collector 跑全部 32 个源，报告每源「抓到条数 / 新鲜通过数 / 拒绝原因」。在服务器上运行，重点验证之前被 GitHub runner 封的量子位、Microsoft AI 能否恢复。
- [ ] 模型 API 实测：服务器直连 DashScope（国内/国际 endpoint 各测）与 DeepSeek，记录延迟与可用性。不通则回退方案：LLM 调用走同机 ResearchPilot 网关模式。
- [ ] **内存基线**：`systemd-run --scope -p MemoryMax=768M` 跑一次完整 dry-run，测峰值 RSS。超限则先在 Collector 加全局并发上限，再定 MemoryHigh/Max 数值——限额以实测为准，不拍脑袋。
- [ ] 腾讯云安全组确认 80/443 入站放行（Caddy 为新主机名签证书需要 HTTP challenge 可达）。
- [ ] DNS：阿里云添加 `daily.jiayutool.cn A 101.32.114.8`（用户操作；TTL 设 600 便于灾难恢复时重指）。
- [ ] 建用户 `ai-daily`（无 sudo）、装 uv、clone 仓库（只读 deploy key，主仓库无写权限）。

**验收**：连通性报告 + 内存基线数字 + 模型 API 实测结果三份产出；据此确认或修订后续阶段范围。

## 阶段 1 — 契约先行（2 天，渲染与流水线改造共同依赖，先做）

- [ ] `published/<date>.json` **带版本号的显式 schema**：按 level 判别的 discriminated union（L0/L1 含完整 digest + drafts + viewpoints；L2a/L2b 含条目列表；L3 只有状态）。现有 `RunArtifact` 不含 EditorialPlan/drafts，这是新模型，不是复用。
- [ ] 降级状态机落为代码可执行的映射表 + 单测（每个失败类别一条）。
- [ ] 发布事务的接口定义与幂等语义（含升级覆盖规则）。
- [ ] 预算台账落盘格式与分阶段子预算。
- [ ] v4 marker 定义与重算校验函数。
- [ ] L1 宽松校验契约（独立函数，不动 L0 的严格校验）。

**验收**：schema + 状态机 + 事务顺序有设计短文与类型定义，评审通过后阶段 2/3 才并行开工。

## 阶段 2 — 渲染层重建（3 天，依赖阶段 1 schema）

- [ ] `src/ai_daily/render/`：Jinja2 模板（base / index / daily / archive / rss / fallback），输入 = `published/<date>.json`，四个级别各有渲染分支；**全部输出走自动转义**（敌意源文本与模型文本不可信）。
- [ ] 开发数据：`tests/fixtures/editorial-preview.json` + 为 L1/L2a/L2b/L3 各造一份 fixture。
- [ ] 视觉：`/design-taste-frontend` 起稿 → `/design-review` 真浏览器验证；移动端优先。
- [ ] `history.py` 历史去重改读本地 `published/`（45 天窗口不变）。
- [ ] 新 CLI：`ai-daily render` / `ai-daily publish-local`（执行完整发布事务）/ `ai-daily verify`（返回 level）/ `ai-daily probe-sources`。
- [ ] RSS：feedgen 从 `published/` 生成；guid = 新域名 URL，稳定不变。
- [ ] 测试：四级别渲染快照 + HTML 转义（敌意输入 fixture）+ RSS 排序与 guid 稳定性。

## 阶段 3 — 流水线稳定性与质量改造（3 天，依赖阶段 1，与阶段 2 并行）

稳定性：
- [ ] **修 Collector 客户端 bug**（准确表述：传入 client 时丢失自定义 UA 与 pinned-DNS 连接一致性；`_validate_public_dns` 仍在跑，问题是校验时与连接时解析可能不一致 + UA 缺失被源站拒）。修法：Collector 拥有并关闭自己的 client，`cli.py` 不再注入。补生产构造路径回归测试。
- [ ] judge 结果**逐批持久化**（L2a 的数据前提）。
- [ ] 降级状态机接入 `pipeline.py`，`QualityGateFailed` 仅存在于 L3 边界。
- [ ] 预算台账 + 删除预留机制 + 分阶段子预算。
- [ ] 漏刊语义实现（目标日期规则 + 归档缺期标注）。

质量：
- [x] `evidence_quote` 覆盖**事实字段**（tldr / facts），claim 级 `{text, evidence_id, quote}`（`models.FactClaim`，与 `publication.Claim` 同形）；校验 = 归一化（空白全删 / 全角半角标点折叠）后子串匹配，quote ≥ 12 字符，且只在**所引 evidence_id 自身的 excerpt** 中查找，不跨证据搜索（`content.quote_supports` / `content.validate_evidence_quotes`）。`why_it_matters` / `action` / `caveat` 是解读性字段，**不加引用要求**——强制引用只会让模型粘贴无关句子；这些字段继续由现有推测正则把关。明确边界：**引用匹配证明文本有支撑，不证明逻辑蕴含**——模型仍可能引用真实句子却推出无根据结论。该机制只是收窄幻觉，不能消除幻觉，故现有正则防线全部保留，直到评测基线（tests/evals 扩展）证明引用机制拦截率 ≥ 正则后再分批退役。
- [x] lead 佐证校验器（本阶段实现，不是口号）：第一方来源（channel 为 `official` / `release`），或 ≥2 个不同可注册域（canonical 化后取 eTLD+1，显式多段公共后缀表覆盖 `.com.cn` / `.co.uk` / `.org.cn` 等，不引入新依赖）；同稿转载与聚合站转帖不算独立（`content.validate_lead_corroboration`，接入 `validate_editorial_plan`）。
- [ ] HF 仓库监控降级为佐证信号，删除其 4 处文案补丁。
- [ ] 源清单修正（基于阶段 0 报告）：恢复量子位、Microsoft AI；**新增候选仅**：机器之心、Meta AI Blog、xAI News（DeepMind/Mistral/TechCrunch AI/The Verge AI **已在配置中**）；Reddit r/LocalLLaMA 需要新适配器（`SourceConfig.kind` 目前不支持），单列为独立任务，首批不做。
- [ ] 跨源 enrichment 白名单：**这是 SSRF 安全模型变更**，不是配置开关——单独设计（白名单域、重定向策略、来源归属标注），阶段 5 实施，首批不放开。
- [ ] 覆盖率可度量：每周一次漏报审计——抽样机器之心/量子位当日选题 N 条，核对候选池命中率，结果写入 status；这是「尽可能全面」的验收方式。

## 阶段 4 — 服务器部署（2 天，依赖阶段 2/3）

- [ ] systemd 完整规格（不是示意）：
  - `ai-daily.service`：`Type=oneshot`、`User=ai-daily`、`WorkingDirectory=/www/wwwroot/ai-daily/app`、`ExecStart=<uv绝对路径> run ai-daily run --mode publish`、`TimeoutStartSec=1800`、`EnvironmentFile=/etc/ai-daily/env`（0600）；内存限额取阶段 0 实测值。
  - 加固：`NoNewPrivileges=yes`、`ProtectSystem=strict` + `ReadWritePaths=`（仅数据目录）、`ProtectHome=yes`、`PrivateTmp=yes`、`PrivateDevices=yes`、`RestrictAddressFamilies=AF_INET AF_INET6`、`CapabilityBoundingSet=`——这个进程解析敌意网页内容，同机还有别人的密钥。
  - `ai-daily.timer`：`OnCalendar=*-*-* 04:20|05:05|07:00`（服务器本地即 CST）、`Persistent=true`；同名 service 天然不并发，另加 `flock` 数据目录锁防手动运行与 timer 重叠、防部署期间运行。
- [ ] 部署流程（不是裸 `git pull`）：`deploy.sh` = 取 flock → `git fetch && git checkout <pinned tag>` → `uv sync --frozen` → 配置校验（`AppConfig` 加载）→ 释放锁；回滚 = checkout 上一 tag 重跑。
- [ ] Caddy：
  ```caddyfile
  daily.jiayutool.cn {
      root * /www/wwwroot/ai-daily/current
      file_server
      encode zstd gzip
      header Cache-Control "public, max-age=300"
      handle /status.json {
          root * /www/wwwroot/ai-daily/status
          header Cache-Control "no-store"
          file_server
      }
  }
  ```
  改配置前 `caddy validate`；确认签证书成功（依赖阶段 0 的 80/443 放行）。
- [ ] 预构建 fallback 页 + 首个占位 release 上线（首刊失败不 404）。
- [ ] 磁盘保留策略全覆盖：releases 保留 30 个；`artifacts/` 90 天；日志走 journald + `SystemMaxUse=200M`；uv cache 月度 prune；`published/`（小 JSON）永久保留。
- [ ] 备份与恢复：
  - 每日发布后 push `published/` + `digest.md` + `run.json` 摘要至备份仓库；push 失败不阻塞发布、写入 status；提交身份固定为 bot 名。
  - **恢复 runbook 成文**：新机器从零到重新出刊的完整步骤（装 uv → clone 两仓库 → 恢复 env 密钥 → render → Caddy → DNS 重指，含 TTL 600 的生效预期）。
  - **恢复演练一次**：在 Mac 上按 runbook 从备份仓库实际恢复出可浏览站点，作为本阶段验收项。

**验收**：手动触发全流程走通；故障注入（断源 / 模型 mock 失败 / 渲染抛异常 / 断电模拟 kill -9 于事务各步骤间）下各级别行为符合状态机；恢复演练完成。

## 阶段 5 — 浸泡与切换（0.5 天 + 2 天浸泡）

- [ ] 浸泡期 2 天：服务器每日自动跑（真实发布到 daily.jiayutool.cn），老站照常、仍是权威版本。每天人工对比两边选题与文案。模型费双倍（各自 ¥5 内）。
- [ ] 浸泡通过标准：连续 2 天自动产出 L0/L1，无人工干预。
- [ ] 切换日：
  - 禁用 `daily.yml` cron（不删文件，保留 30 天）；
  - **老站保留为只读存档，不下线**——既有 issue URL、外链、搜索收录全部不断链；仅首页顶部加迁移横幅指向新域名；
  - 旧 RSS 推送最后一条迁移公告。明确接受：RSS 订阅者需手动换源，GitHub Pages 无法 301，新 feed guid 全新（不迁移旧条目，避免重复推送）；
  - README、仓库描述更新。
- [ ] 回滚 runbook（不只是一句话）：重新启用 cron + 移除老站横幅 + 验证老链路依赖（isite/Zola 版本 pin 仍可用）+ 公告回滚。切换前把该 runbook 走查一遍。
- [ ] 观察期 2 周：每日自查 `status.json`，每周复盘 artifacts 与降级触发记录。

## 阶段 6 — 质量迭代（切换后持续）

- [ ] 引用校验评测基线达标后，分批退役事后正则。
- [ ] 跨源 enrichment 白名单设计评审后实施。
- [ ] Reddit 适配器（新 `SourceConfig.kind`）。
- [ ] 每周漏报审计例行化；每月 `benchmark-models`。

---

## 测试清单（阶段 2–4 分摊，来自 codex #43，全部纳入）

- 发布事务每个步骤间崩溃的重入测试；软链切换与回滚原子性
- 并发/手动运行与 timer 的锁互斥
- 同日 L3→L2→L1→L0 升级、禁止降级覆盖
- 降级发布的校验通过 vs 升级资格判断
- 首次运行无任何历史 release（fallback 生效）
- 跨午夜宕机后的目标日期与缺期标注
- 同日三次运行的累计成本不超 ¥5
- `published/*.json` 损坏/截断的处理
- 备份 push 失败不阻塞发布
- 按 runbook 从备份全新恢复（演练）
- HTML 自动转义（敌意源文本/模型文本 fixture）
- RSS 排序、guid 稳定性、迁移行为
- evidence_quote 蕴含性评测（evals，不只单测子串）
- 真实 cgroup 内存限额与超时故障测试

## 时间汇总（v3，按 codex 修正）

| 阶段 | 工作量 | 依赖 |
|---|---|---|
| 0 前置盘点 | 1 天 | SSH、DNS 权限 |
| 1 契约先行 | 2 天 | 0 |
| 2 渲染层 | 3 天 | 1 |
| 3 流水线改造 | 3 天 | 1（与 2 并行）|
| 4 服务器部署 | 2 天（含恢复演练） | 2、3 |
| 5 浸泡与切换 | 0.5 天 + 2 天浸泡 | 4 |
| 6 质量迭代 | 持续 | 5 |

合计约 **11–12 个专注工作日** + 2 天浸泡 + 2 周观察。
（v2 估的 5–6 天不成立：它漏掉了失败语义、事务模型、迁移兼容与故障测试这些最硬的部分。）

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 新加坡 IP 被个别国内源封禁 | 阶段 0 用真实 Collector 实测；个别源不可达保留替代源 |
| DashScope/DeepSeek 新加坡访问受限 | 阶段 0 实测；回退 = LLM 经同机 ResearchPilot 网关 |
| 2G 内存与同机服务争抢 | 阶段 0 实测峰值后定限额；Collector 全局并发上限 |
| 无推送通知 | 已明确为「接受的检测延迟」；降级阶梯保产出下限，L3 兜底页本身可见 |
| 服务器单机故障 | 每日备份 push + 成文 runbook + 已演练的恢复路径 |
| 解析敌意内容与同机密钥共存 | systemd 加固（最小文件系统/能力面）+ 专用用户 |
| 切换后老链接断裂 | 老站只读存档永不下线，URL 不断链 |

---

## 审阅记录

- 2026-08-13 codex consult 审阅（session `019ffb47…`，43 条意见，tokens 1,245,663）。
- 处置：P0 全部吸收（L3 独立兜底、降级状态机显式化、published schema、发布事务、日预算台账、漏刊语义、升级重发）；P1/部署/质量/切换意见吸收；时间估算按 10–16 天区间收敛为 11–12 天专注工作量。
- 未采纳/修正说明：#19 Caddy file_server 无服务端缓存，风险面仅客户端缓存，仍按建议对 status.json 设 no-store 并让校验器直连；#2 不改北极星目标本身，改为在文首诚实标注 L3 的语义。
