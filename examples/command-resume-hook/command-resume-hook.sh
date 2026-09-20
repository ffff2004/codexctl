#!/usr/bin/env bash
#
# Run a command synchronously, persist its output, and notify the current
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
  command-resume-hook.sh [--thread-id ID] [--output-dir DIR] --list
  command-resume-hook.sh [--thread-id ID] [--output-dir DIR] --job JOB_ID

The thread ID defaults to CODEX_THREAD_ID. Command stdout and stderr are
stored separately in a unique job directory below DIR. DIR defaults to
${TMPDIR:-/tmp}/codexctl-jobs. Jobs are grouped by thread ID.
Queries need no running codexctl service. Missing results mean unknown status,
not proof that a command is still running. Query exit codes describe the query;
command_exit_code in result.txt describes the command. Values printed with
shell escaping are for display; never source job files.
EOF
}

die() {
  printf 'command-resume-hook: %s\n' "$1" >&2
  exit 2
}

thread_id=${CODEX_THREAD_ID-}
output_root=${CODEXCTL_JOB_DIR:-${TMPDIR:-/tmp}/codexctl-jobs}
codexctl_bin=${CODEXCTL_BIN:-codexctl}
query_mode=
job_id=

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
    --list)
      [[ -z $query_mode ]] || die "choose only one query"
      query_mode=list
      shift
      ;;
    --job)
      (($# >= 2)) || die "--job requires a value"
      [[ -z $query_mode ]] || die "choose only one query"
      query_mode=job
      job_id=$2
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
[[ $thread_id =~ ^[a-zA-Z0-9_-]+$ ]] || die "invalid thread ID"
thread_dir=$output_root/$thread_id

show_job() {
  local directory=$1
  printf 'job_id=%s\njob_dir=%q\n' "${directory##*/}" "$directory"
  if [[ -f $directory/result.txt ]]; then
    cat -- "$directory/result.txt" || return
  else
    printf 'status=unknown\n'
  fi
  if [[ -f $directory/job.txt ]]; then
    cat -- "$directory/job.txt" || return
  fi
  printf 'stdout=%q\nstderr=%q\n\n' "$directory/stdout.log" "$directory/stderr.log"
}

if [[ -n $query_mode ]]; then
  (($# == 0)) || die "queries do not accept a command"
  if [[ $query_mode == job ]]; then
    [[ $job_id =~ ^job\.[a-zA-Z0-9]+$ ]] || die "invalid job ID"
    [[ -d $thread_dir/$job_id ]] || die "job not found: $job_id"
    show_job "$thread_dir/$job_id"
    exit $?
  fi
  shopt -s nullglob
  for directory in "$thread_dir"/job.*; do
    [[ -d $directory ]] || continue
    show_job "$directory" || exit 2
  done
  exit 0
fi

(($# > 0)) || die "a command is required after --"
umask 077

doctor_output_file=$(mktemp "${TMPDIR:-/tmp}/codexctl-doctor.XXXXXX") \
  || die "cannot create doctor output file"

cleanup_doctor_output() {
  rm -f -- "$doctor_output_file"
}
trap cleanup_doctor_output EXIT

if "$codexctl_bin" doctor >"$doctor_output_file" 2>&1; then
  cleanup_doctor_output
  trap - EXIT
else
  doctor_exit_code=$?
  cat "$doctor_output_file"
  exit "$doctor_exit_code"
fi

if ! mkdir -p -- "$thread_dir"; then
  die "cannot create output directory: $thread_dir"
fi

thread_dir=$(cd -- "$thread_dir" && pwd -P) || die "cannot resolve output directory"
job_dir=$(mktemp -d "$thread_dir/job.XXXXXX") || die "cannot create job directory"
stdout_file=$job_dir/stdout.log
stderr_file=$job_dir/stderr.log
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

{
  printf 'thread_id=%s\n' "$thread_id"
  printf 'command=%s\n' "$command_text"
  printf 'started_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$job_dir/job.txt.tmp" && mv -- "$job_dir/job.txt.tmp" "$job_dir/job.txt" \
  || die "cannot register job"
: >"$stdout_file" && : >"$stderr_file" || die "cannot create command logs"
printf 'job_id=%s\njob_dir=%q\n' "${job_dir##*/}" "$job_dir"
printf 'stdout=%q\nstderr=%q\n' "$stdout_file" "$stderr_file"

# Capture bash's `time` output separately so it does not pollute the command's
# stderr artifact.
TIMEFORMAT='%R'
if command_wall_clock_time=$( { time "$@" >"$stdout_file" 2>"$stderr_file"; } 2>&1); then
  command_exit_code=0
else
  command_exit_code=$?
fi

# Publish the command outcome before attempting any notification. A missing
# result is deliberately unknown: the wrapper may have been killed.
{
  printf 'status=completed\n'
  printf 'command_exit_code=%d\n' "$command_exit_code"
  printf 'wall_clock_seconds=%s\n' "$command_wall_clock_time"
  printf 'finished_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'stdout=%q\nstderr=%q\n' "$stdout_file" "$stderr_file"
} >"$job_dir/result.txt.tmp" && mv -- "$job_dir/result.txt.tmp" "$job_dir/result.txt" \
  || die "cannot persist command result in $job_dir"

resume_prompt=$(cat <<EOF
An asynchronous command has finished

Job ID: ${job_dir##*/}
Result file: $job_dir/result.txt
Command: $command_text
Exit code: $command_exit_code
Wall clock time: ${command_wall_clock_time}s
stdout file: $stdout_file
stderr file: $stderr_file
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
