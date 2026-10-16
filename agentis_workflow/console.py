"""Lidský výpis pro ruční běh: průběh kroků (události enginu), hlavička a souhrn `result.json`.

Patří jen k `--events console` a ručnímu `run`; strojový výstup jde přes JSONL v `cli.py`. Barvy se zapínají
jen na TTY a vypínají se proměnnou `NO_COLOR`. Všechno jde na zadaný stream (stdout), diagnostika zůstává na stderr.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, TextIO

from agentis_workflow.engine import PreparedRun
from agentis_workflow.protocol import RunResult
from agentis_workflow.schema import COMMENT_STATUS_ALIASES

LOG_TAIL_LINES = 30
COMMENT_PREVIEW_LINES = 60
_JOB_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_STATUS_NAMES = {value: name for name, value in COMMENT_STATUS_ALIASES.items()}


def use_color(out: TextIO) -> bool:
    return out.isatty() and "NO_COLOR" not in os.environ


class _Painter:
    def __init__(self, out: TextIO, color: bool) -> None:
        self.out = out
        self.color = color

    def paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def line(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()


class ConsoleSink:
    """Přijímá události enginu (`on_event`) a vypisuje je jako průběh; volá se jen z hlavního vlákna schedulerů."""

    def __init__(self, out: TextIO, *, run_dir: Path, verbose: bool = False, color: bool | None = None) -> None:
        self.paint = _Painter(out, use_color(out) if color is None else color)
        self.run_dir = run_dir
        self.verbose = verbose

    def __call__(self, event: dict[str, Any]) -> None:
        handler = getattr(self, f"_on_{event.get('type')}", None)
        if handler is not None:
            handler(event)

    def _on_step_started(self, event: dict[str, Any]) -> None:
        self.paint.line(f"{self.paint.paint('36', '▶')} {event['step']}")

    def _on_step_succeeded(self, event: dict[str, Any]) -> None:
        attempts = event.get("attempts", 1)
        suffix = f", attempts {attempts}" if attempts > 1 else ""
        took = f" ({event.get('duration_ms', 0) / 1000:.1f}s)"
        self.paint.line(f"{self.paint.paint('32', '✓')} {event['step']}{self.paint.paint('2', took + suffix)}")
        self._verbose_log(event)

    def _on_step_failed(self, event: dict[str, Any]) -> None:
        attempts = event.get("attempts", 1)
        suffix = f", attempts {attempts}" if attempts > 1 else ""
        tolerated = " — continueOnError" if event.get("continue_on_error") else ""
        took = f" ({event.get('duration_ms', 0) / 1000:.1f}s)"
        self.paint.line(
            f"{self.paint.paint('31', '✗')} {event['step']}{self.paint.paint('2', took + suffix + tolerated)}"
        )
        if event.get("error"):
            self.paint.line(f"    {event['error']}")
        if not self._verbose_log(event):
            for line in (event.get("log_tail") or "").rstrip().splitlines()[-LOG_TAIL_LINES:]:
                self.paint.line(self.paint.paint("2", f"    │ {line}"))

    def _on_step_skipped(self, event: dict[str, Any]) -> None:
        if event.get("reason") == "condition":
            reason = f"if {event.get('condition')}"
        else:
            reason = f"workflow failed at {event.get('failed_step')}"
        self.paint.line(f"{self.paint.paint('33', '↷')} {event['step']} — skipped ({reason})")

    def _on_warning(self, event: dict[str, Any]) -> None:
        details = ", ".join(f"{key}={value}" for key, value in (event.get("data") or {}).items())
        suffix = f" ({details})" if details else ""
        self.paint.line(f"{self.paint.paint('33', '!')} {event.get('message')}{suffix}")

    def _verbose_log(self, event: dict[str, Any]) -> bool:
        """V `--verbose` vypíše celý log kroku z `run_dir/logs`; vrací, zda se něco vypsalo."""

        job = str(event.get("job") or "")
        if not self.verbose or not _JOB_NAME_RE.fullmatch(job):
            return False
        try:
            text = (self.run_dir / "logs" / f"{job}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in text.rstrip().splitlines():
            self.paint.line(self.paint.paint("2", f"    │ {line}"))
        return bool(text.strip())


def render_header(out: TextIO, prepared: PreparedRun, *, color: bool | None = None) -> None:
    paint = _Painter(out, use_color(out) if color is None else color)
    paint.line(paint.paint("1", f"Workflow {prepared.workflow_file.stem}") + f"  ({prepared.workflow_file})")
    paint.line(
        paint.paint(
            "2",
            f"workdir {prepared.workdir} · executor {prepared.executor} · namespace {prepared.namespace} · "
            f"run {prepared.run_dir.name}",
        )
    )
    paint.line()


def render_summary(
    out: TextIO,
    result: RunResult,
    *,
    run_dir: Path,
    elapsed: float,
    color: bool | None = None,
) -> None:
    """Souhrn na konci ručního runu: status, vars a outputs z `result.json`."""

    paint = _Painter(out, use_color(out) if color is None else color)
    paint.line()
    if result.status == "success":
        paint.line(paint.paint("32", f"✓ Workflow finished in {elapsed:.1f}s"))
    elif result.status == "aborted":
        paint.line(paint.paint("31", f"✗ Workflow aborted after {elapsed:.1f}s"))
    else:
        where = f" at step {result.failed_step!r}" if result.failed_step else ""
        paint.line(paint.paint("31", f"✗ Workflow failed{where} after {elapsed:.1f}s"))

    for name, value in result.vars.items():
        paint.line(f"  var {name}={value}")
    for comment in result.outputs.comments:
        _render_comment(paint, comment)
    if result.outputs.session_id:
        paint.line(f"  session_id: {result.outputs.session_id}")
    for attachment in result.outputs.attachments:
        paint.line(f"  attachment {attachment.label}: {attachment.value}")
    for artifact in result.outputs.artifacts:
        paint.line(f"  artifact: {artifact.name} ({artifact.size} B) -> {artifact.path}")
    for followup in result.followups:
        paint.line(f"  followup: {followup.title} -> {followup.workflow}")
    for warning in result.warnings:
        paint.line(paint.paint("33", f"  warning: {warning.message}"))
    paint.line(f"  run dir:    {run_dir}")
    logs = run_dir / "logs"
    if logs.is_dir():
        paint.line(f"  logs:       {logs}")


def _render_comment(paint: _Painter, comment: Any) -> None:
    author = comment.author_name or "agent"
    status = _STATUS_NAMES.get(comment.status, comment.status) if comment.status is not None else "-"
    paint.line()
    paint.line(paint.paint("1", f"┌─ Comment — author: {author}, status: {status}"))
    lines = comment.body.splitlines()
    for line in lines[:COMMENT_PREVIEW_LINES]:
        paint.line(f"│ {line}")
    if len(lines) > COMMENT_PREVIEW_LINES:
        paint.line(paint.paint("2", f"│ … {len(lines) - COMMENT_PREVIEW_LINES} more lines"))
    paint.line(paint.paint("1", "└─"))


__all__ = ["ConsoleSink", "render_header", "render_summary", "use_color"]
