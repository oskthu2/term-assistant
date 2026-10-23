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

### Administrationsvyn (webbapp)

```bash
term-assistant admin --workspace ./arbetsyta --base-url https://terminologi.example.se/fhir
# öppna http://127.0.0.1:8000
```

1. **Ny inläsning**: ladda upp Excel, CSV eller PDF. Du ser bedömningen per flik (avvisad med tips,
   urval, familj, referenslista …) och ett mallförslag per kodverk eller urval.
2. **Skapa projekt**: raderna kopieras in, en gång. Textkataloger viks in som vanliga kolumner,
   och härledda kolumner (t.ex. `KV_X.KOD.RBK`) räknas fram i stället för att lagras.
3. **Underhåll**: tabellvyn och formuläret byggs från mallen, med kryss för användningskontext,
   rullistor, datum och överordnad kod. Varje ändring loggas med användarnamn.
4. **Kolumner**: byt vad en kolumn står för (egenskap, designation, kontext …) eller lägg till
   nya. Befintliga egenskaper från registret föreslås först. Raderna behöver inte migreras.
5. **Releaser**: en releasekandidat (`X.Y.Z-rc.N`) byggs till FHIR och jämförs med senaste
   release. Den kontrolleras också mot reglerna: borttag av släppta koder spärras om policyn
   kräver det, och då avvecklar man i stället. Godkänn sedan som release, ladda ner som
   Bundle eller publicera till terminologitjänsten (`TX_PUBLISH=1`).
6. **Urval**: kör `$validate-code` för alla koder mot `TX_BASE_URL` direkt från vyn.

Vyn har ingen egen inloggning. Kör den bakom en proxy som skickar `X-Remote-User`.
Projekten lagras som JSON i arbetsytan och går att versionshantera med git.

### MCP-servern

```bash
export TX_BASE_URL=https://tx-nordics.fhir.org/fhir   # eller tx.fhir.org/r4, Ineras tjänst …
export TX_TOKEN=...                            # valfritt, bearer-token
term-assistant mcp                             # stdio
term-assistant mcp --http                      # streamable HTTP
```

`.mcp.json` i repot registrerar servern i Claude Code (pekar mot tx-nordics).
Verifierat mot tx-nordics (Ontoserver): SNOMED CT med nordiska moduler och NPU
(`http://npu-terminology.org`) finns där. Svenska SWE-tillägg och LOINC saknas.

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
  admin/
    app.py               webbappen (Starlette + Jinja2, serverrenderad)
    ops.py               projekt, celler, kolumnändringar, avveckling, releaser
    store.py             arbetsyta på disk (JSON) och ändringslogg
    templates/, static/  sidor och stil
```
