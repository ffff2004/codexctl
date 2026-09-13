# Implementation review orchestrator

This standalone example runs an implementation and review workflow whose
durable checkpoints are Git commits. It uses the documented
[`codexctl` contract](../../docs/reference.md), the Python standard library,
and a single deep [`Workflow`](impl_review.py) interface.

The caller supplies a clean checkout. The orchestrator freezes its current
`HEAD`, creates and leaves checked out a new work branch, and asks each Worker
to append commits. It runs configured gates against every candidate commit
before review. A fresh reviewer cohort audits the cumulative base-to-candidate
subject. If that audit fails, the same cohort reviews repairs incrementally;
an incremental pass rotates to another fresh cumulative audit before the run
can be certified.

## Run

From this directory:

```sh
python3.14 impl_review.py start \
  --cwd /path/to/checkout \
  --spec /path/to/spec.md \
  --worker-prompt prompts/worker.md \
  --repair-prompt prompts/repair.md \
  --reviewer standards=prompts/reviewers/standards.md \
  --reviewer spec=prompts/reviewers/spec.md \
  --gate 'uv run --locked ruff format --check .' \
  --gate 'uv run --locked ruff check .' \
  --gate 'uv run --locked pyright' \
  --gate 'uv run --locked pytest' \
  --gate 'uv build'
```

`--branch` selects the new branch; otherwise the name is
`impl-review/<run-id>`. `--max-auto-worker-rounds` defaults to 2 and counts the
initial Worker. `--worker-approve-for-me` is a run-level, Worker-only switch.
`--gate-timeout-seconds` defaults to 1800 for each gate. Prompt, rubric, gate,
model, effort, isolation, and approval policy inputs are snapshotted at start.
If a reviewer rubric contains the optional `{spec}` placeholder, the supplied
specification is injected there when the reviewer prompt is composed.

Workers run in `workspace-write`; reviewers run in `read-only`. Every start and
resume uses `--no-goals --no-agents`. Agent starts detach first, then the
example follows the thread with `follow --persist --replay-turns :`. The full
history replay and live subscription form one deduplicated stream, so recovery
reconstructs prior evidence without missing a turn that finishes while the
follower attaches. Follow is observational and sends no isolation overrides.
Reviewers inspect the exact commit range themselves and do not run configured
gates.

An agent attempt is one logical execution initiated by an orchestrator start or
resume. It may span multiple turns, including continuation turns created after
a Worker hands off a long command through the
[`command-resume-hook`](../command-resume-hook/). Each attempt receives a fresh
128-bit correlation token. A Worker finishes only when its final agent message
ends with `WORKER_DONE: TOKEN` and that same turn completes successfully. A
reviewer analogously ends with `REVIEW_RESULT: TOKEN PASS` or
`REVIEW_RESULT: TOKEN FAIL`. The token-bound marker is semantic completion;
leading and trailing whitespace on its line is ignored. The marker otherwise
must be the unique last non-empty line of the final agent message in its turn.
The Worker result must still pass the branch, clean-checkout, changed-HEAD, and
linear-history checks. A successful intermediate turn without the marker leaves
the persistent follower waiting for another turn. There is deliberately no idle
timeout.

While `start` or `resume` is running, concise operational progress is written
to stderr and flushed immediately. The initial or loaded run ID and state path
appear before long work. Once an agent's detach receipt is durable, progress
includes its Worker or reviewer role, attempt ID, thread ID, and initial turn ID, so a
caller can observe it independently with `codexctl follow THREAD_ID`. Progress
also marks checkpoint, gate, review, waiting, and terminal boundaries and
references artifacts instead of printing prompts, model messages, or raw gate
and agent output. There is no heartbeat.
Progress delivery is best-effort: a closed or failing progress consumer cannot
change workflow execution, durable state, or recovery.

The final report is the only stdout output. In particular, `--output json`
keeps stdout parseable while progress continues on stderr. `inspect` is a
short read-only operation and emits no progress.

## Resume and inspect

A waiting report lists its valid typed actions. For example:

```sh
python3.14 impl_review.py resume RUN_ID --action START_NEXT_ROUND \
  --additional-prompt 'Preserve the public API.'

python3.14 impl_review.py resume RUN_ID --action RETRY_REVIEWERS
python3.14 impl_review.py inspect RUN_ID --output json
```

`--additional-prompt-file PATH` snapshots the same persistent amendment;
`PATH=-` reads it from stdin. Amendments are cumulative and are included in all
later Worker and reviewer prompts. The closed action vocabulary and current
state transitions are owned by [`impl_review.py`](impl_review.py), while the
behavioral coverage is in [`tests/test_impl_review.py`](tests/test_impl_review.py).
During a persisted in-flight Worker operation, `inspect` reports `RUNNING`
without requiring the checkout to match the saved checkpoint. Stable waiting,
review, and terminal states continue to report checkout drift strictly.

Exit 0 means a current gate attestation and fresh full-audit certificate bind
the same commit. Exit 2 means the run is waiting. Exit 3 means explicit review
findings were accepted as a waiver; a waiver is never a certificate. Fatal,
usage, and internal failures exit 1.

State defaults to
`$XDG_STATE_HOME/codexctl/impl-review-orchestrator` (or
`~/.local/state/codexctl/impl-review-orchestrator`). State replacement is
atomic, advancement uses a cross-process run lock, and `inspect` is read-only.
The artifact manifest records SHA-256 digests for prompt snapshots, amendments,
gate streams, agent JSONL, and final messages. Agent artifacts also identify
their owning attempt or review session, role, initial and terminal turn IDs,
and every observed turn ID. Attempt state remains `RUNNING` across successful
intermediate turns and becomes terminal only after marker-bound completion or
an explicit failed/interrupted outcome.
Git commit identity remains the repository's native object ID.

The orchestrator never creates a worktree, adopts external commits, commits on
behalf of a Worker, merges, pushes, rebases, amends, resets, deletes the work
branch, or switches back at handoff. Checkout concurrency between different
runs remains the caller's responsibility.
