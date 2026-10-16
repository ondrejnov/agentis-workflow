# Vzory kroků pro agentis-workflow

Příklady předpokládají, že `output_root` je adresář, nad kterým run běží (`workdir`, v ručním běhu `--workdir`).
Pokud chceš vlastní run adresář jako output root, zapisuj do `"$AGENTIS_RUN_DIR/outputs/…"` a deklaruj `outputs/…`.

## Obsah

1. Agent nad zadáním (`agent:` krok)
2. Přímé volání `agentiscode` s vlastním vstupem (LLM zpracuje data)
3. LLM vybírá, výstup se renderuje deterministicky
4. Proměnné mezi kroky + podmínky
5. Paralelní kroky
6. Úklid a report selhání přes `always`
7. Artefakty a přílohy
8. Rozhodovací krok (`var` výstup pro volajícího)
9. Kroky nad jiným adresářem

---

## 1. Agent nad zadáním (`agent:` krok)

Krok `agent:` vygeneruje bash volání `agentiscode`. Prompt se načte z `promptFile` (nebo inline `prompt:`), vstupy
se připojí za něj a finální odpověď jde do `output`:

```yaml
- name: Write summary
  timeoutSeconds: 1200
  agent:
    adapter: ${SUMMARY_ADAPTER}
    model: ${SUMMARY_MODEL}
    promptFile: ${WORKDIR}/prompts/summary.md
    inputs:
      - label: "Data:"
        path: ${AGENTIS_RUN_DIR}/outputs/data.json
    output: ${AGENTIS_RUN_DIR}/outputs/summary.md
  outputs:
    - type: artifact
      name: summary.md
      path: outputs/summary.md
```

Hodnoty `${NAME}` se expandují z env; nepovinné piš jako `${NAME:-}`. Inline `prompt` se neexpanduje.
Se `outputSchema` je `output` zvalidovaný JSON; když ani oprava nepomůže, krok selže (`retries` / `continueOnError`).

## 2. Přímé volání `agentiscode` (LLM zpracuje data z předchozího kroku)

```yaml
- name: Summarize
  run: |
    { cat "$AGENTIS_PROMPT_FILE"; printf '\n---\n'; cat "$AGENTIS_RUN_DIR/outputs/data.json"; } \
      | agentiscode --adapter opencode --json \
          --model "${SUMMARY_MODEL:-openai/gpt-5.4-mini}" \
          ${AGENTIS_EFFORT:+--effort "$AGENTIS_EFFORT"} \
          --final-output "$AGENTIS_RUN_DIR/outputs/summary.md"
  outputs:
    - type: artifact
      name: summary.md
      path: outputs/summary.md
```

Užitečné volby: `--adapter opencode|claude`, `--model`, `--effort`, `--resume`, `--json` (stream na stdout, vidět v logu).
Prompt jde na stdin nebo jako poziční argument. Formát výstupu (Markdown, HTML…) si řekni v promptu.

## 3. LLM vybírá, výstup se renderuje deterministicky

LLM vrátí jen JSON s ID položek; titulky a odkazy se doplní ze stažených dat, takže nic nevymyslí.

```yaml
- name: Select items
  run: |
    {
      printf 'Vyber nejvýše 5 položek. Odpověz POUZE JSON polem [{"id": ..., "why": "..."}].\n'
      printf 'Preference:\n'; cat "$AGENTIS_PROMPT_FILE"
      printf '\nData:\n'; cat "$AGENTIS_RUN_DIR/outputs/items.json"
    } | agentiscode --adapter opencode --model "$REPORT_MODEL" \
          --final-output "$AGENTIS_RUN_DIR/outputs/selection.json"

- name: Render digest
  run: |
    python3 - <<'PY'
    import html, json, os, re
    from pathlib import Path
    out = Path(os.environ["AGENTIS_RUN_DIR"]) / "outputs"
    raw = (out / "selection.json").read_text(encoding="utf-8")
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())   # LLM občas obalí do ```json
    items = {str(i["id"]): i for i in json.loads((out / "items.json").read_text())}
    picked = [(items[str(s["id"])], s.get("why", "")) for s in json.loads(raw) if str(s.get("id")) in items]
    (out / "digest.html").write_text("<ul>" + "".join(
        f'<li><a href="{html.escape(i["url"])}">{html.escape(i["title"])}</a> — {html.escape(w)}</li>'
        for i, w in picked) + "</ul>")
    PY
  outputs:
    - type: artifact
      name: digest.html
      path: outputs/digest.html
