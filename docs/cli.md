# CLI kontrakt `agentis-workflow`

Engine se volá výhradně z příkazové řádky. Volající (například `agentis-adapter` přes `subprocess`) nepoužívá Python
import. Kontrakt tvoří argumenty, `spec.json`, JSONL události na stdout, `result.json` v run adresáři a exit kódy.
Verze kontraktu je `PROTOCOL_VERSION` (aktuálně `1`). Mění se jen vědomě a s aktualizací této stránky.

## Příkazy

```bash
agentis-workflow --version                              # verze balíčku + protokol
agentis-workflow run --spec <run_dir>/spec.json --events jsonl   # strojový běh (adapter)
agentis-workflow run <name> "..." --workdir . --var KEY=value --executor local   # ruční běh
agentis-workflow run <name> --prompt-file prompt.md --verbose
agentis-workflow check --spec <run_dir>/spec.json       # synchronní kontrola bez spuštění kroků
agentis-workflow validate [--json] <soubor.yaml> [...]  # kontrola YAML
agentis-workflow list [--workdir .]                     # dostupná workflow v search_paths
agentis-workflow abort --executor kubernetes --namespace <ns> --label agentis.task_id=<id>
```

| Příkaz | Účel |
| --- | --- |
| `run --spec` | Spustí run podle `spec.json`. Události jdou na stdout (JSONL), výsledek do `result.json`. |
| `run <name>` | Ruční běh bez spec souboru. Flagy se interně složí do stejného spec objektu. |
| `check --spec` | Načte a zvaliduje workflow, vyřeší executor, ověří images a busy stav. Nic nespouští. |
| `validate [--json]` | Validace YAML souborů (syntaxe, `extends`, `uses`, tokeny, `needs`, `if`, followupy). |
| `list` | Vypíše dostupná workflow v `search_paths`. |
| `abort` | Zruší aktivní joby/kontejnery/procesy podle labelů. Použije se po restartu volajícího. |

### Ruční režim `run <name>`

| Flag | Význam |
| --- | --- |
| `--workdir PATH` | Pracovní adresář (default `.`); výchozí `search_paths` = `<workdir>/.agentis/workflows` |
| `--search-path PATH` | Další adresář s workflow (opakovatelné, precedence v pořadí zadání) |
| `PROMPT` (poziční) / `--prompt-file PATH` (`-` = stdin) | Zadání; zapíše se do `prompt.md` v run dir. Bez obojího se čte stdin, pokud není TTY |
| `--var NAME=value` | Hodnota interpolačního tokenu (opakovatelné) |
| `--env NAME=value` | Nesecretní env pro kroky (opakovatelné) |
| `--executor local\|docker\|kubernetes` | Vynucený executor |
| `--namespace NAME` | Namespace (kubernetes); default generovaný z názvu runu |
| `--run-dir PATH` | Kam zapsat run soubory; default `<workdir>/.agentis/runs/<run_id>` |
| `--events jsonl\|console\|none` | Výstup průběhu; default `console` u ručního běhu (`jsonl` u `--spec`) |
| `--verbose` | Vypíše celý log každého kroku (ne jen posledních řádků) |

Ruční běh v režimu `console` vypisuje lidsky čitelný průběh a na konci souhrn (status, vars, komentáře, přílohy,
artefakty) na stdout; `result.json` se zapíše stejně jako u `--spec`. Diagnostika jde vždy na stderr.

## Vstup: `spec.json`

