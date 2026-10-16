# Workflow YAML

Deklarativní YAML definuje kroky (bash skripty, případně `agent:` volání `agentiscode`), jejich závislosti,
podmínky, retries a výstupy. `agentis-workflow` soubor načte, zvaliduje, zmrazí a spustí na zvoleném executoru.
Engine nezná Agentis, WebSocket, tasky ani git; pole označená níže jako „Agentis-specifická“ jen validuje
a vrací v `result.json`. Interpretuje je volající (například `agentis-adapter`).

Zdroje pravdy:

| Soubor | Role |
| --- | --- |
| `agentis_workflow/schema.py` | Pydantic schéma YAML, interpolace tokenů, `if` podmínky, `extends`, `uses` |
| `agentis_workflow/agent_step.py` | Generování `run` skriptu z `agent:` kroku |
| `agentis_workflow/engine.py` | Plánování DAG, retries, `always`, `continueOnError`, timeouty, abort, outputs |
| `agentis_workflow/runtime.py`, `local_runtime.py`, `docker_runtime.py` | Kubernetes, local a Docker runnery |
| `agentis_workflow/protocol.py` | Kontrakt `spec.json` / `result.json` (viz [cli.md](cli.md)) |

## Výběr workflow souboru

`agentis-workflow run <name>` hledá `<name>.yaml` v `search_paths` (výchozí `.agentis/workflows` relativně k `workdir`).
Pořadí adresářů určuje precedenci: první nalezený soubor vyhrává. Volající může přidat další adresáře (adapter přidává
bundled `workflows/` jako fallback).
Cesta k souboru (obsahující `/` nebo končící `.yaml`) se použije přímo.

Soubor se načte, vyřeší se `extends`, interpoluje a **zmrazí jednou na začátku runu**. Pozdější změny YAML běžící
workflow neovlivní.

## Executory

Executor vybírá volající nebo YAML, v tomto pořadí:

1. `spec.executor` (vynucený; CLI `--executor`),
2. `workflow.executor` v YAML,
3. `spec.executor_default`, jinak env `WORKFLOW_EXECUTOR`, jinak `kubernetes`.

### `kubernetes`

Každý krok je `batch/v1 Job` obsluhovaný přes `kubectl` (apply / wait / logs / delete). Vyžaduje platný kube context
(`workflow.context`, jinak aktuální). Namespace dodává volající (`spec.namespace`). Každý krok musí mít výslednou
`image` (na kroku nebo z `workflow.image`). `mounts`, `imagePullSecrets` a `steps[].resources` se promítají do Job manifestu.

### `local`

Kroky běží jako lokální bash subprocessy nad `workdir`, pod uživatelem procesu, **bez izolace**. Pole `context`,
`image`, `steps[].image`, `mounts`, `imagePullSecrets` a `steps[].resources` se ignorují s varováním.
`ttlSecondsAfterFinished` a `deleteNamespace` se ignorují bez varování. Logy kroků jdou do `<run_dir>/logs/<job>.log`.
Z hostitelského prostředí se nepropisují `AGENTIS_TOKEN`, `AGENTIS_API_TOKEN` ani `AGENTIS_SERVICE_TOKEN`;
explicitně je lze předat přes `pass_env`.

Local executor není sandbox. Spouští příkazy z YAML přímo jako uživatel procesu.

### `docker`

Každý krok běží jako kontejner spuštěný přes `docker run --rm --privileged`; příkaz lze změnit přes env
`DOCKER_COMMAND`. Vyžaduje výslednou `image`, nepoužívá kube context ani namespace. Kontejnery dostávají stejné labely
jako Kubernetes Joby, takže busy-check a abort používají `docker ps` a `docker rm --force`. Timeout kontejner odstraní.

