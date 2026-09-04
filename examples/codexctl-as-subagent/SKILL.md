---
name: codexctl-as-subagent
description: Run Codex workers through codexctl
---

Install `codexctl` from GitHub as a `uv` tool when it is not already
available:

```sh
uv tool install git+https://github.com/ffff2004/codexctl.git
```

Use `codexctl` to delegate an independent, bounded task to a persistent Codex
thread. A worker shares the checkout and runtime, but has its own context.

1. Give each worker a non-overlapping responsibility. Its prompt states the
   objective, files or area it owns, constraints, required verification, and
   the handoff expected. Tell it that other agents may change the checkout, so
   it must preserve unrelated changes and report any friction or assumptions.

   Start the worker and save the returned `threadId`:
   (For Codex: if skill `command-resume-hook` exists, use it to run `codexctl start`)

   ```sh
   codexctl start --cwd "$PWD" --approve-for-me -- \
     "Implement <bounded task>. You own <files or area>. <constraints>. Run <checks>. At handoff, summarize changes, checks, assumptions, and friction."
   ```

   Use `--sandbox read-only` instead of `--approve-for-me` for investigation-only work.
   Start separate threads only when their responsibilities cannot conflict.

2. Wait until it ends, or continue work that does not overlap the worker if the user asks so.
   
   Inspect its state only when an answer is useful:

   ```sh
   codexctl status --json THREAD_ID
   ```

   An active thread is still working; an idle thread has finished its turn.
   Do not use `resume` to ask for progress: it starts a new turn and fails
   while the worker is active. Use `steer THREAD_ID -- "..."` only to add
   genuinely useful mid-turn guidance.

3. When the worker is idle, collect its final handoff and inspect its changes:

   ```sh
   codexctl history --json --turns -1 THREAD_ID \
     | jq -r '.turns[0].items | map(select(.type == "agentMessage")) | last | .text // empty'
   ```

   Verify the claimed checks yourself before accepting the work. If follow-up
   is needed, resume the same idle thread with a precise request:

   ```sh
   codexctl resume THREAD_ID -- "<specific follow-up>"
   ```

4. Treat a failed, interrupted, or unsupported-interaction turn as a handoff
   failure. Read its history, resolve the blocker in the parent task, then
   resume the same thread only when a new turn is appropriate. Never replace a
   failed thread with a new one merely to hide the failure.

See [Reference](https://github.com/ffff2004/codexctl/blob/main/docs/reference.md) for details.
