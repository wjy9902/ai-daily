# 甲鱼AI日报

> 每日 AI 前沿技术情报，自动生成。内容由 AI 辅助创作，可能存在错误，请以原始信息为准。

🔗 **网站**: https://wjy9902.github.io/ai-daily/  
📡 **RSS**: https://wjy9902.github.io/ai-daily/rss.xml

## 内容板块

- 🧪 **前沿论文** — HuggingFace / arXiv 热门论文精选
- 🔥 **技术热点** — Hacker News / KOL 技术讨论
- 🛠️ **值得试的项目** — GitHub Trending / Product Hunt
- 📊 **行业动态** — 融资、发布、格局变化
- 🇨🇳 **国内 AI 动态** — 大模型、产品、政策
- 💡 **产品机会** — 技术→产品的可行性分析
- ✅ **今日行动项** — 具体可执行建议

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
```

生产运行、密钥配置、故障恢复和回滚说明见 [`docs/OPERATIONS.md`](docs/OPERATIONS.md)。完整实施设计见 [`docs/plan/PLAN.md`](docs/plan/PLAN.md)。

---

Powered by 🍗 鸡胸肉
