# Changelog

Verze kontraktu (`protocol`) v `spec.json` / `result.json` / JSONL je v `agentis_workflow/protocol.py` (`PROTOCOL_VERSION`).

## 0.1.0 — protocol 1

- První verze samostatného enginu: `run` (`--spec` i ruční režim), `check --spec`, `validate`, `list`, `abort`, `--version`.
- Kontrakt: `spec.json` (protocol 1), JSONL události na stdout, `result.json` v run dir, exit kódy 0/1/2/3/130/143.
- Secrety jdou jen přes `pass_env` z env procesu; nikdy do `spec.json` ani argv.
- Výstupy `agent_comment`, `session_id`, `url`, `text`, `artifact`, `var` a followupy; výsledek vrací v `result.json`, interpretuje ho volající.
