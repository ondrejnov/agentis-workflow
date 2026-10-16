---
name: agentis-workflow
description: Návrh, psaní, úprava a validace deklarativních workflow YAML pro engine `agentis-workflow` (kroky v bashi, local/Docker/Kubernetes executor, závislosti `needs`, podmínky `if`, outputs a artefakty). Použij vždy, když uživatel chce vytvořit nebo upravit workflow, automatizaci, pipeline, report, periodický úkol nebo krok, který má běžet přes `agentis-workflow` — i když neřekne slovo „workflow“ (např. „udělej denní report z logů“, „přidej krok s testy před agentem“, „proč se mi nepropsal artefakt z workflow“, „spusť to jako pipeline“).
---

# agentis-workflow

`agentis-workflow` je samostatný engine. Vezme YAML workflow nad adresářem, naplánuje kroky podle závislostí,
spustí je na zvoleném executoru a vrátí výsledek v `result.json`. Neví nic o Agentisu, WebSocketu ani gitu.
Konkrétní volající (například `agentis-adapter`) si z výsledku vezme, co potřebuje. Agentis-specifická pole
(`agent_comment`, `session_id`, `followups`) engine validuje a vrací beze změny.

Autoritativní zdroje (když si nejsi jistý detailem, čti je, ne paměť):

- `docs/workflow.md` v repu enginu — kompletní reference syntaxe.
- `docs/cli.md` — příkazy, `spec.json`, JSONL události, `result.json`, exit kódy.
- `agentis_workflow/schema.py` — Pydantic schema; co tu není, YAML nesmí obsahovat.
- `examples/` — malé generické příklady (`hello.yaml`, `report.yaml`, `parallel.yaml`).
- `references/patterns.md` v tomto skillu — hotové vzory kroků. Čti, když skládáš konkrétní kroky.

## Postup

### 1. Ujasni zadání

Potřebuješ vědět:

- **Co má workflow udělat** krok po kroku (zdroj dat → zpracování → výstup).
- **Kde má výsledek skončit**: soubor, artefakt, e-mail, komentář volajícího…
- **Nad jakým adresářem** běží (`workdir`) a **jak se bude spouštět** (ruční `agentis-workflow run`, volající přes `--spec`).
- Potřebné tajné údaje (SMTP, API klíče) — nikdy je nepiš do YAML; předávají se přes env procesu (`pass_env`) nebo host-side `envFiles`.

Co jde odvodit z repozitáře (image, mounty, existující `_base.yaml`), odvoď sám a neptej se.

### 2. Zvol jméno souboru a umístění

Workflow se hledá v `search_paths` (default `.agentis/workflows/` relativně k `workdir`). Jméno smí obsahovat
jen `[A-Za-z0-9_.-]`. Soubor `_base.yaml` je jen rodič pro `extends` a sám se nespustí.

### 3. Vyjdi z existujícího základu

Existuje-li `_base.yaml` vedle workflow, použij `extends: _base`. Zdědíš image, `envFiles`, mounty a šablony
`stepTemplates`. Pozor: `steps` a `followups` se nedědí nikdy; `env` se merguje po klíčích; `mounts`, `envFiles`
a `imagePullSecrets` se spojují (stejné `name` se přepíše). Dědičnost má jen jednu úroveň.

Když `_base.yaml` neexistuje, začni z `assets/template-report.yaml` nebo z `examples/`.

### 4. Napiš kroky

Každý `run` běží v bash wrapperu `set -euo pipefail` a `cd` do `workingDir`. Z toho plyne:

- Nedefinovaná proměnná shodí krok → volitelné hodnoty piš jako `${VAR:-default}` / `${VAR:+--flag "$VAR"}`.
- Selhání kdekoli v pipe shodí krok → tam, kde je chyba přijatelná, použij `|| true` vědomě.
- Velké inline skripty (Python přes heredoc `<<'PY'`) jsou v pořádku; uvozovkovaný delimiter zabrání expanzi.
  YAML block scalar (`run: |`) odstraní jen společné odsazení, takže ukončovací `PY` musí být na stejné úrovni
  jako první řádek skriptu. Validátor to nepozná.
