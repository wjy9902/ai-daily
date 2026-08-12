# 多云模型 Provider 层调研

> 调研日期：2026-08-12（Asia/Shanghai）
> 目标：让日报可以在 OpenAI、Anthropic、Google、DeepSeek、OpenRouter 和本机 Ollama 等模型之间改配置切换，同时保持结构化输出、错误可见和低运维。

## 1. 我们实际需要什么

日报不是聊天机器人，也不需要模型调用网关的后台管理系统。它只需要：

1. 用统一的 `provider:model` 配置选择不同云模型；
2. 把事件评审和日报条目解析为 Pydantic 类型，而不是自己截取 JSON；
3. 支持 async、timeout、有限重试、用量记录和测试替身；
4. 可以配置跨 provider 的故障链，但实际用了哪个模型必须可审计；
5. 模型切换不能影响采集、聚类、验证和发布代码；
6. 不增加 Redis、数据库、常驻 proxy、控制台或另一套部署。

因此，“支持最多 provider”不是第一标准。边界小、结构化输出可靠和失败语义明确更重要。

## 2. 候选项目

Star 数是 2026-08-12 的页面快照，只用于说明项目成熟度，不作为选型分数。

| 项目 | Star 快照 | 许可证/形态 | 适配范围 | 结构化输出 | 跨模型故障链 | 额外服务 | 结论 |
|---|---:|---|---|---|---|---|---|
| [Pydantic AI](https://github.com/pydantic/pydantic-ai) | 19.2k | MIT，Python 库 | 原生主流 provider + OpenAI-compatible + OpenRouter/Ollama | Pydantic 原生 | `FallbackModel` | 无，提供 slim 安装 | **采用** |
| [Instructor](https://github.com/567-labs/instructor) | 13.7k | MIT，Python 库 | 主流云、OpenRouter、LiteLLM、Ollama | 核心强项 | 没有同等完整的统一故障模型 | 无 | 最轻量备选 |
| [LiteLLM](https://github.com/BerriAI/litellm) | 56.2k | 核心 MIT，SDK/网关 | 100+ provider | 支持 JSON Schema/Pydantic | Router 重试、冷却、fallback | SDK 可无；网关会新增服务 | 暂不采用 |
| [aisuite](https://github.com/andrewyng/aisuite) | 16.1k | MIT，Python 库 | OpenAI、Anthropic、Google、Ollama 等 | 不是主要设计中心 | 需自行设计 | 无 | 不如前两者匹配 |
| [OpenRouter](https://openrouter.ai/docs/quickstart) | SaaS | 托管聚合 API | 数百模型/多下游 provider | 支持，但取决于模型/路由端点 | 默认有 provider fallback | 无自托管服务，但新增中间商 | 可选 provider，不做唯一入口 |
| [Portkey Gateway](https://github.com/Portkey-AI/gateway) | 12.7k | MIT，网关/托管服务 | 250+ LLM | OpenAI-compatible | 重试、fallback、负载均衡 | 需要 gateway 进程或托管服务 | 对单日报过重 |
| [BAML](https://github.com/BoundaryML/baml) | 8.9k | Apache-2.0，独立语言/运行时 | 多 provider | 强类型 | 有运行时能力 | 新语言、编译和生成层 | 对本项目过重 |

## 3. 逐项判断

### 3.1 Pydantic AI：最符合完整需求

官方文档显示它支持 OpenAI、Anthropic、Gemini、xAI、Bedrock、Mistral、Groq、OpenRouter 等原生 provider，并可通过 OpenAI-compatible provider 接入 DeepSeek、Together、Fireworks、Ollama 等端点。模型可以用 `provider:model` 字符串声明，也可以在运行时替换。

与日报直接相关的能力：

- 输出类型就是 Pydantic model，校验失败会按明确预算让同一模型重试；
- model profile 可以在调用前检查 JSON Schema 等能力；
- `FallbackModel` 可以按顺序尝试跨 provider 模型，并保留最终 `model_name`；
- HTTP 错误保留状态码、headers 和 `Retry-After`；
- 支持并发限制、timeout、request/token/cost limits；
- `TestModel` 和 `FunctionModel` 适合不调用真实 API 的单元/E2E 测试；
- [`pydantic-ai-slim`](https://ai.pydantic.dev/install/) 可以只安装 `openai`、`anthropic`、`google`、`openrouter` 等需要的 extra，不必安装 CLI、MCP、Web UI、Logfire 和 durable execution。

它的完整产品是 Agent framework，但我们只使用其中的 typed model/provider/output 部分：不注册工具，不启用 MCP、图、Web UI、Logfire 或多 Agent。这样不会改变日报的确定性流水线边界。

### 3.2 Instructor：非常合适的轻量备选

[Instructor provider 文档](https://python.useinstructor.com/integrations/)支持 OpenAI、Anthropic、Google、DeepSeek、Bedrock、Vertex、Groq、OpenRouter、LiteLLM、Ollama 等，并统一提供 Pydantic response model、验证重试、async 和 hooks。

如果需求只有“切模型 + 获取可靠 JSON”，Instructor 的边界比 Pydantic AI 更小。它没有同等完整的模型 profile、用量限制和跨 provider `FallbackModel` 语义；为满足 06:00 全自动发布的故障链，我们还要自己写一层模型错误分类和 fallback。综合后，Pydantic AI Slim 更少自研代码。

### 3.3 LiteLLM：能力最广，但现在不需要网关

[LiteLLM](https://github.com/BerriAI/litellm)既能作为 Python SDK，也能运行 OpenAI-compatible AI Gateway。它支持 100+ provider、Pydantic/JSON Schema、模型 alias、重试、冷却、fallback、成本和路由；[Router 文档](https://docs.litellm.ai/docs/routing)还提供跨部署负载均衡。

这些能力更适合多个应用、多个团队或大请求量。Router 的生产级用量/冷却状态会引入 Redis，Proxy 又新增常驻服务、控制台和密钥层。日报每天只有一批调用，先引入它会扩大故障面。未来若多个本机项目都需要统一模型预算、密钥、审计和路由，再单独评估 LiteLLM Gateway。

### 3.4 aisuite：统一调用很轻，但结构化契约较弱

[aisuite](https://github.com/andrewyng/aisuite)提供 OpenAI 风格的 Chat Completions，用一个 `provider:model` 字符串切换 OpenAI、Anthropic、Google、Ollama 等 provider，基础包也不强装所有 SDK。

它适合统一普通聊天调用，但官方当前把工具和 Agent API 也扩进了项目；结构化输出、provider capability profile 和跨云失败策略不是它相对 Pydantic AI/Instructor 的优势。日报强依赖严格 schema，因此不选。

### 3.5 OpenRouter：便利的可选入口，不应默认成为中间层

[OpenRouter Quickstart](https://openrouter.ai/docs/quickstart)提供一个 API 访问数百模型，可以通过 Pydantic AI 的 `openrouter:` provider 使用。它特别适合临时比较模型，或用户只想维护一个账户/Key 的场景。

但它在直连厂商之外新增一个数据和可用性中间层。其[路由文档](https://openrouter.ai/docs/guides/routing/provider-selection)显示，默认会在下游 provider 间做负载均衡和 fallback，且数据策略需要显式配置。若启用，日报必须：

- 指定确切 model slug，不使用会自动漂移的 `latest`/auto router；
- `require_parameters=true`，确保下游支持所请求的结构化参数；
- 默认 `allow_fallbacks=false`，由我们的显式模型链控制故障切换；
- `data_collection=deny`，能满足时再加 `zdr=true`；
- 记录实际下游 provider/model，并验证 structured output 能力。

因此 OpenRouter 是配置中的一个可选 provider，不是所有调用的必经网关。

### 3.6 Portkey 与 BAML：解决了更大的问题

Portkey 是完整 AI Gateway，提供重试、fallback、负载均衡、guardrail、日志和密钥控制；BAML 现在是带类型系统、编译/代码生成和测试能力的 Agent 编程语言。两者都能实现需求，但会为一个每日批处理引入新的运行面或语言层，不符合“干净日报”的目标。

## 4. 最终方案

### 4.1 采用方式

- 使用 `pydantic-ai-slim`，只安装当前启用 provider 的 extras。
- 在项目内定义一个很小的 `ModelGateway` 接口，业务代码只认识 `assess()`、`write_item()` 和 typed output，不直接 import 厂商 SDK。
- 模型名称、角色、timeout、token/cost budget 和显式 fallback 链放在 `config/models.yaml`；API key 只从环境变量/GitHub Secrets 读取。
- Ollama 作为一个可选择的本地 profile，绝不在云模型失败时自动接管生产任务。
- 不启动 Pydantic AI Gateway、LiteLLM Proxy、Portkey 或任何模型控制台。

概念配置：

```yaml
profiles:
  judge:
    primary: "<provider>:<model>"
    fallbacks:
      - "<different-provider>:<model>"
    timeout_seconds: 90
    output_retries: 1
  editor:
    primary: "<provider>:<model>"
    fallbacks:
      - "<different-provider>:<model>"
    timeout_seconds: 120
    output_retries: 1
```

这里的 fallback 是显式配置，不是自动选择“便宜/快的任意模型”。每次调用都记录 profile、请求模型、实际模型、fallback 原因、耗时、tokens 和可用成本数据。

### 4.2 故障规则

- 只在 timeout、连接失败、429、可恢复 5xx 等 provider/API 故障时进入下一个模型。
- Authentication、bad request、context overflow、schema 配置错误立即失败，不用另一个模型掩盖配置问题。
- 输出校验失败先在同一模型内重试一次；仍失败则该阶段失败，不把未经验证文本交给下一阶段。
- fallback 被使用不是“正常无事发生”：写入 `model-runs.json` 和 Actions Summary。
- 所有模型都失败时停止发布，并由 05:05 的自动恢复运行重新执行；不生成标题拼接版日报。

### 4.3 初始模型怎么选

计划不在文档中硬编码一个会过期的模型名。实现阶段用最近 20–30 期旧日报构建固定评测集，对至少两家独立云 provider 的候选模型比较：

| 维度 | 权重 |
|---|---:|
| 对证据的忠实度、无虚构 | 35% |
| 选题准确率与漏报 | 20% |
| 结构化输出一次通过率 | 15% |
| 中文编辑质量 | 15% |
| 端到端延迟和稳定性 | 10% |
| 单期成本 | 5% |

得分最高的模型作为 `editor.primary`；便宜、稳定且判断一致的模型可以作为 `judge.primary`。fallback 必须来自另一家 provider，避免单云故障。模型切换只改配置并重新跑同一组评测与 smoke test，不修改业务代码。

## 5. 何时升级成独立模型网关

只有出现以下任一情况才重新评估 LiteLLM/Portkey：

- 至少三个独立应用需要共享同一套模型 keys、预算和审计；
- 日均请求量明显增加，需要跨部署负载均衡和集中 rate limit；
- 必须给多人发虚拟 key 或做团队级成本控制；
- 直接 provider SDK 的差异已经无法由 Pydantic AI 的 model profile 吸收。

在此之前，进程内适配层是更小、更可靠的方案。
