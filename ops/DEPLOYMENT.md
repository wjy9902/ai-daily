# 部署现状

> 最后更新：2026-08-27。恢复步骤见 [`RESTORE.md`](RESTORE.md)，设计见
> [`../docs/plan/SELF-HOST-MIGRATION.md`](../docs/plan/SELF-HOST-MIGRATION.md)。

## 线上

| 项 | 值 |
|---|---|
| 站点 | https://daily.jiayutool.cn （HTTPS 自动签发，证书验证通过） |
| 服务器 | 腾讯云新加坡 `101.32.114.8`，OpenCloudOS 9 |
| DNS | 阿里云 A 记录 `daily → 101.32.114.8`，TTL 600 秒 |
| 站点根 | `/www/wwwroot/ai-daily` |
| 运行用户 | `ai-daily`（system 用户，nologin，无 sudo） |
| 定时 | 日报 04:20 / 05:05 / 07:00 / 08:30；论文 06:10 CST |
| 反代 | Caddy，与 `zb.jiayutool.cn` 同实例，互不影响 |

## 目录

```
/www/wwwroot/ai-daily/
├── app/          仓库检出（只读 deploy key）
├── toolchain/    uv + Python 3.13 + cache（不在 /home，见下）
├── .ssh/         两把 deploy key（app 只读 / backup 读写）
├── published/    <date>.json —— 唯一需要备份的状态
├── published-papers/ <date>.json —— 论文刊期记录，同样需要备份
├── releases/     <date>-<ts>/ 每次发布一个，保留 30 个
├── current →     软链，指向当前 release
├── fallback/     预构建兜底页，渲染器挂了也能服务
├── status/       出刊状态（Caddy no-store）
├── budget/       <date>.json 日报台账；papers-<date>.json 论文独立台账
```

论文页由独立的 `ai-daily-papers.service` / `.timer` 在 06:10 运行，最长 7200 秒。
部署时把两个 unit 复制到 `/etc/systemd/system/`，执行 `systemctl daemon-reload` 后启用
`ai-daily-papers.timer`。`S2_API_KEY` 是可选增强；未配置时当前实现不调用 Semantic Scholar。

**两把锁，别再混用**（2026-09-03 教训）：

- `.publish.lock` 是日报和论文共用的**发布临界区**，只包住渲染、写记录、翻 `current`，
  几秒钟。论文最多等它 10 分钟（10 次 × 60s）。
- `.daily-run.lock` 只属于日报，包住整轮运行，作用是让第二个日报进程在花钱之前就失败。

这两把锁曾经是同一把：日报整轮持有 `.publish.lock` 23–28 分钟，论文的 10 分钟等待窗口
永远等不到，09-02 和 09-03 各丢了一期已经建好的论文刊。**任何新增的发布者都只能碰
`.publish.lock`，且只在临界区里碰。**

**超时预算怎么算的**：选片约 15 分钟 + 深读循环自带的 40 分钟 deadline
（`papers.DEEP_READ_DEADLINE_SECONDS`）+ 抢锁最多 10 分钟 = 65 分钟。原来的
`TimeoutStartSec=3600` 装不下，深读一旦用满自己的预算就必被 systemd 杀掉。改动这三个
数字中的任何一个，都要重新对账（`tests/test_papers.py` 里有守这条的契约测试）。

**一期建好但没发出去怎么补**：`publication.json` 留在
`artifacts/<date>/papers-<run>/` 下，直接补发，不重跑不重复花钱：

```
sudo -u ai-daily ... uv run --frozen ai-daily papers --mode publish \
    --publish-artifact /www/wwwroot/ai-daily/artifacts/<date>/papers-<run>/publication.json
```

marker 校验不过的产物会被拒绝，不会发出去。

**工具链为什么不在 `/home`**：unit 开着 `ProtectHome=yes`。这个进程解析敌意网页内容，
同机还存着别的服务的凭证，没有任何理由看到 home 目录。把 uv、Python、cache 和 SSH 身份
都放到站点根下，`ReadWritePaths` 就只剩一条。启动报 `203/EXEC` 通常意味着有东西被挪回
了 `/home`。

