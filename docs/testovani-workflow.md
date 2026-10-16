# Testování workflow před nasazením

Workflow (`.agentis/workflows/*.yaml` nebo `examples/*.yaml`) se dá vyzkoušet přímo příkazem `agentis-workflow`.
Engine nepotřebuje Agentis, WebSocket ani git worktree.

```bash
cd /var/www/muj-projekt                          # adresář, ve kterém workflow hledáme
agentis-workflow list                            # jaká workflow jsou dostupná
agentis-workflow validate .agentis/workflows/*.yaml
agentis-workflow run ai-news --prompt "Zaměř se na nové modely" --executor local
```

Výstup `run` vypadá zhruba takto (průběh na stderr, výsledek v `result.json`):

```
Workflow ai-news  (/var/www/muj-projekt/.agentis/workflows/ai-news.yaml)
executor local · run 3f2a…

▶ Fetch news
✓ Fetch news (8.2s)
▶ Select and summarize
✓ Select and summarize (41.0s)
▶ Render newsletter
✓ Render newsletter (0.1s)
↷ Step skipped (if: SEND_EMAIL): Send newsletter email

✓ Workflow finished in 49.6s  status=success
  run dir:    /tmp/agentis-workflow/3f2a…
  result:     /tmp/agentis-workflow/3f2a…/result.json
  outputs:    /tmp/agentis-workflow/3f2a…/outputs
```

U selhaného kroku se vypíše konec jeho logu. Exit kódy: `0` úspěch, `1` workflow selhalo,
`2` chybný vstup (neexistující workflow, nevalidní YAML, špatné flagy), `3` busy, `130`/`143` přerušeno
(`Ctrl+C` posílá SIGINT, `kill` SIGTERM). Přerušení zastaví běžící kroky. Kompletní kontrakt je v [cli.md](cli.md).

## `run`

```
agentis-workflow run WORKFLOW [volby]
```

`WORKFLOW` je jméno (`ai-news`, hledá se v `.agentis/workflows/` relativně k `--workdir`) nebo cesta k YAML.

| Volba | Výchozí | Význam |
| --- | --- | --- |
| `--prompt TEXT` | prázdný | Text zadání → `$AGENTIS_PROMPT_FILE` (v run dir jako `prompt.md`) |
| `--prompt-file PATH` | — | Zadání ze souboru, `-` = stdin (nelze kombinovat s `--prompt`) |
| `--workdir DIR` | `.` | Pracovní adresář; kroky běží nad ním (`WORKDIR`) |
| `--var NAME=VALUE` | — | Hodnota interpolačního tokenu `[%NAME%]` (opakovatelné) |
| `--env NAME=VALUE` | — | Nesecretní env pro kroky (opakovatelné) |
| `--executor local\|docker\|kubernetes` | podle YAML / `WORKFLOW_EXECUTOR` | Vynucený executor |
| `--namespace NAME` | vygenerovaný | Namespace (jen `kubernetes`) |
| `--run-dir DIR` | `/tmp/agentis-workflow/<run_id>` | Kam zapsat prompt, logy, outputs a `result.json` |
| `-v, --verbose` | vypnuto | Po každém kroku vypíše jeho log; u neúspěšného kroku celý log místo posledních 30 řádků |

Interpolační tokeny `[%TASK_TITLE%]`, `[%BRANCH%]` apod. nastavíš přes `--var`. Chybějící token se nahradí prázdným
stringem. Secrety do ručního běhu nepředávej jako `--env`. Engine je přečte z env procesu přes `pass_env` v YAML.

### Volba executoru

- **`local`** — kroky běží jako bash procesy přímo na hostu, pod aktuálním uživatelem, bez sandboxu.
  Nejrychlejší pro ladění skriptů. `image`, `mounts` a `imagePullSecrets` se ignorují, takže **neověří**, že
  image obsahuje potřebné nástroje.
- **`docker`** — každý krok v kontejneru přes `docker run`. Ověří image bez Kubernetes.
- **`kubernetes`** — joby přes `kubectl` (vyžaduje kube context). Nejvěrnější produkci.

Než workflow nasadíš, pusť ho aspoň jednou v executoru, v jakém poběží v produkci.

## `validate`

```bash
agentis-workflow validate .agentis/workflows/*.yaml        # lidský výstup
agentis-workflow validate --json examples/hello.yaml       # strojový výstup
```

Načte YAML stejným loaderem jako engine: syntaxe, `extends`, `uses`, povolené `[%TOKEN%]`, striktní schéma (neznámé
klíče), `needs` a `if`. Nekontroluje obsah image, dostupnost `envFiles` ani to, zda cesty v `outputs` odpovídají
souborům, které kroky zapisují. To ukáže až `run`.

Ve VS Code totéž průběžně hlásí rozšíření z `vscode-agentis-workflow/`.

## Kroky s vedlejšími účinky

`run` vykoná **všechny** kroky, včetně odeslání e-mailu, pushe nebo deploye. Takové kroky dej pod podmínku a v testu
proměnnou nenastavuj:

```yaml
workflow:
  env:
    SEND_EMAIL: "1"      # v produkci; pro test zakomentuj nebo nastav na ""
  steps:
    - name: Send newsletter email
      if: SEND_EMAIL
```

Holá podmínka bere chybějící, prázdnou, `0`, `false` i `no` hodnotu jako nepravdu, takže se krok přeskočí (ve výpisu `↷`).
Alternativně přesměruj příjemce přes `workflow.env` na testovací adresu.

## Kde hledat výsledky

Každý běh dostane run dir (`--run-dir`, default `/tmp/agentis-workflow/<run_id>`):

```
<run_dir>/
├── prompt.md               # zadání (--prompt / --prompt-file)
├── context.json            # kontext předaný krokům
├── logs/<job>.log          # stdout/stderr každého kroku
├── outputs/                # soubory, které kroky zapsaly do $AGENTIS_RUN_DIR/outputs
├── artifact-staging/       # zkopírované artefakty z `type: artifact` outputů
└── result.json             # výsledek runu (status, vars, outputs, followups, warnings)
```

Cestu vypíše `run` na konci. Výsledek lze přečíst strojově, například `jq '.status, .vars' <run_dir>/result.json`.

## Strojová varianta: spec.json

Stejný běh jde připravit jako `spec.json` a spustit přes `run --spec` (to dělá adapter). Viz [cli.md](cli.md).
Pro ruční testy stačí flagy výše.

## Instalace

```bash
cd /var/www/agentis-workflow
poetry install
poetry run agentis-workflow --version
```

Pro globální příkaz nainstaluj balíček přes pipx jako editable, aby se změny v repu projevily bez reinstalace:

```bash
pipx install --force -e /var/www/agentis-workflow
```

## Kontrolní seznam

- [ ] `agentis-workflow validate` prošel
- [ ] `agentis-workflow run <jméno>` skončil `status=success` a `result.json` obsahuje očekávané `outputs`
- [ ] Soubory v run dir (`outputs/`, `artifact-staging/`) mají očekávaný obsah
- [ ] Běh aspoň jednou ve stejném executoru jako v produkci (`--executor docker` nebo `kubernetes`)
- [ ] Kroky s vedlejšími účinky byly při testu vypnuté nebo přesměrované
