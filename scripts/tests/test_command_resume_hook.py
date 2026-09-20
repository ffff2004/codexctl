"""Exercise the wrapper through its CLI, replacing only the external CLI."""

import os
import subprocess
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[2]
    / "examples/command-resume-hook/command-resume-hook.sh"
)


@pytest.fixture
def hook(tmp_path):
    cli = tmp_path / "codexctl"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        'doctor) exit "${DOCTOR_EXIT:-0}" ;;\n'
        "steer)\n"
        '  test -n "$(find "$CODEXCTL_JOB_DIR" -name result.txt -print)" || exit 99\n'
        '  printf \'{"code":"%s"}\\n\' "${STEER_CODE:-none}"\n'
        '  exit "${STEER_EXIT:-0}" ;;\n'
        'resume) printf "resumed\\n" ;;\n'
        "esac\n"
    )
    cli.chmod(0o755)
    env = {
        **os.environ,
        "CODEX_THREAD_ID": "test-thread",
        "CODEXCTL_JOB_DIR": str(tmp_path / "jobs with spaces"),
        "CODEXCTL_BIN": str(cli),
    }

    def run(*args, **overrides):
        return subprocess.run(
            ["bash", str(WRAPPER), *args],
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=10,
        )

    return run, Path(env["CODEXCTL_JOB_DIR"]) / "test-thread"


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("notify", ["success", "failure", "fallback"])
def test_result_survives_notification_and_session_loss(hook, exit_code, notify):
    run, directory = hook
    overrides = {}
    if notify != "success":
        overrides = {
            "STEER_EXIT": "1",
            "STEER_CODE": "NO_ACTIVE_TURN" if notify == "fallback" else "OTHER",
        }
    result = run(
        "--",
        "bash",
        "-c",
        f"printf out; printf err >&2; exit {exit_code}",
        **overrides,
    )
    assert result.returncode == exit_code
    job = next(directory.iterdir())
    assert (job / "stdout.log").read_text() == "out"
    assert (job / "stderr.log").read_text() == "err"
    assert f"command_exit_code={exit_code}" in (job / "result.txt").read_text()
    wake = (job / "wake.result.txt").read_text()
    assert "steer_exit_code=99" not in wake
    assert f"wake_method={'resume' if notify == 'fallback' else 'steer'}" in wake
    assert (job / "resume.stdout.log").read_text() == (
        "resumed\n" if notify == "fallback" else ""
    )
    recovered = run("--job", job.name, CODEXCTL_BIN="/nonexistent")
    assert recovered.returncode == 0
    assert f"command_exit_code={exit_code}" in recovered.stdout


def test_multiple_jobs_and_unknown_status(hook):
    run, directory = hook
    for _ in range(2):
        assert run("--", "true").returncode == 0
    jobs = list(directory.iterdir())
    assert len(jobs) == 2
    (jobs[0] / "result.txt").unlink()
    listing = run("--list", CODEXCTL_BIN="/nonexistent")
    assert listing.returncode == 0
    assert "status=unknown" in listing.stdout
    assert "status=completed" in listing.stdout
    assert all(job.name in listing.stdout for job in jobs)


def test_doctor_failure_prevents_execution(hook):
    run, directory = hook
    assert run("--", "true", DOCTOR_EXIT="9").returncode == 9
    assert not directory.exists()


def test_query_validation_and_thread_isolation(hook):
    run, _ = hook
    assert run("--", "true").returncode == 0
    assert run("--thread-id", "other-thread", "--list").stdout == ""
    assert run("--job", "job.missing").returncode == 2
    assert run("--job", "../outside").returncode == 2
    assert run("--thread-id", "../outside", "--list").returncode == 2
    assert run("--list", "--", "true").returncode == 2
