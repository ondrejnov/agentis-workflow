"""Testy schématu workflow YAML: parsování, interpolace, podmínky, `extends`, šablony kroků a `agent:` kroky."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentis_workflow.schema import (
    COMMENT_STATUS_ALIASES,
    WORKFLOW_FILE_RELPATH,
    WorkflowConditionError,
    WorkflowExtendsError,
    WorkflowFile,
    WorkflowInterpolationError,
    evaluate_condition,
    interpolate_tokens,
    load_workflow_file,
    load_workflow_followups,
)
from pydantic import ValidationError


WORKFLOW_YAML = """
version: 1
workflow:
  followups:
    - title: Git merge
      prompt: Sloučit změny z task větve do hlavní větve.
      workflow: merge
    - title: Zavřít prostředí
      prompt: Uklidit prostředí, worktree a task větev.
      workflow: close
  image: registry.example/agent:1.0
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 120
  ttlSecondsAfterFinished: 60
  env:
    HOME: /root
    IS_SANDBOX: 1
    MAIN_DIR: "[%MAIN_DIR%]"
  mounts:
    - name: www
      mountPath: /var/www
      hostPath:
        path: /var/www
  steps:
    - name: Run agent
      run: |
        mkdir -p .agentis/outputs
        agentiscode < "$AGENTIS_PROMPT_FILE"
      outputs:
        - type: agent_comment
          bodyFrom: .agentis/outputs/final-comment.md
          status: in_review
          nameFrom: .agentis/outputs/agent-name
        - type: session_id
          valueFrom: .agentis/outputs/session-id
    - name: Create pull request
      image: registry.example/other:2.0
      timeoutSeconds: 30
      run: echo hotovo
      outputs:
        - type: url
          label: Pull Request
          valueFrom: .agentis/outputs/pull-request-url
"""


PROJECT_WORKFLOW_YAML = """
version: 1
workflow:
  image: registry.example/agent:1.0
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 120
  steps:
    - name: Run agent
      run: |
        mkdir -p "$AGENTIS_RUN_DIR/outputs"
        agentiscode < "$AGENTIS_PROMPT_FILE"
      outputs:
        - type: agent_comment
          bodyFrom: outputs/final-comment.md
          status: 5
        - type: session_id
          valueFrom: outputs/session-id
"""


CONDITIONAL_WORKFLOW_YAML = """
version: 1
workflow:
  image: registry.example/agent:1.0
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 120
  steps:
    - name: Check environment
      run: check-env
      outputs:
        - type: var
          name: ENV_READY
          valueFrom: .agentis/outputs/env-ready
    - name: Install dependencies
      if: ENV_READY != 'true'
      run: poetry install
      outputs:
        - type: url
          label: Install Log
          valueFrom: .agentis/outputs/install-log-url
    - name: Run agent
      run: agentiscode < "$AGENTIS_PROMPT_FILE"
      outputs:
        - type: agent_comment
          bodyFrom: .agentis/outputs/final-comment.md
          status: 4
