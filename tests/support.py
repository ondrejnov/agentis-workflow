"""Sdílené fakes a pomocné funkce testů enginu (bez Dockeru, kubectl a sítě)."""

from __future__ import annotations

import textwrap
import threading
from pathlib import Path
from typing import Any

from agentis_workflow.engine import WorkflowEngine
from agentis_workflow.protocol import RunResult, RunSpec
from agentis_workflow.runtime import StepResult
from agentis_workflow.schema import WorkflowFile
from agentis_workflow.settings import EngineSettings

WORKFLOW_RELPATH = ".agentis/workflows/default.yaml"


class FakeRunner:
    """Záznam spuštěných kroků; výsledky lze předepsat per krok (fronta statusů, jinak `succeeded`)."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.prepared: list[str] = []
        self.aborted: list[tuple[str, dict[str, str]]] = []
        self.deleted_namespaces: list[str] = []
        self.results_by_step: dict[str, list[str]] = {}
        self.log_tail = "boom log"
        self.active = False
        self.lock = threading.Lock()

    def prepare(self, workflow: WorkflowFile, *, namespace: str, run_dir: Path) -> None:
        self.prepared.append(namespace)

    def has_active_run(self, namespace: str, task_label: str) -> bool:
        return self.active

    def run_step(
        self,
        workflow: WorkflowFile,
        step: Any,
        *,
        namespace: str,
        name: str,
        labels: dict[str, str],
        env: dict[str, str],
        timeout: float,
        abort_event: threading.Event,
        run_dir: Path,
    ) -> StepResult:
        with self.lock:
            self.steps.append(
                {
                    "step": step.name,
                    "name": name,
                    "namespace": namespace,
                    "labels": dict(labels),
                    "env": dict(env),
                    "timeout": timeout,
                }
            )
            queue = self.results_by_step.get(step.name)
            status = queue.pop(0) if queue else "succeeded"
        return StepResult(status=status, log_tail="" if status == "succeeded" else self.log_tail)

    def abort(self, namespace: str, labels: dict[str, str]) -> str:
        self.aborted.append((namespace, dict(labels)))
        return "deleted"

    def delete_namespace(self, namespace: str) -> None:
        self.deleted_namespaces.append(namespace)

    def step_names(self) -> list[str]:
        return [record["step"] for record in self.steps]


class BlockingRunner(FakeRunner):
    """Každý krok blokuje, dokud engine nenastaví abort event; pak vrací `aborted`."""

    def run_step(self, workflow: Any, step: Any, **kwargs: Any) -> StepResult:
        with self.lock:
            self.steps.append({"step": step.name, "labels": dict(kwargs["labels"]), "env": dict(kwargs["env"])})
        assert kwargs["abort_event"].wait(timeout=5)
        return StepResult(status="aborted")


def write_workflow(workdir: Path, text: str, relpath: str = WORKFLOW_RELPATH) -> Path:
    path = workdir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def make_spec(tmp_path: Path, **overrides: Any) -> RunSpec:
    workdir = tmp_path / "wt"
    workdir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "protocol": 1,
        "workflow": "default",
        "search_paths": [".agentis/workflows"],
        "workdir": str(workdir),
        "run_dir": str(tmp_path / "run"),
        "namespace": "task-77-test",
        "labels": {"agentis.task_id": "task-77", "agentis.run_id": "run-12345678"},
        "executor": "local",
    }
    data.update(overrides)
    return RunSpec.model_validate(data)


def run_workflow(
    tmp_path: Path,
    text: str,
    *,
    runner: FakeRunner | None = None,
    environ: dict[str, str] | None = None,
    **spec_overrides: Any,
) -> tuple[RunResult, list[dict[str, Any]], FakeRunner]:
    """Připraví workflow v `tmp_path/wt`, spustí ho synchronně a vrátí výsledek, události a runner."""

    fake = runner or FakeRunner()
    spec = make_spec(tmp_path, **spec_overrides)
    write_workflow(Path(spec.workdir), text)
    engine = WorkflowEngine(EngineSettings(), runner=fake, environ=environ or {})
    prepared = engine.prepare(spec)
    events: list[dict[str, Any]] = []
    result = engine.run(prepared, events.append)
    return result, events, fake


def event_types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]