Existující absolutní cesty `WORKDIR`, `AGENTIS_RUN_DIR` a `MAIN_DIR` se bind-mountují na stejnou cestu v kontejneru.
`workflow.mounts` se převede na bind mounty, pokud položka používá `hostPath`; `readOnly`, relativní `subPath` a typy
`DirectoryOrCreate`/`FileOrCreate` jsou podporované. Jiné volume sources, `subPathExpr` a `mountPropagation` skončí
čitelnou chybou. `imagePullSecrets`, `context` a `steps[].resources` se ignorují s varováním; přihlášení k registry musí
být v Docker credential store uživatele. `ttlSecondsAfterFinished` a `deleteNamespace` se ignorují.

### Společné

- `envFiles` se načtou jednou na hostu při startu runu a jejich hodnoty se zmrazí do env kroků. Soubory se
  nemountují ani nekopírují do kontejneru nebo Podu. Pozdější soubor přepisuje dřívější a přepisuje `workflow.env`,
  runtime env i `steps[].env`. Jde o dotenv data (`KEY=value`, uvozovky, `export`), ne o shell. Chybějící soubor ukončí
  run chybou.
- Všechny executory spouští `run` skript přes stejný bash wrapper: `set -euo pipefail`, potom `cd` do `workingDir`
  kroku (jinak `workflow.workingDir`, jinak `$WORKDIR`).

## Struktura YAML

```yaml
version: 1                      # povinné, vždy 1
extends: _base                  # volitelné: dědičnost z jiného souboru (viz níže)
workflow:
  executor: docker              # volitelné: kubernetes | docker | local
  context: my-kube-context      # volitelné: kubectl context; local jej ignoruje
  image: registry/image:tag     # default image; v K8s musí mít výslednou image každý krok
  imagePullSecrets:
    - name: registry
  workingDir: "[%WORKDIR%]"
  timeoutSeconds: 14400         # default timeout kroku (sekundy)
  maxParallel: 4                # maximum současně běžících kroků v jednom runu
  ttlSecondsAfterFinished: 3600 # TTL dokončených K8s Jobů
  deleteNamespace: false        # po úspěchu smazat celý namespace (jen kubernetes)
  envFiles:                     # host-side dotenv soubory injektované do env kroků
    - /etc/myproject/app.env
  env:                          # env společné všem krokům
    TASK_NUMBER: "[%TASK_NUMBER%]"
  mounts:                       # jen kubernetes a docker (hostPath); generuje volumeMounts i volumes
    - name: data
      mountPath: /data
      readOnly: true
      hostPath:
        path: /srv/data
  followups: [...]              # Agentis-specifické: engine vyhodnotí `if` a vrátí v result.json
  stepTemplates:                # sdílené definice kroků pro `uses` (viz níže)
    run-agent:
      run: ...
  steps:                        # povinné, aspoň jeden krok
    - name: Build
      needs: []                 # volitelné: explicitní závislosti podle jmen kroků
      uses: run-agent           # volitelně: defaulty kroku ze šablony
      run: |                    # bash skript (povinný, pokud ho nedodá `uses` nebo `agent`)
        ...
      agent: {...}              # místo `run`: deklarativní volání agenta (viz níže)
      if: ENV_READY != 'true'   # volitelná podmínka (viz níže)
      continueOnError: false    # selhání kroku nezastaví workflow
      retries: 0                # počet opakování selhaného kroku
      always: false             # krok běží i po selhání dřívějšího kroku
      image: ...                # přepis workflow image (jen kubernetes, docker)
      env: {}                   # env navíc pro tento krok
      workingDir: ...          # přepis pracovního adresáře
      timeoutSeconds: 600       # přepis timeoutu
      ttlSecondsAfterFinished: 300
      resources: {}            # K8s resources (jen kubernetes)
      outputs: [...]            # viz níže
```

Schéma je s výjimkou položek `mounts` striktní (`extra="forbid"`). Neznámé klíče jsou chyba. Mount povoluje
dodatečné klíče jako Kubernetes volume source a předává je do manifestu; jejich platnost ověří až Kubernetes API.

### Dědičnost (`extends`)

