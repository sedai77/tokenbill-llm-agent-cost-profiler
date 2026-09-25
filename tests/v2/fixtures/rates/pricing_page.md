# Pricing (synthetic fixture page)

Synthetic page in the table layout of the Claude pricing page; prices are the registry values.

## Model pricing

| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| :---- | :---- | :---- | :---- | :---- | :---- |
| Claude Fable 5.1 | $10 / MTok | $12.5 / MTok | $20 / MTok | $0.25 / MTok<sup>1</sup> | $50 / MTok |
| Claude Mythos 5.1 ([limited availability](https://example.test/glasswing)) | $10 / MTok | $12.5 / MTok | $20 / MTok | $0.25 / MTok<sup>1</sup> | $50 / MTok |
| Claude Fable 5 | $10 / MTok | $12.5 / MTok | $20 / MTok | $1 / MTok | $50 / MTok |
| Claude Mythos 5 ([limited availability](https://example.test/glasswing)) | $10 / MTok | $12.5 / MTok | $20 / MTok | $1 / MTok | $50 / MTok |
| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.2 / MTok | $20 / MTok |
| Claude Opus 5 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.5 / MTok | $25 / MTok |
| Claude Opus 4.8 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.5 / MTok | $25 / MTok |
| Claude Opus 4.7 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.5 / MTok | $25 / MTok |
| Claude Opus 4.6 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.5 / MTok | $25 / MTok |
| Claude Opus 4.5 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.5 / MTok | $25 / MTok |
| Claude Opus 4.1 | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.5 / MTok | $75 / MTok |
| Claude Opus 4 | $15 / MTok | $18.75 / MTok | $30 / MTok | $1.5 / MTok | $75 / MTok |
| Claude Sonnet 5 | $2 / MTok | $2.5 / MTok | $4 / MTok | $0.2 / MTok | $10 / MTok |
| Claude Sonnet 4.6 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.3 / MTok | $15 / MTok |
| Claude Sonnet 4.5 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.3 / MTok | $15 / MTok |
| Claude Sonnet 4 | $3 / MTok | $3.75 / MTok | $6 / MTok | $0.3 / MTok | $15 / MTok |
| Claude Haiku 4.5 | $1 / MTok | $1.25 / MTok | $2 / MTok | $0.1 / MTok | $5 / MTok |
| Claude Haiku 3.5 | $0.8 / MTok | $1 / MTok | $1.6 / MTok | $0.08 / MTok | $4 / MTok |

## Data residency pricing

For Claude 4.6 and later models, using `inference_geo: "us"` applies a 1.1x pricing multiplier.

### Fast mode pricing

| Model | Input | Output |
| ---- | ---- | ---- |
| Claude Opus 5.5 | $8 / MTok | $40 / MTok |
| Claude Opus 5 / Claude Opus 4.8 | $10 / MTok | $50 / MTok |

### Batch processing

| Model | Batch input | Batch output |
| :---- | :---- | :---- |
| Claude Fable 5.1 | $5 / MTok | $25 / MTok |
| Claude Mythos 5.1 | $5 / MTok | $25 / MTok |
| Claude Fable 5 | $5 / MTok | $25 / MTok |
| Claude Mythos 5 | $5 / MTok | $25 / MTok |
| Claude Opus 5.5 | $2 / MTok | $10 / MTok |
| Claude Opus 5 | $2.5 / MTok | $12.5 / MTok |
| Claude Opus 4.8 | $2.5 / MTok | $12.5 / MTok |
| Claude Opus 4.7 | $2.5 / MTok | $12.5 / MTok |
| Claude Opus 4.6 | $2.5 / MTok | $12.5 / MTok |
| Claude Opus 4.5 | $2.5 / MTok | $12.5 / MTok |
| Claude Opus 4.1 | $7.5 / MTok | $37.5 / MTok |
| Claude Opus 4 | $7.5 / MTok | $37.5 / MTok |
| Claude Sonnet 5 | $1 / MTok | $5 / MTok |
| Claude Sonnet 4.6 | $1.5 / MTok | $7.5 / MTok |
| Claude Sonnet 4.5 | $1.5 / MTok | $7.5 / MTok |
| Claude Sonnet 4 | $1.5 / MTok | $7.5 / MTok |
| Claude Haiku 4.5 | $0.5 / MTok | $2.5 / MTok |
| Claude Haiku 3.5 | $0.4 / MTok | $2 / MTok |

#### Web search tool

Web search is available for **$10 per 1,000 searches**, plus token costs.
