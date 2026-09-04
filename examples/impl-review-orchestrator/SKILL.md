---
name: impl-review-orchestrator
description: Run or resume a commit-checkpoint implementation and review workflow.
disable-model-invocation: true
---

# Implementation review orchestrator

Use this skill when a task needs Codex Workers to implement committed changes,
configured gates to attest each candidate, and isolated Reviewer cohorts to
certify the resulting Git commit. It operates one existing checkout; it is not
for a one-off code review or a workflow that must retain uncommitted edits.

The public behavior and prompt templates are maintained in
[README.md](README.md), [impl_review.py](impl_review.py), and
[prompts/](prompts/). Treat the command's `allowedActions` report as the
authority for a particular run's next transition.

`impl_review.py` and the example [prompts/](prompts/) directory are siblings
of this `SKILL.md`.

## Start a run

1. Confirm that `--cwd` is the root of a clean Git checkout, its Git identity
   (`user.name` and `user.email`) is configured, and no other run is changing
   that checkout. Choose a state directory when the default state location is
   not appropriate.

2. Collect a concrete specification plus Worker, repair, and Reviewer prompt
   files. Select gates that check the checkout without mutating it. Give each
   Reviewer a distinct `NAME=PATH` rubric; include a spec reviewer and a
   standards reviewer unless the task calls for another cohort.

   Completion: every prompt path and gate command is intentional and can run
   in the target checkout.

3. Start the workflow with `python3.14` from this skill dir. Use absolute
   paths for target-repository inputs when the invocation directory might vary.

   ```sh
   python3.14 impl_review.py start \
     --cwd /absolute/path/to/checkout \
     --spec /absolute/path/to/spec.md \
     --worker-prompt prompts/worker.md \
     --repair-prompt prompts/repair.md \
     --reviewer standards=prompts/reviewers/standards.md \
     --reviewer spec=prompts/reviewers/spec.md \
     --gate 'uv run pytest' \
     --gate 'uv build'
   ```

   `--branch` chooses the created work branch; otherwise it is
   `impl-review/<run-id>`. `--max-auto-worker-rounds` defaults to two. Add
   `--worker-approve-for-me` only when the Worker is authorized to approve its
   own requested operations. `--model` and `--effort` apply to every started
   agent. Capture the emitted run ID and state directory.

   Completion: stdout reports the run ID, branch, candidate checkpoint, and
   either a terminal status or a waiting status. Progress belongs on stderr;
   use `--output json` when stdout is consumed programmatically.

## Advance a waiting run

Inspect first; it is read-only and names the only valid actions:

```sh
python3.14 impl_review.py inspect RUN_ID --output json
```

Before resuming, keep the orchestrator-created branch checked out and preserve
the saved checkpoint. Do not add, amend, rebase, merge, reset, switch away, or
otherwise change the checkout outside the Worker. A mismatch becomes
`CHECKOUT_DRIFT` and requires human recovery.

Choose an action from `allowedActions` and resume:

```sh
python3.14 impl_review.py resume RUN_ID \
  --action START_NEXT_ROUND \
  --additional-prompt 'Preserve the public API.'
```

- `START_NEXT_ROUND` asks a Worker to repair or continue. It is the only action
  that accepts `--additional-prompt` or `--additional-prompt-file`; that
  amendment is snapshotted and included in later prompts.
- `RETRY_GATES` retries an interrupted or errored gate; `RETRY_REVIEWERS`
  retries an incomplete Reviewer session.
- `REQUIRE_FRESH_AUDIT` reruns gates as needed and starts a fresh full audit.
- `ACCEPT_FINDINGS` records an explicit waiver, producing
  `READY_WITH_WAIVER`, not a certificate.
- `CONTINUE_WORKER` and `ACCEPT_WORKER_RESULT` are recovery options exposed
  only for the reported interrupted Worker state.

Completion: resume returns a new waiting report, `READY_CERTIFIED`, or
`READY_WITH_WAIVER`. Re-inspect after any interrupted invocation rather than
repeating `start`.

## Handoff

`READY_CERTIFIED` (exit code 0) means a current gate attestation and a fresh
full Reviewer audit bind the same commit. Exit code 2 means the run needs an
allowed action; exit code 3 is a waiver. The tool leaves the work branch
checked out and never pushes, merges, deletes the branch, or returns to the
original branch. State artifacts contain the gate streams, agent JSONL, final
messages, and digests needed to investigate a result.