"""


def _write_workflow(worktree: Path) -> Path:
    path = worktree / WORKFLOW_FILE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKFLOW_YAML, encoding="utf-8")
    return path


def _values(worktree: Path) -> dict[str, str]:
    return {
        "NAMESPACE": "task-77-test",
        "WORKDIR": str(worktree),
        "MAIN_DIR": "/var/www/project",
        "RUN_ID": "run-12345678",
        "TASK_ID": "task-77",
        "TASK_NUMBER": "77",
        "TASK_TITLE": "Test task",
        "BRANCH": "task-task-77",
        "BASE_BRANCH": "master",
        "GITHUB_REPO": "org/repo",
    }


def test_workflow_schema_parses_and_interpolates(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    workflow = load_workflow_file(tmp_path / WORKFLOW_FILE_RELPATH, _values(tmp_path))

    assert isinstance(workflow, WorkflowFile)
    spec = workflow.workflow
    assert spec.workingDir == str(tmp_path)
    assert spec.env["IS_SANDBOX"] == "1"  # env hodnoty se koercují na string
    assert spec.env["MAIN_DIR"] == "/var/www/project"
    assert [step.name for step in spec.steps] == ["Run agent", "Create pull request"]
    assert spec.steps[1].image == "registry.example/other:2.0"
    assert spec.steps[0].outputs[0].type == "agent_comment"
    assert spec.steps[0].outputs[0].status == 4  # alias `in_review` se mapuje na číslo
    assert spec.maxParallel == 4
    assert spec.mounts[0].volume_mount() == {"name": "www", "mountPath": "/var/www"}
    assert spec.mounts[0].volume() == {"name": "www", "hostPath": {"path": "/var/www"}}


def test_workflow_schema_accepts_optional_kubernetes_context(tmp_path: Path) -> None:
    path = tmp_path / "context.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  context: microk8s\n  image: x\n  steps:\n    - name: a\n      run: echo\n",
        encoding="utf-8",
    )

    assert load_workflow_file(path, _values(tmp_path)).workflow.context == "microk8s"


def test_workflow_schema_rejects_mount_without_volume_source(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  mounts:\n"
        "    - name: cache\n      mountPath: /cache\n"
        "  steps:\n    - name: a\n      run: echo\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="requires a Kubernetes volume source"):
        load_workflow_file(path, _values(tmp_path))


def test_workflow_schema_accepts_needs_and_max_parallel(tmp_path: Path) -> None:
    path = tmp_path / "parallel.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  maxParallel: 2\n  steps:\n"
        "    - name: a\n      run: echo a\n"
        "    - name: b\n      needs: []\n      run: echo b\n"
        "    - name: c\n      needs: [a, b]\n      run: echo c\n",
        encoding="utf-8",
    )

    workflow = load_workflow_file(path, _values(tmp_path))

    assert workflow.workflow.maxParallel == 2
    assert workflow.workflow.steps[0].needs is None
    assert workflow.workflow.steps[1].needs == []
    assert workflow.workflow.steps[2].needs == ["a", "b"]


def test_workflow_schema_rejects_future_needs_and_duplicate_names(tmp_path: Path) -> None:
    future = tmp_path / "future.yaml"
    future.write_text(
        "version: 1\nworkflow:\n  image: x\n  steps:\n"
        "    - name: a\n      needs: [b]\n      run: echo a\n"
        "    - name: b\n      run: echo b\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="unknown or future 'needs'"):
        load_workflow_file(future, _values(tmp_path))

    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(
        "version: 1\nworkflow:\n  image: x\n  steps:\n"
        "    - name: a\n      run: echo a\n"
        "    - name: a\n      needs: []\n      run: echo b\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="unique step names"):
        load_workflow_file(duplicate, _values(tmp_path))


def test_comment_status_aliases_map_to_agentis_enum() -> None:
    assert COMMENT_STATUS_ALIASES == {
        "backlog": 1,
        "todo": 2,
        "in_progress": 3,
        "in_review": 4,
        "done": 5,
        "cancelled": 6,
        "blocked": 7,
    }


def test_workflow_schema_rejects_unknown_status_alias(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  steps:\n"
        "    - name: a\n      run: echo\n      outputs:\n"
        "        - type: agent_comment\n          bodyFrom: out.md\n          status: closed\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="Unknown comment status alias 'closed'"):
        load_workflow_file(path, _values(tmp_path))


def test_workflow_schema_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  parallel: true\n  steps:\n    - name: a\n      run: echo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_workflow_file(path, _values(tmp_path))


def test_workflow_schema_accepts_logical_condition(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  steps:\n"
        "    - name: a\n      if: GITHUB_REPO && ENV_READY != 'true' || FORCE\n      run: echo\n",
        encoding="utf-8",
    )
    workflow = load_workflow_file(path, _values(tmp_path))
    assert workflow.workflow.steps[0].if_ == "GITHUB_REPO && ENV_READY != 'true' || FORCE"


def test_workflow_schema_parses_if_and_var_outputs(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(CONDITIONAL_WORKFLOW_YAML, encoding="utf-8")
    workflow = load_workflow_file(path, _values(tmp_path))

    steps = workflow.workflow.steps
    assert steps[0].if_ is None
    assert steps[0].outputs[0].type == "var"
    assert steps[0].outputs[0].name == "ENV_READY"
    assert steps[1].if_ == "ENV_READY != 'true'"


def test_workflow_schema_rejects_invalid_condition_and_var_output(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  steps:\n    - name: a\n      if: 'A &&'\n      run: echo\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_workflow_file(path, _values(tmp_path))

    path.write_text(
        "version: 1\nworkflow:\n  image: x\n  steps:\n"
        "    - name: a\n      run: echo\n      outputs:\n        - type: var\n          valueFrom: out\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_workflow_file(path, _values(tmp_path))


FOLLOWUPS_WITH_CONDITION_YAML = """
version: 1
workflow:
  image: x
  followups:
    - title: Git merge
      if: PR_CREATED
      workflow: merge
    - title: Zavřít prostředí
      workflow: close
  steps:
    - name: a
      run: echo