```jsonc
{
  "protocol": 1,
  "workflow": "default",                         // jméno (basename bez .yaml) nebo cesta k souboru
  "search_paths": [".agentis/workflows", "/opt/project/workflows"],   // pořadí = precedence
  "workdir": "/work/project",
  "run_dir": "/work/project/.agentis/runs/0193ab...",
  "output_root": "/work/project",                 // proti čemu se resolvují valueFrom/path; default workdir
  "prompt_file": "prompt.md",                    // relativně k run_dir; obsah zapisuje volající
  "context_file": "context.json",                // opaque pro engine, jen se vystaví v env
  "executor": "kubernetes",                      // vynucený executor (nebo null)
  "executor_default": "kubernetes",              // použije se, když executor není vynucen a YAML nic neříká
  "namespace": "run-12-foo",
  "labels": {"agentis.task_id": "...", "agentis.run_id": "...", "agentis.attempt": "0193ab..."},
  "vars": {"TASK_TITLE": "Denní report", "BRANCH": "task/12"},   // hodnoty [%TOKEN%]; chybějící = ""
  "env": {"AGENTIS_MODEL": "opus"},              // nesecretní env pro kroky
  "pass_env": ["AGENTIS_SERVICE_TOKEN"],         // jména proměnných z env PROCESU enginu
  "require_vars": ["APPROVED"]                   // var outputy, které workflow musí deklarovat
}
```

Pravidla:

- Modely jsou striktní (`extra="forbid"`); neznámé klíče jsou chyba (exit `2`).
- Relativní `prompt_file` a `context_file` se resolvují vůči `run_dir`.
- Labely určují busy kontrolu a `abort`. Platí pro ně omezení jmen a hodnot (Kubernetes label syntax).
- `namespace` se používá jen u `kubernetes`; u ostatních executorů se jen zobrazí v metadatech.
- **Secrety nikdy do `spec.json`, `argv`, logů ani `result.json`.** Ve spec je jen jméno v `pass_env`; hodnota se
  vezme z env procesu enginu a předá se krokům. Volající ji nastaví v env child procesu, například
  `subprocess.Popen(argv, env={**os.environ, "AGENTIS_SERVICE_TOKEN": token})`. `ps` ani soubory na disku ji nevidí.

## Kontrola: `check --spec`

Synchronní kontrola bez spuštění kroků. Adapter ji volá v `start` před `run`, aby chyby přišly hned v odpovědi.

Na stdout vypíše JSON:

```json
{"protocol":1,"workflow_file":"/work/project/.agentis/workflows/default.yaml","executor":"kubernetes",
 "namespace":"run-12-foo","steps":[{"name":"Prepare","needs":[],"image":"registry/img:1"}]}
```

Pole `steps` obsahuje kroky v pořadí YAML: `name`, `needs` (resolvované závislosti), `image` (`null` u `local`).

| Exit | Význam |
| --- | --- |
| `0` | v pořádku |
| `2` | nevalidní workflow, spec, neznámý executor nebo chybějící image |
| `3` | busy: pro stejné labely už běží aktivní joby/kontejnery/procesy |

Chybová zpráva jde na stderr a neobsahuje hodnoty secretů.

## Validace: `validate`

```bash
agentis-workflow validate .agentis/workflows/*.yaml          # lidský výstup
agentis-workflow validate --json .agentis/workflows/*.yaml   # strojový výstup
```

Ověří YAML syntaxi, `extends`, `uses`, povolené `[%TOKEN%]`, striktní schéma, `needs`, `if` a followupy.
Nespouští kroky a nekontroluje dostupnost images ani nástrojů v nich. Existenci `envFiles` ověřuje relativně k projektu
(adresář nad `.agentis/`). Exit `0`, pokud všechny soubory
projdou; jinak `2`.

## Události: JSONL na stdout (`run --spec --events jsonl`)

Každý řádek je JSON objekt s `type`. Diagnostika enginu jde na stderr, nikdy do stdout.

```jsonc
{"type":"workflow_started","attempt":"...","namespace":"...","executor":"kubernetes","steps":["prepare","agent"]}
{"type":"step_started","index":0,"step":"prepare","needs":[]}
{"type":"step_succeeded","index":0,"step":"prepare","job":"...","attempts":1,"duration_ms":1234,"vars":{"FOO":"1"}}
{"type":"step_failed","index":1,"step":"agent","attempts":3,"log_tail":"...","continue_on_error":false}
{"type":"step_skipped","index":2,"step":"deploy","reason":"condition","condition":"FOO == '1'"}
{"type":"warning","message":"artifact glob matched no files","data":{}}
{"type":"workflow_finished","status":"failed","failed_step":"agent"}
```