Top-level `extends: <name>` načte před validací soubor `<name>.yaml` ze stejného adresáře jako vybrané workflow
a smerguje ho s potomkem. Rodič nemusí mít `steps`, takže se samostatně nespustí. Podporovaná je **jediná úroveň**:
rodič s vlastním `extends` (řetězení i cyklus) je chyba `WorkflowExtendsError`; chybějící cíl je `FileNotFoundError`.

Merge probíhá nad surovým YAML a **interpolace `[%TOKEN%]` běží až po merge**. Sémantika po polích:

| Pole | Sémantika |
| --- | --- |
| skaláry (`image`, `workingDir`, `timeoutSeconds`, `deleteNamespace`, …) | potomek přepisuje rodiče; bez hodnoty v potomkovi platí rodič |
| `env` | merge po klíčích, potomek vyhrává |
| `stepTemplates` | merge po jménech šablon; potomek přepisuje **celou** šablonu |
| `envFiles`, `mounts`, `imagePullSecrets` | konkatenace rodič + potomek; položka se stejným `name` se přepíše na místě, přesný duplikát se vynechá |
| `steps`, `followups` | **nedědí se nikdy** — potomek je musí definovat sám |

### Sdílené kroky (`stepTemplates` + `uses`)

```yaml
workflow:
  stepTemplates:
    run-agent:
      env:
        RUN_AGENT_FLAGS: --json
      run: |
        agentiscode ${RUN_AGENT_FLAGS:-} ...
      outputs:
        - type: session_id
          valueFrom: outputs/session-id
  steps:
    - name: Run agent
      uses: run-agent
      env:
        RUN_AGENT_FLAGS: ""    # odchylka jen tam, kde je potřeba
```

Šablona má stejná pole jako krok kromě `name`, `uses` a `needs`. Pravidla:

- pole, které krok sám deklaruje, vyhrává nad šablonou **celé** (deklarované `outputs` nahradí outputs šablony),
- `env` se merguje po klíčích, krok vyhrává,
- `run` je u kroku s `uses` volitelný; krok bez `run` i `uses` je chyba, stejně jako `uses` na neexistující šablonu.

Resoluce proběhne při načtení (po `extends`). Parametrizace skriptu se dělá přes env s defaulty v bashi
(`${VAR:-default}`), ne přes tokeny.

### Agentní kroky (`agent`)

Krok s `agent:` popisuje volání `agentiscode` daty místo ručně psaného skriptu. Při načtení se z něj vygeneruje `run`
(runtime vidí obyčejný bash krok). `agent` a `run` na jednom kroku jsou chyba; `agent` přebíjí `run` dodaný šablonou.

```yaml
- name: Select and summarize
  timeoutSeconds: 1200
  agent:
    adapter: ${NEWSLETTER_ADAPTER}       # povinné: claude | claude-p | opencode
    model: ${NEWSLETTER_MODEL}           # volitelné
    effort: high                         # volitelné
    taskOverrides: true                  # default: AGENTIS_ADAPTER/MODEL/EFFORT z env přepisují hodnoty výše
    promptFile: ${WORKDIR}/prompts/news-selection.md   # nebo inline `prompt: |`
    inputs:                              # soubory připojené za prompt, v tomto pořadí
      - label: "Už odvysíláno:"
        path: ${AGENTIS_RUN_DIR}/outputs/history.md
        optional: true                   # chybějící/prázdný soubor se přeskočí (jinak krok selže)
    outputSchema: ${WORKDIR}/schemas/news-selection.schema.json
    schemaRetries: 2                     # opravné turny přes --resume
    output: ${AGENTIS_RUN_DIR}/outputs/selection.json
    sessionOutput: ${AGENTIS_RUN_DIR}/outputs/session-id
    sessionTitle: Výběr zpráv
    extraArgs: ["--max-turns", "20"]
```

