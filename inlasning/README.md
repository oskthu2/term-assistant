# Inläsningstjänsten (`term-inlasning`)

En av tre fristående komponenter i [term-assistant](../README.md). Den läser in
kodverk och urval från Excel, CSV eller PDF en gång, mappar kolumnerna mot FHIR
med AI-stöd och gör resultatet till en **mall**. Mallen är sedan specifikationen
för en skräddarsydd administrationsvy där kodverket förvaltas vidare:
redaktionell kedja, releasekandidater och releaser, byte av vilken egenskap en
kolumn står för och nya kolumner.

Releaser publiceras som versionerade FHIR-paket (`.tgz`) till en paketkatalog
eller via FHIR-API. Tjänsten pratar inte med MCP-servern eller paketbyggaren.
Paketbyggaren läser paketkatalogen, och alla tre pratar med terminologitjänsten
via FHIR.

Designen beskrivs i [../docs/arkitektur.md](../docs/arkitektur.md).

## Kom igång

```bash
cd inlasning
pip install -e '.[dev,ai]'
pytest
```

### Administrationsvyn (webbapp)

```bash
term-inlasning admin --workspace ./arbetsyta
# öppna http://127.0.0.1:8000
```

1. **Ny inläsning**: ladda upp Excel, CSV eller PDF och välj **namnrymd** (canonical URL, OID-rot,
   utgivare) och **informationsklass**. Du ser bedömningen per flik (avvisad med tips,
   urval, familj, referenslista …) och ett mallförslag per kodverk eller urval.
2. **Skapa projekt**: raderna kopieras in, en gång. Textkataloger viks in som vanliga kolumner,
   och härledda kolumner (t.ex. `KV_X.KOD.RBK`) räknas fram i stället för att lagras.
3. **Underhåll**: tabellvyn och formuläret byggs från mallen, med kryss för användningskontext,
   rullistor, datum och överordnad kod. Varje ändring loggas med användarnamn.
4. **Kolumner**: byt vad en kolumn står för (egenskap, designation, kontext …) eller lägg till
   nya. Befintliga egenskaper från registret föreslås först. Raderna behöver inte migreras.
5. **Avveckling och ersättning**: ett begrepp avvecklas (inaktivt) i stället för att tas bort,
   valfritt med en ersättande kod och relation. Det ger egenskapen `replacedBy` och en
   ConceptMap (`<namn>_ERSATTNINGAR`) i releasen.
6. **Redaktionell kedja**: ändringar kan gå som förslag som en annan person granskar.
   AI-granskning (Claude) föreslår rättelser som alltid granskas och sparas med proveniens.
7. **Releaser**: en releasekandidat (`X.Y.Z-rc.N`) byggs till FHIR. Den profilvalideras och
   kontrolleras mot namnrymden (URL-mönster, OID) och mot reglerna, där borttag av släppta koder
   spärras om policyn kräver det. Den jämförs också med senaste release. Godkännandet kräver
   fyra ögon. Releasen laddas ner som **FHIR-paket** (`.tgz`) eller publiceras till ett mål;
   målen spärras efter informationsklass.
8. **Urval**: "Kontrollera urvalet" flaggar inaktiva, okända och ersatta koder, med källor
   både i lokalt förvaltade kodverk och i terminologitjänsten (`$validate-code`, `$lookup`,
   `$translate`). Förslag på ersättare byts in med ett klick.

Vyn har ingen egen inloggning. Kör den bakom en proxy som skickar `X-Remote-User`.
Projekten lagras som JSON i arbetsytan och går att versionshantera med git.

**Inställningar** (`<arbetsyta>/settings.json`, visas under *Inställningar*):

```json
{
  "namespaces": [{"key": "region", "label": "Regionens kodverk", "base_url": "https://terminologi.example.se/fhir",
                  "oid_root": "1.2.752.xxx.yy", "require_oid": true, "publisher": "Region X",
                  "package_prefix": "se.regionx.kodverk"}],
  "classifications": [{"key": "öppen", "label": "Öppen", "confidentiality": "U", "rank": 0},
                      {"key": "intern", "label": "Intern", "confidentiality": "N", "rank": 1},
                      {"key": "begränsad", "label": "Begränsad", "confidentiality": "R", "rank": 2}],
  "targets": [{"key": "ontoserver", "label": "Inläsning Ontoserver", "kind": "package-dir",
               "directory": "/srv/ontoserver/paket", "max_classification": "intern"},
              {"key": "publik", "label": "Publik testserver", "kind": "fhir-api",
               "base_url": "https://tx-nordics.fhir.org/fhir", "max_classification": "öppen"}],
  "editorial": {"review_manual_edits": true, "four_eyes_release": true, "block_rc_with_open_proposals": true}
}
```

