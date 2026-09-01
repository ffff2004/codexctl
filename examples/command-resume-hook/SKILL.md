---
name: command-resume-hook
description: Handoff long-running shell commands with the command resume hook when completion should wake the current Codex thread and leave stdout/stderr available for on-demand reading.
---

# Command resume hook

**For Codex only**

Use this skill for a command that is expected to outlive the current Codex
turn. The handoff is the important operation: start the wrapper in the
foreground, let the execution tool return a live session, then end the turn.
The wrapper keeps running, saves the command result, and makes one
best-effort wake-up attempt when the command finishes: it tries `steer` first,
then falls back to `codexctl resume` only for `NO_ACTIVE_TURN`.

Install `codexctl` from GitHub as a `uv` tool when it is not already
available:

```sh
uv tool install git+https://github.com/ffff2004/codexctl.git
```

## Handoff

1. Choose the handoff only when waiting for the command would make the
   current turn spend its time polling. Use an ordinary foreground execution
   for short commands whose result is needed immediately.
   Completion criterion: the command is expected to outlive the current turn.

2. Use `CODEX_THREAD_ID` as the target thread ID. It is inherited by child
   processes. Do not substitute the execution tool's `session_id`; that ID
   only identifies a command process for optional later polling.
   Completion criterion: a non-empty target thread ID is selected.

3. Invoke the wrapper with all wrapper options before `--`, followed by the
   command argv:
   (The wrapper script is in the same dir of this SKILL.md)

   ```sh
   command-resume-hook.sh -- COMMAND ARG ...
   ```

   For a shell pipeline or redirection, make the shell explicit:

   ```sh
   command-resume-hook.sh -- \
     bash -lc 'COMMAND | OTHER_COMMAND'
   ```
   Completion criterion: the wrapper receives the intended command argv after
   its `--` delimiter.

4. Run that command in the foreground and ask the execution tool to return
   after a short initial wait. When it returns a live `session_id` without a
   final exit code, the handoff is complete: report that the wrapper has
   started and end the current turn.

   Do not append `&` or use `nohup`. They detach from the shell, but the
   execution supervisor may still clean up those descendants when the tool
   invocation ends. Do not poll the live session before ending the turn.

The handoff step is complete only when a live execution session was returned,
the target thread ID was available, and no polling was performed afterward.

## Wake-up

When the completion prompt resumes the thread, read the paths in that prompt
as needed. Start with `result.txt`, then inspect `stdout.log` and `stderr.log`.

The default job directory is `${TMPDIR:-/tmp}/codexctl-jobs/job.*`. A job
contains:

- `stdout.log` and `stderr.log` — the wrapped command's separate streams;
- `result.txt` — the escaped command, command exit code, and stream paths;
- `steer.stdout.log` and `steer.stderr.log` — the steer command's streams;
- `resume.stdout.log` and `resume.stderr.log` — the resume fallback's streams;
- `wake.result.txt` — the wake method, exit codes, and steer error code.

The completion prompt contains the command, its exit code, the stdout path,
the stderr path, and the result metadata path. Treat the command exit code as
the command outcome; use `wake.result.txt` to diagnose notification delivery.
The wrapper's own stdout is a short summary containing the job directory,
command, wake method, wake exit code, and both command-output paths.

## Limits

The wrapper makes exactly one steer attempt and at most one resume fallback.
It does not poll or retry. If steer returns an error other than
`NO_ACTIVE_TURN`, no resume is attempted. If the command finishes before the
original turn ends, steer can deliver the prompt without creating a new turn;
if the original turn has ended, resume can create one. The command artifacts
remain authoritative when either wake-up operation fails.

Use `--thread-id ID` when deliberately targeting another thread. Use
`--output-dir DIR` when the default temporary location is unsuitable. Keep
the output directory available until the resumed turn has read the needed
files, and treat command output as potentially sensitive.