## 已验证（2026-08-28 实测）

- **源连通性**：84 个源，83 个启用，82 个可达。新增的 9 个（openai-changelog、
  midjourney-updates、cohere-blog、fal-blog、aisi-blog、x-chatgpt、x-fal、
  x-inclusionai、x-tibo）全部 `ok`。仅存的失败仍是 xAI（`xai-news` 已禁用、
  `x-xai` 失败）。
- **两个只在生产暴露的坑**：采集器不跟 HTTP 跳转，`developers.openai.com` 的
  changelog 308 跳到 `learn.chatgpt.com`，配置必须写终点；RSSHub 对 `AntLingAGI`
  连续返回合法但零条目的 feed，改用母账号 `TheInclusionAI`。
  本机 curl 通不代表生产能采，加源后一律以服务器 probe 为准。
- **验证办法（不必部署）**：`scp config/*.yaml root@…:/tmp/probe-cfg/` 后跑
  `ai-daily --config-dir /tmp/probe-cfg probe-sources`；`--config-dir` 是全局参数，
  必须放在子命令**前面**。

## 已验证（2026-08-13 实测）

- **源连通性**：36 个源，35 个可达，0 失败（xai-news 已禁用，见下）。21 个在窗口内产出新鲜条目。
- **量子位恢复**：GitHub runner 时代完全封禁，新加坡出口可达，抓到 30 条。
  另修了日期解析（它只用 `<span class="date">` 标记，无 meta / JSON-LD / `<time>`），
  现在稳定产出 19 条新鲜条目。国内 AI 覆盖从「只剩雷峰网」变为「量子位 + 雷峰网」。
- **xai-news 已禁用**：`x.ai/news` 对非浏览器客户端仍返回 403。每次必失败的源是健康报告里的
  噪声，不是覆盖率。探测显示能返回链接后再启用。
- **降级阶梯**：在无模型密钥的真实条件下跑通，产出 L2B 快讯刊并正常上线，
  横幅明确标注「本期为自动快讯模式」。没有出现零产出。
- **完整出刊**：2026-08-14 以 L1 出刊，9 篇详报 + 15 条快讯，当日模型花费 ¥0.75（上限 ¥5）。
- **内存实测**：峰值 155M，`MemoryMax=900M` 有充足余量。
- **升级守卫**：同日 L2B → L2B 被拒绝（`L2B would not improve it`），不产生多余 release。
- **systemd 加固**：完整加固下 `Result=success`，CPU 10.3s。
- **备份**：`backup: pushed 1 issues` → `wjy9902/ai-daily-site-backup`。

## 模型

| 角色 | 主模型 | 回退 |
|---|---|---|
| 判定（judge） | `deepseek-v4-flash` | `gpt-5.6-terra` |
| 编辑（editor） | `deepseek-v4-pro` | `gpt-5.6-terra` |

DashScope 未配置，所以主备都不用它。两个 DeepSeek 模型都会产出推理 token，且推理计入
`max_output_tokens`——按 JSON 体积设上限会把正文截断，这是 2026-08-13 规划连续失败的直接原因。

`gpt-5.6-terra` 只接受 `temperature=1`。它的单价是**估算值并刻意偏高**，因为日成本上限的
准确性完全取决于这两个数字；等回退真的被用到之后，用账单实际值订正。

## 已切换

老站 `wjy9902.github.io/ai-daily` 于 2026-08-13 停止更新：`daily.yml` 的 cron 已移除
（workflow 保留可手动触发），站点作为归档保留、加了指向新站的横幅，**所有旧 issue 链接继续有效**。
旧 RSS 不再更新。

## 待办

1. **轮换密钥**：DeepSeek 与 OpenAI 的 key 曾以明文出现在配置对话中，应当视为已泄露，
   在各自控制台重新签发并更新 `/etc/ai-daily/env`。
2. **回退模型单价**：见上，用账单订正 `config/models.yaml` 里的估算值。
3. ~~跨媒体聚类~~：已于 2026-08-27 实现——同一事件按标题中的版本化产品名
   （GLM-5.3-Flash、Kimi K3 等）跨媒体、跨语言合并，带年份/榜单/同语言重叠三道
   护栏。当天实测 681 条原始条目下，跨域佐证事件从 1 个升至 7 个，
   「两个独立域佐证」首次实际可达。