Vygenerovaný skript pošle prompt a vstupy na stdin `agentiscode --json`. Hodnoty se do skriptu vkládají v uvozovkách
a expandují se v nich jen odkazy `$NAME`, `${NAME}` a `${NAME:-text}`. Cokoli jiného (`$(…)`, backticky, uvozovky)
zůstane doslovným textem. Kroky běží pod `set -u`, proto nepovinné proměnné piš jako `${NAME:-}`. Inline `prompt`
se neexpanduje vůbec.

**Strukturovaný výstup.** S `outputSchema` dostane agent JSON Schema (Claude nativně, OpenCode v promptu). Odpověď se
validuje lokálně. Při neshodě naváže `agentiscode` na session (`--resume`) se seznamem chyb, nejvýš `schemaRetries`×.
Soubor `output` pak obsahuje zvalidovaný JSON. Když ani oprava nepomůže, `output` se nezapíše a krok selže.

Krok vyžaduje `agentiscode` s podporou `--output-schema` v image (nebo na hostu u `local` executoru). Balíček není
závislostí enginu.

### Paralelní kroky (`needs` + `maxParallel`)

Workflow je DAG nad `workflow.steps`:

- `needs` neuvedeno — implicitní závislost na předchozím kroku (`[]` u prvního kroku), tedy sekvenční pořadí,
- `needs: []` — root krok; může startovat hned, jakmile je volný slot,
- `needs: ["A", "B"]` — krok čeká na dokončení dříve definovaných kroků `A` a `B`.

`needs` smí ukazovat jen na kroky definované výše. Pokud workflow používá `needs`, názvy kroků musí být unikátní.
Forward reference, duplicitní položky a neznámé názvy jsou chyba validace.

`workflow.maxParallel` (default `4`, minimum `1`) omezuje současně běžící kroky. Retry kroku zabírá jeden slot.

`needs` je ordering dependency, ne success gate:

- dependency přeskočená přes `if` dependent odblokuje,
- dependency selhaná s `continueOnError: true` dependent odblokuje,
- fatální selhání bez `continueOnError` přepne run do fail-fast režimu (viz Error handling).

Autor ručí za bezpečnost paralelních zápisů do sdíleného `workdir` a output adresářů. Engine nepozná, jestli skript
čte, nebo zapisuje, a žádný lock nedává.

### Interpolace tokenů

Ve stringových hodnotách YAML lze použít `[%NAME%]`; nahradí se při načtení. Jiné jméno je chyba validace. Neznámá
hodnota se nahradí prázdným stringem. Hodnoty dodá volající přes `spec.vars` (`--var NAME=value` v CLI).

| Token | Hodnota |
| --- | --- |
| `NAMESPACE` | Namespace runu z `spec.namespace`; local a docker jej používají jen v metadatech |
| `WORKDIR` | absolutní cesta k `workdir` |
| `RUN_DIR` | adresář run souborů (prompt, context, outputs, logy) |
| `MAIN_DIR` | hlavní adresář projektu; hodnota z `spec.vars`, jinak prázdná |
| `RUN_ID` / `TASK_ID` / `TASK_NUMBER` / `TASK_TITLE` | Agentis-specifické identifikace; engine je jen předá dál |
| `BRANCH` / `BASE_BRANCH` | Agentis/git-specifické hodnoty; jen předávají, engine s git nepracuje |
| `GITHUB_REPO` | Agentis-specifické; jen předává |

Allowlist je v `schema.py` (`INTERPOLATION_ALLOWLIST`). Když volající token nedodá ve `vars`, interpoluje se na prázdný
string.

### Prostředí kroků

Každý krok dostane:

- `workflow.env` a `step.env` (v tomto pořadí; `step.env` vyhrává),
- `spec.env` (nesecretní), a pak hodnoty ze `spec.pass_env` z env procesu enginu,
- všechny interpolační tokeny jako env proměnné (`WORKDIR`, `RUN_DIR`, …),
- `AGENTIS_RUN_DIR`, `AGENTIS_PROMPT_FILE` (soubor s promptem), `AGENTIS_CONTEXT_FILE` (context JSON),
- `AGENTIS_WORKFLOW_STATUS` a `AGENTIS_FAILED_STEP` jen pro `always` kroky (viz níže),
- proměnné z `var` outputů transitivních `needs` kroku.

