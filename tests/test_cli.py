"""Testy CLI kontraktu: `run --spec` (JSONL, result.json, exit kódy, abort), ruční `run`, `check`, `validate`,
`list`, `abort`.

Běhy s krokem jsou subprocesy nad lokálním executorem (bash, žádný Docker ani kubectl). `check` s busy stavem
používá in-process `main()` s fake runnerem. Čekání na abort jde přes přečtení události `step_started`, ne přes sleep.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentis_workflow import cli
from agentis_workflow.engine import WorkflowEngine
from tests.support import FakeRunner, write_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]

FLOW = """
version: 1
workflow:
  executor: local
  steps:
    - name: Prepare
      run: |
        mkdir -p outputs
        printf 'ready' > outputs/state
        printf '# Report' > outputs/report.md
      outputs:
        - type: var
          name: STATE
          valueFrom: outputs/state
        - type: artifact
          name: report.md
          path: outputs/report.md
    - name: Note
      run: echo verbose-marker
      outputs:
        - type: agent_comment
          bodyFrom: outputs/report.md
    - name: Skipped
      if: STATE == 'nope'
      run: echo never
"""

FAILING = """
version: 1
workflow:
  executor: local
  steps:
    - name: Break
      run: echo broken; exit 3
    - name: Never
      run: echo never
    - name: Cleanup
      always: true
      run: echo "status=$AGENTIS_WORKFLOW_STATUS failed=$AGENTIS_FAILED_STEP"
"""

SLEEPING = """
version: 1
workflow:
  executor: local
  steps:
    - name: Wait
      run: sleep 30
"""

SECRET = """
version: 1
workflow:
  executor: local
  steps:
    - name: Auth
      run: |
        mkdir -p outputs
        if [ -n "$AGENTIS_SERVICE_TOKEN" ]; then printf ok > outputs/auth; fi
      outputs:
        - type: var
          name: AUTH
          valueFrom: outputs/auth
"""

GREET = """
version: 1
workflow:
  executor: local
  env:
    GREETING_NAME: "[%TASK_TITLE%]"
  steps:
    - name: Greet
      run: printf 'Hello, %s, prompt=%s\\n' "$GREETING_NAME" "$(cat "$AGENTIS_PROMPT_FILE")" > greeting.txt
"""

KUBERNETES_FLOW = """
version: 1
workflow:
  executor: kubernetes
  image: registry.example/agent:1
  steps:
    - name: Build
      run: echo build