| Miljövariabel | Betydelse |
|---|---|
| `FHIR_VERSION` | `R4` (standard) eller `R5` för hela lösningen: paketens `fhirVersions`, ConceptMap (`equivalence`/`relationship`), validering |
| `TX_BASE_URL`, `TX_TOKEN` | terminologitjänst för uppslag, kontroll av urval och API-publicering |
| `TX_PUBLISH=1` | slår på publicering via FHIR-API (paketmål kräver det inte) |
| `FHIR_VALIDATOR_JAR` | sökväg till HL7:s `validator_cli.jar`; används då vid "Validera paketet" och `term-inlasning validate-package` |
| `ANTHROPIC_API_KEY` | för AI-granskning (eller `ant auth login`) |

### Mappningar (ConceptMap)

Ett mappningsprojekt har en rad per mappning: källkod, relation, målkod och kommentar. En källkod
kan ha flera mål.

- **Skapa:** **Ny mappning** utgår från en källa (ValueSet eller CodeSystem) och ett målkodsystem
  på terminologitjänsten, och lägger upp en rad per källkod. Ett förslag från MCP-servern kan
  också tas in via **Förslag från terminologitjänsten**.
- **Redigera:** kodfälten söker i käll- eller målkodsystemet medan du skriver, och den valda
  kodens text fylls i.
- **Relationer:** likvärdig, målet är bredare, målet är smalare, närliggande och ingen
  motsvarighet. De blir `equivalence` i R4 och `relationship`/`noMap` i R5. "Målet är smalare"
  och "närliggande" kräver kommentar, och saknas den stoppas releasekandidaten.
- **Kontrollera mappningen:** kontrollerar käll- och målkoder mot terminologitjänsten (okända,
  inaktiva, avvikande text) och listar källkoder som saknar mappning.
- **Release:** redaktionell kedja, fyra ögon och paket fungerar som för andra projekt. När en
  release som ersätter ett förslag publiceras till terminologitjänsten tas förslagstaggen bort.

### Förslag från terminologitjänsten

**Förslag från terminologitjänsten** listar utkast som MCP-servern har skapat, taggade
`urn:term-assistant:arbetsflode#forslag`:

- **Mappningar och urval** tas in som projekt.
- **Översättningar** (CodeSystem-supplement) visas, men granskas än så länge på terminologitjänsten.

Publicering till terminologitjänsten kräver `TX_PUBLISH=1`.

### Terminologitjänst

`TX_BASE_URL` anger vilken terminologitjänst urvalskontrollen och API-publiceringen
går mot, t.ex. tx-nordics eller den mockade HAPI-servern (`../mock-tx`). MCP-servern
är en egen komponent: se [../mcp-server](../mcp-server/README.md).

### Inläsning från kommandoraden

```bash
python examples/make_examples.py          # syntetiska exempelfiler

# 0. Kvalitetsgrind: duger filen? (Excel, CSV eller PDF; exit 1 = avvisad)
term-inlasning assess examples/lista_utan_koder.pdf

# 1. Föreslå mallar för alla kodverk och urval i filen (plus Claude med --ai)
term-inlasning analyze examples/kodverk_familj.xlsx --base-url http://example.org/fhir -o mallar.json

# 2. Granska/redigera mallarna och bekräfta: registret lär sig rubrikerna
term-inlasning confirm mallar.json

# 3. Bygg FHIR-resurser (Bundle med CodeSystem/ValueSet, status draft)
term-inlasning build examples/kodverk_familj.xlsx --template mallar.json -o bundle.json
term-inlasning build examples/lab_urval.xlsx --template lab.json --validate   # koder mot TX_BASE_URL

# 4. Jämför versioner, med kontroll mot mallens releasepolicy
term-inlasning diff v1.json v2.json --template mallar.json
```

### Välj mål före inläsning

I förslaget väljer du för varje kolumn vad den står för, med exempelvärden
bredvid. Befintliga egenskaper står först i listan, följda av designationstyper,
värdemängdsroller och "ny egenskap". Valen sparas i egenskapsregistret, så att
samma rubrik gissas rätt nästa gång. Kryssa i "Låt Claude granska förslagen"
för att få en AI-granskning av tolkningarna innan du väljer.

