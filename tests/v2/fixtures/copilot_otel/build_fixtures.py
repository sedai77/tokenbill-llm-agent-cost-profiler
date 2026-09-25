"""Build the CP-OTEL JSON-lines fixtures (run as a script: ``python build_fixtures.py``).

Every fixture is synthetic: shapes from SPEC-v0.2-COPILOT.md §5.10 (VS Code OTel-JS
``readableSpanToJson`` dumps, OTLP/JSON collector files, the experimental CLI envelope) and §5.14
(gh-aw ``token-usage.jsonl`` field list), values invented. The content canary is planted in every
content attribute, span name, tool argument, log body and repository name; the canary login in the
identity attributes. Output is byte-deterministic (sorted keys, fixed timestamps).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CANARY = "TB-CANARY-7f3a91"
CANARY_LOGIN = "tb-canary-login-7f3a91"
CANARY_EMAIL = f"canary.{CANARY}@example.com"
T0_S = 1_790_157_600          # 2026-09-23T10:00:00Z


def hr(offset_ms: int) -> list[int]:
    """OTel-JS HrTime ``[seconds, nanos]`` at T0 + *offset_ms*."""
    total = T0_S * 1000 + offset_ms
    return [total // 1000, (total % 1000) * 1_000_000]


def nanos(offset_ms: int) -> str:
    """OTLP ``*UnixNano`` decimal string at T0 + *offset_ms*."""
    return str((T0_S * 1000 + offset_ms) * 1_000_000)


def iso(offset_ms: int) -> str:
    import datetime as dt
    t = dt.datetime.fromtimestamp(T0_S + offset_ms / 1000, tz=dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def content(tag: str) -> dict[str, str]:
    """Content attributes (every one must be counted, never decoded)."""
    return {
        "gen_ai.input.messages": json.dumps([{"role": "user", "parts": [
            {"type": "text", "content": f"{tag} refactor the billing module {CANARY}"}]}]),
        "gen_ai.output.messages": json.dumps([{"role": "assistant", "parts": [
            {"type": "text", "content": f"done {CANARY}"}]}]),
        "gen_ai.system_instructions": f"You are Copilot {CANARY}",
        "gen_ai.tool.definitions": json.dumps([{"name": f"read_file_{CANARY}"}]),
    }


# ---------------------------------------------------------------------------------------------
# (a) VS Code OTel-JS dump
# ---------------------------------------------------------------------------------------------

VSCODE_RESOURCE = {"attributes": {"service.name": "copilot-chat", "service.version": "0.35.3",
                                  "user.name": CANARY_LOGIN, "host.name": f"host-{CANARY}",
                                  "vcs.repository.name": f"acme/{CANARY}-repo"}}


def js_span(trace: str, span: str, parent: str | None, name: str, start: int, end: int,
            attrs: dict[str, Any], *, resource: dict[str, Any] | None = None,
            events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "traceId": trace, "spanId": span, "name": name, "kind": 2,
        "startTime": hr(start), "endTime": hr(end), "attributes": attrs,
        "status": {"code": 0}, "events": events or [], "links": [],
        "resource": resource if resource is not None else VSCODE_RESOURCE,
        "instrumentationScope": {"name": "copilot-chat", "version": "0.35.3"},
    }
    if parent is not None:
        obj["parentSpanContext"] = {"traceId": trace, "spanId": parent, "traceFlags": 1}
    return obj


def vscode_otel() -> list[dict[str, Any]]:
    trace = "a1" * 16
    conv = "conv-vscode-1"
    root = js_span(trace, "a100000000000001", None, f"invoke_agent GitHub Copilot {CANARY}", 0,
                   9000, {"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": conv,
                          "gen_ai.agent.name": "GitHub Copilot Chat",
                          "gen_ai.input.messages": f"[{CANARY}]"})
    chat1 = js_span(trace, "a100000000000002", "a100000000000001",
                    f"chat claude-sonnet-4.5 {CANARY}", 100, 4100, {
                        "gen_ai.operation.name": "chat", "gen_ai.provider.name": "github",
                        "gen_ai.request.model": "claude-sonnet-4.5",
                        "gen_ai.response.model": "claude-sonnet-4.5",
                        "gen_ai.response.id": "resp_vs_001", "gen_ai.conversation.id": conv,
                        "gen_ai.usage.input_tokens": 20000,
                        "gen_ai.usage.cache_read.input_tokens": 15000,
                        "gen_ai.usage.cache_creation.input_tokens": 3000,
                        "gen_ai.usage.output_tokens": 700,
                        "copilot_chat.copilot_usage_nano_aiu": 12_345_600_000,
                        "copilot_chat.turn.index": 0, "copilot_chat.location": "panel",
                        "copilot_chat.time_to_first_token": 850, **content("t0")},
                    events=[{"name": "gen_ai.content.prompt", "time": hr(150),
                             "attributes": {"gen_ai.prompt": f"prompt {CANARY}"}}])
    tool = js_span(trace, "a100000000000003", "a100000000000001", f"execute_tool {CANARY}", 4200,
                   4300, {"gen_ai.operation.name": "execute_tool",
                          "gen_ai.tool.name": "read_file",
                          "gen_ai.tool.call.arguments": json.dumps({"path": f"/src/{CANARY}.py"}),
                          "gen_ai.tool.call.result": f"contents {CANARY}",
                          "github.copilot.tool.parameters.file_path": f"/src/{CANARY}.py"})
    chat2 = js_span(trace, "a100000000000004", "a100000000000001", f"chat auto {CANARY}", 4400,
                    8400, {
                        "gen_ai.operation.name": "chat", "gen_ai.provider.name": "github",
                        "gen_ai.request.model": "auto", "gen_ai.response.model": "gpt-5-mini",
                        "gen_ai.response.id": "resp_vs_002", "gen_ai.conversation.id": conv,
                        "gen_ai.request.reasoning.level": "medium",
                        "gen_ai.usage.input_tokens": 8000,
                        "gen_ai.usage.cache_read.input_tokens": 6000,
                        "gen_ai.usage.cache_creation.input_tokens": 0,
                        "gen_ai.usage.output_tokens": 400,
                        "gen_ai.usage.reasoning.output_tokens": 100,
                        "copilot_chat.copilot_usage_nano_aiu": 1_000_000_000,
                        "copilot_chat.request.max_prompt_tokens": 128000,
                        "copilot_chat.turn.index": 1, "github.copilot.cost": 0.33,
                        **content("t1")})
    log = {"hrTime": hr(8500), "hrTimeObserved": hr(8500), "severityNumber": 9,
           "severityText": "INFO", "body": f"user prompt {CANARY}",
           "attributes": {"event.name": "copilot_chat.user_prompt", "prompt": CANARY},
           "resource": VSCODE_RESOURCE,
           "instrumentationScope": {"name": "copilot-chat", "version": "0.35.3"}}
    return [root, chat1, tool, chat2, log]


# ---------------------------------------------------------------------------------------------
# (c) CLI envelope (experimental)
# ---------------------------------------------------------------------------------------------

CLI_RESOURCE = {"attributes": {"service.name": "github-copilot", "service.version": "1.0.70",
                               "enduser.pseudo.id": CANARY_LOGIN}}


def cli_span(span: str, parent: str | None, name: str, start: int, end: int,
             attrs: dict[str, Any], events: list[dict[str, Any]] | None = None
             ) -> dict[str, Any]:
    trace = "c2" * 16
    obj: dict[str, Any] = {
        "type": "span", "name": name, "spanContext": {"traceId": trace, "spanId": span},
        "startTime": hr(start), "endTime": hr(end), "attributes": attrs,
        "events": events or [], "status": {"code": 1}, "resource": CLI_RESOURCE,
        "instrumentationScope": {"name": "github.copilot.cli"},
    }
    if parent is not None:
        obj["parentSpanId"] = parent
    return obj


def cli_otel() -> list[dict[str, Any]]:
    conv = "cli-session-1"
    base = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "github",
            "gen_ai.conversation.id": conv}
    chats = [
        cli_span("c200000000000002", "c200000000000001", f"chat {CANARY}", 100, 2100, {
            **base, "gen_ai.request.model": "claude-opus-4.8", "gen_ai.response.model":
            "claude-opus-4.8", "gen_ai.response.id": "resp_cli_1",
            "gen_ai.usage.input_tokens": 30000, "gen_ai.usage.cache_read.input_tokens": 25000,
            "gen_ai.usage.cache_creation.input_tokens": 4000, "gen_ai.usage.output_tokens": 900,
            "github.copilot.nano_aiu": 5_000_000_000, "github.copilot.cost": 1,
            **content("c1")}),
        cli_span("c200000000000003", "c200000000000001", f"chat {CANARY}", 2200, 4200, {
            **base, "gen_ai.request.model": "auto", "gen_ai.response.model": "claude-haiku-4.5",
            "gen_ai.response.id": "resp_cli_2", "gen_ai.usage.input_tokens": 12000,
            "gen_ai.usage.cache_read.input_tokens": 10000, "gen_ai.usage.output_tokens": 300,
            "github.copilot.nano_aiu": 2_500_000_000, **content("c2")}),
        cli_span("c200000000000004", "c200000000000001", f"chat {CANARY}", 4300, 4800, {
            **base, "gen_ai.request.model": "gpt-4o-mini", "gen_ai.response.model":
            "gpt-4o-mini", "gen_ai.response.id": "resp_cli_3",
            "gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 20,
            "github.copilot.nano_aiu": 0}),
    ]
    events = [
        {"name": "github.copilot.session.compaction_start", "time": hr(4850), "attributes": {}},
        {"name": "github.copilot.session.compaction_complete", "time": hr(4900),
         "attributes": {"trigger": "threshold", "pre_compaction_tokens": 90000,
                        "post_compaction_tokens": 12000, "summary": f"summary {CANARY}"}},
        {"name": "github.copilot.session.truncation", "time": hr(4950),
         "attributes": {"tokens_removed": 5000}},
        {"name": "github.copilot.session.shutdown", "time": hr(5000), "attributes": {}},
    ]
    root = cli_span("c200000000000001", None, f"invoke_agent {CANARY}", 0, 5000, {
        "gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": conv,
        "github.copilot.nano_aiu": 7_500_000_000, "github.copilot.cost": 1,
        "gen_ai.input.messages": CANARY}, events)
    return [root, *chats]


# ---------------------------------------------------------------------------------------------
# (b) OTLP/JSON collector files
# ---------------------------------------------------------------------------------------------

def any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def kv(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": any_value(v)} for k, v in attrs.items()]


def otlp_span(trace: str, span: str, parent: str | None, name: str, start: int, end: int,
              attrs: dict[str, Any], events: list[dict[str, Any]] | None = None
              ) -> dict[str, Any]:
    return {"traceId": trace, "spanId": span, "parentSpanId": parent or "", "name": name,
            "kind": 3, "startTimeUnixNano": nanos(start), "endTimeUnixNano": nanos(end),
            "attributes": kv(attrs), "events": events or [], "status": {}}


def resource_spans(resource: dict[str, Any], spans: list[dict[str, Any]],
                   scope: str = "copilot") -> dict[str, Any]:
    return {"resource": {"attributes": kv(resource)},
            "scopeSpans": [{"scope": {"name": scope}, "spans": spans}]}


def chat_attrs(conv: str, model: str, resp: str | None, inp: int, read: int, create: int,
               out: int, nano: int | None, turn: int | None,
               key: str = "github.copilot.nano_aiu") -> dict[str, Any]:
    attrs: dict[str, Any] = {"gen_ai.operation.name": "chat", "gen_ai.conversation.id": conv,
                             "gen_ai.request.model": model, "gen_ai.response.model": model,
                             "gen_ai.usage.input_tokens": inp,
                             "gen_ai.usage.cache_read.input_tokens": read,
                             "gen_ai.usage.output_tokens": out}
    if turn is not None:
        attrs["copilot_chat.turn.index"] = turn
    if create:
        attrs["gen_ai.usage.cache_creation.input_tokens"] = create
    if resp is not None:
        attrs["gen_ai.response.id"] = resp
    if nano is not None:
        attrs[key] = nano
    return attrs


def copilot_otlp() -> list[dict[str, Any]]:
    """Native CLI spans + a VS Code synthesized duplicate in the same trace; a subagent; a trace
    with only a synthesized span."""
    t5, t6 = "e5" * 16, "e6" * 16
    conv = "cli-conv-9"
    native = [
        otlp_span(t5, "e500000000000000", None, f"invoke_agent {CANARY}", 0, 9000,
                  {"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": conv,
                   "github.copilot.nano_aiu": 4_000_000_000}),
        otlp_span(t5, "e500000000000001", "e500000000000000", f"chat {CANARY}", 100, 1100,
                  {**chat_attrs(conv, "claude-sonnet-4.5", "resp_native_1", 5000, 4000, 500,
                                300, 3_000_000_000, 0), **content("n1")}),
        otlp_span(t5, "e500000000000002", "e500000000000000", f"execute_tool {CANARY}", 1200,
                  6000, {"gen_ai.operation.name": "execute_tool",
                         "gen_ai.tool.call.arguments": CANARY}),
        otlp_span(t5, "e500000000000003", "e500000000000002", f"invoke_agent {CANARY}", 1300,
                  5900, {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.id": "sub-1",
                         "gen_ai.agent.name": "explore", "gen_ai.conversation.id": conv,
                         "github.copilot.nano_aiu": 1_000_000_000},
                  [{"timeUnixNano": nanos(5800), "name": "github.copilot.session.truncation",
                    "attributes": kv({"tokens_removed": 700})}]),
        otlp_span(t5, "e500000000000004", "e500000000000003", f"chat {CANARY}", 1400, 2400,
                  chat_attrs(conv, "gpt-5-mini", "resp_native_2", 2000, 1000, 0, 100,
                             1_000_000_000, 0)),
    ]
    synthesized = [
        otlp_span(t5, "e5000000000000f1", None, f"chat {CANARY}", 110, 1090,
                  chat_attrs(conv, "claude-sonnet-4.5", None, 5000, 4000, 0, 300,
                             3_000_000_000, 0, "copilot_chat.copilot_usage_nano_aiu")),
        otlp_span(t6, "e600000000000001", None, f"chat {CANARY}", 20000, 21000,
                  chat_attrs("cli-conv-10", "gpt-5-mini", None, 1000, 0, 0, 50, 500_000_000, 0,
                             "copilot_chat.copilot_usage_nano_aiu")),
    ]
    line = {"resourceSpans": [
        resource_spans({"service.name": "github-copilot", "service.version": "1.0.70"}, native,
                       "github.copilot"),
        resource_spans({"service.name": "copilot-chat", "service.version": "0.35.3"},
                       synthesized, "copilot-chat"),
    ]}
    return [line]


def jetbrains_otlp() -> list[dict[str, Any]]:
    trace = "d4" * 16
    spans = [
        otlp_span(trace, "d400000000000001", None, f"chat {CANARY}", 0, 1000,
                  chat_attrs("jb-conv-1", "claude-sonnet-4.5", "resp_jb_1", 4000, 3000, 0, 200,
                             None, None)),
        otlp_span(trace, "d400000000000002", None, f"chat {CANARY}", 2000, 3000,
                  chat_attrs("jb-conv-1", "gpt-5-mini", "resp_jb_2", 1500, 0, 0, 80, None, None)),
        otlp_span(trace, "d400000000000003", None, f"jetbrains.request {CANARY}", 3100, 3200,
                  {"jetbrains.request.kind": "completion", "prompt": CANARY}),
    ]
    return [{"resourceSpans": [resource_spans(
        {"service.name": "copilot-intellij", "service.version": "1.5.52-241",
         "user.name": CANARY_LOGIN}, spans, "com.github.copilot.intellij")]}]


def claude_logs() -> dict[str, Any]:
    """Two Claude Code ``api_request`` events (TELEM's ``otlp`` owns them)."""
    common = {"session.id": "claude-sess-1", "user.email": CANARY_EMAIL,
              "query_source": "repl_main_thread", "model": "claude-opus-5-5"}
    records = []
    for i, (inp, out, read, create) in enumerate(((12, 800, 0, 30000), (8, 400, 30000, 500))):
        attrs = {"event.name": "api_request", "request_id": f"req_claude_{i}",
                 "duration_ms": 3000, "input_tokens": inp, "output_tokens": out,
                 "cache_read_tokens": read, "cache_creation_tokens": create, **common}
        records.append({"attributes": kv(attrs), "body": {"stringValue": "claude_code.api_request"},
                        "timeUnixNano": nanos(60_000 + i * 10_000), "severityNumber": 9})
    return {"resourceLogs": [{"resource": {"attributes": kv({"service.name": "claude-code"})},
                              "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code"},
                                             "logRecords": records}]}]}


