# 甲鱼AI日报

> 每日 AI 前沿技术情报，自动生成。内容由 AI 辅助创作，可能存在错误，请以原始信息为准。

🔗 **网站**: https://wjy9902.github.io/ai-daily/  
📡 **RSS**: https://wjy9902.github.io/ai-daily/rss.xml

## 内容板块

- **今日重点（4–5 条）** — 当天最重要的模型、产品、公司与产业变化
- **值得关注（5–7 条）** — 影响明确、值得继续跟踪的进展
- **快讯（8–12 条）** — 用紧凑格式补齐工具、融资、研究和社区动态
- **编辑观点** — 只基于本期证据提炼跨新闻趋势，不做无来源推断

采集覆盖 30 余个公开来源，包括 AI 实验室与平台官方站点、中文及英文科技媒体、研究源和少量高信号项目发布。研究论文与版本更新设有篇幅上限，避免挤占重大产品和行业新闻。

## 技术栈

- [Zola](https://www.getzola.org/) 静态站点生成器
- [isite](https://github.com/kemingy/isite) GitHub Issues → Zola content
- GitHub Actions 自动构建部署
- GitHub Pages 托管

## 自动生成器

新的生成流水线位于 `src/ai_daily`，仅采集公开网站、RSS 和官方 API。模型密钥只从环境变量读取，不写入仓库。

```bash
uv sync --frozen --all-groups
uv run ai-daily run --date 2026-08-12 --mode dry-run
uv run ai-daily benchmark-models --dataset tests/evals
uv run python scripts/render_fixture.py \
  --fixture tests/fixtures/editorial-preview.json \
  --output /tmp/ai-daily-preview.md
```

生产运行、密钥配置、故障恢复和回滚说明见 [`docs/OPERATIONS.md`](docs/OPERATIONS.md)。完整实施设计见 [`docs/plan/PLAN.md`](docs/plan/PLAN.md)。

---

Powered by 🍗 鸡胸肉