Förvaltningskolumner (Status, Tillagd, Borttagetdatum, Ersätts av, Senast ändrad …)
har fasta mål under "Livscykel och förvaltning" och blir aldrig egna egenskaper.
I ett eget kodverk blir de FHIR:s standardegenskaper (`status`, `inactive`,
`effectiveDate`, `retirementDate`) och tillägget `codesystem-replacedby`. I ett
urval styr de vilka koder som ingår. Förslaget visar också kodverkets metadata
(utgivare, beskrivning, kontakt, OID, licens) med var i filen värdet hittades.
Se [arkitekturen](docs/arkitektur.md#livscykel-och-förvaltning-lifecyclepy).

### Typfall

| Fil | Bedömning | Resultat |
|---|---|---|
| `lista_utan_koder.pdf` – listor med bara benämningar | **avvisad** | Minimikraven (en kod per rad, benämning, rubrikrad, ett kodverk per flik) och specifika tips: tre separata listor, lämna Excel/CSV i stället för PDF |
| `lab_urval.xlsx` – NPU/SWE-koder i flera flikar | **godkänd med anmärkningar** | Flikarna känns igen som *urval* ur NPU och SWE-tillägg och blir ValueSet, ett per grupp ("Grupp"/"Kategori"). Den stora fliken blir referenslista. `NPU1059` flaggas som felskriven, med förslaget `NPU10599` från en annan flik där samma analys finns |
| `vardemangdsdefinition.xlsx` – kodsystem per rad, regel, inkludera, version, källa | **godkänd med anmärkningar** (URL för ICD-10-SE ska anges) | Värdemängdsdefinition. Kolumnerna blir `compose.include/exclude`, filter (`is-a`, ECL) och kommentarstillägg i stället för nya egenskaper. SNOMED-versioner blir utgåve-URI:er |
| `eget_kodverk.xlsx` – litet eget kodverk med "Om kodverket"-flik och förvaltningskolumner | **godkänd** | Metadata (namn, titel, version, utgivare, kontakt, beskrivning, syfte, licens, OID) hämtas ur fliken, med källa. Status, Tillagd, Borttagetdatum och Ersätts av blir standardegenskaper och `codesystem-replacedby`. Senast ändrad och Ändringsorsak stannar i arbetskopian. Inga nya egenskaper |
| `kodverk_familj.xlsx` – flikar med samma struktur | **godkänd** | En mall för hela familjen och ett CodeSystem per flik. Kryssen under "Används av" blir designations, och deras `use` pekar på ett genererat kontextkodverk. Text-ID:n som går att räkna fram ur koden blir `derived`. Katalogen ger OID och ägare, historiken ger version, och regeln "koder får inte tas bort" blir releasepolicy. Textkatalogen ger designations med textroller (RBK/HLP) |

Exempel på utdata från `analyze` för ett enkelt kodverk (`examples/vardkontaktorsak.xlsx`):

```
Kod                → code
Benämning          → display
Beskrivning        → definition
Överordnad kod     → parent          (värdena finns i kodkolumnen)
Benämning (en)     → designation     language=en
Synonymer          → designation     SNOMED 900000000000013009 (synonym)
Giltig fr.o.m.     → lifecycle       effectiveDate   (fast mål, ingen egen egenskap)
Inaktiv            → lifecycle       inactive
Anmärkning         → property        comment
Sorteringsordning  → property (NY)   sorteringsordning  ? fråga till terminologen
Ansvarig enhet     → property (NY)   ansvarigEnhet      ? fråga till terminologen
Tom                → ignore
```

Efter `confirm` mappas *Sorteringsordning* och *Ansvarig enhet* automatiskt i nästa
ark med samma rubriker. Det är så registret konvergerar.

## Struktur

```
src/term_inlasning/
  config.py              terminologitjänst (TX_BASE_URL m.fl.) och FHIR-version
  fhir/client.py         FHIR-terminologiklient (egen kopia – komponenten är fristående)
  cli.py                 kommandorad (term-inlasning)
  importer/
    workbook.py          Excel/CSV: rubrikrad, nyckel–värde-huvud, grupprubriker, fotnoter
    pdf.py               PDF-utskrifter av kalkylark (kolumner ur ordpositioner)
    assess.py            kvalitetsgrind: bladtyper, utfall, tips, familjer, relationer, katalog
    external.py          igenkänning av NPU, SWE, SNOMED CT, LOINC, ATC, ICD-10, KVÅ
    plan.py              importplan för hela arbetsboken (en mall per familj/urval)
    lifecycle.py         regler för förvaltningskolumner (status, tillagd, borttagen, ersätts av …)
    metadata.py          metadata om kodverket: var den hittas, när den krävs
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