def mixed_otlp() -> list[dict[str, Any]]:
    trace = "f7" * 16
    copilot = {"resourceSpans": [
        resource_spans({"service.name": "github-copilot"}, [otlp_span(
            trace, "f700000000000001", None, f"chat {CANARY}", 1000, 2000,
            {**chat_attrs("mix-conv-1", "claude-sonnet-4.5", "resp_mix_1", 6000, 5000, 0, 250,
                          2_000_000_000, 0), **content("m1")})], "github.copilot"),
        resource_spans({"service.name": "acme-copilot-proxy"}, [otlp_span(
            trace, "f700000000000002", None, f"chat {CANARY}", 3000, 4000,
            chat_attrs("mix-conv-2", "gpt-5-mini", "resp_mix_2", 3000, 1000, 0, 90, None, None))],
            "acme.proxy"),
        resource_spans({"service.name": "acme-agent"}, [otlp_span(
            trace, "f700000000000003", None, f"chat {CANARY}", 5000, 6000,
            chat_attrs("mix-conv-3", "claude-haiku-4.5", "resp_mix_3", 2500, 2000, 0, 60,
                       700_000_000, None))], "acme.agent"),
    ]}
    return [claude_logs(), copilot]


# ---------------------------------------------------------------------------------------------
# gh-aw token-usage.jsonl
# ---------------------------------------------------------------------------------------------

