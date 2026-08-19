---
codex_repository: openai/codex
codex_submodule_path: vendor/codex
codex_git_tag: rust-v0.147.0
codex_commit: be6e8eac029b183056b7e4402879f15d2c85f61b
codex_commit_date: 2026-08-06
---

# Codex source reference

Record of the Codex source files consulted when implementing codexctl's
interoperability with the Codex app-server runtime.

This document is an upstream reference only: it records *where each
protocol fact was read from*, not the facts themselves. Current public
behavior is defined in [reference.md](reference.md); current internal
design in [architecture.md](architecture.md). Do not use this document
as an alternative source for current architecture or behavior.

## Referenced files

### Control socket and transport

Consulted for the websocket-over-unix-socket transport, the default control
socket path, and the common WebSocket handshake behavior used by external TCP
endpoints.

- [codex-rs/app-server/README.md](../vendor/codex/codex-rs/app-server/README.md) — transport list: the unix socket
  endpoint serves websocket connections (standard HTTP Upgrade
  handshake) at `$CODEX_HOME/app-server-control/app-server-control.sock`.
- [codex-rs/app-server-transport/src/transport/mod.rs](../vendor/codex/codex-rs/app-server-transport/src/transport/mod.rs) — socket
  directory/file name constants confirming the default path layout.
- [codex-rs/app-server-transport/src/transport/unix_socket.rs](../vendor/codex/codex-rs/app-server-transport/src/transport/unix_socket.rs) — control
  socket acceptor implementation and its `tokio_tungstenite::accept_async`
  upgrade error path.
- [codex-rs/app-server-transport/src/transport/unix_socket_tests.rs](../vendor/codex/codex-rs/app-server-transport/src/transport/unix_socket_tests.rs) —
  upstream Unix socket handshake test using `tokio_tungstenite`.
- [codex-rs/app-server-transport/src/transport/websocket.rs](../vendor/codex/codex-rs/app-server-transport/src/transport/websocket.rs) — WebSocket
  transport handshake and request-header handling consulted for TCP endpoint
  compatibility.

### Stdio proxy distinction

Consulted to distinguish the raw stdio relay used by `codex app-server proxy`
from Codex's line-delimited stdio app-server transport.

- [codex-rs/stdio-to-uds/src/lib.rs](../vendor/codex/codex-rs/stdio-to-uds/src/lib.rs) — copies raw bytes between stdio and a Unix socket; it does not parse JSON or WebSocket frames.
- [codex-rs/cli/src/main.rs](../vendor/codex/codex-rs/cli/src/main.rs) — exposes the `app-server proxy` command and its socket option.
- [codex-rs/app-server-transport/src/transport/unix_socket.rs](../vendor/codex/codex-rs/app-server-transport/src/transport/unix_socket.rs) — performs the WebSocket Upgrade on the Unix socket side of the transparent relay.

### JSON-RPC protocol surface

Consulted for method names, parameter shapes, and handshake order.

- [codex-rs/app-server-protocol/src/protocol/v2/thread.rs](../vendor/codex/codex-rs/app-server-protocol/src/protocol/v2/thread.rs) —
  `thread/start`, `turn/start`, `turn/steer` (`expectedTurnId`),
  `turn/interrupt`, `thread/read`, `thread/unsubscribe`; camelCase
  field names; `approvalPolicy` / `sandbox` values.
- [codex-rs/app-server-protocol/src/protocol/v1.rs](../vendor/codex/codex-rs/app-server-protocol/src/protocol/v1.rs) and
  [codex-rs/app-server-protocol/src/rpc.rs](../vendor/codex/codex-rs/app-server-protocol/src/rpc.rs) — wire format detail
  (omitted `"jsonrpc":"2.0"` header), `initialize` request
  (`clientInfo` + `capabilities`) followed by `initialized`
  notification.

### Status types

Consulted for the projected status vocabulary.