"""


def test_followup_invalid_condition_fails_full_load_but_followups_loader_swallows(tmp_path: Path) -> None:
    path = tmp_path / "ci.yaml"
    path.write_text(FOLLOWUPS_WITH_CONDITION_YAML.replace("PR_CREATED", "PR_CREATED &&"), encoding="utf-8")

    # plné načtení (start workflow) je chyba ...
    with pytest.raises(ValidationError):
        load_workflow_file(path, _values(tmp_path))
    # ... best-effort loader lokálních sessions ji dál polyká
    assert load_workflow_followups(path) == []


def test_load_workflow_followups_skips_conditional_actions(tmp_path: Path) -> None:
    # lokální sessions nemají runtime env ani `var` outputs runu — followup s `if` se konzervativně nenabízí
    path = tmp_path / "ci.yaml"
    path.write_text(FOLLOWUPS_WITH_CONDITION_YAML, encoding="utf-8")

    workflow = load_workflow_file(path, _values(tmp_path))
    assert [followup.if_ for followup in workflow.workflow.followups] == ["PR_CREATED", None]

    assert [followup.title for followup in load_workflow_followups(path)] == ["Zavřít prostředí"]


# ---------------------------------------------------------------------------
# Dědičnost přes `extends`
# ---------------------------------------------------------------------------


BASE_YAML = """
version: 1
workflow:
  image: registry.example/agent:1.0
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 600
  envFiles:
    - /root/.config/agentis/agentis.env
  env:
    HOME: /root
    SHARED: base
  imagePullSecrets:
    - name: registry
  mounts:
    - name: www
      mountPath: /var/www
      hostPath:
        path: /var/www
    - name: config
      mountPath: /root/.config
      readOnly: true
      hostPath:
        path: /root/.config
  followups:
    - title: Nesmí se dědit
      workflow: merge
"""

CHILD_YAML = """
version: 1
extends: _base
workflow:
  timeoutSeconds: 60
  envFiles:
    - /root/.config/agentis/agentis.env
    - /etc/child.env
  env:
    SHARED: child
    EXTRA: "1"
  mounts:
    - name: config
      mountPath: /root/.config
      hostPath:
        path: /root/.config
    - name: cache
      mountPath: /root/.cache
      hostPath:
        path: /root/.cache
  steps:
    - name: Run agent
      run: agentiscode