- `index` je pozice v YAML; události přicházejí v reálném pořadí běhu, ne nutně v pořadí YAML.
- `log_tail` obsahuje posledních ~50 řádků logu kroku.
- Události jsou informační. Autoritativní výsledek je `result.json`.

## Výsledek: `result.json`

Zapisuje se atomicky (tmp soubor + přejmenování) do `run_dir` na konci runu, i při selhání nebo abortu.

```jsonc
{
  "protocol": 1,
  "status": "success | failed | aborted",
  "failed_step": null,
  "vars": {"APPROVED": "1"},
  "outputs": {                                    // jen z úspěšných, nepřeskočených kroků, v pořadí YAML
    "comments": [{"body": "...", "status": "in_review", "author_name": null}],
    "session_id": "...",
    "attachments": [{"label": "Preview", "value": "https://...", "type": "url"}],
    "artifacts": [{"name": "report.pdf", "filename": "report.pdf", "path": "artifact-staging/report.pdf", "size": 1234}]
  },
  "followups": [{"title": "Git merge", "prompt": "...", "workflow": "merge", "continue_previous_run": false}],
  "warnings": [{"message": "...", "data": {}}]
}
```

- `followups` obsahují jen akce, jejichž `if` platí; při `failed` nebo `aborted` jsou prázdné.
- `comments`, `session_id`, `attachments`, `followups` jsou **Agentis-specifická** data: engine je validuje a vrací
  beze změny. Interpretuje je volající.
- Outputy skipped a failed kroků se nikdy nezapisují.
- Artefakty jsou zkopírované do `run_dir/artifact-staging/`; `path` je relativní k `run_dir`.

### Artefakty

Limity enginu (konstanty v `agentis_workflow/engine.py`):

| Limit | Hodnota |
| --- | --- |
| Počet souborů v jednom runu | 50; další soubory se přeskočí s `warning` |
| Celková velikost | 50 MiB; soubor, který by limit překročil, se přeskočí s `warning` |
| Délka jména artefaktu | 255 znaků |

Cesty, které po resolvu ukazují mimo `output_root` (včetně přes symlink), se ignorují.

## Exit kódy

| Kód | Význam |
| --- | --- |
| `0` | run `success` |
| `1` | run `failed` (fatální selhání kroku) |
| `2` | nevalidní workflow, spec nebo usage |
| `3` | busy (aktivní joby se stejnými labely) |
| `130` | aborted přes SIGINT |
| `143` | aborted přes SIGTERM |

`result.json` se zapisuje pro běhy, které dospěly do fáze runu (kódy `0`, `1`, `130`, `143`).

## Abort

Volající pošle `SIGTERM` procesu enginu (nebo skupině procesů). Engine nastaví abort event, zruší běžící kroky
(Kubernetes Joby, Docker kontejnery, lokální procesy), nespustí pending ani `always` kroky, zapíše `result.json`
se `status: "aborted"` a skončí `143`.

Když volající proces nemá (například po restartu), použije `agentis-workflow abort --executor ... --label ...`.
Ten smaže nebo zastaví joby/kontejnery/procesy s odpovídajícími labely a je idempotentní.

## Chyby a logy

- Stdout je jen pro `--events jsonl`, `--events console`, `check`, `validate` a `list`. Ostatní diagnostika jde na stderr.
- Chybové zprávy neobsahují hodnoty z `pass_env`, `AGENTIS_*TOKEN` ani hlavičky autentizace.
- Logy kroků jsou v `run_dir/logs/<job>.log`.

## Verzování

`--version` vypíše verzi balíčku a `protocol` číslo. Volající před spuštěním ověří, že podporuje tuto verzi protokolu.
Změna `spec`, `result` nebo JSONL typů zvyšuje `PROTOCOL_VERSION` a musí být zapsaná v této stránce.