def gh_aw_line(offset: int, rid: str, model: str, inp: int, read: int, write: int, out: int,
               credits: str, total: str, path: str, *, include: bool | None = True,
               reasoning: int | None = None, provider: str = "copilot", status: int = 200
               ) -> str:
    fields = [
        '"_schema":"token-usage/v1"', f'"timestamp":"{iso(offset)}"', '"event":"token_usage"',
        f'"request_id":"{rid}"', f'"provider":"{provider}"', f'"model":"{model}"',
        f'"path":"{path}"', f'"status":{status}', '"streaming":true',
        f'"input_tokens":{inp}', f'"output_tokens":{out}', f'"cache_read_tokens":{read}',
        f'"cache_write_tokens":{write}']
    if reasoning is not None:
        fields.append(f'"reasoning_tokens":{reasoning}')
    fields += [
        '"duration_ms":2000', '"response_bytes":4096', '"x_initiator":"agent"',
        f'"ai_credits_this_response":{credits}', f'"ai_credits_total":{total}',
        '"ai_credits_pricing_source":"models.dev"', '"ai_credits_pricing_tier":"standard"',
        '"ai_credits_accounting_policy":"per_response"',
        '"ai_credits_fallback_pricing_used":false']
    if include is not None:
        fields.append(f'"input_tokens_include_cache":{str(include).lower()}')
    return "{" + ",".join(fields) + "}"


