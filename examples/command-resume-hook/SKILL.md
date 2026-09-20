---
name: command-resume-hook
description: Run long (>30s) non-interactive shell commands while continuing independent work or handing off when the next step depends on the result; recover output and exit codes without an execution session ID.
---

# Command resume hook

**For Codex only.** Use the [wrapper](command-resume-hook.sh) for long commands
whose output and exit code must survive loss of the execution session ID.
Choose **continue** when there is independent work to advance that does not
depend on the command's output. Choose **handoff** when no independent work
remains and the next step needs the command's result.
Use ordinary foreground execution for short commands.

## Start

1. Ensure `codexctl` is available. If missing, install it with
   `uv tool install git+https://github.com/ffff2004/codexctl.git`.
   The wrapper checks `codexctl doctor` before running the command; a failed
   check prints its output and exits without executing the command.
2. Run the wrapper in the foreground with a short initial tool wait:

   ```sh
   command-resume-hook.sh -- COMMAND ARG ...
   ```

   Use the script beside this SKILL.md. Place wrapper options before `--`.
   For pipelines or redirection, pass an explicit shell:

   ```sh
   command-resume-hook.sh -- bash -lc 'COMMAND | OTHER_COMMAND'
   ```

3. The wrapper prints the job ID, directory and log paths before executing
   the command. A live tool `session_id` means execution continues. Follow
   the chosen workflow below; if execution already ended, read its result.

Keep the wrapper attached to the execution tool: do not use `&` or `nohup`.
The command's streams go to files, so this is for non-interactive commands,
not commands requiring a terminal or user input.

## Continue independent work in the current turn

Keep the turn active and advance work that does not depend on the command's
output. The completion notification reaches the active turn through `steer`.
When the independent work is done, read the command's result if available;
if the command is still running, switch to **handoff** below.

If the job path is lost, recover tasks using:

```sh
command-resume-hook.sh --list
command-resume-hook.sh --job JOB_ID
```

Match the command and start time, then read its result and logs if needed.
Queries work without `codexctl` or a running service.

**A missing result means unknown status**:
the command may still be running, or the wrapper may have been terminated.
Investigate before deciding what to do.

## Handoff to a later turn

With a live execution session and the wrapper's job registration for the
target thread confirmed, report the job location and end the current turn.
This applies both immediately after starting the command and after finishing
independent work. Once choosing handoff, end the turn without further polling.
This completes the handoff, not the command's work.

When notified in a later turn, read the result and logs if needed, then
continue the pending task. Recover via `--list` if necessary.

## Results and notifications

Jobs live under `${TMPDIR:-/tmp}/codexctl-jobs/THREAD_ID/job.*` by default.
Each execution gets a separate directory, including concurrent executions.

- `job.txt`: thread ID, shell-escaped command and start time.
- `result.txt`: command exit code, elapsed time, completion time and log paths;
  published atomically **before** notification. `status=completed` means the
  command exited, including failure; inspect `command_exit_code`.
- `stdout.log` and `stderr.log`: command output, readable during execution.
- `wake.result.txt`: notification method and exit codes; this describes
  delivery, not command success.
- `steer.*.log` and `resume.*.log`: notification command streams.

Read these files as data; never source them as shell code. Query exit codes
describe query success, while normal execution returns the wrapped command's
exit code unless setup or result persistence fails.

Use `--thread-id ID` to select a different thread (default: `CODEX_THREAD_ID`).
Use `--output-dir DIR` or `CODEXCTL_JOB_DIR` to choose a different job root;
use the same root for recovery queries. `--codexctl PATH` selects the CLI.
Keep artifacts until the result has been consumed. Temporary storage may be
cleaned externally; use a persistent root when recovery must survive that.
