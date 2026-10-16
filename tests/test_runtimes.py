"""Testy runtimů: lokální procesy (skutečný bash), Kubernetes Job manifest a kubectl/docker (fake subprocess)."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from agentis_workflow import runtime as runtime_module
from agentis_workflow.docker_runtime import DockerContainerRunner
from agentis_workflow.local_runtime import LocalProcessRunner
from agentis_workflow.runtime import (
    WORKFLOW_LABEL,
    KubectlJobRunner,
    build_bash_wrapper,
    build_job_manifest,
    job_name,
    safe_step_name,
)
from agentis_workflow.schema import WorkflowFile, load_workflow_file
from agentis_workflow.settings import EngineSettings
from tests.support import write_workflow

WORKFLOW = """
version: 1
workflow:
  image: registry.example/agent:1.0
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 120
  env:
    HOME: /root
  mounts:
    - name: www
      mountPath: /var/www
      hostPath:
        path: /var/www
  steps:
    - name: Run agent
      run: agentiscode
    - name: Create pull request
      image: registry.example/other:2.0
      timeoutSeconds: 30
      run: echo hotovo
"""


def _load(tmp_path: Path, text: str = WORKFLOW) -> WorkflowFile:
    path = write_workflow(tmp_path, text, relpath="wf.yaml")
    return load_workflow_file(path, {"WORKDIR": str(tmp_path)})


def _labels() -> dict[str, str]:
    return {WORKFLOW_LABEL: "true", "agentis.task_id": "task-77", "agentis.run_id": "run-12345678"}


# ---------------------------------------------------------------------------
# lokální executor (skutečný bash, žádný Docker ani kubectl)
# ---------------------------------------------------------------------------


def _local_step(tmp_path: Path, script: str, *, timeout_seconds: int = 30) -> tuple[WorkflowFile, Any]:
    text = f"""
    version: 1
    workflow:
      executor: local
      timeoutSeconds: {timeout_seconds}
      steps:
        - name: step
          run: |
            {script}
    """
    workflow = _load(tmp_path, text)
    return workflow, workflow.workflow.steps[0]


def test_local_runner_runs_step_with_env_and_writes_log(tmp_path: Path) -> None:
    workflow, step = _local_step(tmp_path, 'printf "%s" "$GREETING" > out.txt; echo log-line')
    runner = LocalProcessRunner(EngineSettings())
    run_dir = tmp_path / "run"

    result = runner.run_step(
        workflow,
        step,
        namespace="ns",
        name="wf-step",
        labels=_labels(),
        env={"GREETING": "ahoj", "WORKDIR": str(tmp_path)},
        timeout=30,
        abort_event=threading.Event(),
        run_dir=run_dir,
    )

    assert result.status == "succeeded"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "ahoj"
    assert "log-line" in (run_dir / "logs" / "wf-step.log").read_text(encoding="utf-8")


def test_local_runner_failed_step_returns_log_tail(tmp_path: Path) -> None:
    workflow, step = _local_step(tmp_path, "echo chyba-v-logu >&2; exit 3")
    result = LocalProcessRunner(EngineSettings()).run_step(
        workflow,
        step,
        namespace="ns",
        name="wf-fail",
        labels=_labels(),
        env={"WORKDIR": str(tmp_path)},
        timeout=30,
        abort_event=threading.Event(),
        run_dir=tmp_path / "run",
    )

    assert result.status == "failed"
    assert "chyba-v-logu" in result.log_tail


def test_local_runner_timeout_kills_step(tmp_path: Path) -> None:
    workflow, step = _local_step(tmp_path, "sleep 30")
    result = LocalProcessRunner(EngineSettings()).run_step(
        workflow,
        step,
        namespace="ns",
        name="wf-slow",
        labels=_labels(),
        env={"WORKDIR": str(tmp_path)},
        timeout=0.3,
        abort_event=threading.Event(),
        run_dir=tmp_path / "run",
    )

    assert result.status == "timeout"


def test_local_runner_abort_event_kills_running_process(tmp_path: Path) -> None:
    workflow, step = _local_step(tmp_path, "sleep 30")
    abort_event = threading.Event()
    runner = LocalProcessRunner(EngineSettings())
    results: list[Any] = []
    worker = threading.Thread(
        target=lambda: results.append(
            runner.run_step(
                workflow,
                step,
                namespace="ns",
                name="wf-abort",
                labels=_labels(),
                env={"WORKDIR": str(tmp_path)},
                timeout=30,
                abort_event=abort_event,
                run_dir=tmp_path / "run",
            )
        )
    )
    worker.start()
    abort_event.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert results[0].status == "aborted"


def test_local_runner_missing_bash_reports_spawn_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentis_workflow.local_runtime._resolve_bash", lambda: None)
    workflow, step = _local_step(tmp_path, "echo x")
    result = LocalProcessRunner(EngineSettings()).run_step(
        workflow,
        step,
        namespace="ns",
        name="wf-nobash",
        labels=_labels(),
        env={"WORKDIR": str(tmp_path)},
        timeout=30,
        abort_event=threading.Event(),
        run_dir=tmp_path / "run",
    )

    assert result.status == "failed"
    assert "spawn failed" in result.log_tail


# ---------------------------------------------------------------------------
# Kubernetes: manifest a kubectl volání (fake subprocess)
# ---------------------------------------------------------------------------


def test_job_manifest_uses_step_overrides_and_labels(tmp_path: Path) -> None:
    workflow = _load(tmp_path)
    step = workflow.workflow.steps[1]
    name = job_name("run-12345678", "abcd1234", 1, step.name)
    labels = {**_labels(), "agentis.step_index": "1", "agentis.step": safe_step_name(step.name)}

    manifest = build_job_manifest(
        workflow, step, namespace="task-77-test", name=name, labels=labels, env={"AGENTIS_RUN_ID": "run-12345678"}
    )

    assert name.startswith("wf-run12345-abcd1234-1-")
    assert manifest["metadata"]["labels"][WORKFLOW_LABEL] == "true"
    assert manifest["spec"]["activeDeadlineSeconds"] == 30
    pod = manifest["spec"]["template"]["spec"]
    assert pod["volumes"][0]["name"] == "www"
    container = pod["containers"][0]
    assert container["image"] == "registry.example/other:2.0"
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["AGENTIS_RUN_ID"] == "run-12345678"
    assert env["HOME"] == "/root"


def test_bash_wrapper_sets_pipefail_and_workdir() -> None:
    wrapper = build_bash_wrapper("echo ahoj", workdir="/work")
    assert wrapper.splitlines()[:2] == ["set -euo pipefail", 'cd "/work"']
    assert build_bash_wrapper("echo").splitlines()[1] == 'cd "$WORKDIR"'


class _KubectlRecorder:
    def __init__(self, stdout: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self.stdout = stdout

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"args": args, "input": kwargs.get("input")})
        return subprocess.CompletedProcess(args, 0, stdout=self.stdout, stderr="")


def test_kubectl_runner_uses_configured_command_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _KubectlRecorder(stdout='{"items": []}')
    monkeypatch.setattr(runtime_module.subprocess, "run", recorder)
    runner = KubectlJobRunner(EngineSettings(kubectl_command="/opt/kubectl"), kube_context="microk8s")

    assert runner.has_active_run("task-77-test", "task-77") is False

    assert recorder.calls[0]["args"][:3] == ["/opt/kubectl", "--context", "microk8s"]
    assert recorder.calls[0]["args"][3] == "get"


def test_kubectl_has_active_run_detects_active_job(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"items": [{"status": {"active": 1}}]})
    monkeypatch.setattr(runtime_module.subprocess, "run", _KubectlRecorder(stdout=payload))

    assert KubectlJobRunner(EngineSettings()).has_active_run("ns", "task-77") is True


def test_kubectl_abort_deletes_jobs_and_pods_by_label_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _KubectlRecorder()
    monkeypatch.setattr(runtime_module.subprocess, "run", recorder)

    KubectlJobRunner(EngineSettings()).abort("task-77-test", {"agentis.task_id": "task-77", "agentis.run_id": "r1"})

    commands = [call["args"] for call in recorder.calls]
    assert any(
        args[:2] == ["kubectl", "delete"] and "job" in args and "agentis.task_id=task-77,agentis.run_id=r1" in args
        for args in commands
    )
    pod_delete = next(args for args in commands if "pod" in args)
    assert "--force" in pod_delete and "--grace-period=0" in pod_delete


def test_kubectl_delete_namespace_does_not_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _KubectlRecorder()
    monkeypatch.setattr(runtime_module.subprocess, "run", recorder)

    KubectlJobRunner(EngineSettings()).delete_namespace("task-77-test")

    assert recorder.calls[0]["args"] == [
        "kubectl",
        "delete",
        "namespace",
        "task-77-test",
        "--ignore-not-found",
        "--wait=false",
    ]


# ---------------------------------------------------------------------------
# Docker: argv bez hodnot secretů
# ---------------------------------------------------------------------------


def test_docker_command_passes_secrets_by_name_only(tmp_path: Path) -> None:
    workflow = _load(tmp_path)
    step = workflow.workflow.steps[0]
    runner = DockerContainerRunner(EngineSettings(docker_command="/opt/docker"))

    command = runner._container_command(
        workflow,
        step,
        name="wf-step",
        labels=_labels(),
        env={"AGENTIS_SERVICE_TOKEN": "s3cr3t", "WORKDIR": str(tmp_path)},
        image="registry.example/agent:1.0",
    )

    assert command[:2] == ["/opt/docker", "run"]
    assert "s3cr3t" not in " ".join(command)
    assert "AGENTIS_SERVICE_TOKEN" in command
    assert "agentis.workflow=true" in command
    assert command[command.index("--entrypoint") + 1] == "/bin/bash"
    assert command[-3] == "registry.example/agent:1.0"
    assert command[-2] == "-lc"
    assert command[-1] == build_bash_wrapper("agentiscode", workdir=str(tmp_path), workdir_env="WORKDIR")


def test_docker_runner_rejects_host_mount_outside_contract(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      image: img:1
      mounts:
        - name: bad
          mountPath: relative/path
          hostPath:
            path: /tmp
      steps:
        - name: a
          run: a
    """
    workflow = _load(tmp_path, text)
    with pytest.raises(ValueError, match="mountPath must be absolute"):
        DockerContainerRunner(EngineSettings())._bind_mounts(workflow, {})
