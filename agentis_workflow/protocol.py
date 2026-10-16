"""Kontrakt CLI enginu: vstupní `spec.json` (RunSpec) a výsledek `result.json` (RunResult).

Volající (adapter nebo ruční použití) zapíše spec, engine ho přečte a výsledek vrátí jako
JSON. Modely jsou strict (`extra="forbid"`), aby překlepy ve spec nepřešly tiše do runu.
Secrety do spec nepatří — předávají se přes `pass_env` z env procesu.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentis_workflow.schema import INTERPOLATION_ALLOWLIST

#: Verze kontraktu spec/result; adapter ji ověří před spuštěním a engine ji vrací v každém výstupu.
PROTOCOL_VERSION = 1

#: Interpolační tokeny, které spec nastavuje sám (z `namespace`, `workdir`, `run_dir`) — ve `vars` nejsou.
_RESERVED_VAR_TOKENS = frozenset({"NAMESPACE", "WORKDIR", "RUN_DIR"})

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LABEL_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,252}")
_LABEL_VALUE_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?)?")
_NAMESPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")

Executor = Literal["kubernetes", "docker", "local"]


def validate_labels(value: Mapping[str, str]) -> dict[str, str]:
    """Labely jdou do kubectl/docker selektorů; mimo tuto gramatiku by mohly přidat další podmínky."""

    for key, item in value.items():
        if not _LABEL_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid label key {key!r}")
        if not _LABEL_VALUE_RE.fullmatch(item):
            raise ValueError(f"invalid value for label {key!r}")
    return dict(value)


def validate_namespace(value: str) -> str:
    if not _NAMESPACE_RE.fullmatch(value):
        raise ValueError(f"invalid namespace {value!r}")
    return value


class RunSpec(BaseModel):
    """Vstup enginu (`spec.json`). Relativní `prompt_file` / `context_file` se resolvují vůči `run_dir`."""

    model_config = ConfigDict(extra="forbid")

    protocol: Literal[1]
    #: Jméno workflow (basename bez `.yaml`) nebo cesta k souboru; jméno se hledá v `search_paths`.
    workflow: str = Field(min_length=1)
    #: Adresáře s workflow soubory, v pořadí priority; relativní cesty jsou vůči `workdir`.
    search_paths: list[str] = Field(default_factory=lambda: [".agentis/workflows"])
    workdir: str = Field(min_length=1)
    run_dir: str = Field(min_length=1)
    #: Kořen, vůči kterému se čtou `bodyFrom` / `valueFrom` / artifact globy; default `workdir`.
    output_root: str | None = None
    prompt_file: str = "prompt.md"
    context_file: str = "context.json"
    #: Vynucený executor (má přednost před YAML); None = rozhodne YAML, pak `executor_default`.
    executor: Executor | None = None
    executor_default: Executor | None = None
    namespace: str = Field(min_length=1)
    #: Labely jobů/kontejnerů; podle nich se hledají aktivní běhy (busy) a ruší se (abort).
    labels: dict[str, str] = Field(default_factory=dict)
    #: Hodnoty interpolačních tokenů `[%NAME%]` (allowlist ze schema); chybějící = prázdný string.
    vars: dict[str, str] = Field(default_factory=dict)
    #: Nesecretní env pro kroky.
    env: dict[str, str] = Field(default_factory=dict)
    #: Jména env proměnných, jejichž hodnoty se vezmou z env procesu enginu (secrety).
    pass_env: list[str] = Field(default_factory=list)
    #: Názvy `var` outputů, které musí workflow deklarovat (jednou) — např. `APPROVED`.
    require_vars: list[str] = Field(default_factory=list)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_labels(value)

    @field_validator("vars")
    @classmethod
    def validate_vars(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if name not in INTERPOLATION_ALLOWLIST:
                raise ValueError(f"unknown interpolation token {name!r}")
            if name in _RESERVED_VAR_TOKENS:
                raise ValueError(f"interpolation token {name!r} is set by the engine, not by vars")
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if not _ENV_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid env name {name!r}")
        return value

    @field_validator("pass_env", "require_vars")
    @classmethod
    def validate_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _ENV_NAME_RE.fullmatch(name):
                raise ValueError(f"invalid name {name!r}; expected [A-Za-z_][A-Za-z0-9_]*")
        return value

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        return validate_namespace(value)

    @model_validator(mode="after")
    def resolve_run_files(self) -> RunSpec:
        run_dir = Path(self.run_dir)
        self.prompt_file = str(_under(run_dir, self.prompt_file))
        self.context_file = str(_under(run_dir, self.context_file))
        return self


def _under(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


class RunComment(BaseModel):
    body: str
    status: int | None = None
    author_name: str | None = None


class RunAttachment(BaseModel):
    label: str
    value: str
    type: Literal["url", "text"]


class RunArtifact(BaseModel):
    name: str
    filename: str
    #: Cesta k zkopírovanému souboru, relativně k `run_dir` (`artifact-staging/...`).
    path: str
    size: int


class RunOutputs(BaseModel):
    comments: list[RunComment] = Field(default_factory=list)
    session_id: str | None = None
    attachments: list[RunAttachment] = Field(default_factory=list)
    artifacts: list[RunArtifact] = Field(default_factory=list)


class RunFollowup(BaseModel):
    title: str
    prompt: str = ""
    workflow: str
    continue_previous_run: bool = False


class RunWarning(BaseModel):
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    """Výsledek runu (`result.json`). Outputs a followups pocházejí jen z úspěšných, nepřeskočených kroků."""

    protocol: Literal[1] = PROTOCOL_VERSION
    status: Literal["success", "failed", "aborted"]
    failed_step: str | None = None
    vars: dict[str, str] = Field(default_factory=dict)
    outputs: RunOutputs = Field(default_factory=RunOutputs)
    followups: list[RunFollowup] = Field(default_factory=list)
    warnings: list[RunWarning] = Field(default_factory=list)


__all__ = [
    "PROTOCOL_VERSION",
    "RunArtifact",
    "RunAttachment",
    "RunComment",
    "RunFollowup",
    "RunOutputs",
    "RunResult",
    "RunSpec",
    "RunWarning",
    "validate_labels",
    "validate_namespace",
]
