# Codex source reference

Record of the Codex source files consulted when implementing codexctl's
interoperability with the Codex app-server runtime.

This document is a external reference only: it records *where each
protocol fact was read from*, not the facts themselves. Current public
behavior is defined in [reference.md](reference.md); current internal
design in [architecture.md](architecture.md). Do not use this document
as an alternative source for current architecture or behavior.

## Codex repository baseline

- Repository: [`openai/codex`](https://github.com/openai/codex)
- Git tag: `rust-v0.147.0`
- Commit: `be6e8eac029b183056b7e4402879f15d2c85f61b` (2026-08-06)

## Referenced files

### Control socket and transport

Consulted for the websocket-over-unix-socket transport and the default
control socket path.

- `codex-rs/app-server/README.md` — transport list: the unix socket
  endpoint serves websocket connections (standard HTTP Upgrade
  handshake) at `$CODEX_HOME/app-server-control/app-server-control.sock`.
- `codex-rs/app-server-transport/src/transport/mod.rs` — socket
  directory/file name constants confirming the default path layout.

### JSON-RPC protocol surface

Consulted for method names, parameter shapes, and handshake order.

- `codex-rs/app-server-protocol/src/protocol/v2/thread.rs` —
  `thread/start`, `turn/start`, `turn/steer` (`expectedTurnId`),
  `turn/interrupt`, `thread/read`, `thread/unsubscribe`; camelCase
  field names; `approvalPolicy` / `sandbox` values.
- `codex-rs/app-server-protocol/src/protocol/v1.rs` and
  `codex-rs/app-server-protocol/src/rpc.rs` — wire format detail
  (omitted `"jsonrpc":"2.0"` header), `initialize` request
  (`clientInfo` + `capabilities`) followed by `initialized`
  notification.

### Status types

Consulted for the projected status vocabulary.

- `codex-rs/app-server-protocol/src/protocol/v2/*` — `ThreadStatus`
  tagged union (`notLoaded` / `idle` / `systemError` / `active` with
  `activeFlags`) and `TurnStatus` (`completed` / `interrupted` /
  `failed` / `inProgress`).

### Daemon lifecycle

Consulted for `codex app-server daemon start` semantics.

- `codex-rs/app-server-daemon/src/lib.rs` — idempotent start; JSON
  status on stdout (`alreadyRunning` / `bootstrapped`, `socketPath`,
  version fields).

### Rollout files

Consulted for rollout location and token-usage records.

- `codex-rs/rollout/` (writer and metadata modules) — file layout
  `sessions/YYYY/MM/DD/rollout-<timestamp>-<session_id>.jsonl` and the
  `event_msg` / `token_count` records carrying token usage.

### Cross-checked only

- `docs/config.md` — `CODEX_HOME` semantics; not an implementation
  source.
- TUI/CLI interaction flows — read for approval/elicitation request
  shapes only; the unattended decline policy is codexctl's own
  specification requirement, not copied Codex behavior.
