# agentis-workflow

Samostatný engine pro deklarativní YAML workflow. Vezme workflow nad adresářem, naplánuje kroky podle závislostí,
spustí je na `local`, `docker` nebo `kubernetes` executoru a výsledek vrátí jako `result.json`. Neví nic o Agentisu,
WebSocketu, taskech ani gitu. Volá se z příkazové řádky, například z `agentis-adapter`, nebo ručně.

## Instalace

Vyžaduje Python 3.13 a [Poetry](https://python-poetry.org/).

```bash
cd /var/www/agentis-workflow
poetry install
poetry run agentis-workflow --version
```

Pro globální příkaz:

```bash
pipx install --python python3.13 -e /var/www/agentis-workflow
# při opakované instalaci: pipx uninstall agentis-workflow && pipx install --python python3.13 -e /var/www/agentis-workflow
```

## Rychlý start

```bash
# kontrola syntaxe workflow
agentis-workflow validate examples/hello.yaml

# lokální běh (bez kontejnerů, bez sandboxu)
agentis-workflow run hello --workdir /tmp/hello --var TASK_TITLE="Ahoj" --executor local

# výsledek
cat /tmp/agentis-workflow/*/result.json
```

Další příklady v `examples/`: `report.yaml` (artefakt, závislosti, `always`) a `parallel.yaml` (paralelní kroky).

## Dokumentace

- [docs/workflow.md](docs/workflow.md) — syntaxe YAML, executory, outputy, podmínky, chování chyb.
- [docs/cli.md](docs/cli.md) — příkazy, `spec.json`, JSONL události, `result.json`, exit kódy, abort.
- [docs/testovani-workflow.md](docs/testovani-workflow.md) — ruční testování workflow.
- [skills/agentis-workflow/](skills/agentis-workflow/) — agent skill pro psaní workflow a validátor.
- [vscode-agentis-workflow/](vscode-agentis-workflow/) — VS Code rozšíření se schématem a diagnostikou.

## Vývoj

```bash
poetry run pytest -q
poetry run ruff check .
```

Pravidla pro přispívání a rozdělení odpovědností jsou v [AGENTS.md](AGENTS.md).
