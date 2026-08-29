# codexctl Architecture

Internal structure of `codexctl`. This document describes modules, seams,
and protocol adaptation only. Everything observable from the outside —
commands, output shapes, error codes, exit codes — is defined once in
[reference.md](reference.md) and linked from here, never repeated.

## Design principle

`codexctl` is built as a chain of deep modules with narrow interfaces:

```text
argv ──> cli.py ──> CodexCtl.run(Command) -> Outcome ──> render.py ──> stdout/stderr
                       │
                       ├── endpoint.py   (where is the runtime?)
                       ├── appserver.py  (how do we speak to it?)
                       └── rollout.py    (best-effort context enrichment)
```

Each seam exists because two real production behaviors differ on either
side of it:

- CLI parsing/rendering vs execution, so output formats never influence
  execution behavior.
- `CodexCtl.run` vs the runtime, so the core vocabulary
  ([model.py](#modelpy--the-closed-vocabulary)) never mentions JSON-RPC,
  sockets, or Codex wire types.
- Core vs endpoint resolution, because managed, external, one-shot stdio, and
  SSH runtimes have different lifecycle responsibilities.
- Core vs protocol, because Codex protocol churn must be absorbed in one
  place (the projection layer).

## Module map

| Module | Responsibility |
|---|---|
| [cli.py](../src/codexctl/cli.py) | argv parsing, output-mode validation, signals, exit codes |
| [model.py](../src/codexctl/model.py) | The closed vocabulary: commands, outcomes, projected events, selectors, error codes |
| [core.py](../src/codexctl/core.py) | `CodexCtl`: command dispatch, orchestration, race handling, follow frontier, error mapping |
| [endpoint.py](../src/codexctl/endpoint.py) | Endpoint resolution: managed daemon, external endpoint, stdio process target, and SSH remote provider |
| [appserver.py](../src/codexctl/appserver.py) | Transport framing, JSON-RPC routing, initialize handshake, unattended interaction policy, projection |
| [rollout.py](../src/codexctl/rollout.py) | Read-only, narrow, best-effort reader of Codex rollout files |
| [render.py](../src/codexctl/render.py) | text/json/jsonl renderers; single source of structured JSON documents |

### model.py — the closed vocabulary

Everything crossing `CodexCtl.run` is defined here: `Command` values
(`Start`, `Resume`, `Status`, `History`, `Follow`, `Steer`, `Interrupt`,
`ListThreads`, `Doctor`), outcome values (snapshots,
`EventStreamOutcome`, `TurnTerminal`), `ProjectedEvent`, selectors, and
the stable error codes enumerated in
[reference.md — Error codes](reference.md#error-codes).

`IsolationOptions` is the frozen, closed value object shared by `StartConfig`
and `Resume`; `Follow` has no isolation configuration because it is
observational.

Selector parsing is pure and follows Python semantics exactly
(`parse_turn_selector`, `parse_replay_selector`, `apply_turn_selector`,
`select_replay_turns`); the accepted syntaxes are specified in
[reference.md — history](reference.md#history) and
[reference.md — follow](reference.md#follow).

### core.py — orchestration

`CodexCtl` owns all lifecycle behavior. Key internal decisions:

- **Races resolve at the authoritative RPC.** `resume` reads thread state
  first as an optimization, but the `turn/start` result is authoritative.
  `steer` sends the `expectedTurnId` observed from a preceding read, keeping
  each operation attached to the turn it inspected. See
  [reference.md — resume](reference.md#resume) and
  [reference.md — steer](reference.md#steer) for the public lifecycle
  semantics.
- **Streaming is pull-based with a terminal future.** `EventStreamOutcome`
  pairs an async iterator of `ProjectedEvent` with a future resolving to
  `TurnTerminal | None`. The iterator owns dedup, token-usage capture, and
  terminal detection; its `finally` block unsubscribes, closes, and
  resolves the future — including a deterministic connection-loss error
  path. Losing the stream never triggers a turn interrupt. When a stream
  or session ends and what the future resolves to are defined in
  [reference.md — follow](reference.md#follow) and
  [reference.md — Exit codes](reference.md#exit-codes).
- **follow frontier.** `follow` resumes for subscription, then one prelude
  yields the replayed continuous suffix of the reconstructed snapshot and,
  when a turn is active at attach time, its synthesized `turn/started`
  (the real start notification predates subscription), after which the
  live phase runs. Both phases record dedup keys in one shared set keyed
  by `(event type, turn id, item id)`, so events visible in replay and
  live are emitted exactly once. Replay only registers keys of events it
  actually emits, so a suppressed replay event is still delivered live.
  Its resume request carries no isolation configuration.
  The public replay, persist, termination, and turn-marker semantics are
  defined in [reference.md — follow](reference.md#follow) and
  [reference.md — Streaming text output](reference.md#streaming-text-output).
- **interrupt waits, never retries.** After `turn/interrupt` succeeds,
  [core.py](../src/codexctl/core.py) polls the thread until the targeted turn is terminal (bounded
  wait, `INTERRUPT_WAIT_SECONDS` / `INTERRUPT_POLL_INTERVAL`). Rejected
  interrupts are handled by `_map_interrupt_error` before general RPC
  heuristics. The public interrupt contract is defined in
  [reference.md — interrupt](reference.md#interrupt).
- **Error translation is centralized.** `_map_rpc_error` and its specialized
  helpers combine RPC codes with provider markers. Recovery failure never
  falls back to creating a new thread. Stable error meanings and recovery
  behavior are defined in
  [reference.md — Error codes](reference.md#error-codes) and
  [reference.md — resume](reference.md#resume).
- **Doctor** reuses the same seams: endpoint resolution, connect +
  initialize handshake, the runtime-provider's asynchronous CLI-version
  probe, the app-server lifecycle compatibility probe, and the optional
  rollout sessions directory check. `RuntimePolicy` supplies lifecycle
  ownership and enrichment capabilities; the public runtime mode remains an
  identity used for diagnostics. The lifecycle probe is the compatibility
  gate; rollout context enrichment remains diagnostic-only.

### endpoint.py — runtime resolution

`RuntimeProvider` exposes an immutable `RuntimePolicy` alongside
`resolve_endpoint() -> AppServerEndpoint(display, target, runtime_pid,
runtime_version, cli_version, socket_path)`. The policy carries the default
cwd, lifecycle ownership, explicit-cwd requirements, and whether local rollout
enrichment or remote-socket diagnostic metadata is supported;
the public `mode` remains an identity and is not used to select behavior in
core. `target` is a closed transport detail and is opaque to
[core.py](../src/codexctl/core.py):

- `ManagedRuntimeProvider` contains all daemon lifecycle knowledge: probe
  the default control socket first (connecting + initializing to verify
  the runtime responds), otherwise run `codex app-server daemon start`
  and parse the last JSON line of stdout for `socketPath`/`pid`, plus optional
  runtime and CLI version metadata. No JSON
  means `INCOMPATIBLE_CODEX`; binary-missing or non-zero exit means
  `APP_SERVER_UNAVAILABLE`. The binary can be overridden with
  `CODEXCTL_CODEX_BIN` (see
  [reference.md — Runtime resolution](reference.md#runtime-resolution)).
- `ExternalRuntimeProvider` resolves external endpoint configuration using
  the [public endpoint contract](reference.md#runtime-resolution), then
  performs no lifecycle mutation. Its resolved target is transport-private;
  token contents are never part of the resolved endpoint.
- `StdioRuntimeProvider` resolves `display="stdio"` plus an exact argv tuple
  and a `StdioFraming` selector without launching anything. The private
  `_OwnedStdioProcess` owns the child process for a connection; JSONL and
  WebSocket stdio transports share that lifecycle. Their externally visible
  mode contract is defined in
  [reference.md — Runtime resolution](reference.md#runtime-resolution).
  Child cleanup is deliberately bounded: it escalates from stdin close to
  SIGTERM and SIGKILL under finite waits. Cancellation during stalled process
  creation does not wait indefinitely, so cleanup may finish asynchronously;
  immediate PID disappearance from the process table is not part of the
  internal contract.
- `SshRuntimeProvider` manages the remote daemon lifecycle when no external
  socket is supplied and resolves SSH to the existing WebSocket-over-stdio
  `StdioTarget`; public behavior is defined in
  [reference.md — SSH runtime](reference.md#ssh-runtime).

The runtime-provider interface also carries an asynchronous, best-effort
`probe_cli_version()` used by doctor. Managed providers use an asyncio child
process for the local probe; external and stdio providers return no version.
The public status context behavior is defined in
[reference.md — status](reference.md#status), and the public doctor checks are
defined in [reference.md — doctor](reference.md#doctor).

### appserver.py — the compatibility firewall

Raw Codex protocol messages never leave this module. Callers use typed
thread/turn operations and see projected results, `JsonRpcError`, and
`ProjectedEvent` only. The generic JSON-RPC request helper is private to this
module; protocol method names and payload construction do not cross the client interface.

The private isolation serializer is the only place that translates the
domain-level `IsolationOptions` into Codex config overrides. An empty value
produces no config field.

Transport and session facts (verified against the Codex source):

- `connect_app_server()` is the sole transport entry point. It owns transport
  selection, credential-file loading, connection options, process startup,
  and construction of the app-server session; compression is disabled
  for Codex compatibility. Startup, framing, and cleanup behavior are defined
  in [reference.md — Runtime resolution](reference.md#runtime-resolution).
- Messages are JSON-RPC 2.0-shaped without the `"jsonrpc"` header.
- Every connection performs the `initialize` request
  (`clientInfo`, `capabilities: {experimentalApi: false}`) followed by the
  `initialized` notification before any other traffic.

An `AppServerSession` sits above the message transports
`WebSocketMessageTransport` and `JsonlStdioMessageTransport`. The session owns
JSON decoding, pending-request routing, notification projection, and
server-initiated request handling. `WebSocketOverStdioConnection` drives the
WebSocket Sans-I/O client over `_RawByteStream`; it performs the fixed
`ws://localhost/` Upgrade, disables compression, handles control frames and
fragmentation, and exposes only complete text or binary messages. The public
framing and failure contract is defined in
[reference.md — Runtime resolution](reference.md#runtime-resolution), and the unattended interaction policy is defined in
[reference.md — Unattended operation](reference.md#unattended-operation).
The session lifecycle is `OPEN -> CLOSING -> CLOSED`: `CLOSING` rejects new
requests but does not claim that resources are gone. The session owns one
shared transport-cleanup task; runtime reader failures fail pending requests
before awaiting it, and `close()` joins the same task. Both waiters shield the
cleanup task from caller cancellation, retaining cancellation while repeatedly
waiting if needed, so the reader and close operation do not finish before
transport and child-process cleanup completes.
The session also probes the required lifecycle RPC surface for `doctor` using
sentinel requests; method-not-found responses are reported as unavailable,
while ordinary domain errors prove that dispatch succeeded.
Internally, declined interactions are re-emitted as a synthetic
`codexctl/unsupportedInteraction` notification so the core and renderers
see them as ordinary stream events; that method name never appears on the
wire.

Projection is a set of pure functions (`project_thread_status`,
`project_item`, `project_notification`) mapping Codex tagged unions into
the stable vocabulary. Unknown variants are dropped, additive fields are
tolerated, and command output content is intentionally not projected in
v1. The public shapes they produce are specified in
[reference.md — Projected items](reference.md#projected-items) and
[reference.md — Streaming event records](reference.md#streaming-event-records-jsonl).

### rollout.py — best-effort enrichment

Rollout files are an internal Codex storage format. `codexctl` reads them
only for optional context-window enrichment (`status`) and diagnostics
(`doctor`). The reader parses narrowly (only `event_msg` / `token_count`
records), ignores unknown or malformed lines, and returns `None` instead
of raising on any drift. It is never a primary history source.

### cli.py and render.py — outside the seam

[cli.py](../src/codexctl/cli.py) maps argv to commands and outcomes to exit codes. The
output-mode matrix and the error-code → exit-code mapping implement the
contracts specified in [reference.md — Output modes](reference.md#output-modes)
and [reference.md — Exit codes](reference.md#exit-codes). SIGINT returns
130 and sends no turn interrupt.

Renderers consume outcomes and projected events; they never influence
execution. `render.snapshot_document` is the single source of the JSON
snapshot shapes listed in
[reference.md — Snapshot documents](reference.md#snapshot-documents-json).
Stream construction (stdout lazily resolved) keeps renderers testable
with captured streams.

## Testing strategy

Tests drive `CodexCtl` through a scripted `FakeAppServer` implementing
the `AppServerClient` shape (see [tests/conftest.py](../tests/conftest.py)); no sockets are
opened. This covers all lifecycle behaviors pinned down above: start and
resume contracts, busy detection, steer with `expectedTurnId`, interrupt
waiting, read-only status, selector semantics, follow replay/live dedup
and exit conditions, stream-loss behavior, and error mapping. Projection
functions, rollout parsing, endpoint lifecycle-JSON parsing, and the CLI
matrix are tested as units. The behavior assertions mirror
[reference.md](reference.md); the test suite is where the specification
becomes executable.

## Compatibility stance

`codexctl` does not gate on version numbers. Compatibility is discovered
at runtime through endpoint initialization and capability probes. Projection
drops unknown wire variants so newer Codex releases degrade additively. The
public compatibility verdict and error meanings are defined in
[reference.md — Error codes](reference.md#error-codes).
