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

1. **Läs** (`workbook.py`): hitta rubrikraden (titelrader ovanför hoppas över
   och sparas som `preamble`), gör rubrikerna unika och normalisera datum och tal.
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
| `ignore` | – |

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

## Nästa steg

* **Administrationsvy** som renderas från mallen, t.ex. en webbapp ovanpå samma
  Python-kärna (FastAPI) eller en separat frontend som läser mall + CodeSystem.
* **Lagring** av mallar, arbetskopior och releasekandidater, t.ex. i git eller
  en databas. Releaser publiceras till terminologitjänsten.
* **Autentisering** mot Inera (OAuth2/SITHS) och skrivbehörighet för
  publicering.
* **ConceptMap-stöd vid inläsning**: kolumner som innehåller koder från andra
  kodsystem kan valideras mot tjänsten (`validate_code`) och föreslås bli
  ConceptMap i stället för egenskaper.
* **Validering av befintliga koder** mot tjänsten när ett ark refererar till
  t.ex. SNOMED CT eller ICD-10-SE.
