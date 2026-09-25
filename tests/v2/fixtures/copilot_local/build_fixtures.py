"""Regenerate the checked-in CP-LOCAL fixture ``home/`` (run as a script from the repository root:
``python tests/v2/fixtures/copilot_local/build_fixtures.py``).

Synthetic data only, shaped after ``github/copilot-sdk`` ``nodejs/src/generated/session-events.ts``
@075f027 (the interfaces used are unchanged on ``main``, read 2026-09-25). Every content field
(prompts, responses, reasoning, tool arguments and results, summaries, paths, branch, repository,
error text, changed files) carries the canary ``TB-CANARY-7f3a91``.

Session A (``…0a``): interactive, Claude Sonnet 4.5, long-context tier; a chunked response with a tool
call; a checkpoint with a 300 s cache TTL; a subagent call; a model switch to GPT-5.4; a truncation;
an error; a clean shutdown whose rollup arithmetic is 23,399 = 6 + 10,069 + 13,324.
Session B (``…0b``): Auto model selection; a threshold compaction carrying the G11 ``tokenDetails``
payload (Appendix C); a clean shutdown.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.v2.copilot_local.helpers import (  # noqa: E402
    HOME,
    SID_A,
    SID_B,
    T0,
    Events,
    session_file,
    usage,
)


def session_a() -> Events:
    ev = Events(T0, prefix="a")
    ev.start(SID_A, selectedModel="claude-sonnet-4.5", contextTier="long_context",
             reasoningEffort="high", sessionLimits={"maxAiCredits": 500})
    ev.user()
    ev.add("assistant.turn_start", {"turnId": "0", "model": "claude-sonnet-4.5"})
    ev.message("chatcmpl-A1", 120, turnId="0", chunkIndex=0, chunkCount=2)
    ev.tool("tc-1", "bash")
    ev.message("chatcmpl-A1", 120, turnId="0", chunkIndex=1, chunkCount=2)
    ev.add("session.usage_checkpoint", {
        "totalNanoAiu": 1_000_000_000,
        "modelCacheState": [{"modelId": "claude-sonnet-4.5", "cacheTtlSeconds": 300,
                             "cacheExpiresAt": "2026-09-20T10:15:00.000Z"}]})
    ev.message("chatcmpl-A2", 80, turnId="1")
    ev.add("subagent.started", {"agentName": "explore", "agentDisplayName": "Explore",
                                "agentDescription": "search the tree for TB-CANARY-7f3a91",
                                "toolCallId": "tc-2"})
    ev.message("chatcmpl-S1", 50, agent="agent-1", turnId="0")
    ev.add("session.model_change", {"newModel": "gpt-5.4", "previousModel": "claude-sonnet-4.5",
                                    "source": "model_command"})
    ev.add("session.truncation", {
        "tokensRemovedDuringTruncation": 4_000, "messagesRemovedDuringTruncation": 3,
        "performedBy": "BasicTruncator", "postTruncationMessagesLength": 10,
        "postTruncationTokensInMessages": 90_000, "preTruncationMessagesLength": 13,
        "preTruncationTokensInMessages": 94_000, "tokenLimit": 128_000})
    ev.message("chatcmpl-A3", 60, model="gpt-5.4", turnId="2")
    ev.add("session.error", {"errorType": "rate_limit", "statusCode": 429,
                             "message": "limited TB-CANARY-7f3a91",
                             "stack": "at TB-CANARY-7f3a91"})
    ev.add("session.title_changed", {"title": "TB-CANARY-7f3a91"}, ephemeral=True)
    ev.shutdown({"claude-sonnet-4.5": usage(23_399, 10_069, 13_324, 250),
                 "gpt-5.4": usage(5_000, 1_000, 0, 60)},
                nano={"claude-sonnet-4.5": 2_000_000_000, "gpt-5.4": 300_000_000})
    return ev


def session_b() -> Events:
    ev = Events(T0 + 3_600_000, prefix="b")
    ev.start(SID_B, selectedModel="auto", contextTier="default")
    ev.user()
    ev.add("session.auto_mode_resolved", {"chosenModel": "claude-haiku-4.5",
                                          "availableModels": ["claude-haiku-4.5", "gpt-5.4"]})
    ev.message("chatcmpl-B1", 40, model="claude-haiku-4.5", turnId="0")
    ev.compaction()
    ev.message("chatcmpl-B2", 30, model="claude-haiku-4.5", turnId="1")
    ev.shutdown({"claude-haiku-4.5": usage(2_000, 1_000, 500, 70)},
                nano={"claude-haiku-4.5": 40_000_000})
    return ev


def main() -> None:
    session_a().write(session_file(HOME, SID_A))
    session_b().write(session_file(HOME, SID_B))
    print(f"wrote {HOME}")


if __name__ == "__main__":
    main()