```

Pokud má schéma záviset na datech runu (např. povolená ID), předchozí krok vygeneruje jeho kopii do run adresáře.

## 4. Proměnné mezi kroky + podmínky

```yaml
- name: Detect changes
  run: |
    mkdir -p outputs
    if git diff --quiet "origin/$BASE_BRANCH"...HEAD; then printf '' ; else printf 'true'; fi > outputs/has-changes
  outputs:
    - type: var
      name: HAS_CHANGES
      valueFrom: outputs/has-changes

- name: Run tests
  if: HAS_CHANGES
  run: poetry run pytest -q
```

`var` se čte hned po úspěchu kroku; závislé kroky ho dostanou v `if` i jako env (`$HAS_CHANGES`).
Holý test `VAR` bere `""`, `0`, `false`, `no` jako nepravdu. `if` může použít i env (`workflow.env`) a built-in tokeny.

## 5. Paralelní kroky

```yaml
workflow:
  maxParallel: 3
  steps:
    - name: Fetch A
      needs: []
      run: ...
    - name: Fetch B
      needs: []
      run: ...
    - name: Merge results
      needs: ["Fetch A", "Fetch B"]
      run: ...
```

Paralelní kroky nesmí zapisovat stejný soubor. `var` z paralelní větve vidí jen kroky, které na ní přes `needs`
závisí. Příklad celé varianty: `examples/parallel.yaml`.

## 6. Úklid a report selhání přes `always`

```yaml
- name: Report failure
  always: true
  run: |
    [ "${AGENTIS_WORKFLOW_STATUS:-success}" = "failed" ] || exit 0
    mkdir -p outputs
    printf 'Workflow selhalo v kroku „%s“.\n' "$AGENTIS_FAILED_STEP" > outputs/failure.txt
  outputs:
    - type: artifact
      name: failure.txt
      path: outputs/failure.txt
```

Po úspěšném runu soubor nevznikne, takže se artefakt nevrátí. `AGENTIS_WORKFLOW_STATUS` nelze použít v `if`
(přidává se až po jeho vyhodnocení), proto test v `run`. Vzorový běh: `examples/report.yaml`.

## 7. Artefakty a přílohy

```yaml
outputs:
  - type: artifact
    name: Screenshots
    path: outputs/screens/*.png        # glob; drž ho úzký (limit 50 souborů / 50 MiB na run)
  - type: url
    label: Pull request
    valueFrom: outputs/pr-url
  - type: text
    label: Test log
    valueFrom: outputs/pytest.txt
```

Engine vrací artefakty v `result.json` (`outputs.artifacts`, cesty do `artifact-staging/`) a přílohy v `outputs.attachments`.
Co s nimi volající udělá, je jeho věc.

## 8. Rozhodovací krok (`var` výstup pro volajícího)

Když volající potřebuje rozhodnutí (například schválení), krok zapíše `var` output a volající ho přečte z
`result.vars`. Příkaz nikdy nevykonávej ani neinterpoluj do shellu — zpracuj ho jako data přes `jq`:

```yaml
- name: Decide
  run: |
    command="$(jq -r '.command // ""' <<< "${DECISION_INPUT:-null}")"
    if [[ "$command" == "git status --short" ]]; then echo 1; else echo 0; fi > "$AGENTIS_RUN_DIR/decision.data"
  outputs:
    - type: var
      name: DECISION
      valueFrom: decision.data
```

Vstupní data předá volající přes `vars` nebo `env` (nesecretní), nikoli jako kód v YAML.

## 9. Kroky nad jiným adresářem

`workingDir` na kroku přepíše výchozí adresář, např. `workingDir: "[%MAIN_DIR%]"`. V kontejnerových executorech musí
být adresář dostupný přes mount (`mounts` s `hostPath`). `WORKDIR`, `RUN_DIR` a absolutní cesty `AGENTIS_RUN_DIR`
a `MAIN_DIR` Docker executor bind-mountuje sám.
