# codexctl Reference

The public interface and behavior of `codexctl` v1. This document is the
single source of truth for anything observable from the outside: commands,
flags, output modes, output shapes, error codes, and exit codes.

## Overview

`codexctl` controls Codex threads through a shared Codex app-server
runtime. A thread is a long-lived object: it survives process exits,
terminal closures, and machine restarts. `codexctl` itself stores nothing.

Lifecycle invariants visible to callers:

- `start` always creates a new thread; it never reuses one.
- `resume` never queues a turn and never becomes a steer: it either starts
  one new turn on the thread or fails.
- Recovery failure never silently creates a replacement thread.
- Detaching (`--detach`) and local interruption (Ctrl+C) never interrupt the
  running turn.
- `status` and `history` are strictly read-only; they never resume or mutate.
- `follow` is observational: loading and subscribing do not send app-server
  config overrides.
- `codexctl` runs unattended: it never blocks waiting for human input (see
  [Unattended operation](#unattended-operation)).

## Commands

| Command | Purpose | Streaming? |
|---|---|---|
| `start` | Create a new thread and start its first turn | yes (unless `--detach`) |
| `resume <thread-id>` | Start a new turn on an existing thread | yes (unless `--detach`) |
| `status <thread-id>` | Read the current state of a thread | no |
| `history <thread-id>` | Read a finite snapshot of thread history | no |
| `follow <thread-id>` | Attach to the thread's current active turn (`--persist`: attach to the thread itself) | yes |
| `steer <thread-id>` | Add steering input to the active turn | no |
| `interrupt <thread-id>` | Interrupt the active turn and wait for it to end | no |
| `list` | List stored threads | no |
| `doctor` | Diagnose runtime compatibility | no |

### Common options

Every command accepts:

| Option | Meaning |
|---|---|
| `-o, --output {text,json,jsonl}` | Output mode (default `text`) |
| `--json` | Shorthand for `-o json` |
| `--jsonl` | Shorthand for `-o jsonl` |
| `--endpoint <uri>` | Use an externally managed app-server endpoint; `codexctl` performs no daemon lifecycle actions in this mode |
| `--endpoint-token-file <path>` | Read a Bearer token from this file immediately before connecting to a `ws://` endpoint |
| `--stdio-exec <executable>` | Run one caller-selected app-server process; its stdin/stdout carry the selected stdio framing |
| `--stdio-framing {jsonl,websocket}` | Select newline-delimited JSON (default) or the Codex-compatible WebSocket-over-stdio framing |
| `--stdio-arg <arg>` | Repeatable; append this exact argument, in order, to `--stdio-exec` |
| `--ssh <destination>` | Connect to a remote shared app-server through OpenSSH |
| `--ssh-arg <token>` | Repeatable; append one complete OpenSSH option token |
| `--remote-codex <executable>` | Remote Codex executable for SSH mode (default: `codex`) |
| `--remote-socket <absolute-path>` | Use an externally managed remote socket in SSH mode |

`--stdio-exec` and `--stdio-arg` select `stdio` mode. Stdio mode is mutually
exclusive with `--endpoint` and `--endpoint-token-file`; `--stdio-arg` and
`--stdio-framing websocket` require `--stdio-exec`. The executable and
arguments are passed directly as one argv vector: there is no shell parsing,
pipeline, redirection, glob expansion, or environment interpolation. The
child inherits codexctl's environment and working directory. Its stdin/stdout
are private pipes owned by codexctl: the child receives framed app-server traffic on its
stdin and writes framed app-server traffic on its stdout. These pipes do not consume or
replace codexctl's own stdin/stdout; the child's stderr is forwarded to
codexctl's stderr.
Dash-prefixed values are accepted; use the attached spelling
`--stdio-arg=--` when passing a literal `--` before the prompt delimiter.

### SSH runtime

SSH mode uses OpenSSH to reach the remote shared Codex runtime. SSH
v1 supports POSIX remotes only. The destination is opaque to codexctl: it does
not parse user, host, or port fields, resolve DNS, or expand SSH config.

Without `--remote-socket`, each invocation runs `codex app-server daemon start`
once on the remote and strictly consumes its single JSON lifecycle response.
The accepted statuses are `started` and `alreadyRunning`; the response must
contain a non-empty absolute POSIX `socketPath`. The remote proxy is then run
against that socket. `daemon bootstrap` is never run implicitly.

`--remote-socket` selects externally managed lifecycle ownership. It skips
`daemon start` and connects directly to the specified absolute POSIX socket.
It cannot be combined with `--remote-codex`; the external socket is not
assumed to belong to a particular executable. `--remote-codex` accepts one
executable name without `/` or one absolute POSIX path. It does not accept
relative paths, `~`, or extra command arguments.

Each `--ssh-arg` is one complete OpenSSH option token. Options that need a
value must attach it to that token, for example `-Jbastion`, `-p2222`,
`-i/home/me/.ssh/key`, or `-oConnectTimeout=10`. Session-shaping options are
rejected; codexctl owns non-interactive binary operation by enforcing `-T` and
`BatchMode=yes`. Connection, authentication, routing, and multiplexing
policy remains in OpenSSH and `~/.ssh/config`; codexctl does not manage
ControlMaster, ControlPersist, or ControlPath.

Remote cwd is never inferred from local cwd. `start` requires an explicit
absolute POSIX `--cwd`; `list` requires one unless `--all` is used. No remote
directory preflight is performed. Ctrl+C or disconnect closes the proxy and
SSH process, but does not send `turn/interrupt` or stop the shared daemon. SSH
v1 provides no automatic reconnect or resume after a connection is lost.

Examples:

```sh
codexctl start --ssh devbox --cwd /srv/repos/foo --detach -- "run tests"
codexctl status --ssh devbox THREAD_ID
codexctl follow --ssh devbox THREAD_ID
codexctl list --ssh devbox --cwd /srv/repos/foo
codexctl list --ssh devbox --all
codexctl list --ssh devbox --remote-socket /run/user/1000/codex.sock --all
```

### start

```sh
codexctl start [--detach] [--no-goals] [--no-agents] [--cwd DIR] [--model MODEL] [--effort EFFORT]
               [--sandbox {read-only,workspace-write,danger-full-access}]
               [--approve-for-me] (-- PROMPT... | -)
```

Creates a new thread and starts its first turn with the prompt. Everything
after the bare `--` is prompt text (flags included). A positional `-` reads
the complete prompt from standard input.

- By default the approval policy is `never` (unattended): approval requests
  are declined automatically and no reviewer is configured.
- `--approve-for-me` switches the thread to auto review: the approval policy
  becomes `on-request` and the runtime's auto reviewer resolves approval
  requests, so `codexctl` still never blocks on human input.
- The sandbox defaults to `workspace-write` when `--sandbox` is omitted.
- When `--cwd` is omitted, `codexctl` passes its current directory explicitly
  as the new thread's cwd.
- SSH mode requires `--cwd` to be an explicit absolute POSIX path; it never
  infers a remote cwd from the local directory.
- `--detach` returns as soon as the turn has started and disconnects; the
  turn keeps running in the shared runtime.
- `--no-goals` and `--no-agents` apply the closed config overrides
  `features.goals=false` and `agents.enabled=false`. They do not expose a
  generic config map. Use them again on `resume` when isolation must survive
  a thread unload/reload.

Foreground output streams projected events until the turn reaches a
terminal state, following the shared
[streaming text output](#streaming-text-output) contract in text mode.
The exit code reflects the turn's terminal status, see
[Exit codes](#exit-codes).

### resume

```sh
codexctl resume <thread-id> [--detach] [--no-goals] [--no-agents] (-- PROMPT... | -)
```

Recovers the thread in the shared runtime and starts one new turn. A
positional `-` reads the complete prompt from standard input. Fails
with `THREAD_BUSY` if the thread already has an active turn,
`THREAD_NOT_FOUND` if the thread does not exist, and
`THREAD_RECOVERY_FAILED` if recovery itself fails.
The isolation flags have the same meaning as on [`start`](#start) and are
reapplied while loading the thread.

### status

```sh
codexctl status <thread-id>
```

Read-only snapshot:

```text
Thread: <thread-id>
Status: idle | active | notLoaded | systemError
Flags:  waitingOnApproval, waitingOnUserInput   (only when present)
Active turn: <turn-id> | -
Context: 83k / 200k (38%) | -
```

Context usage is best-effort enrichment from the Codex runtime or rollout
store for runtimes that support local enrichment; `-` means unavailable.
External, stdio, and SSH runtimes do not read the local rollout store, so their
status context is unavailable. `usedTokens` is the latest context size, not a
cumulative session total. `ratio` is the effective context usage fraction
after reserving 12,000 tokens for the system prompt, fixed tool instructions,
and compaction space; the text output shows its rounded percentage.
`status` never resumes the thread.

### history

```sh
codexctl history <thread-id> [--turns SELECTOR]
```

Finite snapshot of projected items per turn. `--turns` follows Python
indexing and slicing semantics exactly:

- `--turns 3` — fourth turn (out of range is a usage error)
- `--turns -1` — last turn
- `--turns 1:3` — turns 1 and 2
- `--turns ::-1` — all turns, newest first
- omitted — all turns

### follow

```sh
codexctl follow <thread-id> [--replay-turns SELECTOR] [--persist]
```

Attaches to the thread's current active turn. Without `--persist`, fails
with `NO_ACTIVE_TURN` if there is none. Before streaming live events,
`codexctl` replays a continuous suffix of the reconstructed history; events
visible in both phases are emitted once. Follow is observational: loading and
subscribing do not send app-server config overrides.

`--replay-turns` accepts exactly three forms. The anchor is the active turn
when one exists, otherwise the end of history (reachable only with
`--persist`):

| Selector | Meaning |
|---|---|
| `-1` (default) | Replay only the anchor turn's known history |
| `-N:` | Replay the latest N turns including the anchor turn |
| `:` | Replay the entire available history |

Without `--persist`, `follow` exits when the followed turn reaches a
terminal state, with the corresponding [exit code](#exit-codes).

With `--persist`, `follow` attaches to the **thread** rather than to a
single turn:

- With no active turn, it does not fail with `NO_ACTIVE_TURN`; it
  subscribes silently and waits for the next turn to start. No synthetic
  events (waiting/idle markers) are ever emitted.
- It keeps streaming across turn boundaries, covering every turn of the
  thread regardless of origin (another `codexctl` process, the Codex UI,
  and so on).
- A turn ending `failed` or `interrupted` does not end the session; each
  `turn/completed` event carries that turn's `status`.
- There are exactly two exits: local Ctrl+C (exit code 130, sending no turn
  interrupt) and connection loss (an `APP_SERVER_PROTOCOL_ERROR` error
  event in the stream, exit code 5). Exit codes 0 and 4 are unreachable in
  persist mode.
- There is no thread-existence polling or detection: if the thread
  disappears while idle, the stream stays silent until the user exits or
  the connection is lost.

### steer

```sh
codexctl steer <thread-id> (-- INPUT... | -)
```

Adds steering input to the active turn. A positional `-` reads the complete
input from standard input. For all three prompt-bearing commands, stdin input
preserves internal, leading, and trailing newlines exactly; zero-length stdin
is a `USAGE_ERROR` (exit code 2). The `-` form is not accepted by other
commands. The request carries the turn id
observed immediately before sending; if the turn changed or ended in the
meantime, the command fails with `NO_ACTIVE_TURN`. If the active turn does
not accept steering, it fails with `TURN_NOT_STEERABLE`. `steer`
acknowledges delivery only; it does not stream the turn.

### interrupt

```sh
codexctl interrupt <thread-id>
```

Interrupts the active turn and waits until that turn reaches a terminal
state (bounded wait). Fails with `NO_ACTIVE_TURN` when there is no active
turn or the request is rejected. A rejected interrupt is never retried
against a different turn.

### list

```sh
codexctl list [--cwd DIR] [--all]
```

By default, lists stored threads whose workspace is the selected cwd, newest
activity first. Local runtimes default that cwd to the current directory.
SSH mode requires an explicit absolute POSIX `--cwd`; `--all` lists threads
across all workspaces without a cwd:

```text
<thread-id>  idle  <preview>
```

In text mode, preview newlines are rendered as the two characters `\n`, and
the preview is truncated to the remaining terminal width after the thread ID,
status, and separators. If the terminal width cannot be determined, `128` is
used. JSON mode returns the original preview without this text formatting.

### doctor

```sh
codexctl doctor
```

Runs diagnostics and prints a compatibility verdict: endpoint
reachability, the initialize handshake, required lifecycle operations, the
local `codex` CLI version for runtimes with managed lifecycle ownership, and
rollout-based context enrichment availability when that capability is enabled.
External and stdio runtimes omit the enrichment check. Context enrichment is
optional and does not make a runtime incompatible.
Exit code 0 means the checks completed (regardless of verdict).

## Output modes

The allowed output modes per command (the full contract):

| Command | text | json | jsonl |
|---|---|---|---|
| `start` | ✓ | — (✓ with `--detach`) | ✓ (— with `--detach`) |
| `resume` | ✓ | — (✓ with `--detach`) | ✓ (— with `--detach`) |
| `status` | ✓ | ✓ | — |
| `history` | ✓ | ✓ | ✓ |
| `follow` | ✓ | — | ✓ |
| `steer` | ✓ | ✓ | — |
| `interrupt` | ✓ | ✓ | — |
| `list` | ✓ | ✓ | — |
| `doctor` | ✓ | ✓ | — |

Requesting an unsupported mode is a usage error and exits 2 without touching
the runtime. The error uses the requested output mode: human-readable text is
written to stderr, while `json` and `jsonl` use their structured error shapes
on stdout.

- **text** — human-readable rendering on stdout; diagnostics on stderr.
- **json** — exactly one complete JSON document on stdout (snapshot
  commands and `--detach` only). Errors are also one JSON document on
  stdout: `{"error": {...}}`.
- **jsonl** — one complete JSON object per line on stdout, flushed
  immediately. Errors are one line: `{"type": "error", "error": {...}}`.

### Streaming event records (jsonl)

Streaming commands in jsonl mode emit projected events, one per line:

```json
{"type": "turn/started", "threadId": "...", "turnId": "...", "source": "live"}
{"type": "item/started", "threadId": "...", "turnId": "...", "item": {...}, "source": "live"}
{"type": "item/completed", "threadId": "...", "turnId": "...", "item": {...}, "source": "live"}
{"type": "thread/tokenUsage/updated", "threadId": "...", "turnId": "...", "source": "live", "usage": {"usedTokens": 83000, "windowTokens": 200000, "ratio": 0.38}}
{"type": "turn/completed", "threadId": "...", "turnId": "...", "status": "completed", "source": "live"}
```

- `source` is `"live"` for events delivered from the runtime and
  `"replay"` for events reconstructed from history (`follow`, and
  `history -o jsonl`). Replay records additionally carry `turnIndex`.
- `item` is a projected item, see [Projected items](#projected-items).
- `turn/completed` carries `status` (`completed`, `interrupted`, or
  `failed`) and, when present, `error: {"message": ...}`.
- `thread/tokenUsage/updated` carries `usage` with the latest context size in
  `usedTokens`, the model context window in `windowTokens`, and the
  effective usage fraction in `ratio` when the runtime provides a context
  window; unavailable usage is omitted.
- `error` records carry `error: {"code", "message"}` with a stable
  [error code](#error-codes).

`history -o jsonl` emits one finite lifecycle-shaped sequence per selected
turn: `turn/started`, one `item/completed` per item, `turn/completed` —
omitted for a turn still in progress, which yields no `turn/completed`
record (same as `follow` replay).

### Streaming text output

Streaming commands in text mode share one rendering contract, in both
`follow` modes:

- The header prints only the `Thread:` line.
- Every `turn/started` event emits a turn marker line: `Turn: <turn-id>`.
  For `start`/`resume` the `turn/started` prelude is the first event, so
  the marker opens the stream. For `follow`, the replay block precedes it,
  so the marker lands after the replay block, marking the replay/live
  boundary.
- Projected items are rendered as they arrive.
- The per-turn context usage line is event-stream-driven: after each
  `turn/completed` event, the latest `thread/tokenUsage/updated` usage seen
  in the stream is printed (`Context: 83k / 200k (38%)`); nothing is
  printed when no usage data was seen.

### Snapshot documents (json)

`start`/`resume` with `--detach`:

```json
{"threadId": "...", "turnId": "...", "detached": true}
```

`status`:

```json
{
  "threadId": "...",
  "status": "active",
  "activeTurnId": "...",
  "context": {"usedTokens": 83000, "windowTokens": 200000, "ratio": 0.38, "source": "live|rollout"},
  "activeFlags": ["waitingOnApproval"]
}
```

`context` is `null` when unavailable; `activeFlags` is present only when
non-empty.

`history`:

```json
{"threadId": "...", "turns": [{"id": "...", "index": 0, "items": [...]}]}
```

`steer`: `{"threadId": "...", "turnId": "..."}`

`interrupt`: `{"threadId": "...", "turnId": "...", "status": "interrupted"}`

`list`:

```json
{"threads": [{"threadId": "...", "status": "idle", "preview": "...", "updatedAt": 1755200000}]}
```

`doctor`:

```json
{
  "codexctlVersion": "0.1.0",
  "endpointMode": "managed|external|stdio|ssh",
  "lifecycleOwnership": "managed|external",
  "remoteSocket": "/run/user/1000/codex.sock",
  "codexCliVersion": "codex-cli 0.101.0",
  "appServerVersion": "0.101.0",
  "compatible": true,
  "checks": [{"name": "...", "ok": true, "detail": "..."}]
}
```

### Projected items

Items are projected into a stable, metadata-only vocabulary; unknown item
types and unknown fields are dropped. Command output content is not
projected in v1. This set is representative, not closed: future versions
may add new item types and fields (callers must ignore unknowns).

```json
{"id": "...", "type": "userMessage", "text": "..."}
{"id": "...", "type": "agentMessage", "text": "..."}
{"id": "...", "type": "commandExecution", "command": "ls", "cwd": "...", "status": "completed", "exitCode": 0}
{"id": "...", "type": "fileChange", "changes": [{"path": "a.py", "kind": "added"}]}
{"id": "...", "type": "contextCompaction"}
```

## Runtime resolution

Without `--endpoint`, `codexctl` uses the managed shared runtime:

1. Probe the default control socket
   (`$CODEX_HOME/app-server-control/app-server-control.sock`, with
   `CODEX_HOME` defaulting to `~/.codex`). A reachable, responding runtime
   is used as-is.
2. Otherwise run `codex app-server daemon start` (idempotent) and parse its
   lifecycle JSON response for the socket path.

If the daemon command produces no parseable lifecycle JSON, `codexctl`
reports `INCOMPATIBLE_CODEX` rather than guessing.

External endpoints use one of these forms:

- `unix:///absolute/path` connects over the local Unix control socket.
- `ws://HOST:PORT[/PATH][?QUERY]` connects over WebSocket and preserves the path
  and query exactly. `--endpoint-token-file` is accepted only for this form;
  its trimmed, non-empty contents are sent as `Authorization: Bearer ...`.
  Credential-bearing query parameters (`token`, `access_token`, `id_token`,
  `refresh_token`, `bearer_token`, and `authorization`, case-insensitively)
  are rejected; credentials must not be placed in an endpoint URL.

Malformed endpoint configuration is a `USAGE_ERROR`. An unavailable endpoint,
or a missing, unreadable, or empty token file, is `APP_SERVER_UNAVAILABLE`.

Stdio mode starts a fresh child for each command invocation. The default
`--stdio-framing jsonl` uses newline-delimited JSON: blank lines are ignored,
LF and CRLF are accepted, and a final valid line need not end with a newline.
Each line must contain exactly one JSON object.

`--stdio-framing websocket` requires `--stdio-exec` and connects to the child
using the fixed internal URI `ws://localhost/`. The child pipes carry the raw
HTTP Upgrade exchange and WebSocket wire frames; the child is a transparent
Codex-compatible relay and doesn't parse JSON or WebSocket framing.
Compression is disabled. Text WebSocket messages must contain one JSON object;
binary messages, malformed JSON, non-object values, and any later framing
failure are `APP_SERVER_PROTOCOL_ERROR` failures with no resynchronization.
WebSocket control frames, fragmentation, masking, ping/pong, and close
handling are internal transport details.
The initialize/startup deadline is 15 seconds. Executable failures,
permission failures, pre-initialize child exits, and startup timeouts are
`APP_SERVER_UNAVAILABLE`; unexpected runtime exits are
`APP_SERVER_PROTOCOL_ERROR`. The child is not reconnected or restarted.

On normal cleanup codexctl closes stdin, waits briefly, terminates the child
process group, and uses a final kill fallback when necessary. Cleanup status
does not replace a successful command result. `doctor` reports the endpoint
mode and lifecycle ownership; it does not expose the stdio executable or
argument list.
As with other modes, local Ctrl+C returns 130 and sends no turn interrupt;
with stdio, the caller must not assume a detached or interrupted command's
active turn survives termination of the child process.

Environment variables:

| Variable | Effect |
|---|---|
| `CODEXCTL_CODEX_BIN` | Override the `codex` binary used in managed mode |
| `CODEX_HOME` | Codex home directory (inherited Codex convention) |

## Unattended operation

`codexctl` never blocks on human input:

- Command approval requests are declined automatically. With
  `start --approve-for-me`, approval decisions are instead resolved by the
  runtime's auto reviewer server-side; `codexctl` still brokers no
  interactive approval itself.
- Elicitations (user input requests) are declined automatically.
- Any other server-initiated interaction is rejected and surfaced as an
  `UNSUPPORTED_INTERACTION` error event in the stream; the turn continues
  or fails on its own, and `codexctl` keeps streaming.

## Error codes

Stable, codexctl-owned codes carried in all structured errors:

| Code | Meaning | Exit code |
|---|---|---|
| `THREAD_NOT_FOUND` | The thread does not exist | 3 |
| `THREAD_BUSY` | The thread already has an active turn | 3 |
| `NO_ACTIVE_TURN` | No active turn for follow (without `--persist`), steer, or interrupt | 3 |
| `TURN_NOT_STEERABLE` | The active turn does not accept steering | 3 |
| `TURN_FAILED` | The followed turn ended in failure | 4 |
| `TURN_INTERRUPTED` | The followed turn ended interrupted | 4 |
| `APP_SERVER_UNAVAILABLE` | Runtime unreachable / daemon cannot start | 5 |
| `APP_SERVER_PROTOCOL_ERROR` | Unexpected protocol behavior or lost connection | 5 |
| `THREAD_RECOVERY_FAILED` | Resume/follow could not reconstruct the thread | 5 |
| `UNSUPPORTED_INTERACTION` | The runtime requested an interaction codexctl cannot broker | 5 |
| `INCOMPATIBLE_CODEX` | The local Codex lacks required app-server capabilities | 5 |
| `OUTPUT_MODE_NOT_SUPPORTED` | The requested `-o/--output` mode is rejected by the output-mode matrix for this command | 2 |
| `USAGE_ERROR` | Any other command-line usage error: missing prompt/input, invalid `--turns`/`--replay-turns` selector, out-of-range turn index | 2 |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (streaming commands: the turn completed; unreachable for `follow --persist`) |
| 2 | Usage error (argument/output-mode/selector problems) |
| 3 | Domain conflict (see error-code table) |
| 4 | The followed turn failed or was interrupted (unreachable for `follow --persist`) |
| 5 | Runtime/protocol error |
| 130 | Local interruption (Ctrl+C). Never sends a turn interrupt. |

`follow --persist` exits only 130 (local interruption) or 5 (connection
loss).
