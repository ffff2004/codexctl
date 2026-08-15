# codexctl

Command-line interface for starting, resuming, observing, steering, and
interrupting Codex threads through a shared Codex app-server runtime.

`codexctl` is a lifecycle tool: it treats a Codex thread as a long-lived
object that survives process exits, terminal closures, and machine restarts,
and it never queues, forks, or silently replaces threads.

## Install

```sh
uv sync
```

Requires a local `codex` CLI with app-server support (managed mode starts
the shared daemon automatically), or an externally managed endpoint via
`--endpoint`.

## Quick start

```sh
codexctl start -- fix the failing test in src/
codexctl follow <thread-id>
codexctl steer <thread-id> -- also run the integration tests
codexctl interrupt <thread-id>
codexctl status <thread-id>
codexctl history <thread-id> --turns -2:
codexctl list
codexctl doctor
```

## Documentation

- [Reference](docs/reference.md): commands, flags, output modes, JSON
  shapes, error codes, and exit codes (public contract).
- [Architecture](docs/architecture.md): internal module layout, seams, and
  protocol adaptation.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```