"""


def _cli_env(**extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT), "LANG": "C.UTF-8"}
    env.update(extra)
    return env


def run_cli(
    *args: str, cwd: Path, stdin: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    stdin_arg: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(
        [sys.executable, "-m", "agentis_workflow.cli", *args],
        cwd=cwd,
        env=env or _cli_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        **stdin_arg,
    )


def write_spec(tmp_path: Path, workdir: Path, **overrides: Any) -> Path:
    data: dict[str, Any] = {
        "protocol": 1,
        "workflow": "flow",
        "search_paths": [".agentis/workflows"],
        "workdir": str(workdir),
        "run_dir": str(tmp_path / "run"),
        "executor": "local",
        "namespace": "wf-test-1",
        "labels": {"agentis.task_id": "task-1", "agentis.run_id": "run-1"},
        "vars": {"TASK_TITLE": "Ahoj"},
    }
    data.update(overrides)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_workdir(tmp_path: Path, text: str = FLOW, name: str = "flow") -> Path:
    workdir = tmp_path / "wt"
    workdir.mkdir(parents=True, exist_ok=True)
    write_workflow(workdir, text, relpath=f".agentis/workflows/{name}.yaml")
    return workdir


def read_events(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


# ----------------------------------------------------------------------
# --version, usage, abort
# ----------------------------------------------------------------------


def test_version_reports_package_and_protocol(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0

    assert capsys.readouterr().out.strip() == "agentis-workflow 0.1.0 (protocol 1)"


def test_unknown_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["nope"]) == cli.EXIT_INVALID


def test_abort_without_running_process_reports_zero_killed(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["abort", "--executor", "local", "--label", "agentis.task_id=task-9"])

    assert code == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "killed 0 process(es)"


def test_abort_kubernetes_requires_namespace(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["abort", "--executor", "kubernetes", "--label", "agentis.task_id=task-9"])

    assert code == cli.EXIT_INVALID
    assert "requires --namespace" in capsys.readouterr().err


# ----------------------------------------------------------------------
# validate / list
# ----------------------------------------------------------------------


def test_validate_reports_steps_and_json_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workdir = make_workdir(tmp_path)
    path = workdir / ".agentis/workflows/flow.yaml"

    assert cli.main(["validate", "--json", str(path)]) == cli.EXIT_OK

    report = json.loads(capsys.readouterr().out)[0]
    assert report["ok"] is True
    assert report["executor"] == "local"
    assert [step["name"] for step in report["steps"]] == ["Prepare", "Note", "Skipped"]
    assert report["steps"][2]["if"] == "STATE == 'nope'"


def test_validate_directory_skips_base_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workdir = make_workdir(tmp_path)
    write_workflow(
        workdir,
        "version: 1\nworkflow:\n  steps:\n    - name: Base\n      run: echo base\n",
        relpath=".agentis/workflows/_base.yaml",
    )

    assert cli.main(["validate", str(workdir / ".agentis/workflows")]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "flow.yaml" in out
    assert "_base.yaml" not in out


def test_validate_invalid_workflow_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workdir = make_workdir(tmp_path, "version: 1\nworkflow:\n  unknownKey: 1\n  steps:\n    - name: a\n      run: a\n")

    code = cli.main(["validate", str(workdir / ".agentis/workflows/flow.yaml")])

    assert code == cli.EXIT_INVALID
    assert capsys.readouterr().out.startswith("FAIL ")


def test_validate_missing_env_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workdir = make_workdir(
        tmp_path, "version: 1\nworkflow:\n  envFiles: [missing.env]\n  steps:\n    - name: a\n      run: a\n"
    )

    code = cli.main(["validate", str(workdir / ".agentis/workflows/flow.yaml")])

    assert code == cli.EXIT_INVALID
    assert "envFiles" in capsys.readouterr().out


def test_validate_missing_path_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["validate", str(tmp_path / "absent.yaml")]) == cli.EXIT_INVALID
    assert "no such file or directory" in capsys.readouterr().err


def test_list_shows_project_workflows_without_base_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workdir = make_workdir(tmp_path)
    write_workflow(
        workdir,
        "version: 1\nworkflow:\n  steps:\n    - name: a\n      run: a\n",
        relpath=".agentis/workflows/_base.yaml",
    )

    assert cli.main(["list", "--workdir", str(workdir)]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "  flow  (" in out
    assert "_base" not in out


# ----------------------------------------------------------------------
# check
# ----------------------------------------------------------------------


def test_check_prints_resolved_plan_without_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workdir = make_workdir(tmp_path)
    spec_path = write_spec(tmp_path, workdir)

    assert cli.main(["check", "--spec", str(spec_path)]) == cli.EXIT_OK

    plan = json.loads(capsys.readouterr().out)
    assert plan["protocol"] == 1
    assert plan["executor"] == "local"
    assert plan["namespace"] == "wf-test-1"
    assert plan["workflow_file"].endswith("flow.yaml")
    assert plan["steps"] == [
        {"name": "Prepare", "needs": [], "image": None},
        {"name": "Note", "needs": ["Prepare"], "image": None},
        {"name": "Skipped", "needs": ["Note"], "image": None},
    ]
    assert not (tmp_path / "run").exists()


def test_check_invalid_workflow_exits_2_with_empty_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workdir = make_workdir(tmp_path, "version: 1\nworkflow:\n  steps: []\n")
    spec_path = write_spec(tmp_path, workdir)

    code = cli.main(["check", "--spec", str(spec_path)])

    captured = capsys.readouterr()
    assert code == cli.EXIT_INVALID
    assert captured.out == ""
    assert "Invalid workflow" in captured.err


def test_check_kubernetes_without_image_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workdir = make_workdir(tmp_path, "version: 1\nworkflow:\n  steps:\n    - name: a\n      run: a\n")
    spec_path = write_spec(tmp_path, workdir, executor="kubernetes")

    code = cli.main(["check", "--spec", str(spec_path)])

    assert code == cli.EXIT_INVALID
    assert "requires 'image'" in capsys.readouterr().err


def test_check_busy_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = make_workdir(tmp_path, KUBERNETES_FLOW)
    spec_path = write_spec(tmp_path, workdir, executor="kubernetes")
    runner = FakeRunner()
    runner.active = True
    monkeypatch.setattr(cli, "_make_engine", lambda settings: WorkflowEngine(settings, runner=runner))

    code = cli.main(["check", "--spec", str(spec_path)])

    captured = capsys.readouterr()
    assert code == cli.EXIT_BUSY
    assert captured.out == ""
    assert "busy" in captured.err


def test_check_ok_for_kubernetes_with_image_and_idle_runner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = make_workdir(tmp_path, KUBERNETES_FLOW)
    spec_path = write_spec(tmp_path, workdir, executor="kubernetes")
    runner = FakeRunner()
    monkeypatch.setattr(cli, "_make_engine", lambda settings: WorkflowEngine(settings, runner=runner))

    assert cli.main(["check", "--spec", str(spec_path)]) == cli.EXIT_OK

    plan = json.loads(capsys.readouterr().out)
    assert plan["steps"] == [{"name": "Build", "needs": [], "image": "registry.example/agent:1"}]


# ----------------------------------------------------------------------
# run --spec
# ----------------------------------------------------------------------


def test_run_spec_emits_jsonl_and_writes_result(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    spec_path = write_spec(tmp_path, workdir)

    proc = run_cli("run", "--spec", str(spec_path), cwd=tmp_path)

    assert proc.returncode == cli.EXIT_OK, proc.stderr
    events = read_events(proc.stdout)
    assert events[0]["type"] == "workflow_started"
    assert events[0]["steps"] == ["Prepare", "Note", "Skipped"]
    assert [event["step"] for event in events if event["type"] == "step_started"] == ["Prepare", "Note"]
    skipped = [event for event in events if event["type"] == "step_skipped"]
    assert [(event["step"], event["reason"]) for event in skipped] == [("Skipped", "condition")]
    assert events[-1] == {"type": "workflow_finished", "status": "success", "failed_step": None}

    result = json.loads((tmp_path / "run" / "result.json").read_text(encoding="utf-8"))
    assert result["protocol"] == 1
    assert result["status"] == "success"
    assert result["vars"] == {"STATE": "ready"}
    assert result["outputs"]["comments"][0]["body"] == "# Report"
    artifact = result["outputs"]["artifacts"][0]
    assert artifact["name"] == "report.md"
    assert artifact["path"].startswith("artifact-staging/")
    assert (tmp_path / "run" / artifact["path"]).is_file()
    assert not (tmp_path / "run" / "result.json.tmp").exists()


def test_run_spec_console_prints_progress_and_summary(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    spec_path = write_spec(tmp_path, workdir)

    proc = run_cli("run", "--spec", str(spec_path), "--events", "console", cwd=tmp_path)

    assert proc.returncode == cli.EXIT_OK, proc.stderr
    assert "▶ Prepare" in proc.stdout
    assert "✓ Prepare" in proc.stdout
    assert "↷ Skipped" in proc.stdout
    assert "✓ Workflow finished" in proc.stdout
    assert "var STATE=ready" in proc.stdout
    assert "verbose-marker" not in proc.stdout


def test_run_spec_console_verbose_prints_full_step_log(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    spec_path = write_spec(tmp_path, workdir)

    proc = run_cli("run", "--spec", str(spec_path), "--events", "console", "--verbose", cwd=tmp_path)

    assert proc.returncode == cli.EXIT_OK, proc.stderr
    assert "verbose-marker" in proc.stdout


def test_run_spec_events_none_prints_nothing_on_stdout(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    spec_path = write_spec(tmp_path, workdir)

    proc = run_cli("run", "--spec", str(spec_path), "--events", "none", cwd=tmp_path)

    assert proc.returncode == cli.EXIT_OK, proc.stderr
    assert proc.stdout == ""
    assert (tmp_path / "run" / "result.json").is_file()


def test_failed_step_exits_1_and_always_step_still_runs(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, FAILING)
    spec_path = write_spec(tmp_path, workdir)

    proc = run_cli("run", "--spec", str(spec_path), cwd=tmp_path)

    assert proc.returncode == cli.EXIT_FAILED, proc.stderr
    events = read_events(proc.stdout)
    failed = [event for event in events if event["type"] == "step_failed"]
    assert failed[0]["step"] == "Break"
    assert "broken" in failed[0]["log_tail"]
    skipped = [event for event in events if event["type"] == "step_skipped"]
    assert [(event["step"], event["reason"]) for event in skipped] == [("Never", "workflow_failed")]
    succeeded = [event["step"] for event in events if event["type"] == "step_succeeded"]
    assert succeeded == ["Cleanup"]
    assert events[-1] == {"type": "workflow_finished", "status": "failed", "failed_step": "Break"}

    result = json.loads((tmp_path / "run" / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["failed_step"] == "Break"
    assert result["outputs"]["comments"] == []


def test_pass_env_secret_reaches_steps_but_not_spec_or_output(tmp_path: Path) -> None:
    secret = "test-service-token-value"
    workdir = make_workdir(tmp_path, SECRET)
    spec_path = write_spec(tmp_path, workdir, pass_env=["AGENTIS_SERVICE_TOKEN"])

    proc = run_cli("run", "--spec", str(spec_path), cwd=tmp_path, env=_cli_env(AGENTIS_SERVICE_TOKEN=secret))

    assert proc.returncode == cli.EXIT_OK, proc.stderr
    result_text = (tmp_path / "run" / "result.json").read_text(encoding="utf-8")
    assert json.loads(result_text)["vars"] == {"AUTH": "ok"}
    for text in (proc.stdout, proc.stderr, result_text, spec_path.read_text(encoding="utf-8")):
        assert secret not in text


def test_invalid_spec_exits_2(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    spec_path = write_spec(tmp_path, workdir, unexpectedKey=True)

    proc = run_cli("run", "--spec", str(spec_path), cwd=tmp_path)

    assert proc.returncode == cli.EXIT_INVALID
    assert proc.stdout == ""
    assert "invalid spec" in proc.stderr


def test_unknown_workflow_in_spec_exits_2(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    spec_path = write_spec(tmp_path, workdir, workflow="missing")

    proc = run_cli("run", "--spec", str(spec_path), cwd=tmp_path)

    assert proc.returncode == cli.EXIT_INVALID
    assert "not found" in proc.stderr


def test_spec_cannot_be_combined_with_manual_flags(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path)
    spec_path = write_spec(tmp_path, workdir)

    proc = run_cli("run", "--spec", str(spec_path), "--var", "TASK_TITLE=x", cwd=tmp_path)

    assert proc.returncode == cli.EXIT_INVALID
    assert "--var" in proc.stderr


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGTERM, cli.EXIT_SIGTERM), (signal.SIGINT, cli.EXIT_SIGINT)])
def test_signal_during_step_aborts_run(tmp_path: Path, sig: signal.Signals, code: int) -> None:
    workdir = make_workdir(tmp_path, SLEEPING)
    spec_path = write_spec(tmp_path, workdir)
    proc = subprocess.Popen(
        [sys.executable, "-m", "agentis_workflow.cli", "run", "--spec", str(spec_path), "--events", "jsonl"],
        cwd=tmp_path,
        env=_cli_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Čeká se na skutečný start kroku (JSONL řádek), ne na pevnou dobu.
        for line in proc.stdout:
            if json.loads(line)["type"] == "step_started":
                break
        else:
            pytest.fail("workflow ended before step_started")
        proc.send_signal(sig)
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == code, stderr
    events = read_events(stdout) if stdout.strip() else []
    assert events[-1] == {"type": "workflow_finished", "status": "aborted", "failed_step": None}
    result = json.loads((tmp_path / "run" / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "aborted"
    assert result["outputs"] == {"comments": [], "session_id": None, "attachments": [], "artifacts": []}


# ----------------------------------------------------------------------
# run (ruční režim)
# ----------------------------------------------------------------------


def test_manual_run_writes_prompt_and_context_and_runs_workflow(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, GREET, name="greet")
    run_dir = tmp_path / "manual-run"

    proc = run_cli(
        "run",
        "greet",
        "Ahoj svete",
        "--workdir",
        str(workdir),
        "--run-dir",
        str(run_dir),
        "--var",
        "TASK_TITLE=Svete",
        "--executor",
        "local",
        cwd=tmp_path,
    )

    assert proc.returncode == cli.EXIT_OK, proc.stderr
    assert "✓ Workflow finished" in proc.stdout
    assert (run_dir / "prompt.md").read_text(encoding="utf-8") == "Ahoj svete"
    assert (run_dir / "context.json").read_text(encoding="utf-8") == "{}\n"
    assert (workdir / "greeting.txt").read_text(encoding="utf-8") == "Hello, Svete, prompt=Ahoj svete\n"
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "success"


def test_manual_run_reads_prompt_from_stdin(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, GREET, name="greet")
    run_dir = tmp_path / "manual-run"

    proc = run_cli(
        "run",
        "greet",
        "--workdir",
        str(workdir),
        "--run-dir",
        str(run_dir),
        "--events",
        "none",
        "--executor",
        "local",
        cwd=tmp_path,
        stdin="Z stdin\n",
    )

    assert proc.returncode == cli.EXIT_OK, proc.stderr
    assert proc.stdout == ""
    assert (run_dir / "prompt.md").read_text(encoding="utf-8") == "Z stdin\n"


def test_manual_run_rejects_prompt_twice(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, GREET, name="greet")
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("x", encoding="utf-8")

    proc = run_cli("run", "greet", "arg", "--prompt-file", str(prompt_file), "--workdir", str(workdir), cwd=tmp_path)

    assert proc.returncode == cli.EXIT_INVALID
    assert "not both" in proc.stderr


def test_manual_run_accepts_workflow_path(tmp_path: Path) -> None:
    workdir = make_workdir(tmp_path, GREET, name="greet")
    run_dir = tmp_path / "manual-run"
    path = workdir / ".agentis/workflows/greet.yaml"

    proc = run_cli(
        "run",
        str(path),
        "--workdir",
        str(workdir),
        "--run-dir",
        str(run_dir),
        "--events",
        "none",
        "--executor",
        "local",
        cwd=tmp_path,
    )

    assert proc.returncode == cli.EXIT_OK, proc.stderr
    assert json.loads((run_dir / "result.json").read_text(encoding="utf-8"))["status"] == "success"
