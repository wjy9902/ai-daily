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
| 定时 | `ai-daily.timer` → 04:20 / 05:05 / 07:00 / 08:30 CST |
| 反代 | Caddy，与 `zb.jiayutool.cn` 同实例，互不影响 |

## 目录

```
/www/wwwroot/ai-daily/
├── app/          仓库检出（只读 deploy key）
├── toolchain/    uv + Python 3.13 + cache（不在 /home，见下）
├── .ssh/         两把 deploy key（app 只读 / backup 读写）
├── published/    <date>.json —— 唯一需要备份的状态
├── releases/     <date>-<ts>/ 每次发布一个，保留 30 个
├── current →     软链，指向当前 release
├── fallback/     预构建兜底页，渲染器挂了也能服务
├── status/       出刊状态（Caddy no-store）
├── budget/       <date>.json 基础日报模型预算台账
```

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
# 不花钱重建站点（历史刊期都在 published/）
ssh -i ~/.ssh/singapore_rsa root@101.32.114.8 'cd /www/wwwroot/ai-daily/app && sudo -u ai-daily env HOME=/www/wwwroot/ai-daily/toolchain AI_DAILY_SITE_ROOT=/www/wwwroot/ai-daily /www/wwwroot/ai-daily/toolchain/bin/uv run --frozen ai-daily rebuild-site'
```

```bash
# 回滚展示层到上一个 release
ssh -i ~/.ssh/singapore_rsa root@101.32.114.8 'ls -1t /www/wwwroot/ai-daily/releases | head -3'
```

