# Agentis Workflow Guidelines

## Project Overview

`agentis-workflow` is a Python 3.13 declarative workflow engine. It loads YAML workflow files, validates them,
schedules their steps by dependencies, runs the steps through a local, Docker, or Kubernetes executor, and returns
the run result as `result.json`. The public contract is the `agentis-workflow` command line (`run --spec`, `check`,
`validate`, `list`, `abort`). Other projects call the engine through this command line.

The engine knows nothing about Agentis, WebSocket transports, JSON-RPC, tasks, git worktrees, or Agentis callbacks.
`agentis-adapter` (`/var/www/agentis-adapter`) is one of the callers. It must not be imported from this repo.

## Repository Map

- `agentis_workflow/schema.py`: source of truth for workflow YAML schema, interpolation, inheritance, `uses`, conditions, and followups.
- `agentis_workflow/agent_step.py`: generates the bash `run` script from declarative `agent:` steps (calls `agentiscode`).
- `agentis_workflow/runtime.py`: step runner protocol and the Kubernetes executor (`kubectl` Jobs).
- `agentis_workflow/local_runtime.py`: local bash-process executor (no sandbox).
- `agentis_workflow/docker_runtime.py`: Docker container executor.
- `agentis_workflow/engine.py`: run lifecycle, DAG scheduling, retries, `always`, `continueOnError`, timeouts, abort, outputs, artifact staging, and `result.json`.
- `agentis_workflow/protocol.py`: `RunSpec` (`spec.json`) and `RunResult` (`result.json`) models and `PROTOCOL_VERSION`.
- `agentis_workflow/settings.py`: `EngineSettings` read from the process environment (`KUBECTL_COMMAND`, `DOCKER_COMMAND`, `WORKFLOW_EXECUTOR`).
- `agentis_workflow/cli.py`: `agentis-workflow` entrypoint and subcommands.
- `agentis_workflow/console.py`: human-readable console output for manual runs.
- `tests/`: pytest coverage for schema, agent steps, runtimes, engine, protocol, and CLI behavior.
- `docs/workflow.md`: YAML reference. `docs/cli.md`: command-line contract. `docs/testovani-workflow.md`: manual testing.
- `skills/agentis-workflow/`: agent skill for authoring workflows, including `scripts/validate_workflow.py` (symlinked into `~/.claude/skills/`).
- `vscode-agentis-workflow/`: VS Code extension with the editor JSON Schema and live diagnostics.
- `examples/`: small generic workflows that validate against the schema.

## Architecture Rules

- Keep the engine free of Agentis concepts. Agentis-specific YAML fields (`agent_comment`, `session_id`, `followups`, tokens such as `TASK_ID`, `BRANCH`, `GITHUB_REPO`) are validated and returned in `result.json`; interpreting them belongs to the caller.
- The CLI is the only contract between projects. Do not add a Python API intended for other projects, and do not import `agentis_adapter` or `common.*` from this repo.
- Secrets (`AGENTIS_SERVICE_TOKEN`, `AGENTIS_API_TOKEN`, `AGENTIS_TOKEN`, and any `pass_env` value) enter a run only through the environment of the process. Never place them in `spec.json`, `argv`, logs, `result.json`, or error details.
- The `spec.json` model, JSONL event types, `result.json` shape, and exit codes are the protocol. Change them only deliberately: bump `PROTOCOL_VERSION` when the shape changes, and update `docs/cli.md`, the `CHANGELOG`, and focused tests in the same change.
- Keep one active run per set of labels. Busy detection and `abort` work through labels on jobs, containers, or processes, not through in-memory state of another process.
- Keep run state in memory during a run. Do not add persistence beyond the run directory and `result.json` unless explicitly requested.

## Workflow Rules

