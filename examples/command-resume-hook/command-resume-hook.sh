#!/usr/bin/env bash
#
# Run a command in the background, persist its output, and wake the current
# Codex thread when it finishes.
#
# The wrapper first steers the current turn. If the structured steer error is
# NO_ACTIVE_TURN, it makes one resume attempt. It does not poll or retry: the
# command result remains available even when notification delivery fails.

set -u -o pipefail

usage() {
  cat <<'EOF'
Usage:
  command-resume-hook.sh [--thread-id ID] [--output-dir DIR]
                         [--codexctl PATH] -- COMMAND [ARG ...]

The thread ID defaults to CODEX_THREAD_ID. Command stdout and stderr are
stored separately in a unique job directory below DIR. DIR defaults to
${TMPDIR:-/tmp}/codexctl-jobs.
EOF
}

die() {
  printf 'command-resume-hook: %s\n' "$1" >&2
  exit 2
}

thread_id=${CODEX_THREAD_ID-}
output_root=${CODEXCTL_JOB_DIR:-${TMPDIR:-/tmp}/codexctl-jobs}
codexctl_bin=${CODEXCTL_BIN:-codexctl}

while (($# > 0)); do
  case $1 in
    --thread-id)
      (($# >= 2)) || die "--thread-id requires a value"
      thread_id=$2
      shift 2
      ;;
    --output-dir)
      (($# >= 2)) || die "--output-dir requires a value"
      output_root=$2
      shift 2
      ;;
    --codexctl)
      (($# >= 2)) || die "--codexctl requires a value"
      codexctl_bin=$2
      shift 2
      ;;
    -h|--help)
      usage >&1
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      die "options must come before --"
      ;;
  esac
done

[[ -n $thread_id ]] || die "--thread-id or CODEX_THREAD_ID is required"
(($# > 0)) || die "a command is required after --"

if ! mkdir -p -- "$output_root"; then
  die "cannot create output directory: $output_root"
fi

job_dir=$(mktemp -d "$output_root/job.XXXXXX") || die "cannot create job directory"
stdout_file=$job_dir/stdout.log
stderr_file=$job_dir/stderr.log
result_file=$job_dir/result.txt
resume_stdout_file=$job_dir/resume.stdout.log
resume_stderr_file=$job_dir/resume.stderr.log
steer_stdout_file=$job_dir/steer.stdout.log
steer_stderr_file=$job_dir/steer.stderr.log
wake_result_file=$job_dir/wake.result.txt

# Keep the artifact set stable even when steer succeeds or no resume fallback
# is needed.
: >"$resume_stdout_file"
: >"$resume_stderr_file"

# %q makes the command unambiguous in the wake-up prompt while retaining the
# exact argv boundaries used for execution.
printf -v command_text '%q ' "$@"
command_text=${command_text% }

if "$@" >"$stdout_file" 2>"$stderr_file"; then
  command_exit_code=0
else
  command_exit_code=$?
fi

{
  printf 'command=%s\n' "$command_text"
  printf 'exit_code=%d\n' "$command_exit_code"
  printf 'stdout=%s\n' "$stdout_file"
  printf 'stderr=%s\n' "$stderr_file"
} >"$result_file"

resume_prompt=$(cat <<EOF
An asynchronous command has finished.

Command: $command_text
Exit code: $command_exit_code
stdout file: $stdout_file
stderr file: $stderr_file
Result metadata file: $result_file

Read the stdout and stderr files as needed. Do not assume that a non-zero
exit code means the command output is absent.
EOF
)

# Extract a stable error code from codexctl's JSON error document without
# adding a jq dependency. Only NO_ACTIVE_TURN permits the resume fallback.
json_error_code() {
  local response pattern
  response=$(<"$1")
  pattern='"code"[[:space:]]*:[[:space:]]*"([A-Z_]+)"'
  if [[ $response =~ $pattern ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  fi
}

# Keep hook output separate from the wrapped command's output. First try to
# deliver the completion prompt to the active turn.
"$codexctl_bin" steer "$thread_id" --json -- "$resume_prompt" \
  >"$steer_stdout_file" 2>"$steer_stderr_file"
steer_exit_code=$?
steer_error_code=$(json_error_code "$steer_stdout_file")
wake_method=steer
wake_exit_code=$steer_exit_code
resume_exit_code=not_attempted

if [[ $steer_error_code == NO_ACTIVE_TURN ]]; then
  # The original turn has ended, so start one new turn with the same prompt.
  "$codexctl_bin" resume "$thread_id" --detach --json -- "$resume_prompt" \
    >"$resume_stdout_file" 2>"$resume_stderr_file"
  resume_exit_code=$?
  wake_method=resume
  wake_exit_code=$resume_exit_code
fi

{
  printf 'wake_method=%s\n' "$wake_method"
  printf 'wake_exit_code=%s\n' "$wake_exit_code"
  printf 'steer_exit_code=%d\n' "$steer_exit_code"
  printf 'steer_error_code=%s\n' "${steer_error_code:-none}"
  printf 'resume_exit_code=%s\n' "$resume_exit_code"
} >"$wake_result_file"

printf 'job_dir=%s\n' "$job_dir"
printf 'command=%s\n' "$command_text"
printf 'command_exit_code=%d\n' "$command_exit_code"
printf 'wake_method=%s\n' "$wake_method"
printf 'wake_exit_code=%s\n' "$wake_exit_code"
printf 'stdout=%s\n' "$stdout_file"
printf 'stderr=%s\n' "$stderr_file"

# Preserve the wrapped command's exit status. The hook status is recorded
# separately because notification delivery is not the command's result.
exit "$command_exit_code"
