# 自建信源基础设施（X + 微信公众号）

> **状态（2026-08-29）：两条链路均已上线，公众号定时采集已修复。** RSSHub 承载
> 13 个厂商 X 账号，we-mp-rss 承载 12 个公众号（每 10 分钟经微信读书通道采集）。
> 定时采集从建站起一直因 `mps_id` 格式写错而静默失败，2026-08-29 修复，见下方
> 注意事项。下文保留部署过程与运维要点。

## 为什么需要

对照橘鸦「AI 早报」2026-08-27 期做的逐条覆盖审计：对方 19 条里 9 条的第一手
信源是 X（OpenAI 误路由致歉、Antigravity CLI 语音模式、Cowork 内置浏览器、
Fable 5.1 灰度爆料等），另有多条（GLM-5.3-Flash 发布详情）first-party 出处是
微信公众号。厂商现在把大量公告只发 X 和公众号，不进博客——没有这两类信源，
这部分新闻结构性缺失。

Techmeme + TestingCatalog + smol.ai（已配置）能以几小时延迟转述其中大部分，
这是当前的过渡方案；自建信源才能拿到当天早晨可用的第一手条目。

## GitHub 工具调研结论（2026-08-27）

| 项目 | 状态 | 结论 |
|---|---|---|
| [DIYgod/RSSHub](https://github.com/DIYgod/RSSHub)（45.9k★） | 活跃，前一天有提交 | **采用**：X 官方账号→RSS，也覆盖微博/即刻等数百站点 |
| [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss)（4.4k★） | 活跃 | **采用**：公众号→RSS（基于微信读书授权），自带定时抓取和管理界面 |
| cooderl/wewe-rss（9.7k★） | **已归档** 2026-03 | 弃用，we-mp-rss 是其活跃继任者 |
| zedeus/nitter（13.6k★） | **已归档** | 此路已死；公共实例全部下线/410 |
| xcancel.com | 在线 | 需逐个 RSS 阅读器邮件申请白名单，不可自动化 |
| rsshub.app 官方实例 | 在线 | twitter 路由 404（必须自建并配 X 凭据） |
| [vladkens/twscrape](https://github.com/vladkens/twscrape)（2.7k★） | 活跃 | 备选 Plan B：Python 库、多账号轮换，可作为原生 source kind 集成，RSSHub twitter 路由失效时再启用 |
| X API v2 | — | 可行但 Basic 档约 $200/月，最后手段 |
| ourongxing/newsnow（21.5k★）、imsyy/DailyHotApi（4k★） | 活跃 | 二期候选：各平台热榜聚合，作 community 渠道旁证（同 Hacker News 语义，无发布时间不能当新闻时间） |
| searxng/searxng（36.1k★） | 活跃 | 二期候选：自建元搜索，给头条做第二信源佐证，解决"两个独立域"实际不可达的问题 |
| RSS-Bridge（9.2k★） | 活跃 | RSSHub 覆盖不到的站点再考虑 |

## 部署（生产服务器）

采集器有两条硬约束，部署必须满足：**HTTPS + 解析到公网 IP**
（`sources.py` 的 pinned-DNS 校验），所以服务要挂在自己域名的子域下，
不能用 `http://localhost:1200`。

```bash
# 1. 起服务（rsshub + we-mp-rss 一个 compose 管完）
cd ops/feed-infra
echo 'TWITTER_AUTH_TOKEN=<X 小号的 auth_token cookie>' > .env
docker compose up -d
```

2. 给两个子域配 HTTPS 反代。生产机的反代是 **Caddy**（与 daily/zb 两站同实例），
   把 `Caddyfile-feeds` 里的两个站点块追加进现有 Caddyfile 后
   `systemctl reload caddy`（平滑重载，不影响其他站点），证书自动签发：
   - `rsshub.jiayutool.cn` → 127.0.0.1:1200
   - `werss.jiayutool.cn` → 127.0.0.1:8001

   DNS 需要在阿里云控制台加两条 A 记录指向服务器 IP（若已有泛解析则跳过）。

3. 初始化 we-mp-rss：浏览器打开 `https://werss.<domain>`，用微信读书扫码
   授权，然后添加公众号。**注意**：管理界面的"搜索公众号"走公众号平台接口，
   需要另一个扫码；没有平台登录时，用 API `POST /api/v1/wx/mps` 直接添加，
   `mp_id` 传公众号的 biz（base64 串），Feed id 即 `MP_WXS_<biz解码后的数字>`。
   环境变量见仓库里的 `ops/feed-infra/compose.override.yml`（密码走 `.env` 插值，
   文件本身不含明文）。
   biz 可从该公众号任一文章页的 `var biz = "..."` 提取；账号迁移过的（如
   月之暗面 Kimi）要用迁移公告里新账号的 biz。
   采集模式必须用环境变量 `GATHER.MODEL=weread_mp` 设置（配置 API 写库无效，
   `cfg` 只读 yaml/env），并创建一个 cron 消息任务驱动定时采集。
   已配置十二个号：智谱 MP_WXS_3923277442、千问大模型 MP_WXS_3948884294、
   月之暗面 Kimi MP_WXS_3702378138、MiniMax MP_WXS_3191077711、
   机器之心 MP_WXS_3073282833、新智元 MP_WXS_3271041950、
   DeepSeek MP_WXS_3949607775、字节跳动 Seed MP_WXS_3930693616、
   腾讯混元 MP_WXS_3908569425、阶跃 StepFun MP_WXS_3925617892、
   百度文心 MP_WXS_3297523352，以及橘鸦Juya MP_WXS_3220940658。
   **橘鸦是覆盖率对照基准，只订阅、不进 `config/sources.yaml`**——它是用来审计
   我们漏了什么的，不是拿来转述的信源。
   已知限制：微信读书通道每次只返回每个号的最新一篇，同日多篇会被抽样。

```bash
# 4. 验证
curl -s https://rsshub.<domain>/twitter/user/OpenAI | head -40
curl -s '<从 we-mp-rss 界面复制的某个公众号 RSS 地址>' | head -40
```

注意事项：

- `TWITTER_AUTH_TOKEN` 用**小号**，不要用主账号；支持逗号分隔多账号轮换。
  token 属于机密，只放服务器 `.env`，绝不入库（同 API key 轮换纪律）。
- 微信读书授权同理用小号，授权会过期，we-mp-rss 有到期提醒，需要偶尔重新扫码。
- 两个服务都对上游变化敏感，把它们当"经常部分失败"的源对待——管线的
  source health 会记录，不会拖垮刊期。
- compose 里已配 10 分钟缓存，三个 timer 窗口不会对 X 重复施压。
- **采集任务的 `mps_id` 必须是 JSON 对象数组，不是逗号分隔串。**
  `jobs/mps.py` 的 `get_feeds()` 做的是 `json.loads(task.mps_id)` 再取
  `item["id"]`，所以只有 `[{"id":"MP_WXS_..."},...]` 这一种格式能跑。写成
  `MP_WXS_a,MP_WXS_b` 时任务每次触发都抛
  `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`，而**界面和 API
  都照收不误**，日志里也只有一行 ERROR，不会有任何"采集失败"的提示。
  2026-08-27 建站时就写错了，直到 2026-08-29 才发现：11 个号的最新文章全部停在
  08-27（那批其实是建站时手动 `/mps/update/{id}` 拉的），cron 一次都没成功过，
  累计 586 次失败。当天橘鸦早报的头条「腾讯 Hy4 preview 开源」我们完全空掉，
  就是因为腾讯混元这个号明明订阅着却两天没进货。
  改完 `mps_id` 后**必须重启容器**：`job/fresh` 重载接口不会让调度器重新读库，
  改完 DB 也照旧报错，`docker compose restart we-mp-rss` 之后才生效。
- **例行检查**：`select mp_id, max(publish_time) from articles group by mp_id`。
  只要最新时间集体停在同一天，就是采集链路断了，不是"当天没人发文"。
- **正文补抓是独立于采集的第二条链路，三个默认值都不能用**（2026-08-29 实测，
  `compose.override.yml` 已固化）。采集只写标题和链接，`content` 一律留空，正文靠
  `jobs/fetch_no_article.py` 的补抓任务回填。这条链路坏了不会报"采集失败"，只会让
  RSS 的 `<description>` 等于标题、`<content:encoded>` 为空，管线拿到手就是十几个字，
  详报被降级成快讯、主编版连 `QUOTE_MIN_CHARS` 都过不了。
  - `GATHER.CONTENT_AUTO_INTERVAL` 默认 **59**，而代码是 `cron = f"*/{interval} * * * *"`。
    cron 的 `*/59` 只在第 0 分和第 59 分触发——每小时两次、隔 1 分钟，然后空 58 分钟。
    采集每 10 分钟一轮，正文却只在 :59/:00 落地，04:20 那个窗口拿到的新文章必然只有标题。
    改成 `10`，与采集同频。
  - `GATHER.CONTENT_MODE` 默认 **web**，走 Playwright 渲染整页。微信正文页普遍 3MB+
    （实测腾讯混元 3.5MB、Kimi 3.2MB），60s 和 180s 的子进程 wall-clock 硬超时都跑不完，
    而超时是把**整个子进程**杀掉，代码里 `web -> api` 的回退根本轮不到执行。
    改成 `api`：纯 HTTP + BeautifulSoup 取 `#js_content`，同样的 URL 几秒返回。
  - `GATHER.CONTENT_FETCH_TIMEOUT` 一并抬到 `180` 兜底。
  - 顺带：我们原来写的 `WEREAD_CONTENT_INTERVAL` **代码里一处都没引用**，是个死设置，已删。
- **`build_mp_url` 生成的 `~` 短链现在是坏的**（2026-08-29 从生产出口实测）。
  `core/wx/model/weread_mp.py` 里有注释说 token 中的 `~` 必须保留、换成 `_` 会被微信
  302 跳走——**现在反过来了**：`~` 版本稳定返回一个 31612 字节的错误页（无 `#js_content`），
  `_` 版本返回真正的 3.2MB 正文（302 到 `?nwr_flag=1#wechat_redirect`）。
  受影响的号当天是 Kimi、新智元、百度文心。已一次性把库里 `articles.url` 的 `~` 改写成 `_`
  并重置 `fix_fail_count`/`status` 让补抓重试，六篇全部拿到正文。
  **这是上游 bug，新文章仍会带 `~` 进来**——要么定期跑一次同样的改写，要么打补丁/盯上游。
- 判断正文链路是否健康：`select sum(has_content), count(*) from articles`。

## 接入管线

两个服务输出的都是标准 RSS，`config/sources.yaml` 末尾有注释好的模板
（X 账号一段、公众号一段），把占位域名换成实际地址后取消注释即可，无需改代码。

建议首批：

**X 官方账号（tier A / channel official）**
OpenAI、OpenAIDevs、AnthropicAI、claudeai、ClaudeDevs、GoogleDeepMind、
GeminiApp、xai、Alibaba_Qwen、Kimi_Moonshot、MiniMax__AI、ManusAI_HQ、cursor_ai。
员工个人账号噪声高、常被官方账号转发覆盖，第一批不加；跑稳两周后按漏报审计再定。

**微信公众号（tier A / channel official / region china）**
智谱（Z.ai 公告的中文一手出处）、通义千问、月之暗面 Kimi、MiniMax、
机器之心、新智元（机器之心站点 RSS 已死，公众号是其唯一可编程入口；
量子位已有站点源，不必重复）。媒体类公众号 channel 用 news、tier B。

上线后先在服务器跑 `uv run ai-daily probe-sources`，确认每个新源
`status: ok` 且 `with_publication_time > 0`，再正式并入。

## 本地开发注意

本地开发机若开着 fake-IP 模式的代理（198.18.0.0/15），pinned-DNS 校验会
拒绝所有源，`probe-sources` 会显示 0/N。这不是配置问题；用直连网络或在
服务器上探测。
