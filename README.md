# term-assistant

AI-stödd terminologitjänst som kan ställas mot **valfri FHIR-terminologitjänst**,
till exempel Ineras terminologitjänst, tx.nordics eller tx.fhir.org. Systemet har
två ben:

1. **MCP-server för terminologi.** Den är inspirerad av
   [Echidnas MCP-gränssnitt](https://fhir.ohdsi.org/docs/getting-started/mcp) men
   fristående: den ligger *framför* vilken FHIR R4-terminologitjänst som helst och
   använder bara standardoperationer (`$lookup`, `$validate-code`, `$expand`,
   `$subsumes`, `$translate` och sökningar). En AI-assistent (Claude Desktop,
   Claude Code m.fl.) kan då slå upp, validera, expandera, navigera hierarkier
   och översätta via ConceptMap utifrån befintliga FHIR-resurser i stället för
   en specialbyggd datamodell.
2. **Inläsning av kodverk i Excel.** Bladet läses in en gång, kolumnerna mappas
   mot FHIR med AI-stöd, och resultatet blir en **mall**. Samma mall är sedan
   specifikationen för en skräddarsydd administrationsvy där kodverket underhålls
   vidare: releasekandidater, releaser, byte av vilken egenskap en kolumn står
   för och nya kolumner.

Designen beskrivs i [docs/arkitektur.md](docs/arkitektur.md).

## Kom igång

```bash
pip install -e '.[dev,ai]'
pytest
```

### MCP-servern

```bash
export TX_BASE_URL=https://tx.fhir.org/r4      # eller er egen tjänst
export TX_TOKEN=...                            # valfritt, bearer-token
term-assistant mcp                             # stdio
term-assistant mcp --http                      # streamable HTTP
```

`.mcp.json` i repot registrerar servern i Claude Code. Byt `TX_BASE_URL` för att
peka mot Inera eller tx.nordics.

| Verktyg | FHIR-operation |
|---|---|
| `server_info` | `GET /metadata` |
| `list_codesystems`, `list_valuesets`, `list_conceptmaps` | sökning |
| `lookup_code` | `CodeSystem/$lookup` |
| `validate_code` | `CodeSystem/$validate-code` / `ValueSet/$validate-code` |
| `expand_valueset` | `ValueSet/$expand` |
| `search_concepts` | `$expand` av implicit ValueSet med `filter` |
| `concept_hierarchy` | `$lookup` (parent/child), med fallback till `$expand` + `is-a` |
| `subsumes` | `CodeSystem/$subsumes` |
| `translate_code` | `ConceptMap/$translate` |
| `inspect_workbook`, `propose_column_mapping`, `confirm_mapping`, `build_codesystem_from_workbook` | inläsningsflödet |
| `registry_list`, `registry_near_duplicates`, `registry_merge`, `registry_harvest` | egenskapsregistret |

### Inläsning från kommandoraden

```bash
# 1. Föreslå mall (heuristik, plus Claude med --ai)
term-assistant analyze examples/vardkontaktorsak.xlsx \
    --url http://example.org/fhir/CodeSystem/vardkontaktorsak \
    --name Vardkontaktorsak -o mall.json [--ai]

# 2. Granska/redigera mall.json och bekräfta: registret lär sig rubrikerna
term-assistant confirm mall.json

# 3. Bygg CodeSystem (status draft)
term-assistant build examples/vardkontaktorsak.xlsx --template mall.json -o cs.json

# 4. Jämför versioner
term-assistant diff v1.json v2.json
```

Exempel på utdata från `analyze` (`examples/vardkontaktorsak.xlsx`):

```
Kod                → code
Benämning          → display
Beskrivning        → definition
Överordnad kod     → parent          (värdena finns i kodkolumnen)
Benämning (en)     → designation     language=en
Synonymer          → designation     SNOMED 900000000000013009 (synonym)
Giltig fr.o.m.     → property        effectiveDate
Inaktiv            → property        inactive
Anmärkning         → property        comment
Sorteringsordning  → property (NY)   sorteringsordning  ? fråga till terminologen
Ansvarig enhet     → property (NY)   ansvarigEnhet      ? fråga till terminologen
Tom                → ignore
```

Efter `confirm` mappas *Sorteringsordning* och *Ansvarig enhet* automatiskt i nästa
ark med samma rubriker. Det är så registret konvergerar.

## Struktur

```
src/term_assistant/
  config.py              val av terminologitjänst (TX_BASE_URL m.fl.)
  fhir/client.py         FHIR-terminologiklient (standardoperationer)
  mcp_server.py          MCP-servern
  cli.py                 kommandorad
  importer/
    workbook.py          Excel/CSV, detektering av rubrikrad, stickprov
    profiling.py         kolumnprofiler (typ, unikhet, kardinalitet, hierarki)
    registry.py          egenskapsregistret: alias, inlärning, sammanslagning
    suggest.py           heuristiska förslag -> mall
    ai.py                granskning av förslaget med Claude (strukturerad output)
    mapping.py           mallen = inläsningsspec + spec för administrationsvyn
    transform.py         mall + rader -> FHIR CodeSystem
    release.py           releasekandidat, release, diff
```