- [codex-rs/app-server-protocol/src/protocol/v2/*](../vendor/codex/codex-rs/app-server-protocol/src/protocol/v2/) — `ThreadStatus`
  tagged union (`notLoaded` / `idle` / `systemError` / `active` with
  `activeFlags`) and `TurnStatus` (`completed` / `interrupted` /
  `failed` / `inProgress`).

### Experimental API marking

Consulted for how upstream marks an app-server method or param field as
experimental and gates it behind a client capability. Facts read, in
brief: method-level `#[experimental("...")]` attributes inside the
`client_request_definitions!` declaration, field-level marking via the
`ExperimentalApi` derive macro, and runtime rejection unless the client
declared the `experimentalApi` capability in `initialize`.

- [codex-rs/app-server-protocol/src/protocol/common.rs](../vendor/codex/codex-rs/app-server-protocol/src/protocol/common.rs) —
  `client_request_definitions!` macro: the central client-method table;
  optional `#[experimental("reason")]` per method entry (wire names are
  not prefixed), generated `experimental_reason()` dispatch, and the
  `EXPERIMENTAL_CLIENT_METHODS` test constant.
- [codex-rs/app-server-protocol/src/experimental_api.rs](../vendor/codex/codex-rs/app-server-protocol/src/experimental_api.rs) —
  `ExperimentalApi` trait, the `ExperimentalField` inventory registry of
  experimental fields, and the `"... requires experimentalApi capability"`
  error message.
- [codex-rs/codex-experimental-api-macros/src/lib.rs](../vendor/codex/codex-rs/codex-experimental-api-macros/src/lib.rs) —
  `#[derive(ExperimentalApi)]` proc-macro: `#[experimental("method.field")]`
  and `#[experimental(nested)]` field-level marking.
- [codex-rs/app-server-protocol/src/protocol/v1.rs](../vendor/codex/codex-rs/app-server-protocol/src/protocol/v1.rs) —
  `InitializeCapabilities.experimental_api` (wire name `experimentalApi`).
- [codex-rs/app-server/src/request_processors/initialize_processor.rs](../vendor/codex/codex-rs/app-server/src/request_processors/initialize_processor.rs) —
  capability read during `initialize` and stored per connection session.
- [codex-rs/app-server/src/message_processor.rs](../vendor/codex/codex-rs/app-server/src/message_processor.rs) —
  per-request gate: requests with an experimental reason are rejected as
  invalid requests when the session has not enabled the experimental API.

Note: `experimentalFeature/list` and `experimentalFeature/enablement/set`
are themselves stable methods for feature-flag enablement, distinct from
the experimental API capability gating above.

### Daemon lifecycle

Consulted for `codex app-server daemon start` semantics.

- [codex-rs/app-server-daemon/src/lib.rs](../vendor/codex/codex-rs/app-server-daemon/src/lib.rs) — idempotent start; JSON
  status on stdout (`alreadyRunning` / `bootstrapped`, `socketPath`,
  version fields).
- [codex-rs/app-server-daemon/src/client.rs](../vendor/codex/codex-rs/app-server-daemon/src/client.rs) — daemon-side control socket
  probe using `tokio_tungstenite::client_async` and the `ws://localhost/`
  handshake URI.

### Rollout files

Consulted for rollout location and token-usage records.

- [codex-rs/rollout/](../vendor/codex/codex-rs/rollout/) (writer and metadata modules) — file layout
  `sessions/YYYY/MM/DD/rollout-<timestamp>-<session_id>.jsonl` and the
  `event_msg` / `token_count` records carrying token usage.

### Context usage display

The upstream TUI context percentage and its baseline normalization were read
from:

- [codex-rs/tui/src/token_usage.rs](../vendor/codex/codex-rs/tui/src/token_usage.rs) — baseline and remaining-percentage
  calculation.
- [codex-rs/tui/src/chatwidget/status_controls.rs](../vendor/codex/codex-rs/tui/src/chatwidget/status_controls.rs) — conversion from
  remaining percentage to displayed usage percentage.

### Cross-checked only

- [docs/config.md](../vendor/codex/docs/config.md) — `CODEX_HOME` semantics; not an implementation
  source.
- TUI/CLI interaction flows — read for approval/elicitation request
  shapes only; the unattended decline policy is codexctl's own
  specification requirement, not copied Codex behavior.
