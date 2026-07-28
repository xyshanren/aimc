# 🧭 AIMC - AI Model Compass

> Make every token count.

AIMC (AI Model Compass) is a **lightweight LLM gateway and pricing intelligence aggregator** designed for individual developers.

It continuously monitors pricing and promotions from major Chinese AI providers (DeepSeek, Qwen, GLM, etc.), and automatically routes your API requests to the most cost-effective channel based on real-time pricing tiers and rate-limit status — with zero code invasion.

## Features

- **Smart Routing** — Multi-dimensional weighting (time-of-day pricing / quota / rate limits) to pick the cheapest channel
- **Zero-invasion Proxy** — OpenAI SDK compatible; stream/non-stream passthrough without parsing SSE
- **Pricing Intelligence** — Scheduled crawling of provider pricing & promotion pages with AI-powered extraction
- **Rate Limit Management** — In-memory sliding window tracking (QPM/TPM/concurrency/daily quota)
- **Multi-account Failover** — Round-robin across accounts; auto-switch on quota exhaustion
- **Self-healing** — Confidence-based AI parsing; auto-commit high-confidence results, alert on low
- **Minimal Deployment** — Single process, single-file DB, zero external dependencies

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env        # Edit with your API keys
python scripts/manual_import.py
uvicorn main:app --host 0.0.0.0 --port 8080
```

Then point your OpenAI SDK to `http://localhost:8080/v1`.

## Documentation

- [Architecture](docs/architecture.md)
- [Quick Start Guide](docs/quickstart.md)
- [API Reference](docs/api.md)

## License

[MulanPSL-2.0](LICENSE)
