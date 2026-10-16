"""Testy orchestrace enginu: výběr workflow, executor, busy, scheduler, outputs, artefakty, abort a úklid."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agentis_workflow import engine as engine_module
from agentis_workflow.engine import (
    WorkflowBusyError,
    WorkflowEngine,
    WorkflowSpecError,
    abort_by_labels,
)
from agentis_workflow.settings import EngineSettings
from tests.support import (
    BlockingRunner,
    FakeRunner,
    event_types,
    make_spec,
    run_workflow,
    write_workflow,
)

TWO_STEPS = """
version: 1
workflow:
  steps:
    - name: prepare
      run: echo prepare
      outputs:
        - type: var
          name: READY
          valueFrom: out/ready
    - name: agent
      run: echo agent
      outputs:
        - type: agent_comment
          bodyFrom: out/comment.md
          status: in_review
          nameFrom: out/author
        - type: session_id
          valueFrom: out/session
        - type: url
          label: Pull Request
          valueFrom: out/pr-url
"""


def _write_outputs(workdir: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        path = workdir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# prepare: výběr souboru, executor, validace
# ---------------------------------------------------------------------------


def test_prepare_finds_workflow_by_name_in_first_search_path(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, search_paths=["first", "second"], workflow="report")
    write_workflow(Path(spec.workdir), TWO_STEPS, relpath="second/report.yaml")
    write_workflow(Path(spec.workdir), TWO_STEPS.replace("prepare", "winner"), relpath="first/report.yaml")

    prepared = WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)

    assert prepared.workflow_file == Path(spec.workdir) / "first" / "report.yaml"
    assert prepared.workflow.workflow.steps[0].name == "winner"


def test_prepare_accepts_path_to_workflow_file(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, workflow="flows/custom.yaml")
    write_workflow(Path(spec.workdir), TWO_STEPS, relpath="flows/custom.yaml")

    prepared = WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)

    assert [step["name"] for step in prepared.summary()["steps"]] == ["prepare", "agent"]


@pytest.mark.parametrize("name", ["../escape", "a/b", "merge\n", ".hidden"])
def test_prepare_rejects_invalid_workflow_names(tmp_path: Path, name: str) -> None:
    spec = make_spec(tmp_path, workflow=name)
    with pytest.raises(WorkflowSpecError):
        WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)


def test_prepare_missing_workflow_lists_searched_paths(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, workflow="missing")
    with pytest.raises(WorkflowSpecError, match="missing.yaml"):
        WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)


def test_prepare_invalid_yaml_is_spec_error(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    write_workflow(Path(spec.workdir), "version: 1\nworkflow:\n  steps: []\n")
    with pytest.raises(WorkflowSpecError, match="Invalid workflow"):
        WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)


def test_prepare_does_not_create_run_directory(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    write_workflow(Path(spec.workdir), TWO_STEPS)

    WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)

    assert not Path(spec.run_dir).exists()


@pytest.mark.parametrize(
    ("spec_executor", "yaml_executor", "default", "expected"),
    [
        ("docker", "local", "kubernetes", "docker"),
        (None, "local", "kubernetes", "local"),
        (None, None, "docker", "docker"),
        (None, None, None, "kubernetes"),
    ],
)
def test_executor_precedence_spec_yaml_default(
    tmp_path: Path, spec_executor: str | None, yaml_executor: str | None, default: str | None, expected: str
) -> None:
    yaml_line = f"  executor: {yaml_executor}\n" if yaml_executor else ""
    text = f"version: 1\nworkflow:\n{yaml_line}  image: img:1\n  steps:\n    - name: a\n      run: echo a\n"
    spec = make_spec(tmp_path, executor=spec_executor, executor_default=default)
    write_workflow(Path(spec.workdir), text)

    prepared = WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)

    assert prepared.executor == expected


def test_container_executors_require_image_for_every_step(tmp_path: Path) -> None:
    text = "version: 1\nworkflow:\n  steps:\n    - name: a\n      run: echo a\n"
    spec = make_spec(tmp_path, executor="kubernetes")
    write_workflow(Path(spec.workdir), text)
    with pytest.raises(WorkflowSpecError, match="requires 'image'"):
        WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)


def test_local_executor_ignores_missing_image(tmp_path: Path) -> None:
    result, _events, _runner = run_workflow(tmp_path, TWO_STEPS.replace("prepare", "p"), executor="local")
    assert result.status == "success"


def test_require_vars_must_be_declared_exactly_once(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, require_vars=["APPROVED"])
    write_workflow(Path(spec.workdir), TWO_STEPS)
    with pytest.raises(WorkflowSpecError, match="APPROVED"):
        WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)


def test_busy_labels_block_prepare(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.active = True
    spec = make_spec(tmp_path)
    write_workflow(Path(spec.workdir), TWO_STEPS)
    with pytest.raises(WorkflowBusyError):
        WorkflowEngine(EngineSettings(), runner=runner).prepare(spec)


def test_env_files_are_read_relative_to_workdir_and_frozen(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    workdir = Path(spec.workdir)
    (workdir / "step.env").write_text("FROM_FILE=1\nQUOTED='a b'\n", encoding="utf-8")
    write_workflow(
        workdir,
        """
        version: 1
        workflow:
          envFiles:
            - step.env
          steps:
            - name: a
              run: echo a
        """,
    )
    engine = WorkflowEngine(EngineSettings(), runner=FakeRunner())
    prepared = engine.prepare(spec)
    (workdir / "step.env").write_text("FROM_FILE=changed\n", encoding="utf-8")

    result_events: list[dict[str, Any]] = []
    engine.run(prepared, result_events.append)

    assert prepared.workflow.workflow.steps[0].env == {"FROM_FILE": "1", "QUOTED": "a b"}
    assert prepared.workflow.workflow.envFiles == []


def test_missing_env_file_is_spec_error(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    write_workflow(
        Path(spec.workdir),
        "version: 1\nworkflow:\n  envFiles: [nope.env]\n  steps:\n    - name: a\n      run: echo a\n",
    )
    with pytest.raises(WorkflowSpecError, match="nope.env"):
        WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)


# ---------------------------------------------------------------------------
# env kroků a secrety
# ---------------------------------------------------------------------------


def test_step_env_contains_pass_env_secrets_but_summary_and_repr_do_not(tmp_path: Path) -> None:
    spec = make_spec(
        tmp_path,
        env={"AGENTIS_MODEL": "m1"},
        pass_env=["AGENTIS_SERVICE_TOKEN", "NOT_SET_ANYWHERE"],
        vars={"TASK_TITLE": "Titulek"},
    )
    write_workflow(Path(spec.workdir), TWO_STEPS)
    runner = FakeRunner()
    engine = WorkflowEngine(EngineSettings(), runner=runner, environ={"AGENTIS_SERVICE_TOKEN": "s3cr3t"})

    prepared = engine.prepare(spec)
    engine.run(prepared, lambda event: None)

    env = runner.steps[0]["env"]
    assert env["AGENTIS_SERVICE_TOKEN"] == "s3cr3t"
    assert env["AGENTIS_MODEL"] == "m1"
    assert env["TASK_TITLE"] == "Titulek"
    assert env["NAMESPACE"] == "task-77-test"
    assert env["AGENTIS_PROMPT_FILE"] == str(Path(spec.run_dir) / "prompt.md")
    assert env["AGENTIS_RUN_DIR"] == spec.run_dir
    assert "NOT_SET_ANYWHERE" not in env
    assert "s3cr3t" not in json.dumps(prepared.summary())
    assert "s3cr3t" not in repr(prepared)
    assert "AGENTIS_SERVICE_TOKEN" not in json.dumps(runner.steps[0]["labels"])


def test_engine_tokens_cannot_be_spoofed_by_spec_env(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, env={"AGENTIS_RUN_DIR": "/spoofed"})
    write_workflow(Path(spec.workdir), TWO_STEPS)
    runner = FakeRunner()
    engine = WorkflowEngine(EngineSettings(), runner=runner)

    engine.run(engine.prepare(spec), lambda event: None)

    assert runner.steps[0]["env"]["AGENTIS_RUN_DIR"] == spec.run_dir


# ---------------------------------------------------------------------------
# run: outputs, události, result.json
# ---------------------------------------------------------------------------


def test_successful_run_applies_outputs_and_writes_result(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    _write_outputs(
        Path(spec.workdir),
        {
            "out/ready": "true\n",
            "out/comment.md": "Hotovo.",
            "out/author": "Agent - model",
            "out/session": "ses_42\n",
            "out/pr-url": "https://example.test/pr/1\n",
        },
    )
    write_workflow(Path(spec.workdir), TWO_STEPS)
    engine = WorkflowEngine(EngineSettings(), runner=FakeRunner())
    events: list[dict[str, Any]] = []

    result = engine.run(engine.prepare(spec), events.append)

    assert result.status == "success"
    assert result.failed_step is None
    assert result.vars == {"READY": "true"}
    assert result.outputs.comments[0].model_dump() == {
        "body": "Hotovo.",
        "status": 4,
        "author_name": "Agent - model",
    }
    assert result.outputs.session_id == "ses_42"
    assert [item.model_dump() for item in result.outputs.attachments] == [
        {"label": "Pull Request", "value": "https://example.test/pr/1", "type": "url"}
    ]
    assert result.followups == []
    on_disk = json.loads((Path(spec.run_dir) / "result.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "success"
    assert on_disk["outputs"]["session_id"] == "ses_42"
    assert not (Path(spec.run_dir) / "result.json.tmp").exists()

    assert event_types(events) == [
        "workflow_started",
        "step_started",
        "step_succeeded",
        "step_started",
        "step_succeeded",
        "workflow_finished",
    ]
    assert events[2]["vars"] == {"READY": "true"}
    assert events[2]["attempts"] == 1
    assert "duration_ms" in events[2]
    assert events[-1] == {"type": "workflow_finished", "status": "success", "failed_step": None}


def test_outputs_are_read_only_inside_output_root(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, output_root="out-root")
    outside = Path(spec.workdir) / "secret.txt"
    outside.write_text("nelze číst", encoding="utf-8")
    (Path(spec.workdir) / "out-root").mkdir()
    write_workflow(
        Path(spec.workdir),
        f"""
        version: 1
        workflow:
          steps:
            - name: a
              run: echo a
              outputs:
                - type: agent_comment
                  bodyFrom: ../secret.txt
                - type: var
                  name: LEAK
                  valueFrom: {outside}
        """,
    )
    engine = WorkflowEngine(EngineSettings(), runner=FakeRunner())

    result = engine.run(engine.prepare(spec), lambda event: None)

    assert result.outputs.comments == []
    assert result.vars == {"LEAK": ""}


def test_var_outputs_drive_step_conditions_and_dependency_env(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: check
          run: echo check
          outputs:
            - type: var
              name: ENV_READY
              valueFrom: out/ready
        - name: install
          if: ENV_READY != 'true'
          run: echo install
        - name: agent
          run: echo agent
    """
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"out/ready": "true"})
    write_workflow(Path(spec.workdir), text)
    runner = FakeRunner()
    engine = WorkflowEngine(EngineSettings(), runner=runner)
    events: list[dict[str, Any]] = []

    result = engine.run(engine.prepare(spec), events.append)

    assert result.status == "success"
    assert runner.step_names() == ["check", "agent"]
    skipped = [event for event in events if event["type"] == "step_skipped"]
    assert skipped == [
        {
            "type": "step_skipped",
            "index": 1,
            "step": "install",
            "needs": ["check"],
            "reason": "condition",
            "condition": "ENV_READY != 'true'",
            "vars": {"ENV_READY": "true"},
        }
    ]
    agent_env = runner.steps[1]["env"]
    assert agent_env["ENV_READY"] == "true"


