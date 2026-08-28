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
| 定时（主编版） | `ai-daily-persona.timer` → 08:50 / 09:20 / 09:50 CST |
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
├── status/       基础日报与主编版独立状态（Caddy no-store）
├── budget/       <date>.json 基础日报模型预算台账
├── upstream/     marker-keyed 基础日报快照与日期激活指针
├── persona-editions/  已验证的甲鱼主编版结构化成稿
├── persona-runs/ 计划、分析、审稿和渲染回执
├── persona-budget/ 独立模型预算与原子预留台账
├── wechat-targets/ 微信创建前冻结的 HTML、元数据与请求哈希
└── wechat-slots.sqlite3 账号/栏目/日期唯一发布 slot
```

**工具链为什么不在 `/home`**：unit 开着 `ProtectHome=yes`。这个进程解析敌意网页内容，
同机还存着别的服务的凭证，没有任何理由看到 home 目录。把 uv、Python、cache 和 SSH 身份
都放到站点根下，`ReadWritePaths` 就只剩一条。启动报 `203/EXEC` 通常意味着有东西被挪回
了 `/home`。

**主编版窗口必须全部晚于基础日报的最后一个窗口。** 主编版一旦出刊就冻结当天的上游
marker（`_persona_date_is_frozen`），排在基础窗口之前会让那个窗口的升级永远发不出去。
`tests/test_workflows.py` 断言了这个顺序。

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
4. ~~甲鱼主编版尚未部署~~：已上线，`ai-daily-persona.timer` 与 service 均 enabled。
   微信仍是 `draft_only`，freepublish 无权限。
5. **主编版每天出刊失败**：2026-08-28 三个窗口全部 `held`，三次原因各不相同。根因已定位
   并修复（2026-08-28），但**尚未经过一次真实出刊验证**——改动让约束变得可满足，不等于模型
   下次一定满足。明天 08:50 / 09:20 / 09:50 三个窗口是第一次实测。三条各自的根因：
   - `persona plan referenced unknown evidence`：`_planner_event_rows` 只把每个事件的前
     2 条证据给 planner，而 `_validate_plan` 允许引用 3 条、bundle 也确实带 3 条
     （`event.items[:3]`）。当天候选池里有 3 条证据的两个事件恰好都被选中——佐证最强的
     事件正是有三个来源的那些。翻遍所有落盘的 plan，planner 只出现过 `-1` 和 `-2`
     （290 次 / 222 次），`-3` 一次都没有：给它看 2 个却允许它引 3 个。已改为给足 3 条。
   - `edition body length 1738 outside 700-1600`：不是模型写多了。`confirmed_change` 必须
     逐字等于已验证原文（`persona_verifier.py`），长度由源站作者决定：08-27 是 574 字，
     08-28 是 991 字（五条里三条是长英文句）。其余每个字段都被 `AssemblyInterpretiveText`
     限制在 30 字符，主编自己能写的上限约 990。991 加一段写满的正文塞不进 1600。上限改为
     2000（schema 自身 990 上限 + 实测最宽的引用总量），同时修好本该兜底却什么都没做的
     压缩逻辑——`_compact_text` 在截断窗口内找不到句号时会原样返回未压缩文本，而它被调用
     时的窗口约 20 字符，中文分析句在前 20 字符里几乎不可能有句号。
   - `Exceeded maximum output retries (1)`：`OUTPUT_RETRIES` 是全局常量，而 `generate()`
     的外层循环只对网络类错误重试，所以版面编辑一共只有 2 次 provider 请求，去让约 30 个
     独立字段同时满足长度上限、前缀、无第一人称和总字数区间。一个字段写长就整轮报废，
     而此时所有分析师的钱已经花完（每次约 ¥1.1）。改为按角色配置，版面编辑给 3 次。
   顺带修掉一个从未触发过的死结：`no_major_update` 版本的正文只有主旨加最多 2 条观察，
   三个 30 字符文本共 90 字，而下限是 300——真正平静的一天必然 `held`。下限改为 50。
   这是把算术改成一致，不是「平静的一天值 50 个字」的编辑判断；要说更多，得先动
   thesis 与 watchlist 的 30 字符上限。

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

部署主编版代码后安装独立定时器（默认仅网站）：

```bash
cp ops/systemd/ai-daily-persona.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ai-daily-persona.timer
systemctl list-timers ai-daily-persona.timer
```

草稿模式必须在签名授权、永久封面、独立 HMAC keys 全部配置后，人工修改 service 的
`ExecStart` 为 `--mode draft --execute --authorization ...`。不要启用 freepublish 或群发替代路径。