- Když LLM vybírá nebo hodnotí data, chtěj po něm strukturovaný JSON (ID + zdůvodnění) a výstup sestav
  deterministicky v dalším kroku. Tvar JSON vynucuj JSON Schematem (`agent.outputSchema`), ne popisem v promptu.
  Povolená ID dej do schématu jako `enum`.

Prostředí, které krok dostane automaticky:

- `AGENTIS_RUN_DIR`, `AGENTIS_PROMPT_FILE` (zadání), `AGENTIS_CONTEXT_FILE` (JSON kontext),
- všechny interpolační tokeny jako env (`WORKDIR`, `RUN_DIR`, `BRANCH`, …),
- `var` outputy kroků, na kterých krok (transitivně) závisí,
- `workflow.env` / `step.env` a proměnné z `spec.env` a `spec.pass_env`,
- u `always` kroků navíc `AGENTIS_WORKFLOW_STATUS` a `AGENTIS_FAILED_STEP`.

Interpolace `[%TOKEN%]` v YAML se provede při načtení. Povolené tokeny jsou jen `NAMESPACE`, `WORKDIR`, `RUN_DIR`,
`MAIN_DIR`, `RUN_ID`, `TASK_ID`, `TASK_NUMBER`, `TASK_TITLE`, `BRANCH`, `BASE_BRANCH`, `GITHUB_REPO` — cokoli jiného
je chyba. Uvnitř `run` skriptů preferuj env proměnné (`"$WORKDIR"`) před tokeny: hodnota pak nejde přes shell parsing.

Agent/LLM krok: preferuj deklarativní `agent:` (`adapter`, `model`, `prompt`/`promptFile`, `inputs`, `outputSchema`,
`output`). Engine z něj vygeneruje bash, který volá `agentiscode` s bezpečným quotováním. V hodnotách se expandují
jen `$NAME`/`${NAME}`/`${NAME:-text}`. Se `outputSchema` je `output` zvalidovaný JSON; když ani oprava nepomůže, krok
skončí chybou. Kompletní pole: sekce „Agentní kroky (`agent`)“ v `docs/workflow.md`.

### 5. Outputs — nejčastější zdroj „nic se nestalo“

Outputy jsou cesty **relativní k output rootu** (`output_root`, default `workdir`) a nesmí z něj utéct. Když krok
zapíše soubor jinam, než ukazuje deklarace, output se potichu nepropíše.

| Typ | Pole | Co udělá |
| --- | --- | --- |
| `var` | `name`, `valueFrom` | proměnná pro `if` a env závislých kroků; vrací se v `result.vars` |
| `artifact` | `name`, `path` (glob) | soubor(y) zkopírované do `artifact-staging/` a vrácené v `outputs.artifacts`; glob drž úzký |
| `url` / `text` | `label`, `valueFrom` | příloha (odkaz / text) vrácená v `outputs.attachments` — interpretuje volající |
| `agent_comment` | `bodyFrom`, `status`, volitelně `name` | **Agentis-specifické:** engine jen validuje a vrací; komentář posílá volající |
| `session_id` | `valueFrom` | **Agentis-specifické:** session id pro pozdější resume |

Outputy se vrací jen za **úspěšné, nepřeskočené** kroky (i když run jako celek selže). Výsledek je v `result.json`.

### 6. Řízení toku (jen když je potřeba)

- `needs: [...]` → DAG a paralelismus (`needs: []` = startuje hned); bez `needs` běží kroky sekvenčně.
  Při použití `needs` musí být jména unikátní a odkazovat jen nahoru. `workflow.maxParallel` omezuje souběh.
- `if: VAR == 'x' && !OTHER` — gramatika: `VAR`, `!VAR`, `==`, `!=`, `&&` (váže silněji), `||`, bez závorek.
- `continueOnError: true`, `retries: N`, `always: true` (úklid, failure report; má `AGENTIS_WORKFLOW_STATUS`
  a `AGENTIS_FAILED_STEP`), `timeoutSeconds` na kroku.
