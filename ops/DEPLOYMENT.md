# 部署现状

> 最后更新：2026-08-13。恢复步骤见 [`RESTORE.md`](RESTORE.md)，设计见
> [`../docs/plan/SELF-HOST-MIGRATION.md`](../docs/plan/SELF-HOST-MIGRATION.md)。

## 线上

| 项 | 值 |
|---|---|
| 站点 | https://daily.jiayutool.cn （HTTPS 自动签发，证书验证通过） |
| 服务器 | 腾讯云新加坡 `101.32.114.8`，OpenCloudOS 9 |
| DNS | 阿里云 A 记录 `daily → 101.32.114.8`，TTL 600 秒 |
| 站点根 | `/www/wwwroot/ai-daily` |
| 运行用户 | `ai-daily`（system 用户，nologin，无 sudo） |
| 定时 | `ai-daily.timer` → 04:20 / 05:05 / 07:00 CST |
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
├── status/       status.json（Caddy 单独路由，no-store）
└── budget/       <date>.json 当日模型预算台账
```

**工具链为什么不在 `/home`**：unit 开着 `ProtectHome=yes`。这个进程解析敌意网页内容，
同机还存着别的服务的凭证，没有任何理由看到 home 目录。把 uv、Python、cache 和 SSH 身份
都放到站点根下，`ReadWritePaths` 就只剩一条。启动报 `203/EXEC` 通常意味着有东西被挪回
了 `/home`。

## 已验证（2026-08-13 实测）

- **源连通性**：36 个源，35 个可达，0 失败（xai-news 已禁用，见下）。21 个在窗口内产出新鲜条目。
- **量子位恢复**：GitHub runner 时代完全封禁，新加坡出口可达，抓到 30 条。
  另修了日期解析（它只用 `<span class="date">` 标记，无 meta / JSON-LD / `<time>`），
  现在稳定产出 19 条新鲜条目。国内 AI 覆盖从「只剩雷峰网」变为「量子位 + 雷峰网」。
- **xai-news 已禁用**：`x.ai/news` 对非浏览器客户端仍返回 403。每次必失败的源是健康报告里的
  噪声，不是覆盖率。探测显示能返回链接后再启用。
- **降级阶梯**：在无模型密钥的真实条件下跑通，产出 L2B 快讯刊并正常上线，
  横幅明确标注「本期为自动快讯模式」。没有出现零产出。
- **升级守卫**：同日 L2B → L2B 被拒绝（`L2B would not improve it`），不产生多余 release。
- **systemd 加固**：完整加固下 `Result=success`，CPU 10.3s。
- **备份**：`backup: pushed 1 issues` → `wjy9902/ai-daily-site-backup`。

## 待办

1. **模型密钥**：`/etc/ai-daily/env` 目前是空值占位，所以每天只能出 L2B。
   填入 `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` / `DEEPSEEK_API_KEY` 后自动升到 L0。
   密钥只有模型 API 权限，没有任何 GitHub 写权限。
2. **内存基线**：这台机器的 systemd 不暴露 `MemoryPeak`，当前 `MemoryHigh=600M` /
   `MemoryMax=900M` 是按 1.9G 总内存、已用 650M 推的。跑满一次 L0（含 enrichment）后
   用 cgroup 的 `memory.peak` 复核一次。
3. **切换老站**：新站连续两天自动出 L0 之后再做，见迁移计划阶段 5。

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
