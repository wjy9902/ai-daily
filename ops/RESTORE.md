# 灾难恢复 runbook

从零把甲鱼AI日报恢复到出刊状态。这份文档必须能在**服务器彻底失联**时单独使用，
所以不假设你手上有任何服务器上的东西。

前置：一台能上网的 Linux 机器（2C/2G 起）、域名 DNS 的控制权、两个仓库的读取权限、
以及两个模型 API key。甲鱼主编版若要恢复公众号草稿，还需要公众号凭证、永久封面和重新签发的
两把独立 HMAC key。

## 需要的东西

| 项 | 来源 |
|---|---|
| 代码 | `git@github.com:wjy9902/ai-daily.git`（发布分支/tag） |
| 历史刊期 | `git@github.com:wjy9902/ai-daily-site-backup.git` 的 `published/` |
| `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` | 阿里云百炼控制台重新签发 |
| `DEEPSEEK_API_KEY` | DeepSeek 控制台重新签发 |
| DNS | 阿里云万网 `jiayutool.cn` |

密钥不在任何备份里，这是有意的。恢复时重新签发，不要试图找回旧值。

## 步骤

### 1. 系统与用户

```bash
useradd --system --create-home --home-dir /home/ai-daily --shell /usr/sbin/nologin ai-daily
mkdir -p /www/wwwroot/ai-daily/{app,releases,published,artifacts,status,budget,fallback,logs}
mkdir -p /www/wwwroot/ai-daily/{upstream,persona-editions,persona-runs,persona-budget,wechat-targets}
chown -R ai-daily:ai-daily /www/wwwroot/ai-daily
timedatectl set-timezone Asia/Shanghai
timedatectl show --property=Timezone --value
```

### 2. 运行时

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
mkdir -p /www/wwwroot/ai-daily/toolchain/bin
env HOME=/www/wwwroot/ai-daily/toolchain \
  UV_INSTALL_DIR=/www/wwwroot/ai-daily/toolchain/bin sh /tmp/uv-install.sh
chown -R ai-daily:ai-daily /www/wwwroot/ai-daily/toolchain
cd /www/wwwroot/ai-daily   # uv 需要一个可写的 cwd
sudo -u ai-daily env HOME=/www/wwwroot/ai-daily/toolchain \
  /www/wwwroot/ai-daily/toolchain/bin/uv python install 3.13
```

### 3. 代码与历史

```bash
sudo -u ai-daily git clone git@github.com:wjy9902/ai-daily.git /www/wwwroot/ai-daily/app
cd /www/wwwroot/ai-daily/app && sudo -u ai-daily git checkout <发布 tag>
sudo -u ai-daily env HOME=/www/wwwroot/ai-daily/toolchain \
  /www/wwwroot/ai-daily/toolchain/bin/uv sync --frozen --no-dev

# 历史刊期：备份仓库的 published/ 直接放回去
sudo -u ai-daily git clone git@github.com:wjy9902/ai-daily-site-backup.git /tmp/ai-daily-backup
sudo -u ai-daily cp /tmp/ai-daily-backup/published/*.json /www/wwwroot/ai-daily/published/
```

### 4. 密钥

```bash
mkdir -p /etc/ai-daily
cat >/etc/ai-daily/env <<'EOF'
DASHSCOPE_API_KEY=...
DASHSCOPE_BASE_URL=...
DEEPSEEK_API_KEY=...
AI_DAILY_SITE_BASE_URL=https://daily.jiayutool.cn
# 主编版网站模式不需要 WECHAT_*。恢复草稿能力时再填写，并重新生成签名授权。
EOF
chown ai-daily:ai-daily /etc/ai-daily/env
chmod 600 /etc/ai-daily/env
```

### 5. 先把站点恢复出来（不调模型）

历史刊期已经在 `published/`，所以站点可以在不花一分钱、不依赖模型的情况下重建：

```bash
cd /www/wwwroot/ai-daily/app
sudo -u ai-daily env HOME=/www/wwwroot/ai-daily/toolchain AI_DAILY_SITE_ROOT=/www/wwwroot/ai-daily \
  /www/wwwroot/ai-daily/toolchain/bin/uv run --frozen ai-daily rebuild-site
ls -l /www/wwwroot/ai-daily/current
```

同时生成兜底页，保证任何后续失败都不会 404：

```bash
sudo -u ai-daily env HOME=/www/wwwroot/ai-daily/toolchain AI_DAILY_SITE_ROOT=/www/wwwroot/ai-daily \
  /www/wwwroot/ai-daily/toolchain/bin/uv run --frozen ai-daily write-fallback
```

### 6. Caddy

```bash
cat /www/wwwroot/ai-daily/app/ops/caddy/daily.jiayutool.cn.caddy >>/etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

签证书需要新机器的 80/443 从公网可达。云厂商安全组要先放行，否则 Caddy 会一直重试。

### 7. DNS

阿里云万网把 `daily.jiayutool.cn` 的 A 记录改到新机器 IP。TTL 是 600 秒，
所以最坏 10 分钟生效。**改 DNS 之前先完成第 6 步**，否则 Caddy 签不到证书，
访客会看到证书错误而不是旧站。

### 8. 定时器

```bash
cp /www/wwwroot/ai-daily/app/ops/systemd/ai-daily.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ai-daily.timer
systemctl list-timers ai-daily.timer
```

甲鱼主编版数据恢复完成后，再安装 `ai-daily-persona.{service,timer}`。默认保持 `--mode site`；
先验证 `/jiayu/` 和 `status/persona.json`，再单独决定是否恢复微信草稿权限。

### 9. 验收

```bash
curl -fsS https://daily.jiayutool.cn/ >/dev/null && echo page ok
curl -fsS https://daily.jiayutool.cn/rss.xml >/dev/null && echo rss ok
curl -fsS https://daily.jiayutool.cn/status.json | head -30
curl -fsS https://daily.jiayutool.cn/status/persona.json | head -30
```

`status.json` 里的 `level` 和 `generated_at` 是判断"真的在出刊"的依据，
页面能打开只说明文件在。

## 恢复演练

这份 runbook 每次改动后必须实际走一遍（本机跑到第 5 步即可，无需真实 DNS）。
没演练过的 runbook 等于没有 runbook。

## 数据损失边界

- `published/*.json` 每日备份，最多丢一天。
- `artifacts/` 不备份，只用于事后复盘，丢失可接受。
- `budget/*.json` 不备份，恢复后当天预算从零计，最坏多花一天的额度。
- 当前备份策略**不包含** `upstream/`、`persona-editions/`、`persona-runs/`、
  `wechat-targets/` 与 `wechat-slots.sqlite3`。灾难后主编版历史、来源审计链和微信 unknown 对账能力
  可能丢失；这是已接受的恢复风险，不能通过盲目重建微信草稿来弥补。
- 密钥不备份，必须重新签发。
