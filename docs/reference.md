# codexctl Reference

The public interface and behavior of `codexctl` v1. This document is the
single source of truth for anything observable from the outside: commands,
flags, output modes, output shapes, error codes, and exit codes. Internal
structure lives in [architecture.md](architecture.md), which links here
instead of repeating these facts.

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
- `codexctl` runs unattended: it never blocks waiting for human input (see
  [Unattended operation](#unattended-operation)).

## Commands

| Command | Purpose | Streaming? |
|---|---|---|
| `start` | Create a new thread and start its first turn | yes (unless `--detach`) |
| `resume <thread-id>` | Start a new turn on an existing thread | yes (unless `--detach`) |
| `status <thread-id>` | Read the current state of a thread | no |
| `history <thread-id>` | Read a finite snapshot of thread history | no |
| `follow <thread-id>` | Attach to the thread's current active turn | yes |
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

### start

```sh
codexctl start [--detach] [--cwd DIR] [--model MODEL] [--effort EFFORT]
               [--sandbox {read-only,workspace-write,danger-full-access}] -- PROMPT...
```

Creates a new thread and starts its first turn with the prompt. Everything
after the bare `--` is prompt text (flags included).

- The approval policy is fixed to unattended execution; it is not
  caller-configurable.
- The sandbox defaults to `workspace-write` when `--sandbox` is omitted.
- When `--cwd` is omitted, `codexctl` passes its current directory explicitly
  as the new thread's cwd.
- `--detach` returns as soon as the turn has started and disconnects; the
  turn keeps running in the shared runtime.

Foreground output begins with a `Thread:` / `Turn:` header (text mode),
streams projected events, and ends with the terminal turn state. The exit
code reflects the turn's terminal status, see [Exit codes](#exit-codes).

### resume

```sh
codexctl resume <thread-id> [--detach] -- PROMPT...
```

Recovers the thread in the shared runtime and starts one new turn. Fails
with `THREAD_BUSY` if the thread already has an active turn,
`THREAD_NOT_FOUND` if the thread does not exist, and
`THREAD_RECOVERY_FAILED` if recovery itself fails.

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
Context: 83k / 200k (42%) | -
```

Context usage is best-effort enrichment from the Codex rollout store; `-`
means unavailable. `status` never resumes the thread.

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
codexctl follow <thread-id> [--replay-turns SELECTOR]
```

Attaches to the thread's current active turn. Fails with `NO_ACTIVE_TURN`
if there is none. Before streaming live events, `codexctl` replays a
continuous suffix of the reconstructed history; events visible in both
phases are emitted once.

`--replay-turns` accepts exactly three forms:

| Selector | Meaning |
|---|---|
| `-1` (default) | Replay only the active turn's known history |
| `-N:` | Replay the latest N turns including the active turn |
| `:` | Replay the entire available history |

`follow` exits when the followed turn reaches a terminal state, with the
corresponding [exit code](#exit-codes).

### steer

```sh
codexctl steer <thread-id> -- INPUT...
```

Adds steering input to the active turn. The request carries the turn id
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
codexctl list [--all]
```

By default, lists stored threads whose workspace is the current directory,
newest activity first. `--all` lists threads across all workspaces:

```text
<thread-id>  idle  <preview>
```

### doctor

```sh
codexctl doctor
```

Runs diagnostics and prints a compatibility verdict: endpoint
reachability, the initialize handshake, required lifecycle operations, the
local `codex` CLI version (managed mode only), and rollout-based context
enrichment availability. Context enrichment is optional and does not make a
runtime incompatible.
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
{"type": "thread/tokenUsage/updated", "threadId": "...", "turnId": "...", "source": "live", "usage": {"usedTokens": 83000, "windowTokens": 200000, "ratio": 0.415}}
{"type": "turn/completed", "threadId": "...", "turnId": "...", "status": "completed", "source": "live"}
```

- `source` is `"live"` for events delivered from the runtime and
  `"replay"` for events reconstructed from history (`follow`, and
  `history -o jsonl`). Replay records additionally carry `turnIndex`.
- `item` is a projected item, see [Projected items](#projected-items).
- `turn/completed` carries `status` (`completed`, `interrupted`, or
  `failed`) and, when present, `error: {"message": ...}`.
- `thread/tokenUsage/updated` carries `usage` with `usedTokens`,
  `windowTokens`, and `ratio` when the runtime provides a context window;
  unavailable usage is omitted.
- `error` records carry `error: {"code", "message"}` with a stable
  [error code](#error-codes).

`history -o jsonl` emits one finite lifecycle-shaped sequence per selected
turn: `turn/started`, one `item/completed` per item, `turn/completed` —
omitted for a turn still in progress, which yields no `turn/completed`
record (same as `follow` replay).

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
  "context": {"usedTokens": 83000, "windowTokens": 200000, "ratio": 0.415, "source": "live|rollout"},
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
  "endpointMode": "managed|external",
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
- `ws://HOST:PORT[/PATH][?QUERY]` connects over TCP and preserves the path
  and query exactly. `--endpoint-token-file` is accepted only for this form;
  its trimmed, non-empty contents are sent as `Authorization: Bearer ...`.
  Credential-bearing query parameters (`token`, `access_token`, `id_token`,
  `refresh_token`, `bearer_token`, and `authorization`, case-insensitively)
  are rejected; credentials must not be placed in an endpoint URL.

Malformed endpoint configuration is a `USAGE_ERROR`. An unavailable endpoint,
or a missing, unreadable, or empty token file, is `APP_SERVER_UNAVAILABLE`.

Environment variables:

| Variable | Effect |
|---|---|
| `CODEXCTL_CODEX_BIN` | Override the `codex` binary used in managed mode |
| `CODEX_HOME` | Codex home directory (inherited Codex convention) |

## Unattended operation

`codexctl` never blocks on human input:

- Command approval requests are declined automatically.
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
| `NO_ACTIVE_TURN` | No active turn for follow/steer/interrupt | 3 |
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
| 0 | Success (streaming commands: the turn completed) |
| 2 | Usage error (argument/output-mode/selector problems) |
| 3 | Domain conflict (see error-code table) |
| 4 | The followed turn failed or was interrupted |
| 5 | Runtime/protocol error |
| 130 | Local interruption (Ctrl+C). Never sends a turn interrupt. |
