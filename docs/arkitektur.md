# Arkitektur

## Översikt

```
                ┌─────────────────────────────┐
  AI-assistent  │  MCP-server (term-assistant)│        valfri FHIR-terminologitjänst
  (Claude …) ◄─►│  terminologiverktyg ────────┼──────► Inera / tx.nordics / tx.fhir.org
                │  inläsningsverktyg          │        ($lookup, $expand, $translate …)
                └──────────┬──────────────────┘
                           │
           Excel ──► inläsning ──► mall ──► FHIR CodeSystem (draft) ──► release ──► PUT till tjänsten
                           │         │
                    egenskapsregister└──► administrationsvy (genereras från mallen)
```

Terminologitjänsten är alltid sanningskällan. term-assistant har ingen egen
datamodell för begrepp. Den har bara två egna artefakter:

* **egenskapsregistret** – vilka egenskaper och designation-typer som är tillåtna
  och vilka kolumnrubriker som betyder vad;
* **mallen** – hur ett visst kodverk läses in och presenteras.

## Ben 1: MCP mot valfri terminologitjänst

`TerminologyClient` använder bara FHIR R4-standardoperationer, så samma server
fungerar mot alla tjänster. Val av tjänst görs med `TX_BASE_URL`. Autentisering
görs med bearer-token (`TX_TOKEN`). Om tjänsten kräver något annat, t.ex.
klientcertifikat (SITHS), är det klientens `httpx2.AsyncClient` som ska
utökas.

Hierarkinavigering är den operation som skiljer mest mellan servrar. Klienten
frågar först `$lookup` efter `parent`/`child`. Om inga barn kommer tillbaka
expanderar den ett implicit ValueSet med `is-a`-filter. Det ger alla ättlingar,
inte bara direkta barn, och svaret markerar det med `descendants`.

Titelsökning görs med `title:contains` men filtreras även lokalt, eftersom
servrar stöder sökparametrar olika (tx.fhir.org ignorerar t.ex. `title:contains`).

## Ben 2: Inläsning – från kalkylark till mall

### Kvalitetsgrind (`assess.py`)

Innan något mappas bedöms hela filen. Kalkylark som används som kodverk
varierar mycket. Vissa är rena listor utan koder, vissa är urval ur
befintliga kodsystem, och vissa är arbetsböcker med många flikar. Där finns
dokumentation, textkataloger och hjälplistor för datavalidering bredvid
själva kodverken. Varje blad klassas:

| Typ | Kännetecken | Blir |
|---|---|---|
| `codesystem` | unik, kodlik kolumn med egna koder | CodeSystem |
| `subset` | koderna matchar ett externt system (NPU, SWE, SNOMED CT, LOINC, ATC, ICD-10, KVÅ) | ValueSet, ett per grupp |
| `reference` | stort urval som andra blad är urval ur | används för kontroll, publiceras inte |
| `text_catalogue` | nyckelkolumn `<kodverk>.<kod>` som pekar in i andra blad | designations på de begreppen |
| `documentation` | löptext, inledning, revisionshistorik | metadata (katalog, ägare, historik, regler) |
| `helper` | kort lista som Ja/Nej | ignoreras |
| `unusable` | uppfyller inte minimikraven | **avvisas med tips** |

**Minimikrav:** en rad per begrepp, en unik och stabil kod per rad i egen
kolumn, en benämning per kod, en rubrikrad, och ett kodverk per flik med
namn. En fil där inget blad uppfyller kraven avvisas. Avvisningen innehåller
kraven och tips som gäller just den filen, t.ex. "filen innehåller 3
separata listor" eller "lämna Excel/CSV i stället för PDF". Verktyget hittar
aldrig på koder. Om koder saknas måste förvaltaren fastställa dem först.

På arbetsboksnivå hittar bedömningen även:

* **familjer**: blad med identiska rubriker (t.ex. elva KV-flikar), som får en
  gemensam mall och blir ett CodeSystem per flik;