def gh_aw_lines() -> list[str]:
    return [
        gh_aw_line(0, "req-0001", "claude-sonnet-4.5", 12000, 9000, 1000, 800, "1.234567",
                   "1.234567", "/v1/messages"),
        gh_aw_line(3000, "req-0002", "gpt-4o-mini-2024-07-18", 3000, 0, 0, 150, "0.012345",
                   "1.246912", "/chat/completions"),
        gh_aw_line(6000, "req-0003", "gpt-5-mini", 5000, 2048, 0, 600, "0.0421", "1.289012",
                   "/responses", include=None, reasoning=256),
        gh_aw_line(9000, "req-0004", "claude-sonnet-4.5", 15000, 12000, 0, 1000, "1.5",
                   "2.789012", "/v1/models"),
        gh_aw_line(12000, "req-0005", "claude-sonnet-4.5", 9000, 8500, 0, 200, "0.9", "3.689012",
                   "/v1/messages"),
    ]


def gh_aw_mixed_lines() -> list[str]:
    lines = gh_aw_lines()
    lines.insert(2, gh_aw_line(4000, "req-oa-1", "gpt-4.1", 100, 0, 0, 10, "0.001", "0.001",
                               "/chat/completions", provider="openai"))
    lines.append('{"_schema":"token-usage/v1","event":"proxy_start","timestamp":"'
                 + iso(13000) + '"}')
    lines.append('{"_schema":"token-usage/v1","event":"token_usage","provider":"copilot",'
                 '"timestamp":"not-a-time","model":"gpt-5-mini"}')
    lines.append(gh_aw_line(14000, "req-0006", "gpt-5-mini", -5, 0, 0, 1, "0.1", "3.789012",
                            "/responses"))
    lines.append("{not json " + CANARY)
    return lines


# ---------------------------------------------------------------------------------------------

def _dump(objs: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(o, sort_keys=True, separators=(",", ":")) + "\n" for o in objs)


FILES = {
    "vscode_otel/vscode-otel.jsonl": lambda: _dump(vscode_otel()),
    "cli/cli-otel.jsonl": lambda: _dump(cli_otel()),
    "otlp/copilot-otlp.jsonl": lambda: _dump(copilot_otlp()),
    "jetbrains/jetbrains-otlp.jsonl": lambda: _dump(jetbrains_otlp()),
    "mixed/mixed-otlp.jsonl": lambda: _dump(mixed_otlp()),
    "gh_aw/token-usage.jsonl": lambda: "".join(line + "\n" for line in gh_aw_lines()),
    "gh_aw/token-usage-mixed.jsonl": lambda: "".join(line + "\n"
                                                     for line in gh_aw_mixed_lines()),
}


def build(out: Path = HERE) -> dict[str, str]:
    """Write every fixture under *out*; returns {relative path: text}."""
    texts = {rel: make() for rel, make in FILES.items()}
    for rel, text in texts.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return texts


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE)
