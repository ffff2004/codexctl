# Implementation review orchestrator

This is a self-contained demo of a worker/reviewer/gates workflow built on
the documented [`codexctl` JSONL contract](../../docs/reference.md). It is
deliberately one Python file and uses only the standard library.

Run it from this directory, with any clean Git checkout as `--cwd`:

```sh
cd examples/impl-review-orchestrator
python impl_review.py \
  --cwd /path/to/checkout \
  --spec /path/to/spec.md \
  --issue https://github.com/owner/repo/issues/19 \
  --worker-prompt prompts/worker.md \
  --repair-prompt prompts/repair.md \
  --reviewer standards=prompts/reviewers/standards.md \
  --reviewer spec=prompts/reviewers/spec.md \
  --gate 'uv run pytest'
```

`--worker-prompt`, `--repair-prompt`, and every `--reviewer NAME=PATH` prompt
are required UTF-8 files and are snapshotted at run start. A failed first
review starts a fresh worker round and resumes the reviewer threads. Short
prompt templates are provided in
`prompts/worker.md`, `prompts/repair.md`, and
`prompts/reviewers/{standards,spec}.md`.
Prompt templates may use `{{gates}}`; configured `--gate` commands are rendered
one per line.

`--issue` is optional and must be a complete GitHub Issue URI, such as
`https://github.com/owner/repo/issues/19`. The URI is passed through unchanged
when `{{issue}}` is rendered; when omitted, it renders as `none`. The URI also
provides the repository and issue number used by `gh` when publishing review
findings. No issue is fetched automatically. `--publish-review-findings` is
opt-in and requires `--issue`.

A paused run is resumed without reading stdin:

```sh
python examples/impl-review-orchestrator/impl_review.py resume \
  --run-id RUN_ID --decision retry
```

Possible decisions are recorded in `state.json`. Typical ones are `retry`,
`retry-publication`, `accept`, `start-next-round` after a repair round still
has review findings, and `acknowledge-drift` after inspecting a changed
checkout. For example, after the second repair round still fails review:

```sh
python examples/impl-review-orchestrator/impl_review.py resume \
  --run-id RUN_ID --decision start-next-round
```

This creates another repair worker round and resumes the reviewer threads.
The decision can be repeated for subsequent failed rounds. Ambiguous codexctl
output, publication failures, and checkout drift become `WAITING_FOR_USER`.

State defaults to
`$XDG_STATE_HOME/codexctl/impl-review-orchestrator` (or
`~/.local/state/codexctl/impl-review-orchestrator`). Inputs, rendered prompts,
raw JSONL/std streams, final messages, findings, and gate records are
immutable artifacts. `state.json` is atomically replaced. Failed-review
changes remain unstaged while the caller inspects them; when a repair round is
started, the orchestrator stages the checkout with `git add --all` before the
new worker runs. It never commits, merges, creates worktrees, or cleans up.

Use `--output json` (or `--json`) for one machine-readable result containing
the run ID, artifacts, review verdicts, gate results, and handoff status.
