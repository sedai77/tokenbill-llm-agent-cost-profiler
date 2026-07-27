# Security Policy

**Traces contain your prompts — treat trace files as secrets.** A Token Bill
trace line is the full request payload of one API call: your system prompt,
your tool definitions, your conversation history, your tool results. Everything
runs locally; nothing phones home — Token Bill itself never opens a network
connection. The security story is therefore mostly about how you handle the
files it reads and writes.

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a vulnerability

Please report vulnerabilities privately via
[GitHub private vulnerability reporting](https://github.com/sedai77/tokenbill-llm-agent-cost-profiler/security/advisories/new)
— do **not** open a public issue for anything security-sensitive. You should
hear back within 7 days; fixes for confirmed issues ship as a patch release
with credit in the changelog (unless you prefer to stay anonymous).

## Security model (what Token Bill does and does not touch)

Knowing the attack surface helps you decide whether something is in scope:

- **Trace files are secrets.** `tokenbill analyze` reads them, the `Recorder`
  writes them, and both leave them wherever you pointed the path. Do not
  commit traces, attach them to public issues unredacted, or ship them to
  third parties you would not send the underlying prompts to. The bug-report
  template asks for a *redacted* trace line for exactly this reason.
- **No network access, ever.** There is no code path in Token Bill that opens
  a socket. The demo is fully synthetic and offline; analysis and reporting
  are offline; the recorder (`tokenbill.instrument.Recorder`) wraps *your*
  SDK client object — the API calls are made by your SDK with your
  credentials, and Token Bill only observes the arguments and the returned
  usage. CI runs with zero secrets.
- **Zero runtime dependencies.** The package imports only the Python standard
  library at runtime. It never imports the `anthropic` SDK — the recorder is
  duck-typed — so there is no third-party code in the supply chain beyond
  Python itself.
- **Credentials.** Token Bill never reads, stores, or logs API keys. Keys
  live in your SDK client; the recorder captures request payloads and usage,
  not client configuration or headers.
- **Report output.** The HTML report embeds excerpts of your trace (breaker
  evidence spans, segment labels), so treat `report.html` with sensitivity
  similar to the trace itself. The report is self-contained — inline CSS and
  inline SVG, no external resources — and all trace-derived text is escaped
  before embedding. A report generated from a hostile trace file that renders
  as active content in the browser would be a vulnerability — please report
  it.
- **Parsing hostile input.** `read_trace` validates schema, fields, and usage
  values and fails with a precise `TraceError`. A crafted trace file that
  causes anything worse than a clean error (resource exhaustion aside from
  honest file size, code execution, path traversal via report output) is in
  scope.

Issues in provider SDKs or in the provider's cache/pricing behavior belong to
those projects; anything about how Token Bill records, parses, simulates, or
renders is in scope here.
