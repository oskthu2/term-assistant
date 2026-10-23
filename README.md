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
| `inspect_workbook`, `assess_workbook`, `propose_import`, `confirm_mapping`, `build_resources` | inläsningsflödet |
| `validate_valueset`, `resolve_external_systems` | kontroll av urval mot tjänsten, uppslag av t.ex. NPU:s canonical URL |
| `compare_versions` | diff mellan versioner + kontroll mot releasepolicy |
| `registry_list`, `registry_near_duplicates`, `registry_merge`, `registry_harvest` | egenskapsregistret |

### Inläsning från kommandoraden

```bash
python examples/make_examples.py          # syntetiska exempelfiler

# 0. Kvalitetsgrind: duger filen? (Excel, CSV eller PDF; exit 1 = avvisad)
term-assistant assess examples/lista_utan_koder.pdf

# 1. Föreslå mallar för alla kodverk och urval i filen (plus Claude med --ai)
term-assistant analyze examples/kodverk_familj.xlsx --base-url http://example.org/fhir -o mallar.json

# 2. Granska/redigera mallarna och bekräfta: registret lär sig rubrikerna
term-assistant confirm mallar.json

# 3. Bygg FHIR-resurser (Bundle med CodeSystem/ValueSet, status draft)
term-assistant build examples/kodverk_familj.xlsx --template mallar.json -o bundle.json
term-assistant build examples/lab_urval.xlsx --template lab.json --validate   # koder mot TX_BASE_URL

# 4. Jämför versioner, med kontroll mot mallens releasepolicy
term-assistant diff v1.json v2.json --template mallar.json
```

### Tre typfall

| Fil | Bedömning | Resultat |
|---|---|---|
| `lista_utan_koder.pdf` – listor med bara benämningar | **avvisad** | Minimikraven (en kod per rad, benämning, rubrikrad, ett kodverk per flik) och specifika tips: tre separata listor, lämna Excel/CSV i stället för PDF |
| `lab_urval.xlsx` – NPU/SWE-koder i flera flikar | **godkänd med anmärkningar** | Flikarna känns igen som *urval* ur NPU och SWE-tillägg och blir ValueSet, ett per grupp ("Grupp"/"Kategori"). Den stora fliken blir referenslista. `NPU1059` flaggas som felskriven, med förslaget `NPU10599` från en annan flik där samma analys finns |
| `kodverk_familj.xlsx` – flikar med samma struktur | **godkänd** | En mall för hela familjen och ett CodeSystem per flik. Kryssen under "Används av" blir designations, och deras `use` pekar på ett genererat kontextkodverk. Text-ID:n som går att räkna fram ur koden blir `derived`. Katalogen ger OID och ägare, historiken ger version, och regeln "koder får inte tas bort" blir releasepolicy. Textkatalogen ger designations med textroller (RBK/HLP) |

Exempel på utdata från `analyze` för ett enkelt kodverk (`examples/vardkontaktorsak.xlsx`):

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
    workbook.py          Excel/CSV: rubrikrad, nyckel–värde-huvud, grupprubriker, fotnoter
    pdf.py               PDF-utskrifter av kalkylark (kolumner ur ordpositioner)
    assess.py            kvalitetsgrind: bladtyper, utfall, tips, familjer, relationer, katalog
    external.py          igenkänning av NPU, SWE, SNOMED CT, LOINC, ATC, ICD-10, KVÅ
    plan.py              importplan för hela arbetsboken (en mall per familj/urval)
    profiling.py         kolumnprofiler (typ, unikhet, kardinalitet, hierarki, kryss)
    registry.py          egenskapsregistret: alias, inlärning, sammanslagning
    suggest.py           heuristiska förslag -> mall
    ai.py                granskning av förslaget med Claude (strukturerad output)
    mapping.py           mallen = inläsningsspec + spec för administrationsvyn
    transform.py         mall + rader -> FHIR CodeSystem
    subset.py            urval -> ValueSet, validering av koder mot tjänsten
    build.py             alla resurser för en mall (familj, genererade kodverk, textkatalog)
    release.py           releasekandidat, release, diff, releasepolicy
```