- `schema.py` is the source of truth. Add new YAML fields, interpolation tokens, conditions, or output types there before using them at runtime.
- When workflow syntax or behavior changes, update together: `schema.py`, `docs/workflow.md`, `examples/`, `vscode-agentis-workflow/schemas/`, `skills/agentis-workflow/` (SKILL.md, references, assets), and focused tests.
- Workflow files are loaded and frozen at run start. Do not make a running workflow depend on later YAML edits.
- The supported executors are `local`, `docker`, and `kubernetes`. The executor is chosen by `spec.executor` (forced), then `workflow.executor` in YAML, then `spec.executor_default` / `WORKFLOW_EXECUTOR`. Docker and Kubernetes executors require an image for every resolved step.
- The local executor runs commands directly as the calling user. It is not a sandbox.
- Outputs from skipped or failed steps must not be applied. Successful `always` step outputs may still be reported when the run fails.
- Preserve the scheduling semantics: `needs` ordering, `maxParallel`, retries (`N + 1` attempts), `continueOnError`, `always`, `if` conditions, step and run timeouts, abort events, and graceful shutdown.
- Treat workflow paths, artifact globs, and output paths as untrusted input. Keep containment checks (including symlinks) and the artifact file-count and size limits (50 files, 50 MiB per run) intact.
- Do not interpolate untrusted values into shell commands. Pass them through environment variables or structured arguments.

## Security And Data Handling

- Never commit `.env` files or expose secret values in logs, `result.json`, fixtures, or error details. Test fixtures use obviously fake values.
- Do not weaken validation for workflow names, paths, mounts, or artifact globs.
- The `local` executor inherits the host environment except `AGENTIS_TOKEN`, `AGENTIS_API_TOKEN`, and `AGENTIS_SERVICE_TOKEN`. Do not forward any other host secret implicitly; callers pass secrets through `pass_env`.

## Development Commands

```bash
poetry install
poetry run agentis-workflow --version
poetry run agentis-workflow validate examples/*.yaml
poetry run agentis-workflow run hello --workdir /tmp/hello --var TASK_TITLE="Ahoj" --executor local
poetry run pytest -q
poetry run ruff check .
```

Extension checks, from `vscode-agentis-workflow/`: `npm install`, `npm test`, `npm run typecheck`, `npm run package`.

## Testing Conventions

- Run `poetry run pytest -q` and `poetry run ruff check .` before finishing code changes.
- Prefer focused tests while iterating (for example `poetry run pytest -q tests/test_schema.py`), then run the full suite.
- Follow plain pytest style: plain test functions, `tmp_path`, `monkeypatch`, and small local fakes rather than broad mocking frameworks.
- Test the CLI through its entrypoint helpers and the `spec.json` / `result.json` contract, not through internal state.
- Stub subprocess runners, Docker, `kubectl`, and network calls. Tests must not contact a cluster, pull images, or require container infrastructure.
- Keep tests deterministic: wait on explicit events or joins instead of arbitrary sleeps when exercising background step threads.
- Changes to the CLI contract need a test that checks the JSON shape and the exit code.

## Code Style

- Target Python `>=3.13,<3.14`, Pydantic v2, pytest, and Ruff with a 120-character line length.
- Use type hints and `pathlib.Path` for filesystem work.
- Use Pydantic models with `extra="forbid"` for spec, result, and YAML payloads instead of ad-hoc validation.
- Prefer the smallest correct change and reuse existing runners, models, and helpers.
- Keep comments concise and focused on non-obvious lifecycle, concurrency, or compatibility constraints. Czech and English comments are both acceptable; match the surrounding file.

## Change Checklist

- Confirm whether the change affects the CLI contract (`spec.json`, JSONL events, `result.json`, exit codes), the YAML schema, or executor behavior.
- Update models, implementation, tests, examples, the extension schema, the skill, and documentation consistently for the affected boundary.
- Verify secrets and local paths cannot leak through `result.json`, JSONL events, or logs.
- Run the full pytest and Ruff checks and report any checks that could not be run.
