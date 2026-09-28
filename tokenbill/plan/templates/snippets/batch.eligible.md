### batch.eligible — move non-interactive calls to batch processing

CI, evaluation, scheduled, and other non-interactive calls can use an asynchronous batch offering
when their completion deadline and retry path allow it. Use the provider route identified in the
finding:

- Anthropic: Message Batches API.
- OpenAI: Batch API; Flex can suit lower-priority traffic.
- Amazon Bedrock: batch inference, subject to model and pricing support.

Confirm the served tier and billed result after rollout. Do not assume that batch and on-demand
inference have the same prompt-cache behavior.