Prefix `AGENTIS_*` zůstává (kompatibilita). Engine samotný tyto proměnné neinterpretuje.

### Podmínky `if`

Krok s `if` se spustí jen při splnění podmínky. Proměnné podmínky:

- `var` outputy transitivních `needs` kroku,
- built-in hodnoty runu (tokeny z tabulky výše), stejná jména jako env,
- env proměnné kroku (`workflow.env`, `spec.env`, `spec.pass_env`, `step.env`).

Při kolizi jmen vyhrává viditelný `var` output nad built-in hodnotou i nad env. Paralelní větev, na které krok
přes `needs` nezávisí (ani transitivně), jeho `if` neovlivní.

Gramatika: termy `VAR`, `!VAR`, `VAR == hodnota`, `VAR != 'hodnota'` spojené `&&` a `||`. `&&` má přednost před `||`;
závorky nejsou. Negace `!` platí jen na holý term. Hodnota s mezerami nebo se spojkou musí být v uvozovkách.

Neznámá proměnná je prázdný string; holý test `VAR` bere `""`/`0`/`false`/`no` (case-insensitive) jako nepravdu.
Syntaxe se validuje při načtení. Přeskočený krok se hlásí jako `step_skipped` (viz [cli.md](cli.md)); jeho outputy se
neaplikují.

```yaml
- name: Check environment
  run: |
    mkdir -p outputs
    printf 'true' > outputs/env-ready
  outputs:
    - type: var
      name: ENV_READY
      valueFrom: outputs/env-ready
- name: Create virtualenv
  if: ENV_READY != 'true'
  run: python3.13 -m venv .venv
```

### Outputs

Kroky komunikují s volajícím přes soubory. Cesty jsou relativní k output rootu (`spec.output_root`, default `workdir`)
a nesmí z něj utéct, ani přes symlink. Typ `var` se čte hned po úspěšném doběhnutí kroku, aby mohl řídit dependenty.
Ostatní outputy se agregují po konci runu v pořadí kroků a vrátí se v `result.json` v poli `outputs`.

| Typ | Pole | Význam | Interpretuje |
| --- | --- | --- | --- |
| `var` | `name`, `valueFrom` | Proměnná pro `if` a env dalších kroků; vrací se v `result.vars` | engine |
| `agent_comment` | `bodyFrom`, `status`, `name`/`nameFrom` | **Agentis-specifické:** tělo komentáře, cílový status, autor | volající |
| `session_id` | `valueFrom` | **Agentis-specifické:** session id pro pozdější resume | volající |
| `url` / `text` | `label`, `valueFrom` | Příloha (odkaz / text) | volající |
| `artifact` | `name`, `path` | Soubor nebo glob souborů; engine je zkopíruje do staging adresáře a vrátí v `outputs.artifacts` | volající |

Pole `status` u `agent_comment` engine nevynucuje na číselník; validuje pouze tvar a výsledek vrací beze změny.
Schéma navíc přijímá pole `filename`, které engine aktuálně nepoužívá. Neúplný output runtime typicky ignoruje.

