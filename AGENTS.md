# Project guide

`codexctl` is a lifecycle CLI for Codex threads over a shared Codex
app-server runtime: start, resume, status, history, follow, steer,
interrupt, list, doctor, etc.

It stores nothing locally; threads live in the shared runtime.

## Documentation

- `docs/reference.md` — public contract (commands, output modes, JSON
  shapes, error codes, exit codes). Single source of truth for anything
  observable.
- `docs/architecture.md` — internal modules, seams, protocol adaptation.
  Links to reference.md instead of repeating public facts.
- `docs/codex-reference.md` — record of which upstream Codex source files
  each protocol fact was read from, pinned to a Codex stable-release tag.

### Documentation rules

- Single source of truth:
  Link to an authoritative source instead of restating its details,
  except README: it is intended for users/consumers, not maintainers.
- Documents describe current implemention;
  future outcomes belong in tracker specs or tickets.
- Keep changing task state in GitHub rather than maintaining a repository copy.
- Legacy documents must identify themselves as historical and must not be used
  as an alternative source for current architecture or behavior.
- Do not copy full types, schemas, test logs, or
  ADR rationale into prose. Link to the owning source.

## Commit messages

- Use Conventional Commits (`feat:`, `fix:`, `refactor:`, `docs:`,
  `chore:`, ...).
- If a commit closes an issue, add a `Closes #N` line to the
  commit message.
- If a commit is related to an issue, the reference belong in commit message,
  not source files, comments, or docs. 

## Commands

Dependencies are managed with `uv`:

```sh
uv sync          # create .venv, install project + dev dependencies
uv run pre-commit install --hook-type pre-commit --hook-type pre-push --install-hooks  # install commit and push hooks
uv run pytest    # run the test suite (no sockets needed)
uv run codexctl  # run the CLI
uv build         # build dist/ artifacts
```

Python >= 3.11, src layout (`src/codexctl/`), pytest with
`asyncio_mode = "auto"`.

## Code layout

- `model.py` — closed vocabulary crossing `CodexCtl.run`: commands,
  outcomes, projected events, selectors, error codes. No wire types here.
- `core.py` — `CodexCtl`: dispatch, orchestration, race handling, follow
  replay/live frontier, error mapping.
- `endpoint.py` — managed daemon lifecycle vs external `--endpoint`.
- `appserver.py` — JSON-RPC over websocket transports, initialize
  handshake, unattended interaction policy, projection (compatibility
  firewall; raw Codex wire types never leave this module).
- `rollout.py` — best-effort read-only rollout reader; never raises.
- `render.py` / `cli.py` — outside the seam; output formats never
  influence execution behavior.

## Invariants to preserve when changing code

- `resume` never queues and never becomes steer; recovery failure never
  creates a replacement thread.
- `status`/`history` are strictly read-only.
- Detaching and local Ctrl+C never send a turn interrupt.
- Unattended: never block on human input (decline approvals, surface
  `UNSUPPORTED_INTERACTION`).
- Follow emits each event once across replay/live, keyed on stable
  Codex identities (see `docs/architecture.md` for the dedup key).
- Error mapping: `-32601` → `INCOMPATIBLE_CODEX`; rejected interrupt is
  always a domain error (`NO_ACTIVE_TURN`).
- Core behaviors are pinned by tests in `tests/` driven through
  `FakeAppServer` (see `tests/conftest.py`); update them together with
  behavior changes, and keep `docs/reference.md` in sync.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues; use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default canonical triage labels. See `docs/agents/triage-labels.md`.
