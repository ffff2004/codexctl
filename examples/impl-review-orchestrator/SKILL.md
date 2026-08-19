---
name: orchestrate-impl-review
description: Orchestrate a design-implementation-review workflow
disable-model-invocation: true
---

Orchestrate the following workflow:

1. If the requirements are ambiguous, use `grilling` to question the user until the spec is clear. Make the in-scope and out-of-scope behavior explicit so the implementation does not expand beyond the request. After reaching agreement, pause and wait for the user's confirmation.
2. If designing a new module or public interface is necessary, use `codebase-design` to start sub-agents and present the available design options. If the design already exists or can be determined simply, explain why this step can be skipped. Pause and wait for design confirmation before continuing.
3. Run the following implement-review-stage loop with `impl_review.py`. Run two rounds autonomously; if the second review still fails, report the review findings, tell the user that `start-next-round` creates another repair round and `accept` continues to gates, and pause.

   Run the script with the provided example prompts:

   See the [README.md](README.md) for additional usage details.

   ```sh
   python impl_review.py \
     --cwd <checkout-root> \
     --spec <spec-file> \
     [--issue <github-issue-uri>] \
     --worker-prompt prompts/worker.md \
     --repair-prompt prompts/repair.md \
     --reviewer standards=prompts/reviewers/standards.md \
     --reviewer spec=prompts/reviewers/spec.md \
     [--publish-review-findings] \
     --gate '<gate-command>' \
     --gate '<another-gate-command>'
   ```

   If waiting by polling, use the maximum polling interval.

   The `--issue` and `--publish-review-findings` arguments are optional; the latter requires the former. Add `--issue` and `--publish-review-findings` when the task has a related GitHub issue. Add one `--gate` argument for each project gate. The example prompts may be copied and customized when the task needs different worker or reviewer instructions.

   For each implementation or repair round, `impl_review.py` will:

   1. Start a new worker thread. The worker should run gates as appropriate and explain any gate it did not run or that failed. The script verifies that the worker did not change the branch, HEAD, or staged Git state.
   2. Start reviewers concurrently and reuse reviewer threads by reviewer name across rounds. Reviewers receive the complete unstaged diff.
   3. If a reviewer returns `verdict=None` or another ambiguous result, resume the run with `--decision retry`. The script prefers the just-failed thread and sends `output VERDICT: PASS|FAIL` for one retry turn.
   4. If the review fails, the script stages the current changes with `git add --all` before starting the next repair round, then starts a fresh worker. The first failed round automatically starts round two. If round two still fails, pause and report the findings; `start-next-round` can be used to continue manually beyond two rounds.

   When the script returns `WAITING_FOR_USER`, use the `pending.decision` value in the result to resume it. For example, for an ambiguous reviewer result:

   ```sh
   python impl_review.py resume \
     --run-id <run-id> --decision retry
   ```

   To continue with another repair round after a failed second review:

   ```sh
   python impl_review.py resume \
     --run-id <run-id> --decision start-next-round
   ```

   Only after every reviewer passes does the script enter the final gate phase. It runs the configured gates sequentially on the final checkout. On the normal successful path, each gate is executed once by the orchestrator; an explicit `retry` after a failed gate is failure recovery. The script finishes in `READY_FOR_HANDOFF` when the workflow is ready for the caller's handoff steps.

4. Commit the changes.
