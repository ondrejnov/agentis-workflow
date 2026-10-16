"""Engine jednoho workflow runu: výběr a zmrazení YAML, scheduler kroků, outputs a `result.json`.

Engine nezná Agentis, WebSocket, tasky ani git. Volající připraví `RunSpec` (spec.json), zavolá
`prepare()` (synchronně, vyhodí chybu dřív než start) a `run()` (blokuje, průběh posílá do
`on_event`). Prompt a kontext do run adresáře zapisuje volající; engine nic neposílá do sítě.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from agentis_workflow.docker_runtime import DockerContainerRunner
from agentis_workflow.local_runtime import LocalProcessRunner
from agentis_workflow.protocol import (
    PROTOCOL_VERSION,
    RunArtifact,
    RunAttachment,
    RunComment,
    RunFollowup,
    RunOutputs,
    RunResult,
    RunSpec,
    RunWarning,
)
from agentis_workflow.runtime import (
    WORKFLOW_LABEL,
    KubectlJobRunner,
    StepResult,
    WorkflowStepRunner,
    job_name,
    safe_step_name,
)
from agentis_workflow.schema import (
    INTERPOLATION_ALLOWLIST,
    WORKFLOW_EXECUTORS,
    WORKFLOW_NAME_RE,
    WorkflowFile,
    WorkflowOutput,
    WorkflowStep,
    evaluate_condition,
    load_workflow_file,
)
from agentis_workflow.settings import EngineSettings

EventCallback = Callable[[dict[str, Any]], None]

ARTIFACT_OUTPUT_MAX_FILES = 50
ARTIFACT_OUTPUT_MAX_BYTES = 50 * 1024 * 1024
ARTIFACT_OUTPUT_STAGING_DIR = "artifact-staging"
ARTIFACT_NAME_MAX_LENGTH = 255
RESULT_FILE_NAME = "result.json"

_CLEANUP_NAMESPACE_EXECUTOR = "kubernetes"


class WorkflowEngineError(RuntimeError):
    pass


class WorkflowSpecError(WorkflowEngineError):
    """Nevalidní spec, workflow nebo chybějící soubor/image (CLI exit 2)."""


class WorkflowBusyError(WorkflowEngineError):
    """Pro stejné labely už běží aktivní joby/kontejnery (CLI exit 3)."""


@dataclass(frozen=True)
class _StagedArtifact:
    step_index: int
    output_index: int
    match_index: int
    name: str
    filename: str
    staged_path: Path
    size: int


@dataclass(frozen=True)
class _StepExecutionResult:
    index: int
    step_name: str
    job_name: str
    result: StepResult
    attempts: int


@dataclass(frozen=True)
class PreparedRun:
    """Vše, co `prepare()` ověřilo a připravilo; `run()` už jen spouští kroky."""

    spec: RunSpec
    workflow_file: Path
    workflow: WorkflowFile
    executor: str
    namespace: str
    attempt: str
    workdir: Path
    run_dir: Path
    output_root: Path
    labels: dict[str, str]
    interpolation: dict[str, str]
    #: Env pro kroky (bez labelů/attempt); obsahuje hodnoty z `pass_env`, proto se neukazuje v repr.
    base_env: dict[str, str] = field(repr=False)
    runner: WorkflowStepRunner = field(repr=False)
    abort_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def run_key(self) -> str:
        return self.labels.get("agentis.run_id", "run")

    def summary(self) -> dict[str, Any]:
        """Výstup `agentis-workflow check`: co by se spustilo, bez spuštění."""

        steps = self.workflow.workflow.steps
        dependencies = _step_dependencies(steps)
        return {
            "protocol": PROTOCOL_VERSION,
            "workflow_file": str(self.workflow_file),
            "executor": self.executor,
            "namespace": self.namespace,
            "steps": [
                {
                    "name": step.name,
                    "needs": _dependency_names(index, steps, dependencies),
                    "image": None if self.executor == "local" else (step.image or self.workflow.workflow.image),
                }
                for index, step in enumerate(steps)
            ],
        }


@dataclass
class _RunState:
    """Průběžný stav jednoho `run()`; mění se jen z hlavního vlákna schedulerů."""

    prepared: PreparedRun
    emit: EventCallback
    vars: dict[str, str] = field(default_factory=dict)
    skipped_steps: set[int] = field(default_factory=set)
    failed_steps: set[int] = field(default_factory=set)
    staged_artifacts: list[_StagedArtifact] = field(default_factory=list)
    staged_bytes: int = 0
    warnings: list[RunWarning] = field(default_factory=list)

    def warn(self, message: str, **data: Any) -> None:
        self.warnings.append(RunWarning(message=message, data=data))
        self.emit({"type": "warning", "message": message, "data": data})


class WorkflowEngine:
    """Spouští workflow podle `RunSpec`; jedna instance = jeden run (`prepare` → `run`)."""

    def __init__(
        self,
        settings: EngineSettings,
        *,
        runner: WorkflowStepRunner | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        #: Explicitní runner (testy) má přednost před výběrem podle executoru.
        self._runner_override = runner
        #: Zdroj `pass_env` hodnot; None = `os.environ` v okamžiku `prepare()`.
        self._environ = environ
        self._runners: dict[tuple[str, str | None], WorkflowStepRunner] = {}
        self._lock = threading.Lock()
        self._active: PreparedRun | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prepare(self, spec: RunSpec) -> PreparedRun:
        """Najde a zmrazí workflow, vyřeší executor a ověří images a busy stav. Nic nespouští."""

        environ = os.environ if self._environ is None else self._environ
        workdir = Path(spec.workdir)
        if not workdir.is_dir():
            raise WorkflowSpecError(f"workdir does not exist or is not a directory: {workdir}")
        workflow_file = _find_workflow_file(spec, workdir)
        run_dir = _resolve(spec.run_dir, workdir)
        output_root = _resolve(spec.output_root, workdir) if spec.output_root else workdir
        attempt = f"{time.time_ns() // 1_000_000:011x}"
        interpolation = _interpolation_values(spec, workdir=workdir, run_dir=run_dir)

        workflow = _inject_env_files(_load(workflow_file, interpolation), workdir)
        _require_vars(workflow, spec.require_vars, workflow_file)
        executor = (
            spec.executor or workflow.workflow.executor or spec.executor_default or self.settings.workflow_executor
        )
        runner = self._runner_for(executor, workflow.workflow.context)
        if executor in {"kubernetes", "docker"}:
            _require_images(workflow, workflow_file, executor)

        task_label = spec.labels.get("agentis.task_id", "")
        if runner.has_active_run(spec.namespace, task_label):
            raise WorkflowBusyError(
                f"Workflow jobs for {task_label or 'this run'} are still active in {spec.namespace}"
            )

        prepared = PreparedRun(
            spec=spec,
            workflow_file=workflow_file,
            workflow=workflow,
            executor=executor,
            namespace=spec.namespace,
            attempt=attempt,
            workdir=workdir,
            run_dir=run_dir,
            output_root=output_root,
            labels=dict(spec.labels),
            interpolation=interpolation,
            base_env=_base_env(spec, environ, interpolation, run_dir=run_dir),
            runner=runner,
        )
        with self._lock:
            self._active = prepared
        return prepared

    def run(self, prepared: PreparedRun, on_event: EventCallback | None = None) -> RunResult:
        """Spustí workflow a blokuje do konce. Výsledek zapíše do `result.json` v run dir."""

        prepared.run_dir.mkdir(parents=True, exist_ok=True)
        state = _RunState(prepared=prepared, emit=on_event or (lambda event: None))
        status = "success"
        failed_step: str | None = None
        try:
            prepared.runner.prepare(prepared.workflow, namespace=prepared.namespace, run_dir=prepared.run_dir)
            state.emit(
                {
                    "type": "workflow_started",
                    "attempt": prepared.attempt,
                    "namespace": prepared.namespace,
                    "executor": prepared.executor,
                    "steps": [step.name for step in prepared.workflow.workflow.steps],
                }
            )
            failed_step = self._schedule(state)
            if prepared.abort_event.is_set():
                status = "aborted"
            elif failed_step is not None:
                status = "failed"
            else:
                status = "success"
                self._cleanup_namespace(state)
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            state.warn("Workflow run crashed.", error=str(exc))
            sys.stderr.write(f"[workflow] run crashed: {exc!r}\n")
        finally:
            with self._lock:
                self._active = None

        outputs = RunOutputs()
        followups: list[RunFollowup] = []
        if status != "aborted":
            outputs = self._collect_outputs(state)
        if status == "success":
            followups = self._followups(state)

        result = RunResult(
            status=status,
            failed_step=failed_step,
            vars=dict(state.vars),
            outputs=outputs,
            followups=followups,
            warnings=state.warnings,
        )
        state.emit({"type": "workflow_finished", "status": status, "failed_step": failed_step})
        _write_result(prepared.run_dir, result)
        return result

    def abort(self) -> str | None:
        """Thread-safe: nastaví abort event a zruší běžící kroky přes runner. Vrací výsledek úklidu."""

        with self._lock:
            prepared = self._active
        if prepared is None:
            return None
        prepared.abort_event.set()
        if not prepared.labels:
            return "no labels; running steps were stopped by the abort event"
        try:
            return prepared.runner.abort(prepared.namespace, prepared.labels)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[workflow] abort cleanup failed: {exc!r}\n")
            return f"abort cleanup failed: {exc}"

    # ------------------------------------------------------------------
    # Runner selection
    # ------------------------------------------------------------------

    def _runner_for(self, executor: str, kube_context: str | None) -> WorkflowStepRunner:
        if self._runner_override is not None:
            return self._runner_override
        key = (executor, kube_context if executor == "kubernetes" else None)
        runner = self._runners.get(key)
        if runner is None:
            runner = _make_runner(executor, self.settings, kube_context)
            self._runners[key] = runner
        return runner

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def _schedule(self, state: _RunState) -> str | None:
        """Spouští kroky podle závislostí a `maxParallel`; vrací jméno prvního fatálně selhaného kroku."""

        prepared = state.prepared
        steps = prepared.workflow.workflow.steps
        dependencies = _step_dependencies(steps)
        step_vars: dict[int, dict[str, str]] = {}
        terminal_steps: set[int] = set()
        pending_steps: set[int] = set(range(len(steps)))
        running_steps: dict[Future[_StepExecutionResult], int] = {}
        started_at: dict[int, float] = {}
        #: Jméno prvního fatálně selhaného kroku; po selhání běží už jen `always` kroky.
        failed_step: str | None = None
        max_parallel = prepared.workflow.workflow.maxParallel
        abort_event = prepared.abort_event

        with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="workflow-step") as executor:
            while pending_steps or running_steps:
                if abort_event.is_set():
                    pending_steps.clear()
                    if not running_steps:
                        break

                progressed = False
                if failed_step is not None:
                    for index in sorted(pending_steps):
                        step = steps[index]
                        if step.always:
                            continue
                        pending_steps.remove(index)
                        terminal_steps.add(index)
                        state.skipped_steps.add(index)
                        self._emit_step_skipped(state, index, step, dependencies, failed_step=failed_step)
                        progressed = True

                active_regular_steps = any(not steps[index].always for index in running_steps.values())
                for index in sorted(pending_steps):
                    if len(running_steps) >= max_parallel:
                        break
                    step = steps[index]
                    if failed_step is not None and not step.always:
                        continue
                    if failed_step is not None and step.always and active_regular_steps:
                        continue
                    if any(dependency not in terminal_steps for dependency in dependencies[index]):
                        continue

                    dependency_vars = _dependency_vars(index, dependencies, step_vars)
                    condition_vars = _condition_vars(
                        prepared, env=prepared.base_env, step_env=step.env, visible_vars=dependency_vars
                    )
                    if step.if_ is not None and not evaluate_condition(step.if_, condition_vars):
                        pending_steps.remove(index)
                        terminal_steps.add(index)
                        state.skipped_steps.add(index)
                        self._emit_step_skipped(
                            state,
                            index,
                            step,
                            dependencies,
                            condition=step.if_,
                            visible_vars=dependency_vars,
                        )
                        progressed = True
                        continue

                    self._emit_step_started(state, index, step, dependencies)
                    step_env = {**prepared.base_env, **dependency_vars}
                    if step.always:
                        step_env["AGENTIS_WORKFLOW_STATUS"] = "failed" if failed_step else "success"
                        step_env["AGENTIS_FAILED_STEP"] = failed_step or ""
                    started_at[index] = time.monotonic()
                    future = executor.submit(self._run_step_with_retries, state, index, step, step_env)
                    running_steps[future] = index
                    pending_steps.remove(index)
                    progressed = True

                if not running_steps:
                    if pending_steps and not progressed:
                        raise RuntimeError("workflow scheduler deadlocked; no runnable pending steps")
                    continue

                if progressed and len(running_steps) < max_parallel and pending_steps:
                    continue

                done, _pending = wait(running_steps, return_when=FIRST_COMPLETED)
                for future in done:
                    index = running_steps.pop(future)
                    terminal_steps.add(index)
                    step = steps[index]
                    duration_ms = int((time.monotonic() - started_at.pop(index)) * 1000)
                    try:
                        completed = future.result()
                    except Exception as exc:  # noqa: BLE001
                        completed = _StepExecutionResult(
                            index=index,
                            step_name=step.name,
                            job_name=_job_name(prepared, index, step),
                            result=StepResult(status="failed", log_tail=str(exc)),
                            attempts=1,
                        )
                    if completed.result.status == "aborted":
                        abort_event.set()
                        pending_steps.clear()
                        continue
                    if completed.result.status != "succeeded":
                        state.failed_steps.add(index)
                        self._emit_step_failed(state, completed, step, dependencies, duration_ms=duration_ms)
                        if not step.continueOnError and failed_step is None:
                            failed_step = step.name
                        continue

                    new_vars = _collect_step_vars(prepared, step)
                    self._stage_artifacts(state, index, step)
                    self._emit_step_succeeded(
                        state, completed, step, dependencies, duration_ms=duration_ms, new_vars=new_vars
                    )
                    if new_vars:
                        step_vars[index] = new_vars
                        state.vars.update(new_vars)

        return failed_step

    def _run_step_with_retries(
        self,
        state: _RunState,
        index: int,
        step: WorkflowStep,
        step_env: dict[str, str],
    ) -> _StepExecutionResult:
        prepared = state.prepared
        labels = {
            **prepared.labels,
            "agentis.attempt": prepared.attempt,
            "agentis.step_index": str(index),
            "agentis.step": safe_step_name(step.name),
            WORKFLOW_LABEL: "true",
        }
        name = _job_name(prepared, index, step)
        timeout = step.timeoutSeconds if step.timeoutSeconds is not None else prepared.workflow.workflow.timeoutSeconds
        attempt = 0
        while True:
            attempt += 1
            attempt_name = name if attempt == 1 else f"{name[:60].rstrip('-')}-r{attempt}"
            result = prepared.runner.run_step(
                prepared.workflow,
                step,
                namespace=prepared.namespace,
                name=attempt_name,
                labels=labels,
                env=step_env,
                timeout=float(timeout),
                abort_event=prepared.abort_event,
                run_dir=prepared.run_dir,
            )
            if result.status in {"aborted", "succeeded"} or attempt > step.retries:
                return _StepExecutionResult(
                    index=index, step_name=step.name, job_name=name, result=result, attempts=attempt
                )
            if prepared.abort_event.is_set():
                return _StepExecutionResult(
                    index=index,
                    step_name=step.name,
                    job_name=name,
                    result=StepResult(status="aborted"),
                    attempts=attempt,
                )

    def _cleanup_namespace(self, state: _RunState) -> None:
        """Smaže namespace po úspěšném workflow s `deleteNamespace: true` (jen executor kubernetes).

        Selhání úklidu neshodí run, jen se nahlásí jako warning.
        """

        prepared = state.prepared
        if not prepared.workflow.workflow.deleteNamespace or prepared.executor != _CLEANUP_NAMESPACE_EXECUTOR:
            return
        try:
            prepared.runner.delete_namespace(prepared.namespace)
        except Exception as exc:  # noqa: BLE001
            state.warn(f"Deleting namespace {prepared.namespace} failed.", namespace=prepared.namespace, error=str(exc))

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _emit_step_started(
        self, state: _RunState, index: int, step: WorkflowStep, dependencies: list[list[int]]
    ) -> None:
        state.emit(
            {
                "type": "step_started",
                "index": index,
                "step": step.name,
                "needs": _dependency_names(index, state.prepared.workflow.workflow.steps, dependencies),
                "job": _job_name(state.prepared, index, step),
            }
        )

    def _emit_step_succeeded(
        self,
        state: _RunState,
        completed: _StepExecutionResult,
        step: WorkflowStep,
        dependencies: list[list[int]],
        *,
        duration_ms: int,
        new_vars: dict[str, str],
    ) -> None:
        state.emit(
            {
                "type": "step_succeeded",
                "index": completed.index,
                "step": completed.step_name,
                "needs": _dependency_names(completed.index, state.prepared.workflow.workflow.steps, dependencies),
                "job": completed.job_name,
                "attempts": completed.attempts,
                "duration_ms": duration_ms,
                "vars": new_vars,
            }
        )

    def _emit_step_failed(
        self,
        state: _RunState,
        completed: _StepExecutionResult,
        step: WorkflowStep,
        dependencies: list[list[int]],
        *,
        duration_ms: int,
    ) -> None:
        event: dict[str, Any] = {
            "type": "step_failed",
            "index": completed.index,
            "step": completed.step_name,
            "needs": _dependency_names(completed.index, state.prepared.workflow.workflow.steps, dependencies),
            "job": completed.job_name,
            "result": completed.result.status,
            "attempts": completed.attempts,
            "duration_ms": duration_ms,
            "log_tail": completed.result.log_tail,
            "continue_on_error": step.continueOnError,
        }
        agent_error = _agent_error_from_log(completed.result.log_tail)
        if agent_error:
            event["error"] = agent_error
        state.emit(event)

    def _emit_step_skipped(
        self,
        state: _RunState,
        index: int,
        step: WorkflowStep,
        dependencies: list[list[int]],
        *,
        condition: str | None = None,
        visible_vars: dict[str, str] | None = None,
        failed_step: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": "step_skipped",
            "index": index,
            "step": step.name,
            "needs": _dependency_names(index, state.prepared.workflow.workflow.steps, dependencies),
        }
        if condition is not None:
            event.update({"reason": "condition", "condition": condition, "vars": dict(visible_vars or {})})
        else:
            event.update({"reason": "workflow_failed", "failed_step": failed_step})
        state.emit(event)

    # ------------------------------------------------------------------
    # Outputs
    # ------------------------------------------------------------------

    def _collect_outputs(self, state: _RunState) -> RunOutputs:
        """Outputs jen z úspěšných, nepřeskočených kroků (i při selhání workflow — `always` failure komentář)."""

        prepared = state.prepared
        comments: list[RunComment] = []
        session_id: str | None = None
        attachments: list[RunAttachment] = []
        for index, step in enumerate(prepared.workflow.workflow.steps):
            if index in state.skipped_steps or index in state.failed_steps:
                continue
            for output in step.outputs:
                if output.type == "agent_comment":
                    body = _read_output_file(prepared, output.bodyFrom)
                    if body and body.strip():
                        name = _read_output_file(prepared, output.nameFrom) if output.nameFrom else output.name
                        comments.append(
                            RunComment(
                                body=body.strip(),
                                status=output.status,
                                author_name=(name or "").strip() or None,
                            )
                        )
                elif output.type == "session_id":
                    value = _read_output_file(prepared, output.valueFrom)
                    if value and value.strip():
                        session_id = value.strip()
                elif output.type in {"url", "text"}:
                    value = _read_output_file(prepared, output.valueFrom)
                    if value and value.strip():
                        attachments.append(
                            RunAttachment(label=output.label or output.type, value=value.strip(), type=output.type)
                        )

        artifacts = [
            RunArtifact(
                name=staged.name,
                filename=staged.filename,
                path=staged.staged_path.relative_to(prepared.run_dir).as_posix(),
                size=staged.size,
            )
            for staged in sorted(
                state.staged_artifacts, key=lambda item: (item.step_index, item.output_index, item.match_index)
            )
        ]
        return RunOutputs(comments=comments, session_id=session_id, attachments=attachments, artifacts=artifacts)

    def _followups(self, state: _RunState) -> list[RunFollowup]:
        prepared = state.prepared
        followup_vars = _condition_vars(prepared, env=prepared.base_env, visible_vars=state.vars)
        return [
            RunFollowup(
                title=followup.title,
                prompt=followup.prompt,
                workflow=followup.workflow,
                continue_previous_run=followup.continue_previous_run,
            )
            for followup in prepared.workflow.workflow.followups
            if followup.if_ is None or evaluate_condition(followup.if_, followup_vars)
        ]

    def _stage_artifacts(self, state: _RunState, index: int, step: WorkflowStep) -> None:
        """Zkopíruje artefakty úspěšného kroku do `artifact-staging`, dokud je krok nepřepsal další krok."""

        prepared = state.prepared
        staging_dir = prepared.run_dir / ARTIFACT_OUTPUT_STAGING_DIR
        for output_index, output in enumerate(step.outputs):
            if output.type != "artifact" or not output.path:
                continue
            has_glob = _has_glob(output.path)
            paths = _artifact_output_paths(prepared, output.path)
            if not paths:
                state.warn("artifact glob matched no files", step=step.name, path=output.path)
                continue
            root = prepared.output_root.resolve()
            multiple = len(paths) > 1
            for match_index, path in enumerate(paths):
                name = _artifact_name(
                    output, _artifact_display_relpath(root, output.path, path), has_glob=has_glob, multiple=multiple
                )
                if len(name) > ARTIFACT_NAME_MAX_LENGTH:
                    state.warn("artifact name is too long; skipped", step=step.name, path=path.name)
                    continue
                if len(state.staged_artifacts) >= ARTIFACT_OUTPUT_MAX_FILES:
                    state.warn(
                        "artifact file limit reached; remaining files skipped", max_files=ARTIFACT_OUTPUT_MAX_FILES
                    )
                    return
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if state.staged_bytes + size > ARTIFACT_OUTPUT_MAX_BYTES:
                    state.warn("artifact size limit reached; file skipped", step=step.name, path=path.name)
                    continue
                target_dir = staging_dir / f"{index}-{output_index}-{match_index}"
                target = target_dir / path.name
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
                except OSError as exc:
                    state.warn("artifact could not be staged; skipped", step=step.name, path=path.name, error=str(exc))
                    continue
                state.staged_bytes += size
                state.staged_artifacts.append(
                    _StagedArtifact(
                        step_index=index,
                        output_index=output_index,
                        match_index=match_index,
                        name=name,
                        filename=path.name,
                        staged_path=target,
                        size=size,
                    )
                )


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


def abort_by_labels(
    executor: str,
    namespace: str,
    labels: Mapping[str, str],
    settings: EngineSettings,
    *,
    kube_context: str | None = None,
) -> str:
    """Úklid bez běžícího enginu (např. po restartu): smaže joby/kontejnery se shodnými labely."""

    if not labels:
        raise WorkflowSpecError("abort requires at least one label")
    return _make_runner(executor, settings, kube_context).abort(namespace, dict(labels))


def _make_runner(executor: str, settings: EngineSettings, kube_context: str | None) -> WorkflowStepRunner:
    if executor == "local":
        return LocalProcessRunner(settings)
    if executor == "docker":
        return DockerContainerRunner(settings)
    if executor == "kubernetes":
        return KubectlJobRunner(settings, kube_context=kube_context)
    raise WorkflowSpecError(f"Unknown workflow executor {executor!r}; expected one of {WORKFLOW_EXECUTORS}")


def _resolve(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.endswith((".yaml", ".yml"))


def _find_workflow_file(spec: RunSpec, workdir: Path) -> Path:
    """Cesta k workflow souboru; jméno se hledá v `search_paths` (první nalezený vyhrává)."""

    name = spec.workflow
    if _looks_like_path(name):
        path = _resolve(name, workdir)
        if not path.is_file():
            raise WorkflowSpecError(f"Workflow file not found: {path}")
        return path
    if not WORKFLOW_NAME_RE.fullmatch(name):
        raise WorkflowSpecError(f"Invalid workflow name {name!r}; expected a plain name or a path to a .yaml file")
    searched: list[str] = []
    for search_path in spec.search_paths:
        candidate = _resolve(search_path, workdir) / f"{name}.yaml"
        if candidate.is_file():
            return candidate
        searched.append(str(candidate))
    raise WorkflowSpecError(f"Workflow {name!r} not found; searched: {', '.join(searched) or 'no search paths'}")


def _interpolation_values(spec: RunSpec, *, workdir: Path, run_dir: Path) -> dict[str, str]:
    values = {name: spec.vars.get(name, "") for name in INTERPOLATION_ALLOWLIST}
    values.update({"NAMESPACE": spec.namespace, "WORKDIR": str(workdir), "RUN_DIR": str(run_dir)})
    return values


def _base_env(
    spec: RunSpec,
    environ: Mapping[str, str],
    interpolation: dict[str, str],
    *,
    run_dir: Path,
) -> dict[str, str]:
    """Env kroků: `spec.env`, hodnoty z `pass_env` (secrety z env procesu), interpolační tokeny a cesty."""

    env = dict(spec.env)
    for name in spec.pass_env:
        value = environ.get(name)
        if value is not None:
            env[name] = value
    env.update(interpolation)
    env.update(
        {
            "AGENTIS_RUN_DIR": str(run_dir),
            "AGENTIS_PROMPT_FILE": spec.prompt_file,
            "AGENTIS_CONTEXT_FILE": spec.context_file,
        }
    )
    return env


def _load(path: Path, values: dict[str, str]) -> WorkflowFile:
    try:
        return load_workflow_file(path, values)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        # pydantic ValidationError, interpolace, extends i podmínky jsou ValueError podtřídy.
        raise WorkflowSpecError(f"Invalid workflow {path}: {exc}") from exc


def _inject_env_files(workflow: WorkflowFile, workdir: Path) -> WorkflowFile:
    """Načte dotenv soubory z hostitele (relativně k workdir) a zmrazí hodnoty do env každého kroku."""

    env: dict[str, str] = {}
    for filename in workflow.workflow.envFiles:
        path = _resolve(filename, workdir)
        if not path.is_file():
            raise WorkflowSpecError(f"Workflow env file does not exist or is not a file: {path}")
        values = dotenv_values(path, interpolate=False)
        env.update({key: value if value is not None else "" for key, value in values.items()})

    if not env:
        return workflow.model_copy(update={"workflow": workflow.workflow.model_copy(update={"envFiles": []})})

    steps = [step.model_copy(update={"env": {**step.env, **env}}) for step in workflow.workflow.steps]
    spec = workflow.workflow.model_copy(update={"envFiles": [], "steps": steps})
    return workflow.model_copy(update={"workflow": spec})


def _require_vars(workflow: WorkflowFile, names: list[str], workflow_file: Path) -> None:
    for name in names:
        declared = [
            output
            for step in workflow.workflow.steps
            for output in step.outputs
            if output.type == "var" and output.name == name
        ]
        if len(declared) != 1:
            raise WorkflowSpecError(f"Workflow {workflow_file} must declare exactly one var output named {name}")


def _require_images(workflow: WorkflowFile, workflow_file: Path, executor: str) -> None:
    """Kontejnerové executory potřebují image pro každý krok; local executor ho ignoruje."""

    spec = workflow.workflow
    missing = [step.name for step in spec.steps if not (step.image or spec.image)]
    if missing:
        raise WorkflowSpecError(
            f"Workflow executor {executor!r} requires 'image' in {workflow_file} "
            f"(missing for steps: {', '.join(missing)})"
        )


def _job_name(prepared: PreparedRun, index: int, step: WorkflowStep) -> str:
    return job_name(prepared.run_key, prepared.attempt, index, step.name)


def _condition_vars(
    prepared: PreparedRun,
    *,
    env: dict[str, str],
    step_env: dict[str, str] | None = None,
    visible_vars: dict[str, str] | None = None,
) -> dict[str, str]:
    """Proměnné viditelné v `if`; `var` outputs přepisují env i built-ins."""

    return {
        **prepared.workflow.workflow.env,
        **env,
        **(step_env or {}),
        **prepared.interpolation,
        **(visible_vars or {}),
    }


def _step_dependencies(steps: list[WorkflowStep]) -> list[list[int]]:
    name_to_index: dict[str, int] = {}
    dependencies: list[list[int]] = []
    for index, step in enumerate(steps):
        if step.needs is None:
            dependencies.append([index - 1] if index > 0 else [])
        else:
            dependencies.append([name_to_index[name] for name in step.needs])
        name_to_index[step.name] = index
    return dependencies


def _dependency_names(index: int, steps: list[WorkflowStep], dependencies: list[list[int]]) -> list[str]:
    return [steps[dependency].name for dependency in dependencies[index]]


def _dependency_vars(index: int, dependencies: list[list[int]], step_vars: dict[int, dict[str, str]]) -> dict[str, str]:
    """Vars tranzitivních závislostí kroku (v pořadí od nejstarších předků)."""

    values: dict[str, str] = {}
    seen: set[int] = set()

    def visit(dependency: int) -> None:
        if dependency in seen:
            return
        for parent in dependencies[dependency]:
            visit(parent)
        seen.add(dependency)
        values.update(step_vars.get(dependency, {}))

    for dependency in dependencies[index]:
        visit(dependency)
    return values


def _collect_step_vars(prepared: PreparedRun, step: WorkflowStep) -> dict[str, str]:
    values: dict[str, str] = {}
    for output in step.outputs:
        if output.type != "var" or not output.name:
            continue
        value = _read_output_file(prepared, output.valueFrom)
        values[output.name] = (value or "").strip()
    return values


def _read_output_file(prepared: PreparedRun, relpath: str | None) -> str | None:
    """Přečte soubor z output_root; cesty mimo kořen (včetně absolutních a `..`) se ignorují."""

    if not relpath:
        return None
    root = prepared.output_root.resolve()
    path = (root / relpath).resolve()
    if root not in path.parents or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _artifact_output_paths(prepared: PreparedRun, pattern: str) -> list[Path]:
    root = prepared.output_root.resolve()
    if not _has_glob(pattern):
        path = (root / pattern).resolve()
        if root not in path.parents or not path.is_file():
            return []
        return [path]

    if Path(pattern).is_absolute():
        return []
    try:
        candidates = sorted(root.glob(pattern))
    except ValueError:
        return []

    paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.resolve()
        if path in seen or root not in path.parents or not path.is_file():
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _has_glob(pattern: str) -> bool:
    return any(char in pattern for char in "*?[")


def _artifact_display_relpath(root: Path, pattern: str, path: Path) -> str:
    root_relpath = path.relative_to(root).as_posix()
    if not _has_glob(pattern):
        return root_relpath

    base_parts: list[str] = []
    for part in Path(pattern).parts:
        if _has_glob(part):
            break
        base_parts.append(part)
    base = root.joinpath(*base_parts).resolve() if base_parts else root
    if base == root or base in path.parents:
        return path.relative_to(base).as_posix()
    return root_relpath


def _artifact_name(output: WorkflowOutput, relpath: str, *, has_glob: bool, multiple: bool) -> str:
    if output.name and multiple:
        return f"{output.name}/{relpath}"
    if output.name:
        return output.name
    return relpath if has_glob else (output.path or relpath)


def _agent_error_from_log(log_tail: str) -> str | None:
    """Poslední `error` událost z JSON řádků logu `agentiscode` (viditelná zpráva pro report)."""

    error: str | None = None
    for line in log_tail.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "error":
            continue
        message = event.get("message")
        if not isinstance(message, str):
            data = event.get("data")
            message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, str) and message.strip():
            error = message.strip()
    return error


def _write_result(run_dir: Path, result: RunResult) -> None:
    """Atomický zápis `result.json` (tmp soubor + `os.replace`)."""

    path = run_dir / RESULT_FILE_NAME
    tmp = run_dir / f"{RESULT_FILE_NAME}.tmp"
    tmp.write_text(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


__all__ = [
    "ARTIFACT_NAME_MAX_LENGTH",
    "ARTIFACT_OUTPUT_MAX_BYTES",
    "ARTIFACT_OUTPUT_MAX_FILES",
    "ARTIFACT_OUTPUT_STAGING_DIR",
    "PreparedRun",
    "RESULT_FILE_NAME",
    "WorkflowBusyError",
    "WorkflowEngine",
    "WorkflowEngineError",
    "WorkflowSpecError",
    "abort_by_labels",
]