def test_failed_step_stops_workflow_and_always_step_gets_failure_status(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: agent
          run: agent
          outputs:
            - type: agent_comment
              bodyFrom: out/comment.md
        - name: report
          run: report
          always: true
          outputs:
            - type: agent_comment
              bodyFrom: out/failure.md
              status: done
        - name: after
          run: after
          always: false
          outputs:
            - type: session_id
              valueFrom: out/session
    """
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"out/comment.md": "nemá se aplikovat", "out/failure.md": "Selhalo."})
    write_workflow(Path(spec.workdir), text)
    runner = FakeRunner()
    runner.results_by_step["agent"] = ["failed"]
    engine = WorkflowEngine(EngineSettings(), runner=runner)
    events: list[dict[str, Any]] = []

    result = engine.run(engine.prepare(spec), events.append)

    assert result.status == "failed"
    assert result.failed_step == "agent"
    assert runner.step_names() == ["agent", "report"]
    assert runner.steps[1]["env"]["AGENTIS_WORKFLOW_STATUS"] == "failed"
    assert runner.steps[1]["env"]["AGENTIS_FAILED_STEP"] == "agent"
    assert [item.body for item in result.outputs.comments] == ["Selhalo."]
    assert result.outputs.comments[0].status == 5
    assert result.followups == []
    failed = next(event for event in events if event["type"] == "step_failed")
    assert failed["step"] == "agent"
    assert failed["result"] == "failed"
    assert failed["continue_on_error"] is False
    assert failed["log_tail"] == "boom log"
    skipped = [event for event in events if event["type"] == "step_skipped"]
    assert skipped == [
        {
            "type": "step_skipped",
            "index": 2,
            "step": "after",
            "needs": ["report"],
            "reason": "workflow_failed",
            "failed_step": "agent",
        }
    ]


def test_always_step_reports_success_when_nothing_failed(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: work
          run: work
        - name: cleanup
          run: cleanup
          always: true
    """
    result, _events, runner = run_workflow(tmp_path, text)

    assert result.status == "success"
    assert runner.steps[1]["env"]["AGENTIS_WORKFLOW_STATUS"] == "success"
    assert runner.steps[1]["env"]["AGENTIS_FAILED_STEP"] == ""


def test_continue_on_error_keeps_workflow_running_and_drops_its_outputs(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: optional
          run: optional
          continueOnError: true
          outputs:
            - type: agent_comment
              bodyFrom: out/optional.md
        - name: main
          run: main
          outputs:
            - type: agent_comment
              bodyFrom: out/main.md
    """
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"out/optional.md": "skryté", "out/main.md": "viditelné"})
    write_workflow(Path(spec.workdir), text)
    runner = FakeRunner()
    runner.results_by_step["optional"] = ["failed"]
    engine = WorkflowEngine(EngineSettings(), runner=runner)

    result = engine.run(engine.prepare(spec), lambda event: None)

    assert result.status == "success"
    assert [item.body for item in result.outputs.comments] == ["viditelné"]


def test_retries_rerun_failed_step_and_report_attempts(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: flaky
          run: flaky
          retries: 2
    """
    spec = make_spec(tmp_path)
    write_workflow(Path(spec.workdir), text)
    runner = FakeRunner()
    runner.results_by_step["flaky"] = ["failed", "failed"]
    engine = WorkflowEngine(EngineSettings(), runner=runner)
    events: list[dict[str, Any]] = []

    result = engine.run(engine.prepare(spec), events.append)

    assert result.status == "success"
    names = [record["name"] for record in runner.steps]
    assert len(names) == 3
    assert names[1].endswith("-r2") and names[2].endswith("-r3")
    succeeded = next(event for event in events if event["type"] == "step_succeeded")
    assert succeeded["attempts"] == 3


def test_retries_exhausted_reports_failure_with_attempt_count(tmp_path: Path) -> None:
    text = "version: 1\nworkflow:\n  steps:\n    - name: flaky\n      run: flaky\n      retries: 1\n"
    spec = make_spec(tmp_path)
    write_workflow(Path(spec.workdir), text)
    runner = FakeRunner()
    runner.results_by_step["flaky"] = ["failed", "failed"]
    engine = WorkflowEngine(EngineSettings(), runner=runner)
    events: list[dict[str, Any]] = []

    result = engine.run(engine.prepare(spec), events.append)

    assert result.status == "failed"
    failed = next(event for event in events if event["type"] == "step_failed")
    assert failed["attempts"] == 2


def test_parallel_needs_start_independent_steps_and_join_after_them(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      maxParallel: 2
      steps:
        - name: lint
          needs: []
          run: lint
          outputs:
            - type: var
              name: LINT
              valueFrom: out/lint
        - name: test
          needs: []
          run: test
          outputs:
            - type: var
              name: TESTS
              valueFrom: out/tests
        - name: report
          needs: [lint, test]
          run: report
    """
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"out/lint": "ok", "out/tests": "green"})
    write_workflow(Path(spec.workdir), text)
    runner = FakeRunner()
    engine = WorkflowEngine(EngineSettings(), runner=runner)

    result = engine.run(engine.prepare(spec), lambda event: None)

    assert result.status == "success"
    assert runner.step_names()[-1] == "report"
    report_env = runner.steps[-1]["env"]
    assert report_env["LINT"] == "ok"
    assert report_env["TESTS"] == "green"


def test_parallel_fail_fast_skips_pending_normal_steps(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: broken
          needs: []
          run: broken
        - name: waiting
          needs: []
          run: waiting
        - name: after
          needs: [broken]
          run: after
    """
    spec = make_spec(tmp_path)
    write_workflow(Path(spec.workdir), text)
    runner = FakeRunner()
    runner.results_by_step["broken"] = ["failed"]
    engine = WorkflowEngine(EngineSettings(), runner=runner)
    events: list[dict[str, Any]] = []

    result = engine.run(engine.prepare(spec), events.append)

    assert result.status == "failed"
    assert "after" not in runner.step_names()
    skipped = {event["step"] for event in events if event["type"] == "step_skipped"}
    assert {"after"} <= skipped


# ---------------------------------------------------------------------------
# artefakty
# ---------------------------------------------------------------------------


def test_artifact_glob_is_staged_into_run_dir(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: render
          run: render
          outputs:
            - type: artifact
              name: shots
              path: shots/*.png
    """
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"shots/a.png": "AAA", "shots/b.png": "BB"})
    write_workflow(Path(spec.workdir), text)
    engine = WorkflowEngine(EngineSettings(), runner=FakeRunner())

    result = engine.run(engine.prepare(spec), lambda event: None)

    assert [artifact.name for artifact in result.outputs.artifacts] == ["shots/a.png", "shots/b.png"]
    first = result.outputs.artifacts[0]
    assert first.filename == "a.png"
    assert first.size == 3
    assert first.path.startswith("artifact-staging/")
    assert (Path(spec.run_dir) / first.path).read_bytes() == b"AAA"


def test_artifact_snapshot_survives_later_step_deleting_source(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: make
          run: make
          outputs:
            - type: artifact
              path: report.pdf
        - name: cleanup
          run: cleanup
    """
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"report.pdf": "PDF"})
    write_workflow(Path(spec.workdir), text)

    class _DeletingRunner(FakeRunner):
        def run_step(self, workflow: Any, step: Any, **kwargs: Any):  # type: ignore[override]
            if step.name == "cleanup":
                (Path(spec.workdir) / "report.pdf").unlink()
            return super().run_step(workflow, step, **kwargs)

    engine = WorkflowEngine(EngineSettings(), runner=_DeletingRunner())
    result = engine.run(engine.prepare(spec), lambda event: None)

    assert [artifact.name for artifact in result.outputs.artifacts] == ["report.pdf"]
    assert (Path(spec.run_dir) / result.outputs.artifacts[0].path).read_bytes() == b"PDF"


def test_artifact_file_limit_is_enforced_with_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "ARTIFACT_OUTPUT_MAX_FILES", 1)
    text = """
    version: 1
    workflow:
      steps:
        - name: render
          run: render
          outputs:
            - type: artifact
              path: out/*.txt
    """
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"out/a.txt": "a", "out/b.txt": "b"})
    write_workflow(Path(spec.workdir), text)
    engine = WorkflowEngine(EngineSettings(), runner=FakeRunner())

    result = engine.run(engine.prepare(spec), lambda event: None)

    assert len(result.outputs.artifacts) == 1
    assert [warning.message for warning in result.warnings] == ["artifact file limit reached; remaining files skipped"]


def test_artifact_glob_without_match_is_warning_not_failure(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: render
          run: render
          outputs:
            - type: artifact
              path: none/*.png
    """
    result, events, _runner = run_workflow(tmp_path, text)

    assert result.status == "success"
    assert result.outputs.artifacts == []
    assert result.warnings[0].message == "artifact glob matched no files"
    assert any(event["type"] == "warning" for event in events)


# ---------------------------------------------------------------------------
# followups a namespace
# ---------------------------------------------------------------------------


FOLLOWUPS = """
version: 1
workflow:
  steps:
    - name: agent
      run: agent
      outputs:
        - type: var
          name: PR_CREATED
          valueFrom: out/pr
  followups:
    - title: Git merge
      prompt: Sloučit.
      workflow: merge
      if: PR_CREATED == '1'
    - title: Zavřít
      workflow: close
"""


def test_followups_are_offered_when_condition_holds(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"out/pr": "1"})
    write_workflow(Path(spec.workdir), FOLLOWUPS)
    engine = WorkflowEngine(EngineSettings(), runner=FakeRunner())

    result = engine.run(engine.prepare(spec), lambda event: None)

    assert [(item.title, item.workflow, item.continue_previous_run) for item in result.followups] == [
        ("Git merge", "merge", False),
        ("Zavřít", "close", False),
    ]


def test_followups_are_empty_after_failure(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"out/pr": "1"})
    write_workflow(Path(spec.workdir), FOLLOWUPS)
    runner = FakeRunner()
    runner.results_by_step["agent"] = ["failed"]
    engine = WorkflowEngine(EngineSettings(), runner=runner)

    result = engine.run(engine.prepare(spec), lambda event: None)

    assert result.status == "failed"
    assert result.followups == []


def test_kubernetes_namespace_is_deleted_after_success_only(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      image: img:1
      deleteNamespace: true
      steps:
        - name: a
          run: a
    """
    ok_result, _events, ok_runner = run_workflow(tmp_path / "ok", text, executor="kubernetes", runner=FakeRunner())
    assert ok_result.status == "success"
    assert ok_runner.deleted_namespaces == ["task-77-test"]

    failing = FakeRunner()
    failing.results_by_step["a"] = ["failed"]
    failed_result, _events, failed_runner = run_workflow(
        tmp_path / "failed", text, executor="kubernetes", runner=failing
    )
    assert failed_result.status == "failed"
    assert failed_runner.deleted_namespaces == []


def test_namespace_cleanup_failure_is_warning_and_keeps_success(tmp_path: Path) -> None:
    class _BrokenDelete(FakeRunner):
        def delete_namespace(self, namespace: str) -> None:
            raise RuntimeError("kubectl unavailable")

    text = "version: 1\nworkflow:\n  deleteNamespace: true\n  image: img:1\n  steps:\n    - name: a\n      run: a\n"
    result, events, _runner = run_workflow(tmp_path, text, executor="kubernetes", runner=_BrokenDelete())

    assert result.status == "success"
    assert result.warnings[0].message == "Deleting namespace task-77-test failed."
    assert result.warnings[0].data == {"namespace": "task-77-test", "error": "kubectl unavailable"}
    assert events[-2]["type"] == "warning"


def test_delete_namespace_is_ignored_by_local_executor(tmp_path: Path) -> None:
    text = "version: 1\nworkflow:\n  deleteNamespace: true\n  steps:\n    - name: a\n      run: a\n"
    result, _events, runner = run_workflow(tmp_path, text, executor="local")
    assert result.status == "success"
    assert runner.deleted_namespaces == []


def test_crash_inside_run_is_reported_as_failed_result(tmp_path: Path) -> None:
    class _CrashingRunner(FakeRunner):
        def prepare(self, workflow: Any, *, namespace: str, run_dir: Path) -> None:
            raise RuntimeError("namespace apply failed")

    text = "version: 1\nworkflow:\n  steps:\n    - name: a\n      run: a\n"
    result, events, _runner = run_workflow(tmp_path, text, runner=_CrashingRunner())

    assert result.status == "failed"
    assert result.warnings[0].message == "Workflow run crashed."
    assert events[-1] == {"type": "workflow_finished", "status": "failed", "failed_step": None}


# ---------------------------------------------------------------------------
# abort
# ---------------------------------------------------------------------------


def test_abort_from_other_thread_stops_running_step_and_skips_outputs(tmp_path: Path) -> None:
    text = """
    version: 1
    workflow:
      steps:
        - name: long
          run: sleep 100
          outputs:
            - type: agent_comment
              bodyFrom: out/comment.md
    """
    spec = make_spec(tmp_path)
    _write_outputs(Path(spec.workdir), {"out/comment.md": "nemá být"})
    write_workflow(Path(spec.workdir), text)
    runner = BlockingRunner()
    engine = WorkflowEngine(EngineSettings(), runner=runner)
    prepared = engine.prepare(spec)
    started = threading.Event()
    original = runner.run_step

    def _signal_started(workflow: Any, step: Any, **kwargs: Any):
        started.set()
        return original(workflow, step, **kwargs)

    runner.run_step = _signal_started  # type: ignore[method-assign]
    results: list[Any] = []
    worker = threading.Thread(target=lambda: results.append(engine.run(prepared, lambda event: None)))
    worker.start()
    assert started.wait(timeout=5)

    cleanup = engine.abort()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert cleanup == "deleted"
    assert runner.aborted == [("task-77-test", {"agentis.task_id": "task-77", "agentis.run_id": "run-12345678"})]
    result = results[0]
    assert result.status == "aborted"
    assert result.outputs.comments == []
    assert json.loads((Path(spec.run_dir) / "result.json").read_text(encoding="utf-8"))["status"] == "aborted"


def test_abort_before_run_makes_run_aborted_without_starting_steps(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    write_workflow(Path(spec.workdir), "version: 1\nworkflow:\n  steps:\n    - name: a\n      run: a\n")
    runner = FakeRunner()
    engine = WorkflowEngine(EngineSettings(), runner=runner)
    prepared = engine.prepare(spec)

    engine.abort()
    result = engine.run(prepared, lambda event: None)

    assert result.status == "aborted"
    assert runner.steps == []


def test_abort_without_active_run_is_noop(tmp_path: Path) -> None:
    assert WorkflowEngine(EngineSettings(), runner=FakeRunner()).abort() is None


def test_abort_by_labels_requires_labels(tmp_path: Path) -> None:
    with pytest.raises(WorkflowSpecError):
        abort_by_labels("local", "ns", {}, EngineSettings())


def test_summary_lists_steps_and_executor(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, executor="kubernetes")
    write_workflow(
        Path(spec.workdir),
        "version: 1\nworkflow:\n  image: img:1\n  steps:\n"
        "    - name: one\n      run: a\n    - name: two\n      run: b\n",
    )
    prepared = WorkflowEngine(EngineSettings(), runner=FakeRunner()).prepare(spec)

    summary = prepared.summary()

    assert summary["protocol"] == 1
    assert summary["executor"] == "kubernetes"
    assert summary["namespace"] == "task-77-test"
    assert summary["steps"] == [
        {"name": "one", "needs": [], "image": "img:1"},
        {"name": "two", "needs": ["one"], "image": "img:1"},
    ]
    assert summary["workflow_file"].endswith("default.yaml")


@pytest.mark.parametrize(
    "error_event",
    [
        {"type": "error", "message": "Provider is temporarily rate-limited."},
        {"type": "error", "data": {"message": "Legacy provider error."}},
    ],
)
def test_step_failed_event_carries_agent_error_from_log(tmp_path: Path, error_event: dict[str, Any]) -> None:
    runner = FakeRunner()
    runner.results_by_step["agent"] = ["failed"]
    runner.log_tail = "\n".join(
        [
            "[agentiscode] command: agentiscode --json",
            "not valid json",
            json.dumps(error_event),
            json.dumps({"type": "result", "is_error": True}),
        ]
    )

    _result, events, _runner = run_workflow(tmp_path, TWO_STEPS, runner=runner)

    expected_error = error_event.get("message") or error_event["data"]["message"]
    failed = next(event for event in events if event["type"] == "step_failed")
    assert failed["error"] == expected_error
    assert failed["log_tail"] == runner.log_tail
