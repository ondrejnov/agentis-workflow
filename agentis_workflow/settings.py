"""Nastavení enginu načtené z prostředí procesu.

Engine nečte `.env` ani nic z adaptéru — hodnoty přicházejí jen z env procesu (`KUBECTL_COMMAND`,
`DOCKER_COMMAND`, `WORKFLOW_EXECUTOR`) nebo přímo z volajícího kódu.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from agentis_workflow.schema import WORKFLOW_EXECUTORS


@dataclass(frozen=True)
class EngineSettings:
    """Provozní nastavení runtimů; default executor platí, když spec ani YAML executor nevyberou."""

    kubectl_command: str = "kubectl"
    docker_command: str = "docker"
    workflow_executor: str = "kubernetes"

    def __post_init__(self) -> None:
        if self.workflow_executor not in WORKFLOW_EXECUTORS:
            raise ValueError(
                f"Unknown workflow executor {self.workflow_executor!r}; expected one of {WORKFLOW_EXECUTORS}"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EngineSettings:
        env = os.environ if environ is None else environ
        return cls(
            kubectl_command=(env.get("KUBECTL_COMMAND") or "").strip() or "kubectl",
            docker_command=(env.get("DOCKER_COMMAND") or "").strip() or "docker",
            workflow_executor=(env.get("WORKFLOW_EXECUTOR") or "").strip().lower() or "kubernetes",
        )
