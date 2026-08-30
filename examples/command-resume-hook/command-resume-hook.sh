#!/usr/bin/env bash
#
# Run a command in the background, persist its output, and start one new
# Codex turn when it finishes.
#
# The wrapper deliberately makes one resume attempt. It does not poll or
# retry: the command result remains available even when the resume attempt
# fails.

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
resume_result_file=$job_dir/resume.result.txt

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

# Keep hook output separate from the wrapped command's output. This is a
# single best-effort notification; the result files are authoritative.
"$codexctl_bin" resume "$thread_id" --detach --json -- "$resume_prompt" \
  >"$resume_stdout_file" 2>"$resume_stderr_file"
resume_exit_code=$?

printf 'resume_exit_code=%d\n' "$resume_exit_code" >"$resume_result_file"

printf 'job_dir=%s\n' "$job_dir"
printf 'command=%s\n' "$command_text"
printf 'command_exit_code=%d\n' "$command_exit_code"
printf 'resume_exit_code=%d\n' "$resume_exit_code"
printf 'stdout=%s\n' "$stdout_file"
printf 'stderr=%s\n' "$stderr_file"

# Preserve the wrapped command's exit status. The hook status is recorded
# separately because notification delivery is not the command's result.
exit "$command_exit_code"