"""


def _write_extends_pair(tmp_path: Path, child_yaml: str = CHILD_YAML, base_yaml: str = BASE_YAML) -> Path:
    workflows = tmp_path / ".agentis/workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "_base.yaml").write_text(base_yaml, encoding="utf-8")
    child_path = workflows / "child.yaml"
    child_path.write_text(child_yaml, encoding="utf-8")
    return child_path


def test_extends_merges_scalars_env_and_lists(tmp_path: Path) -> None:
    child_path = _write_extends_pair(tmp_path)
    workflow = load_workflow_file(child_path, _values(tmp_path))
    spec = workflow.workflow

    assert spec.image == "registry.example/agent:1.0"  # skalár zděděný z base
    assert spec.timeoutSeconds == 60  # skalár přepsaný potomkem
    assert spec.workingDir == str(tmp_path)  # interpolace běží až po merge
    assert spec.env == {"HOME": "/root", "SHARED": "child", "EXTRA": "1"}
    assert spec.envFiles == ["/root/.config/agentis/agentis.env", "/etc/child.env"]  # dedupe + append
    assert spec.imagePullSecrets == [{"name": "registry"}]
    # mounts: stejné `name` potomek přepisuje na místě, nové se přidává na konec
    assert [mount.model_dump(exclude_none=True) for mount in spec.mounts] == [
        {"name": "www", "mountPath": "/var/www", "hostPath": {"path": "/var/www"}},
        {"name": "config", "mountPath": "/root/.config", "hostPath": {"path": "/root/.config"}},
        {"name": "cache", "mountPath": "/root/.cache", "hostPath": {"path": "/root/.cache"}},
    ]


def test_extends_never_inherits_steps_and_followups(tmp_path: Path) -> None:
    child_path = _write_extends_pair(tmp_path)
    workflow = load_workflow_file(child_path, _values(tmp_path))

    assert [step.name for step in workflow.workflow.steps] == ["Run agent"]
    assert workflow.workflow.followups == []  # followups z base se nedědí


def test_extends_base_is_not_runnable_standalone(tmp_path: Path) -> None:
    _write_extends_pair(tmp_path)
    with pytest.raises(ValidationError):  # base nemá steps
        load_workflow_file(tmp_path / ".agentis/workflows/_base.yaml", _values(tmp_path))


def test_extends_missing_target_raises_file_not_found_with_path(tmp_path: Path) -> None:
    child_path = _write_extends_pair(tmp_path)
    (tmp_path / ".agentis/workflows/_base.yaml").unlink()
    with pytest.raises(FileNotFoundError, match=r"_base\.yaml"):
        load_workflow_file(child_path, _values(tmp_path))


def test_extends_rejects_chaining_and_self_reference(tmp_path: Path) -> None:
    child_path = _write_extends_pair(tmp_path, base_yaml="extends: child\n" + BASE_YAML.lstrip())
    with pytest.raises(WorkflowExtendsError, match="chained 'extends'"):
        load_workflow_file(child_path, _values(tmp_path))

    child_path.write_text(CHILD_YAML.replace("extends: _base", "extends: child"), encoding="utf-8")
    with pytest.raises(WorkflowExtendsError, match="extend itself"):
        load_workflow_file(child_path, _values(tmp_path))


def test_workflow_without_extends_is_unchanged(tmp_path: Path) -> None:
    _write_workflow(tmp_path)
    workflow = load_workflow_file(tmp_path / WORKFLOW_FILE_RELPATH, _values(tmp_path))
    assert workflow.extends is None
    assert [step.name for step in workflow.workflow.steps] == ["Run agent", "Create pull request"]


TEMPLATE_BASE_YAML = """
version: 1
workflow:
  image: registry.example/agent:1.0
  stepTemplates:
    run-agent:
      timeoutSeconds: 900
      env:
        FLAGS: --json
      run: agentiscode $FLAGS
      outputs:
        - type: session_id
          valueFrom: outputs/session-id
"""

TEMPLATE_CHILD_YAML = """
version: 1
extends: _base
workflow:
  steps:
    - name: Run agent
      uses: run-agent
    - name: Run agent tuned
      uses: run-agent
      timeoutSeconds: 60
      env:
        FLAGS: ""
        EXTRA: "1"
      outputs:
        - type: agent_comment
          bodyFrom: outputs/final-comment.md
          status: 4
