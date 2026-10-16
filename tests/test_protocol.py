"""Testy kontraktu CLI: validace `RunSpec` a tvar `RunResult`, nastavení z env."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentis_workflow.protocol import RunResult, RunSpec
from agentis_workflow.settings import EngineSettings


def _spec(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "protocol": 1,
        "workflow": "default",
        "workdir": "/worktrees/task-12",
        "run_dir": "/worktrees/task-12/.agentis/runs/0193ab",
        "namespace": "task-12-foo",
    }
    data.update(overrides)
    return data


def test_minimal_spec_gets_defaults_and_resolves_run_files_against_run_dir() -> None:
    spec = RunSpec.model_validate(_spec())

    assert spec.search_paths == [".agentis/workflows"]
    assert spec.prompt_file == str(Path("/worktrees/task-12/.agentis/runs/0193ab/prompt.md"))
    assert spec.context_file == str(Path("/worktrees/task-12/.agentis/runs/0193ab/context.json"))
    assert spec.executor is None
    assert spec.require_vars == []


def test_absolute_prompt_file_is_kept() -> None:
    spec = RunSpec.model_validate(_spec(prompt_file="/tmp/p.md"))
    assert spec.prompt_file == "/tmp/p.md"


@pytest.mark.parametrize(
    "overrides",
    [
        {"protocol": 2},
        {"unexpected": True},
        {"executor": "windows"},
        {"executor_default": "nomad"},
        {"namespace": ""},
        {"labels": {"agentis.task_id": "a,agentis.workflow=other"}},
        {"labels": {"bad key!": "x"}},
        {"labels": {"agentis.task_id": "line\nbreak"}},
        {"vars": {"NOT_A_TOKEN": "x"}},
        {"vars": {"NAMESPACE": "x"}},
        {"env": {"BAD-NAME": "x"}},
        {"pass_env": ["1BAD"]},
        {"require_vars": ["APPROVED METADATA"]},
        {"workdir": ""},
    ],
)
def test_spec_validation_is_strict(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunSpec.model_validate(_spec(**overrides))


def test_full_spec_round_trips_known_fields() -> None:
    spec = RunSpec.model_validate(
        _spec(
            executor="docker",
            executor_default="kubernetes",
            labels={"agentis.task_id": "task-77", "agentis.run_id": "run-1"},
            vars={"TASK_TITLE": "Titulek", "BRANCH": "task-77"},
            env={"AGENTIS_MODEL": "m"},
            pass_env=["AGENTIS_SERVICE_TOKEN"],
            require_vars=["APPROVED"],
        )
    )

    assert spec.executor == "docker"
    assert spec.labels["agentis.task_id"] == "task-77"
    assert spec.pass_env == ["AGENTIS_SERVICE_TOKEN"]


def test_run_result_serializes_to_documented_shape() -> None:
    result = RunResult(status="success")

    assert result.model_dump(mode="json") == {
        "protocol": 1,
        "status": "success",
        "failed_step": None,
        "vars": {},
        "outputs": {"comments": [], "session_id": None, "attachments": [], "artifacts": []},
        "followups": [],
        "warnings": [],
    }


def test_settings_from_env_defaults_and_overrides() -> None:
    assert EngineSettings.from_env({}) == EngineSettings(
        kubectl_command="kubectl", docker_command="docker", workflow_executor="kubernetes"
    )
    settings = EngineSettings.from_env(
        {"KUBECTL_COMMAND": " /opt/kubectl ", "DOCKER_COMMAND": "podman", "WORKFLOW_EXECUTOR": " Local "}
    )
    assert settings.kubectl_command == "/opt/kubectl"
    assert settings.docker_command == "podman"
    assert settings.workflow_executor == "local"


def test_settings_reject_unknown_executor() -> None:
    with pytest.raises(ValueError, match="Unknown workflow executor"):
        EngineSettings.from_env({"WORKFLOW_EXECUTOR": "nomad"})
