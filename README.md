# codexctl

Command-line interface for starting, resuming, observing, steering, and
interrupting Codex threads through a shared Codex app-server runtime.

`codexctl` is a lifecycle tool: it treats a Codex thread as a long-lived
object that survives process exits, terminal closures, and machine restarts,
and it never queues, forks, or silently replaces threads.

## Install

Install the latest version directly from GitHub as a `uv` tool:

```sh
uv tool install git+https://github.com/ffff2004/codexctl.git
```

## Quick start

Requires Python 3.14+ and a local `codex` CLI with app-server support.
Managed mode starts the shared daemon automatically; see
[Use a different runtime](#use-a-different-runtime) for alternative runtimes.

```sh
codexctl start -- fix the failing test in src/
codexctl start - < prompt.txt
codexctl follow <thread-id>
codexctl steer <thread-id> -- also run the integration tests
codexctl interrupt <thread-id>
codexctl status <thread-id>
codexctl history <thread-id> --turns -2:
codexctl list
codexctl doctor
```

## Use a different runtime

The default `managed` runtime uses the local shared daemon: `codexctl` reuses
it when reachable and starts it when necessary. Use one of the runtime
selectors below when the app-server is owned or reached some other way. The
selectors are mutually exclusive and can be used with `doctor` as a safe
connectivity and compatibility check before running other commands.

```sh
# An externally managed local Unix-socket endpoint.
codexctl doctor --endpoint unix:///run/user/1000/codex.sock

# An externally managed WebSocket endpoint; keep credentials in a file.
codexctl doctor \
  --endpoint ws://127.0.0.1:4500/app-server \
  --endpoint-token-file ~/.config/codex/app-server.token

# Start a caller-selected stdio relay for this invocation.
codexctl doctor \
  --stdio-exec codex \
  --stdio-arg=app-server \
  --stdio-arg=proxy \
  --stdio-framing websocket

# Reach a remote managed runtime through SSH.
codexctl doctor --ssh devbox

# Reach an externally managed socket on that remote host.
codexctl doctor --ssh devbox --remote-socket /run/user/1000/codex.sock
```

`--endpoint` and `--remote-socket` only connect; they do not start or stop a
daemon. SSH without `--remote-socket` starts (or reuses) the remote Codex
daemon for the invocation. Stdio mode starts a fresh child for each command,
so the child must speak the selected framing. See the
[runtime resolution reference](docs/reference.md#runtime-resolution) for
endpoint formats, authentication, SSH options, and framing details.

## Documentation

- [Reference](docs/reference.md): commands, flags, output modes, JSON
  shapes, error codes, and exit codes (public contract).
- [Architecture](docs/architecture.md): internal module layout, seams, and
  protocol adaptation.
- [Examples](examples/): example integrations and workflows.

## Development

See [CONTRIBUTING](CONTRIBUTING.md).