- `workflow.followups` — validované nabídky dalších workflow; engine je vrací v `result.followups` jen po úspěšném runu.

### 7. Validuj

Vždy spusť validátor — používá přesně loader enginu (extends, uses, tokeny, striktní schema, needs, if):

```bash
cd /var/www/agentis-workflow && poetry run python \
  skills/agentis-workflow/scripts/validate_workflow.py <projekt>/.agentis/workflows/<jméno>.yaml
```

Totéž dělá `agentis-workflow validate <soubor.yaml>` (s `--json` pro strojový výstup).

Validátor nekontroluje, zda image obsahuje použité nástroje, zda existují `envFiles` a obsahují potřebné klíče, a zda
jsou cesty v outputs konzistentní se zápisem v `run`. U envFiles kontroluj jen jména klíčů, nikdy hodnoty:
`grep -o '^[A-Za-z_][A-Za-z0-9_]*=' <soubor.env>`. Chybí-li klíč, řekni to uživateli — tajné hodnoty nedoplňuj sám.
Pokud jde krok bezpečně vyzkoušet lokálně (čisté čtení dat), můžeš jeho bash spustit ručně s `AGENTIS_RUN_DIR=$(mktemp -d)`;
kroky s vedlejšími účinky (e-mail, push, deploy) bez souhlasu uživatele nespouštěj.

Pro ověření celého běhu: `agentis-workflow run <jméno> --workdir <dir> --prompt "..." --executor local` a přečti
`<run_dir>/result.json`. Podrobnosti v `docs/testovani-workflow.md`.

### 8. Shrnutí pro uživatele

Uveď: cestu k souboru, co jednotlivé kroky dělají, výsledek validace, jaké tajné hodnoty/env musí být předané
(a které chybí), a jak workflow spustit (`agentis-workflow run`, nebo jako součást volajícího, např. adaptéru přes `--spec`).

## Pravidla, která drží workflow bezpečné a funkční

- **Tajné údaje nikdy do YAML ani do logu** (žádné `echo $TOKEN`, žádné `set -x` kolem secretů). Předávají se přes
  env procesu a `spec.pass_env`, případně host-side `envFiles`.
- **Nedůvěryhodný text (prompt, název, metadata) nevkládej do shellu jako kód.** Čti ho z `$AGENTIS_PROMPT_FILE`/env
  a předávej jako data (stdin, argumenty v uvozovkách, `jq --arg`).
- **Konfigurovatelné hodnoty** (příjemce e-mailu, URL, model) dej do `workflow.env`, ne natvrdo do skriptu.
- `kubernetes` a `docker` executor vyžadují `image` u každého kroku (zděděnou nebo vlastní). `local` executor
  není sandbox — běží přímo jako uživatel procesu.
- Workflow se načte a zmrazí na začátku runu; úprava YAML během běhu nic nezmění.
- Neznámé klíče v YAML jsou chyba (`extra="forbid"`); výjimkou jsou jen položky `mounts` (Kubernetes volume source).

## Časté chyby

| Příznak | Příčina |
| --- | --- |
| `Unknown workflow token [%X%]` | token mimo allowlist — použij env proměnnou |
| `Extra inputs are not permitted` | překlep v klíči nebo pole, které schema nezná |
| `unknown or future 'needs'` | `needs` ukazuje na neexistující nebo níže definovaný krok |
| `… vyžaduje 'image'` | chybí `image` na kroku i ve workflow (kubernetes/docker) |
| `Workflow file not found` | soubor není v `search_paths`; zkontroluj jméno a `--workdir` |
| výstup/artefakt se neobjevil | krok selhal nebo byl přeskočen, nebo cesta nesedí s output rootem (`outputs/` vs `.agentis/outputs/`) |
| krok padá na `unbound variable` | `set -u` — použij `${VAR:-}` |