* **relationer**: blad vars koder till minst 60 % finns i ett annat blad. Koder
  som saknas i det större bladet listas. Om samma benämning finns där med en
  annan kod föreslås den ("'NPU0384' (Pt-Kroppsvikt) saknas, där Pt-Kroppsvikt
  har koden NPU03804"). Det fångar skrivfel och ersatta koder;
* **katalog** i dokumentationsblad: rader som namnger kodverken ger OID
  (`urn:oid:`), ägare och titel. "ej fastställt" tolkas som saknat värde och
  blir en fråga;
* **revisionshistorik**, vars senaste version blir kodverkens version;
* **regler i löptext**, t.ex. "… inte … en kod tas bort", som ger
  `policy.allow_code_removal = false`. Releaser som tar bort koder spärras då
  (`release.check_policy`).

### Läsning av ark

1. **Läs** (`workbook.py`, `pdf.py`):
   - Rubrikraden hittas automatiskt.
   - Nyckel–värde-rader ovanför tabellen ("Kodverk: KV_X", "Avser: …") blir
     metadata, och övriga rader där sparas som `preamble`.
   - En grupprubrik som spänner över flera kolumner ("Används av") blir
     `column_groups`.
   - Fotnoter och fotnotsmarkörer i rubriker ("Klartext*") kopplas ihop.
   - Tomma avdelarrader och avdelarkolumner tas bort.
   - En lista utan rubrikrad får generiska kolumnnamn och bedöms som rubriklös.
   - PDF-utskrifter läses med ordpositioner: en fet eller rubriklik rad startar
     en ny tabell (motsvarande en ny flik), och en ensam rad ovanför blir
     tabellens namn.
2. **Profilera** (`profiling.py`): ifyllnadsgrad, unikhet, typ (integer, decimal,
   boolean, date, code, text), kardinalitet och delmängdsrelationer. En kolumn
   vars värden alla finns i kodkolumnen är en förälderkolumn.
3. **Föreslå** (`suggest.py`): heuristik väljer kod, display, definition och
   förälder. Övriga kolumner matchas mot registret. Rubriker med språkmarkering,
   t.ex. "Benämning (en)" eller "Term engelska", blir designations på det
   språket.
4. **Granska med AI** (`ai.py`, valfritt): Claude får blad, stickprov, profiler,
   register och heuristikens förslag, och svarar med strukturerad output, ett
   `ColumnChoice` per kolumn. Kolumner som modellen hoppar över behåller
   heuristikens beslut. Okända registernycklar behandlas som förslag på ny
   egenskap, aldrig som tysta fel.
5. **Bekräfta** (`confirm_mapping`): terminologen justerar mallen. Registret lär
   sig rubrikerna som alias, och godkända nya egenskaper läggs till.
6. **Bygg** (`transform.py`): mall + rader → `CodeSystem` med status `draft`.
   Typerna kontrolleras (boolean, integer, dateTime …), dubbletter och okända
   föräldrar ger varningar, och flera synonymer i en cell delas på `;`.

Kolumnrollerna motsvarar FHIR-element direkt:

| Roll | FHIR |
|---|---|
| `code` | `concept.code` |
| `display` | `concept.display` |
| `definition` | `concept.definition` |
| `designation` | `concept.designation` (`language`, `use`) |
| `property` | `concept.property` → `CodeSystem.property.code` |
| `parent` | `concept.property` `parent` + `hierarchyMeaning` |
| `context` | `concept.designation` med `use` = kod i ett genererat kontextkodverk (eller Coding-egenskap) |
| `derived` | – (värdet räknas fram ur koden, t.ex. `{codesystem}.{code}.RBK`; visas låst i vyn) |
| `group` | i urval: ett ValueSet per gruppvärde |
| `ignore` | – |

### Användningskontext som designation.use

Ett vanligt mönster ser ut så här: kryss i kolumner under en grupprubrik, där
varje kolumn är ett sammanhang som koden används i (t.ex. intygstyper under
"Används av"). Kolumnetiketterna blir då koder i ett **eget, genererat
kodverk** (`<prefix>_ANVANDS_AV`). Varje kryss blir en designation vars `use`
pekar på kontextkoden och vars värde är klartexten:

```json
{"use": {"system": ".../KV_EX_ANVANDS_AV", "code": "INTYG_A", "display": "Intyg A"},
 "language": "sv", "value": "Min undersökning"}
```

Texten kan därför skilja sig mellan kontexter när det behövs. Det genererade
kodverket finns i mallen (`generated_codesystems`), så kontexter kan döpas om
eller läggas till i administrationsvyn. I mallen kan rollen också ställas om
till `mode: property`. Kontexten blir då en upprepad Coding-egenskap i stället
för en designation.

En **textkatalog** fungerar på samma sätt. Det är ett blad med texter nycklade
på `KV_X.KOD`, t.ex. rubriktext och hjälptext. Textkolumnernas etiketter blir
ett textrollkodverk, och koderna hämtas om möjligt från de härledda
ID-kolumnernas suffix (`.RBK`, `.HLP`). Texterna blir designations med den
rollen på rätt begrepp i rätt kodverk.

### Urval ur befintliga kodsystem

En lista med t.ex. NPU-koder är inget nytt kodverk. Den blir ett ValueSet med
listans egna visningsnamn som `concept.display`, plus ett ValueSet per grupp.
En kod får finnas i flera grupper. För externa system anges canonical URL i
mallen (`external_systems`). URL:er som inte är bekräftade (NPU, SWE-tillägg,
ICD-10-SE, KVÅ) blir frågor, och `resolve_external_systems` letar upp dem på
terminologitjänsten. `validate_valueset` kör `$validate-code` för varje kod och
rapporterar okända och inaktiva koder.

## Konvergens mot få egenskaper

Det svåra problemet är inte att mappa ett ark, utan att hundra ark inte ska ge
hundra varianter av "kommentar". Det hanteras med flera samverkande mekanismer:

| Mekanism | Var |
|---|---|
| **Fröregister** med FHIR:s `concept-properties` (status, inactive, effectiveDate, comment …) och SNOMED-designation-typer (synonym, FSN) | `registry._SEED` |
| **Skörd** av `CodeSystem.property` från kodverk som redan finns på tjänsten. Det som används där återanvänds. | `registry.harvest`, MCP `registry_harvest` |
| **Alias-inlärning**: varje bekräftad mappning gör rubriken till ett alias, så nästa ark mappas automatiskt med hög säkerhet | `registry.learn` |
| **Användningsvikt**: bland likvärdiga kandidater vinner den som redan används mest | `registry.match` |
| **Spärr mot dubbletter**: en ny egenskap med samma kod eller URI som en befintlig avvisas | `registry.add_property` |
| **Synonymdetektering**: poster som liknar varandra (och inte redan har olika URI:er) föreslås för sammanslagning, och `merge` gör den ena till alias för den andra | `near_duplicates`, `merge` |
| **AI-instruktion**: modellen ska välja en befintlig post när betydelsen är densamma och får bara föreslå ny egenskap med motivering | `ai.SYSTEM_PROMPT` |
| **Negationsspärr**: fuzzy-matchning får aldrig para ihop antonymer ("Aktiv" ≠ `inactive`, "Valbar" ≠ "Ej valbar") | `registry.is_negation` |
| **Lågt förtroende → fråga**: nya egenskaper och osäkra matchningar blir konkreta frågor till terminologen i stället för tysta beslut | `suggest.heuristic_choices` |

Synonyma *begrepp* hamnar alltså på samma `CodeSystem.property.code`, eller som
`designation` med gemensam `use`. Synonyma *rubriker* blir alias.

## Mallen är administrationsvyn

`ImportTemplate` (`mapping.py`) innehåller utöver mappningen `UiHints` per
kolumn: etikett, ordning, synlighet, redigerbarhet och widget. Widgeten väljs
från egenskapens typ och kolumnprofilen:

| Underlag | Widget |
|---|---|
| `parent` | `concept-picker` (söker i samma kodverk) |
| boolean | `checkbox` |
| dateTime | `date` |
| integer/decimal | `number` |
| få distinkta värden | `select` med värdena som alternativ |
| långa texter / definition | `textarea` |
| `code` | ej redigerbar (koder ändras inte efter release) |

Administrationsvyn ska därför vara generisk. Den renderar en tabell och ett
formulär från mallen och läser och skriver CodeSystem-resurser. Att byta
vilken egenskap en kolumn står för, eller att lägga till en kolumn, är en
ändring i mallen (en ny `ColumnMapping`) som följer med nästa releasekandidat.
Ändringarna valideras med `ImportTemplate.validate_template()`.

## Releaser

`release.py` följer `CodeSystem.status`:

```
arbetskopia (draft, 1.1.0) → releasekandidat (draft, 1.1.0-rc.1, rc.2 …) → release (active, 1.1.0)
```

`diff` jämför två versioner per begrepp (tillagda, borttagna, ändrade fält) och
per egenskapsdefinition. Diffen är underlaget för releasenoter och granskning
före publicering. Publicering sker med `TerminologyClient.put_resource`.

## Administrationsvyn (`admin/`)

Vyn är en serverrenderad webbapp (Starlette + Jinja2, ingen byggkedja och
inget CDN). Den har tre bärande beslut:

* **Arbetskopian är rader, inte FHIR.** Projektets data sparas som rader per
  flik, precis som i kalkylarket. Mallen översätter raderna till FHIR först när
  en releasekandidat byggs, med samma kod som vid inläsningen
  (`build_all`). Därför är det en ren malländring att byta vad en kolumn står
  för, eller att lägga till en kolumn. Ingen data behöver migreras.
* **Inläsningen städar bort kalkylarkets egenheter.** Textkataloger i separata
  flikar blir vanliga designation-kolumner på begreppen. Härledda kolumner
  lagras inte utan räknas fram och visas låsta.
* **Regler följer med.** Om policyn förbjuder borttag erbjuder vyn bara
  avveckling för koder som finns i en release (egenskapen `inactive`; kolumnen
  läggs till vid behov). En releasekandidat som ändå saknar en släppt kod
  spärras.

Releaseflödet är: arbetskopia → `X.Y.Z-rc.N` (draft, diff mot senaste
release, policykontroll) → godkänd release `X.Y.Z` (active) → valfri
publicering med `PUT` till terminologitjänsten. Publiceringen kräver
`TX_PUBLISH=1`. Allt loggas med användarnamnet från `X-Remote-User`.

## Nästa steg

* **Hämta befintligt kodverk** från terminologitjänsten som nytt projekt
  (mall genererad från `CodeSystem.property`), för kodverk som aldrig legat i Excel.
* **Export till Excel** av arbetskopian för den som behöver granska i kalkylark.
* **Samtidig redigering**: arbetsytan är gjord för en eller ett fåtal
  redaktörer; vid fler behövs låsning eller en databas.
* **Autentisering** mot Inera (OAuth2/SITHS) och skrivbehörighet för
  publicering.
* **ConceptMap-stöd vid inläsning**: kolumner som innehåller koder från andra
  kodsystem kan valideras mot tjänsten (`validate_code`) och föreslås bli
  ConceptMap i stället för egenskaper.
* **Validering av befintliga koder** mot tjänsten när ett ark refererar till
  t.ex. SNOMED CT eller ICD-10-SE.