"""


def test_step_template_fills_defaults_and_step_overrides(tmp_path: Path) -> None:
    child_path = _write_extends_pair(tmp_path, child_yaml=TEMPLATE_CHILD_YAML, base_yaml=TEMPLATE_BASE_YAML)
    steps = load_workflow_file(child_path, _values(tmp_path)).workflow.steps

    plain, tuned = steps
    assert plain.run == "agentiscode $FLAGS"  # run dodala šablona
    assert plain.timeoutSeconds == 900
    assert plain.env == {"FLAGS": "--json"}
    assert [output.type for output in plain.outputs] == ["session_id"]

    assert tuned.run == "agentiscode $FLAGS"
    assert tuned.timeoutSeconds == 60  # pole deklarované krokem vyhrává
    assert tuned.env == {"FLAGS": "", "EXTRA": "1"}  # env merge po klíčích, krok vyhrává
    assert [output.type for output in tuned.outputs] == ["agent_comment"]  # outputs krok přepisuje celé


def test_step_templates_inherited_and_overridable_by_child(tmp_path: Path) -> None:
    child_yaml = TEMPLATE_CHILD_YAML.replace(
        "workflow:\n",
        "workflow:\n  stepTemplates:\n    run-agent:\n      run: child-agent\n",
        1,
    )
    child_path = _write_extends_pair(tmp_path, child_yaml=child_yaml, base_yaml=TEMPLATE_BASE_YAML)
    steps = load_workflow_file(child_path, _values(tmp_path)).workflow.steps

    # Potomek přepsal celou šablonu (žádný deep-merge): env/outputs z base šablony zmizely.
    assert steps[0].run == "child-agent"
    assert steps[0].env == {}
    assert steps[0].outputs == []


def test_step_uses_unknown_template_is_validation_error(tmp_path: Path) -> None:
    child_yaml = TEMPLATE_CHILD_YAML.replace("uses: run-agent", "uses: missing")
    child_path = _write_extends_pair(tmp_path, child_yaml=child_yaml, base_yaml=TEMPLATE_BASE_YAML)
    with pytest.raises(ValidationError, match="unknown step template 'missing'"):
        load_workflow_file(child_path, _values(tmp_path))


def test_step_without_run_and_uses_is_validation_error(tmp_path: Path) -> None:
    child_yaml = TEMPLATE_CHILD_YAML.replace("      uses: run-agent\n", "", 1)
    child_path = _write_extends_pair(tmp_path, child_yaml=child_yaml, base_yaml=TEMPLATE_BASE_YAML)
    with pytest.raises(ValidationError, match="requires 'run'"):
        load_workflow_file(child_path, _values(tmp_path))


def test_evaluate_condition_truthiness_negation_and_comparison() -> None:
    assert evaluate_condition("READY", {"READY": "1"})
    assert evaluate_condition("READY", {"READY": "yes"})
    assert not evaluate_condition("READY", {"READY": "false"})
    assert not evaluate_condition("READY", {"READY": "0"})
    assert not evaluate_condition("READY", {})
    assert evaluate_condition("!READY", {})
    assert not evaluate_condition("!READY", {"READY": "true"})
    assert evaluate_condition("MODE == 'fast'", {"MODE": "fast"})
    assert evaluate_condition('MODE == "a b"', {"MODE": "a b"})
    assert evaluate_condition("MODE == fast", {"MODE": "fast"})
    assert evaluate_condition("MODE != 'fast'", {"MODE": "slow"})
    assert not evaluate_condition("MODE != 'fast'", {"MODE": "fast"})
    # mezery kolem hodnoty ze souboru ořezává manager, ale i tak: neznámá proměnná == prázdno
    assert evaluate_condition("MODE != 'fast'", {})
    with pytest.raises(WorkflowConditionError):
        evaluate_condition("A ==", {})
    with pytest.raises(WorkflowConditionError):
        evaluate_condition("!A == 'x'", {})


def test_evaluate_condition_logical_operators_and_precedence() -> None:
    variables = {"A": "1", "B": "yes", "F": "0"}
    assert evaluate_condition("A && B", variables)
    assert not evaluate_condition("A && F", variables)
    assert evaluate_condition("F || A", variables)
    assert not evaluate_condition("F || MISSING", variables)  # neznámá proměnná = prázdný string
    # `&&` má přednost před `||`: `A && B || C` ≡ `(A && B) || C`
    assert evaluate_condition("A && F || B", variables)
    assert not evaluate_condition("A && F || F", variables)
    assert evaluate_condition("F || A && B", variables)
    assert not evaluate_condition("F || A && F", variables)
    # negace a porovnání jako termy spojek
    assert evaluate_condition("!F && A", variables)
    assert evaluate_condition("A == 1 && B != 'no'", variables)
    assert evaluate_condition("MODE == 'a && b' || F", {"MODE": "a && b"})  # spojka v quoted hodnotě
    assert evaluate_condition("A&&B", variables)  # spojky fungují i bez mezer


def test_parse_condition_rejects_malformed_logical_expressions() -> None:
    for expression in ("", "A &&", "&& B", "A || ", "A & B", "A | B", "(A) && B", "A !B", "!A == 'x' && B"):
        with pytest.raises(WorkflowConditionError):
            evaluate_condition(expression, {})


def test_interpolation_replaces_allowlisted_tokens_and_rejects_unknown() -> None:
    values = {"WORKDIR": "/w", "TASK_TITLE": "Titulek"}
    assert interpolate_tokens("cd [%WORKDIR%] # [%TASK_TITLE%]", values) == "cd /w # Titulek"
    assert interpolate_tokens({"a": ["[%WORKDIR%]"]}, values) == {"a": ["/w"]}
    with pytest.raises(WorkflowInterpolationError):
        interpolate_tokens("[%EVIL_TOKEN%]", values)


@pytest.mark.parametrize("extends", ["../outside", "sub/base", "base.yaml"])
def test_extends_rejects_paths_outside_workflow_directory(tmp_path: Path, extends: str) -> None:
    workflows = tmp_path / ".agentis/workflows"
    workflows.mkdir(parents=True)
    child = workflows / "child.yaml"
    child.write_text(
        f"version: 1\nextends: {extends!r}\nworkflow:\n  steps:\n    - name: a\n      run: a\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowExtendsError):
        load_workflow_file(child, {})