4. **基础日报 plan 阶段份额撞墙**：2026-08-29 全天只花 ¥3.13/¥8，但 plan 阶段
   ¥2.008 撞满自己的 ¥2.00，最后一个窗口降级成 L2A（0 详报 / 12 快讯），
   还剩 ¥4.87 没花。升级守卫保住了当期的 L1，属于运气。
   根因是一个 `STAGE_SHARE` 同时管请求数和钱，而两个阶段要的东西相反：判定用掉
   54 个请求里的 40 个却只花 ¥0.45，规划只用 5 个请求却是最贵的调用。份额得大到
   够判定用请求，规划的钱就只能是同一个比例的零头。已拆成 `STAGE_REQUEST_SHARE`
   与 `STAGE_COST_SHARE` 两张表，**没有抬高 ¥8 上限**——钱仍然是真正的安全阀。
5. **转推当簇代表**：2026-09-04 合并后的 GPT-6 事件由 `x-openai-devs` 的一条
   「RT Hebbia: Astra set a new high…」当选 primary——tier A + official 排最前，
   而那天官方站点 403、@OpenAI 未发帖，同 tier 里只剩转推。影响面是
   `canonical_url` 和送给编辑的 event title；读者看到的来源列表来自 items，
   标题由编辑另写，所以没有改。真要修，得先决定「官方账号转推第三方评价」
   算不算第一方——它同时喂 `lead_is_corroborated` 的第一方分支，不该顺手改。
6. **OpenAI 没有可用的第一方发布入口**：见 `config/sources.yaml` 里
   `openai-news` 上方的注释。发布帖只在 `openai.com/index/<slug>`，不进
   `/news` 列表，站点对非浏览器客户端全线 403（生产出口实测），
   RSSHub 抓的是同一个列表。目前靠跨媒体聚类补上。

## 常用命令

```bash
# 站点状态（替代推送通知，随时可查）
curl -s https://daily.jiayutool.cn/status.json | jq '{level, action, latest_published, degradation_reasons}'
```

```bash
# 源健康诊断（真实 Collector，不是 curl）
ssh -i ~/.ssh/singapore_rsa root@101.32.114.8 'cd /www/wwwroot/ai-daily/app && sudo -u ai-daily env HOME=/www/wwwroot/ai-daily/toolchain AI_DAILY_SITE_ROOT=/www/wwwroot/ai-daily /www/wwwroot/ai-daily/toolchain/bin/uv run --frozen ai-daily probe-sources'
```

```bash
# 手动补跑一次（会被升级守卫保护，不会覆盖更好的当期）
ssh -i ~/.ssh/singapore_rsa root@101.32.114.8 'systemctl start ai-daily.service && journalctl -u ai-daily -n 20 --no-pager'
```

```bash
# 论文页 dry-run（只筛选并把完整信号分解写入 artifacts，不发布）
cd /www/wwwroot/ai-daily/app
sudo -u ai-daily /www/wwwroot/ai-daily/toolchain/bin/uv run --frozen ai-daily papers --mode dry-run
```

```bash
# 手动生成并发布论文深读刊期
systemctl start ai-daily-papers.service
journalctl -u ai-daily-papers -n 50 --no-pager
```

```bash
# 不花钱重建站点（历史刊期都在 published/）
ssh -i ~/.ssh/singapore_rsa root@101.32.114.8 'cd /www/wwwroot/ai-daily/app && sudo -u ai-daily env HOME=/www/wwwroot/ai-daily/toolchain AI_DAILY_SITE_ROOT=/www/wwwroot/ai-daily /www/wwwroot/ai-daily/toolchain/bin/uv run --frozen ai-daily rebuild-site'
```

```bash
# 回滚展示层到上一个 release
ssh -i ~/.ssh/singapore_rsa root@101.32.114.8 'ls -1t /www/wwwroot/ai-daily/releases | head -3'
```
