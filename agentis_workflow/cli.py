"""Příkazová řádka `agentis-workflow`: `run`, `check`, `validate`, `list`, `abort`.

Jediný kontrakt mezi projekty (viz docs/cli.md). Stdout patří strojovému výstupu (JSONL, JSON z `check`
a `validate --json`) nebo lidskému průběhu v console režimu; diagnostika jde na stderr. Návratové kódy jsou
v konstantách `EXIT_*`.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError

from agentis_workflow import __version__
from agentis_workflow.console import ConsoleSink, render_header, render_summary
from agentis_workflow.engine import (
    PreparedRun,
    WorkflowBusyError,
    WorkflowEngine,
    WorkflowSpecError,
    abort_by_labels,
)
from agentis_workflow.protocol import PROTOCOL_VERSION, RunResult, RunSpec, validate_labels, validate_namespace
from agentis_workflow.schema import (
    INTERPOLATION_ALLOWLIST,
    WORKFLOW_DIR_RELPATH,
    WORKFLOW_EXECUTORS,
    WorkflowFile,
    load_workflow_file,
)
from agentis_workflow.settings import EngineSettings

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_BUSY = 3
EXIT_SIGINT = 130
EXIT_SIGTERM = 143

RUNS_DIR = ".agentis/runs"
PROMPT_FILE_NAME = "prompt.md"
CONTEXT_FILE_NAME = "context.json"
_MANUAL_ONLY_FLAGS = {
    "workflow": "NAME",
    "prompt": "PROMPT",
    "prompt_file": "--prompt-file",
    "search_path": "--search-path",
    "run_dir": "--run-dir",
    "var": "--var",
    "env": "--env",
    "pass_env": "--pass-env",
    "executor": "--executor",
    "namespace": "--namespace",
}


class CliError(Exception):
    """Chyba vstupu nebo použití; vypíše se na stderr bez tracebacku a skončí kódem `code`."""

    def __init__(self, message: str, code: int = EXIT_INVALID) -> None:
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------
# Entrypoint a parser
# ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse ukončuje `--version`, `--help` i usage chyby (kód 2) výjimkou; vrátíme kód.
        return exc.code if isinstance(exc.code, int) else EXIT_INVALID
    try:
        return int(args.handler(args))
    except WorkflowBusyError as exc:
        _error(f"busy: {exc}")
        return EXIT_BUSY
    except CliError as exc:
        _error(str(exc))
        return exc.code
    except (WorkflowSpecError, ValueError) as exc:
        _error(str(exc))
        return EXIT_INVALID
    except (RuntimeError, OSError) as exc:
        _error(f"error: {exc}")
        return EXIT_FAILED
    except KeyboardInterrupt:
        return EXIT_SIGINT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentis-workflow",
        description="Deklarativní YAML workflow engine (local, docker, kubernetes).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"agentis-workflow {__version__} (protocol {PROTOCOL_VERSION})",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser("run", help="spustí workflow ze spec.json nebo ručně podle jména/cesty")
    run.add_argument("workflow", nargs="?", help="jméno workflow (hledá se v search paths) nebo cesta k YAML")
    run.add_argument("prompt", nargs="?", help="zadání pro prompt.md; bez něj se čte stdin, pokud není TTY")
    run.add_argument("--spec", type=Path, metavar="FILE", help="spec.json (strojový běh, bez ručních flagů)")
    run.add_argument(
        "--events",
        choices=("jsonl", "console", "none"),
        help="průběh: jsonl na stdout; console = lidský výpis; none = nic (default: jsonl se --spec, jinak console)",
    )
    run.add_argument("--verbose", action="store_true", help="vypíše celý log každého kroku")
    run.add_argument("--prompt-file", metavar="PATH", help="zadání ze souboru ('-' = stdin)")
    run.add_argument("--workdir", default=".", metavar="DIR", help="pracovní adresář (default: aktuální)")
    run.add_argument(
        "--search-path",
        action="append",
        default=[],
        metavar="DIR",
        help="další adresář s workflow (opakovatelné; po <workdir>/.agentis/workflows, před <workdir>)",
    )
    run.add_argument("--run-dir", metavar="DIR", help="run adresář (default: <workdir>/.agentis/runs/<run_id>)")
    run.add_argument("--var", action="append", default=[], type=_key_value, metavar="NAME=VALUE")
    run.add_argument("--env", action="append", default=[], type=_key_value, metavar="NAME=VALUE")
    run.add_argument("--pass-env", action="append", default=[], metavar="NAME", help="secret z env procesu")
    run.add_argument("--executor", choices=WORKFLOW_EXECUTORS, help="vynucený executor")
    run.add_argument("--namespace", help="namespace (kubernetes); default vygenerovaný z názvu runu")
    run.set_defaults(handler=_cmd_run)

    check = commands.add_parser("check", help="ověří spec.json bez spuštění kroků")
    check.add_argument("--spec", required=True, type=Path, metavar="FILE")
    check.set_defaults(handler=_cmd_check)

    validate = commands.add_parser("validate", help="zkontroluje workflow YAML (soubory nebo adresáře)")
    validate.add_argument("paths", nargs="*", metavar="PATH", help="soubor nebo adresář (default: .agentis/workflows)")
    validate.add_argument("--json", action="store_true", help="výstup jako JSON")
    validate.set_defaults(handler=_cmd_validate)

    listing = commands.add_parser("list", help="vypíše workflow v <workdir>/.agentis/workflows")
    listing.add_argument("--workdir", default=".", metavar="DIR")
    listing.set_defaults(handler=_cmd_list)

    abort = commands.add_parser("abort", help="zruší joby, kontejnery nebo procesy podle labelů")
    abort.add_argument("--executor", required=True, choices=WORKFLOW_EXECUTORS)
    abort.add_argument("--namespace", default="", help="povinný pro kubernetes")
    abort.add_argument("--label", action="append", default=[], type=_key_value, metavar="KEY=VALUE", required=True)
    abort.set_defaults(handler=_cmd_abort)

    return parser


def _key_value(text: str) -> tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"expected NAME=VALUE, got {text!r}")
    return name, value


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    if args.spec is not None:
        _reject_manual_flags(args)
        spec = _read_spec(args.spec)
        return _execute(spec, events=args.events or "jsonl", verbose=args.verbose)
    return _run_manual(args)


def _run_manual(args: argparse.Namespace) -> int:
    if args.workflow is None:
        raise CliError("run requires a workflow name or path (or --spec FILE)")
    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        raise CliError(f"workdir does not exist or is not a directory: {workdir}")
    prompt = _read_prompt(args)

    run_id = f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = Path(args.run_dir).expanduser().resolve() if args.run_dir else workdir / RUNS_DIR / run_id
    if _is_path_reference(args.workflow):
        workflow = str(Path(args.workflow).expanduser().resolve())
        name = Path(args.workflow).stem
    else:
        workflow = name = args.workflow
    search_paths = [
        str(workdir / WORKFLOW_DIR_RELPATH),
        *(str(Path(path).expanduser().resolve()) for path in args.search_path),
        str(workdir),
    ]
    spec = _build_spec(
        {
            "protocol": PROTOCOL_VERSION,
            "workflow": workflow,
            "search_paths": search_paths,
            "workdir": str(workdir),
            "run_dir": str(run_dir),
            "executor": args.executor,
            "namespace": args.namespace or _default_namespace(name, run_id),
            "labels": {"agentis.run_id": run_id, "agentis.task_id": f"manual-{run_id}"},
            "vars": dict(args.var),
            "env": dict(args.env),
            "pass_env": args.pass_env,
        }
    )
    return _execute(spec, events=args.events or "console", verbose=args.verbose, prompt=prompt)


def _execute(
    spec: RunSpec,
    *,
    events: str,
    verbose: bool,
    prompt: str | None = None,
) -> int:
    """Připraví a spustí run; `prompt` (ruční běh) zapíše do run dir až po úspěšném `prepare`."""

    engine = _make_engine(EngineSettings.from_env())
    prepared = engine.prepare(spec)
    if prompt is not None:
        prepared.run_dir.mkdir(parents=True, exist_ok=True)
        Path(spec.prompt_file).write_text(prompt, encoding="utf-8")
        Path(spec.context_file).write_text("{}\n", encoding="utf-8")

    console = events == "console"
    if console:
        render_header(sys.stdout, prepared)
    started = time.monotonic()
    with _abort_on_signals(engine, prepared) as received:
        result = engine.run(prepared, _event_sink(events, prepared, verbose))
    if console:
        render_summary(sys.stdout, result, run_dir=prepared.run_dir, elapsed=time.monotonic() - started)
    return _exit_code(result, received)


def _event_sink(events: str, prepared: PreparedRun, verbose: bool) -> Any:
    if events == "jsonl":
        return _jsonl_writer(sys.stdout)
    if events == "console":
        return ConsoleSink(sys.stdout, run_dir=prepared.run_dir, verbose=verbose)
    return None


def _jsonl_writer(out: Any) -> Any:
    def write(event: dict[str, Any]) -> None:
        out.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        out.flush()

    return write


def _exit_code(result: RunResult, received: list[int]) -> int:
    if result.status == "success":
        return EXIT_OK
    if result.status == "failed":
        return EXIT_FAILED
    if received and received[0] == signal.SIGINT:
        return EXIT_SIGINT
    return EXIT_SIGTERM


@contextmanager
def _abort_on_signals(engine: WorkflowEngine, prepared: PreparedRun) -> Iterator[list[int]]:
    """SIGINT/SIGTERM během runu: nastaví abort event a zruší kroky; výsledek se pak zapíše jako `aborted`."""

    received: list[int] = []
    cleanup: list[threading.Thread] = []

    def handle(signum: int, _frame: object) -> None:
        received.append(signum)
        # Event se nastavuje přímo; `engine.abort()` jde do vlákna, protože handler běží v hlavním vlákně a engine zamyká.
        prepared.abort_event.set()
        thread = threading.Thread(target=engine.abort, name="workflow-abort", daemon=True)
        cleanup.append(thread)
        thread.start()

    if threading.current_thread() is not threading.main_thread():
        yield received
        return
    previous = {sig: signal.signal(sig, handle) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield received
    finally:
        for thread in cleanup:
            thread.join(timeout=60)
        for sig, handler in previous.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def _make_engine(settings: EngineSettings) -> WorkflowEngine:
    return WorkflowEngine(settings)


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None and args.prompt_file is not None:
        raise CliError("provide the prompt either as an argument or via --prompt-file, not both")
    if args.prompt_file == "-":
        return sys.stdin.read()
    if args.prompt_file is not None:
        path = Path(args.prompt_file)
        if not path.is_file():
            raise CliError(f"prompt file does not exist: {path}")
        return path.read_text(encoding="utf-8")
    if args.prompt is not None:
        return args.prompt
    if sys.stdin is not None and not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _reject_manual_flags(args: argparse.Namespace) -> None:
    used = [flag for attr, flag in _MANUAL_ONLY_FLAGS.items() if getattr(args, attr) not in (None, [])]
    if args.workdir != ".":
        used.append("--workdir")
    if used:
        raise CliError(f"--spec cannot be combined with manual run options: {', '.join(used)}")


def _is_path_reference(value: str) -> bool:
    return "/" in value or "\\" in value or value.endswith((".yaml", ".yml"))


def _default_namespace(name: str, run_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "run"
    return f"wf-{slug}-{run_id[-8:]}"


def _build_spec(data: dict[str, Any]) -> RunSpec:
    try:
        return RunSpec.model_validate(data)
    except ValidationError as exc:
        raise CliError(_format_validation_error(exc)) from exc


def _read_spec(path: Path) -> RunSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read spec {path}: {exc.strerror or exc}") from exc
    try:
        return RunSpec.model_validate_json(text)
    except ValidationError as exc:
        raise CliError(f"invalid spec {path}: {_format_validation_error(exc)}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    # Jen umístění a zpráva: hodnoty z vstupu se do chyby nedostanou (ve spec nejsou secrety, ale logy jsou veřejné).
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}" for error in exc.errors()
    )


# ----------------------------------------------------------------------
# check
# ----------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> int:
    spec = _read_spec(args.spec)
    prepared = _make_engine(EngineSettings.from_env()).prepare(spec)
    sys.stdout.write(json.dumps(prepared.summary(), ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return EXIT_OK


# ----------------------------------------------------------------------
# validate / list
# ----------------------------------------------------------------------


def _cmd_validate(args: argparse.Namespace) -> int:
    paths = _validation_targets(args.paths)
    reports = [_validate_file(path) for path in paths]
    if args.json:
        sys.stdout.write(json.dumps(reports, ensure_ascii=False, indent=2) + "\n")
    else:
        for report in reports:
            _print_validation(sys.stdout, report)
    return EXIT_OK if all(report["ok"] for report in reports) else EXIT_INVALID


def _validation_targets(paths: list[str]) -> list[Path]:
    if not paths:
        paths = [WORKFLOW_DIR_RELPATH]
    targets: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise CliError(f"no such file or directory: {path}")
        if path.is_dir():
            # `_base.yaml` a další `_*` soubory jsou rodiče pro `extends`, samostatně se nespouští.
            found = sorted(item for item in path.glob("*.yaml") if not item.stem.startswith("_"))
            if not found:
                raise CliError(f"no workflow files in {path}")
            targets.extend(found)
        else:
            targets.append(path)
    return targets


def _validate_file(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": str(path),
        "ok": False,
        "errors": [],
        "executor": None,
        "image": None,
        "steps": [],
        "followups": [],
        "env_files": [],
    }
    try:
        workflow = load_workflow_file(path, {name: f"<{name}>" for name in INTERPOLATION_ALLOWLIST})
    except Exception as exc:  # noqa: BLE001 - každá chyba načtení je nevalidní workflow
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        return report

    report["ok"] = True
    _fill_report(report, workflow, path)
    if report["errors"]:
        report["ok"] = False
    return report


def _fill_report(report: dict[str, Any], workflow: WorkflowFile, path: Path) -> None:
    spec = workflow.workflow
    report["executor"] = spec.executor
    report["image"] = spec.image
    for step in spec.steps:
        report["steps"].append(
            {
                "name": step.name,
                "needs": list(step.needs or []),
                "if": step.if_,
                "outputs": [output.type for output in step.outputs],
            }
        )
    for followup in spec.followups:
        report["followups"].append({"title": followup.title, "workflow": followup.workflow})
    # Stejně jako engine hledá envFiles relativně k projektu (`<project>/.agentis/workflows/<name>.yaml`).
    project = path.parent.parent.parent if path.parent.name == "workflows" else path.parent
    for filename in spec.envFiles:
        env_path = Path(filename).expanduser()
        if not env_path.is_absolute():
            env_path = project / env_path
        exists = env_path.is_file()
        keys = len(dotenv_values(env_path, interpolate=False)) if exists else 0
        report["env_files"].append({"path": str(env_path), "exists": exists, "keys": keys})
        if not exists:
            report["errors"].append(f"envFiles: {env_path} does not exist (the run would fail)")


def _print_validation(out: Any, report: dict[str, Any]) -> None:
    if not report["ok"] and not report["steps"]:
        out.write(f"FAIL {report['path']}\n")
        for error in report["errors"]:
            out.write(f"     {error}\n")
        return
    out.write(f"{'OK  ' if report['ok'] else 'FAIL'} {report['path']}\n")
    out.write(f"     executor: {report['executor'] or '(default)'}  image: {report['image'] or '-'}\n")
    for index, step in enumerate(report["steps"], start=1):
        details = []
        if step["needs"]:
            details.append(f"needs={step['needs']}")
        if step["if"]:
            details.append(f"if={step['if']}")
        if step["outputs"]:
            details.append("outputs=" + ",".join(step["outputs"]))
        out.write(f"     {index}. {step['name']}{'  ' + '  '.join(details) if details else ''}\n")
    for followup in report["followups"]:
        out.write(f"     followup: {followup['title']} -> {followup['workflow']}\n")
    for env_file in report["env_files"]:
        if env_file["exists"]:
            out.write(f"     envFiles: {env_file['path']} ({env_file['keys']} keys)\n")
    for error in report["errors"]:
        out.write(f"     {error}\n")


def _cmd_list(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).expanduser().resolve()
    directory = workdir / WORKFLOW_DIR_RELPATH
    names = (
        sorted(item for item in directory.glob("*.yaml") if not item.stem.startswith("_")) if directory.is_dir() else []
    )
    sys.stdout.write(f"Workdir: {workdir}\n")
    if not names:
        sys.stdout.write(f"  (no workflows in {directory})\n")
    for item in names:
        sys.stdout.write(f"  {item.stem}  ({item})\n")
    return EXIT_OK


# ----------------------------------------------------------------------
# abort
# ----------------------------------------------------------------------


def _cmd_abort(args: argparse.Namespace) -> int:
    labels = validate_labels(dict(args.label))
    if args.executor == "kubernetes" and not args.namespace:
        raise CliError("abort with --executor kubernetes requires --namespace")
    namespace = validate_namespace(args.namespace) if args.namespace else ""
    result = abort_by_labels(args.executor, namespace, labels, EngineSettings.from_env())
    sys.stdout.write(f"{result}\n")
    return EXIT_OK


def _error(message: str) -> None:
    sys.stderr.write(f"agentis-workflow: {message}\n")


if __name__ == "__main__":
    raise SystemExit(main())