Artifact `path` může obsahovat `*`, `?`, `[]` a `**`. Výsledky globu se řadí podle cesty; adresáře a cesty mimo
output root se ignorují. Počet a velikost artefaktů omezují limity enginu (viz [cli.md](cli.md#artefakty)); glob proto musí být dostatečně úzký.

Outputy se aplikují po konci runu **jen za úspěšně doběhlé kroky** v pořadí kroků v YAML, i když run jako celek
selhal. Outputy přeskočených a selhaných kroků se neaplikují. Engine je pouze vrátí; co s nimi volající udělá, je na něm.

### Error handling kroků

Selhaný krok bez příznaků níže přepne run do fail-fast režimu: nové běžné kroky se nestartují, pending ne-`always`
kroky se označí jako skipped a už běžící kroky se nechají doběhnout. Pak se spustí relevantní `always` kroky
a výsledek je `failed` s `failed_step` prvního fatálně selhaného kroku.

- **`continueOnError: true`** — selhání workflow nezastaví a dependenty odblokuje. Krok se hlásí jako `step_failed`
  s `continue_on_error: true`, jeho `var` outputy se nečtou a ostatní outputy se neaplikují.
- **`retries: N`** — selhaný krok se zopakuje až N× bez backoffu (nejvýš `N + 1` spuštění). Failed event obsahuje
  `attempts`. Abort mezi pokusy run ukončí. Opakovaný pokus dostane unikátní jméno jobu (`<job>-r<n>`), takže local
  executor má každý pokus vlastní log. Retry zabírá jeden slot `maxParallel`.
- **`always: true`** — krok běží i po fatálním selhání (úklid, failure komentář). Respektuje `needs` jako pořadí,
  ale nevyžaduje úspěch: čeká, až dependencies skončí. Skript dostane `AGENTIS_WORKFLOW_STATUS` (`failed`/`success`)
  a `AGENTIS_FAILED_STEP`. Tyto proměnné se přidají až po vyhodnocení `if`, takže je lze použít v `run`, ne v podmínce.

`abort` není failure: zastaví aktivní kroky, nespustí pending ani `always` kroky, outputy se neaplikují a run skončí
`status: aborted`.

### Followup akce (Agentis-specifické)

Sekce `workflow.followups` je validovaná stejnou gramatikou jako `if`. Engine vyhodnotí `if` nad výsledkem runu
a vrátí jen nabídnuté akce v poli `followups` v `result.json`. Nabídku a spuštění dalšího workflow řeší volající.
Followupy se nevyhodnocují u `failed` a `aborted` runu.

```yaml
followups:
  - title: Git merge
    if: PR_CREATED && !AGENTIS_AUTO_MERGE
    prompt: Sloučit změny z task větve do hlavní větve.
    workflow: merge
    continue_previous_run: false
```

## Ukázky

Bundled příklady v `examples/` (bez Agentis závislostí):

- `examples/hello.yaml` — local executor, `var` output a podmínka `if`,
- `examples/report.yaml` — krok s artefaktem, závislostmi a `always` reportem,
- `examples/parallel.yaml` — paralelní kroky přes `needs`, `maxParallel` a `continueOnError`.

Testovací běh a kontrola syntaxe jsou v [testovani-workflow.md](testovani-workflow.md).

## Časté chyby

- **Executor `kubernetes` nebo `docker` vyžaduje `image`** — krok nemá `image` ani workflow default; doplň ji, nebo přepni `executor: local`.
- **`Workflow file not found`** — soubor není v žádném `search_paths`; zkontroluj `--workdir` a jméno.
- **`Workflow extends target not found`** — `extends` ukazuje na neexistující soubor vedle workflow.
- **`chained 'extends' is not supported`** — rodič má vlastní `extends`; dědičnost má jen jednu úroveň.
- **`uses unknown step template`** — šablona není ve `workflow.stepTemplates` po `extends` merge.
- **`unknown or future 'needs'`** — `needs` odkazuje na neexistující krok nebo na krok definovaný níže.
- **`workflow steps using 'needs' require unique step names`** — názvy kroků musí být unikátní.
- **`duplicate 'needs' entries`** — jeden krok má v `needs` stejné jméno vícekrát.
- **`Unknown workflow token [%X%]`** — token mimo allowlist.
- **Busy** — pro stejné labely už běží aktivní run; počkej, nebo zavolej `agentis-workflow abort` (exit kód 3, viz [cli.md](cli.md)).
- **Output se nepropsal** — krok byl přeskočen nebo selhal, cesta vede mimo output root, nebo chybí funkčně potřebné pole.
  Prázdné `agent_comment`, `session_id`, `url` a `text` se vynechají; `var` se propaguje jako prázdný string.
