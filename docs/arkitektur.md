# Arkitektur

## Tre fristående komponenter

```
 Excel ─► Inläsningstjänsten ──release (.tgz)──► arbetsyta/paket ─┐
          (term-inlasning)                                          ├─► Paketbyggaren ──► GitHub Release
 tx-nordics ────────────────────────────────────────────────────────┘    (term-paket,         │
                                                                          schemalagt)          │ term-paket ladda
 Claude ◄─► term-mcp-demo ◄─MCP─► MCP-servern ◄──FHIR──► terminologitjänst ◄────────────────────┘
                                   (term-mcp)             (Inera / tx-nordics / mock-tx = HAPI)
```

| Komponent | Ansvar | Egna artefakter | Pratar med |
|---|---|---|---|
| Inläsningstjänsten (`inlasning/`) | Excel → mall → förvaltning → release | egenskapsregistret, mallarna, arbetsytan (projekt, releaser, journal) | terminologitjänsten (kontroll av urval, publicering), paketkatalogen (skriver) |
| MCP-servern (`mcp-server/`) | terminologiverktyg för AI-assistenter | inga | terminologitjänsten |
| Paketbyggaren (`paketbyggare/`) | versionerade paket för distribution, grundladdning | paketbeskrivningar, byggda paket och flöden | FHIR-servrar (läser), paketkataloger (läser), terminologitjänster (grundladdar) |

**Fristående** betyder här:

* ingen delad kod: inläsningstjänsten och MCP-servern har var sin kopia av den lilla
  FHIR-klienten
* egna beroenden: MCP-servern behöver inte openpyxl eller Starlette, och paketbyggaren
  behöver bara `httpx2`
* egna tester, och separat installation och driftsättning
* gränssnitten är standard: FHIR REST-API:t och FHIR NPM-paket. Komponenterna kan bytas
  ut var för sig, till exempel paketbyggaren mot FHIR-communityns egna verktyg, eller
  mock-tjänsten mot Ineras.

Terminologitjänsten är alltid sanningskällan. Ingen komponent har en egen
datamodell för begrepp.

Uppdelningen gjorde också några gränser tydligare. Inläsningsverktygen som tidigare
låg i MCP-servern (`assess_workbook`, `propose_import`, `confirm_mapping`,
`registry_*`) finns nu bara i inläsningstjänsten: webbappen, `term-inlasning` och
registerskörden `term-inlasning harvest`. MCP-servern ser inläsningstjänstens kodverk
först när de publicerats till terminologitjänsten, direkt eller via ett paket.

## MCP-servern (`mcp-server/`)

`TerminologyClient` använder bara FHIR R4-standardoperationer, så samma server
fungerar mot alla tjänster. Val av tjänst görs med `TX_BASE_URL`. Autentisering
görs med en statisk bearer-token (`TX_TOKEN`) eller med OAuth2 client credentials
(`TX_TOKEN_URL`, `TX_CLIENT_ID`, `TX_CLIENT_SECRET`, `TX_SCOPE`). Tokenen hämtas vid
första anropet, förnyas en minut före utgång och en gång till om servern svarar 401.
Om tjänsten kräver något annat, t.ex. klientcertifikat (SITHS), är det klientens
`httpx2.AsyncClient` som ska utökas.

**SNOMED CT på Ontoserver** (Ineras terminologitjänst, tx-nordics):

* **Edition.** Utan versionsangivelse väljer Ontoserver sin standardedition, ofta den
  internationella, och då kommer inga svenska termer. `TX_SNOMED_EDITION` (t.ex. `se` =
  `http://snomed.info/sct/45991000052106`) skickas som `version` i `$lookup`,
  `CodeSystem/$validate-code` och `$subsumes`, och som `system-version` i `$expand` och
  `ValueSet/$validate-code`, men bara för SNOMED CT och bara när anroparen inte angett en
  version. Ett modul-id utan datum ger den senaste versionen av editionen.
* **ECL.** `snomed_ecl` expanderar ett inline-ValueSet med filtret `constraint` och kan
  kombineras med en sökterm. Fritext över hela SNOMED CT ger för mycket brus för att
  modellen ska kunna välja rätt.
* **Kompakta svar.** `$lookup` för SNOMED CT ger alla barn och samma term en gång per
  språkreferensmängd. `lookup_code` räknar barnen och slår ihop termerna per språk.
  `concept_hierarchy` hämtar föräldrar och barn med text via ECL (`>!` och `<!`).
* **ECL-urval som utkast.** `draft_valueset` med `ecl` skapar ett intensionellt ValueSet
  (`compose.include.filter`) och sparar expansionen som ögonblicksbild (upp till 1000
  begrepp), med vilken edition den gjordes mot. Administrationsverktyget tar in urvalet
  som kodlista ur ögonblicksbilden och behöver alltså inte själv nå Ontoserver.

Hierarkinavigering är den operation som skiljer mest mellan servrar. Klienten
frågar först `$lookup` efter `parent`/`child`. Om inga barn kommer tillbaka
expanderar den ett implicit ValueSet med `is-a`-filter. Det ger alla ättlingar,
inte bara direkta barn, och svaret markerar det med `descendants`.

Titelsökning görs med `title:contains` men filtreras även lokalt, eftersom
servrar stöder sökparametrar olika (tx.fhir.org ignorerar t.ex. `title:contains`).
`ValueSet/$validate-code` utan `system` misslyckas på HAPI. Verktyget hämtar då
systemet ur expansionen och försöker igen.

**Endast läsrätt – utkast i stället för skrivning.** MCP-servern skriver aldrig till
terminologitjänsten och behöver bara läsrätt. Den kan därför ställas mot en tjänst i drift
utan skrivbehörighet, och utan inloggning där tjänsten är öppen. Urval, mappningar och
översättningar byggs som **utkast**:

* **urval** (`draft_valueset`): listade koder och/eller ECL
* **mappningar** (`draft_conceptmap`)
* **översättningar** (`draft_translation`), som CodeSystem-supplement

Koderna kontrolleras med `$validate-code` (ECL med `$expand`) och resursen sparas i
MCP-serverns minne (`DraftStore`). Modellen får en sammanfattning och en adress
(`utkast://ValueSet/…`), inte hela resursen, eftersom en ECL-expansion kan vara stor.
Utkasten hämtas som MCP-resurser (`utkast://bundle`, `utkast://{typ}/{id}`), med
`export_drafts` (Bundle) eller i klienterna som FHIR-Bundle eller FHIR-paket (.tgz,
NPM-format). Ett utkast är märkt som förslag (`draft`, `experimental`, taggen
`urn:term-assistant:arbetsflode#forslag`) och får inte ta en canonical URL som en granskad
resurs på tjänsten redan har.

Utkasten lever lika länge som MCP-serverprocessen. Klienterna startar en server per samtal,
så utkasten följer samtalet och försvinner när det avslutas, om de inte har hämtats.

**Fel** från terminologitjänsten blir verktygsfel med serverns meddelande
(`ToolError`). MCP-SDK:n döljer annars texten i oväntade undantag, och då kan modellen
inte rätta sig.

**Demo-klienten** (`term-mcp-demo`) startar servern som underprocess och låter Claude
(`claude-opus-5-5`, effort `medium`, server-side fallback) arbeta med verktygen via
SDK:ns tool runner och MCP-hjälparna (`async_mcp_tool`). Hela dialogen, med
verktygsanrop och verktygssvar, sparas mellan instruktionerna. Därför kan man fråga
vidare ("och vad betyder den andra koden?"). `--verktyg` och `--anropa` fungerar utan
API-nyckel.

## Förslag, granskning och mappningar

Arbetsfördelningen mellan komponenterna följer den redaktionella kedjan:

```
 instruktion ─► MCP-servern (läser) ─► utkast ─► FHIR-Bundle / FHIR-paket (.tgz)
                                                     │  hämtas i klienten
 Administrationsverktyget ◄──────────────────────────┘  "Förslag och utkast" → "Ta in fil"
   rätta, kontrollera mot tjänsten, redaktionell kedja, fyra ögon
   └─► release ─► publicera till terminologitjänsten eller paketkatalogen
```

Filen är det enda som går mellan komponenterna. MCP-servern behöver ingen skrivrätt och
administrationsverktyget behöver inte nå MCP-servern. Administrationsverktyget kan också
lista utkast som någon annan har lagt på terminologitjänsten med förslagstaggen.

* **AI:n föreslår, människan beslutar.** Förslagen bär utgivaren "Förslag från term-mcp (AI-stött,
  ej granskat)" och en motivering. Allt som släpps har gått genom administrationsverktyget.
* **Samma id och URL som utkastet.** Projektet behåller utkastets namn och URL. Om ett utkast
  ligger på terminologitjänsten ersätter releasen det vid publicering. HAPI behåller taggar när
  en resurs uppdateras, så verktyget tar bort förslagstaggen med `$meta-delete`.
* **Filintag.** En Bundle, en enskild resurs eller ett FHIR-paket läses i minnet, utan att
  något packas upp på disk, med gränser för filstorlek (20 MB), uppackad storlek (100 MB) och
  antal filer (2000). Urval, mappningar och översättningar kan tas in. Fullständiga kodsystem
  läses in via Excel/CSV.
* **Mappningsprojekt** (`kind = conceptmap`) har en rad per mappning, där radnumret är nyckel
  eftersom en källkod kan ha flera mål. Kodfälten söker i käll- respektive målkodsystemet via
  `$expand` med filter. Relationerna anges på svenska och översätts till R4 `equivalence` eller R5
  `relationship`/`noMap`. Kravet i R4 på kommentar för smalare och inexakta mappningar stoppar
  releasekandidaten.
* **Översättningsprojekt** (`kind = supplement`) har en rad per kod i kodsystemet. Koden och
  originaltexten är låsta, översättningen redigeras. Releasen blir ett CodeSystem-supplement i
  samma form som HL7 Sveriges språkpaket. Kontrollen mot kodsystemet hittar ändrade
  originaltexter (översättningen kan behöva ses över) och nya koder, som kan hämtas in. Eftersom
  HAPI inte använder supplement i `$lookup` läser MCP-servern dem själv och visar dem under
  `translations`. Översättningsutkast i samma session tas med och markeras som förslag.
* **Webbgränssnittet** (`term-mcp-webb`) är gjort för demo. Varje samtal har en egen
  MCP-underprocess och en egen dialog, stegen strömmas med Server-Sent Events, och Claudes
  markdown renderas på servern med HTML i texten neutraliserad. Utkasten listas bredvid samtalet
  och hämtas som FHIR-paket, FHIR-Bundle eller enskilda resurser.

### AI-assistenten i projektet (`admin/assistant.py`)

Administrationsverktyget pratar själv FHIR med terminologitjänsten för sökning, kontroll och
expansion. MCP används bara där en språkmodell arbetar: i **AI-assistenten** i ett projekt. Där
får redaktören instruera Claude, till exempel "föreslå mål för de rader som saknar målkod" eller
"översätt de koder som saknar översättning". Besluten:

* **MCP-servern som verktygslåda.** Assistenten startar MCP-servern som underprocess
  (`TERM_MCP_COMMAND`, standard `term-mcp`), pekad mot samma terminologitjänst, och ger Claude
  dess läsande verktyg: uppslag, sökning, ECL, validering och översättning. Ingen kod delas mellan
  komponenterna, bara protokollet. Utkastverktygen ges inte till assistenten, eftersom ändringarna
  här går in i projektet.
* **Två egna verktyg.** `project_rows` läser projektets rader (alla eller ofullständiga) och
  `propose_changes` lägger ändringsförslag i den redaktionella kedjan.
* **Inget ändras direkt.** Varje ändring blir ett AI-förslag (`origin = ai`) med proveniens:
  modell, tidpunkt, instruktionen och dess kontrollsumma, terminologitjänsten och MCP-serverns
  uppgifter (`server_info`: SNOMED CT-edition, programvara), de verktyg som anropades och den
  kodsystemversion varje ny målkod kontrollerades mot. En människa granskar den under
  *Förslag*, och ett godkänt förslag förs in bara om raden inte har ändrats under tiden.
* **Kontroll oberoende av modellen.** Innan ett förslag sparas kontrollerar
  administrationsverktyget att kolumnen är redigerbar och att raden finns. Nya målkoder
  valideras mot terminologitjänsten (`$validate-code`) och måltexten fylls i därifrån. Raden
  måste också uppfylla mappningsreglerna: relation, kommentar vid smalare och närliggande, ingen
  målkod vid ingen motsvarighet. Ett förslag som redan är öppet skapas inte igen. Det som stoppas
  rapporteras tillbaka till modellen med orsak.
* **Roll och dialog.** Assistenten kräver rollen redaktör. Dialogen sparas i minnet per projekt
  och användare, så att följdfrågor fungerar, och bara en körning i taget tillåts per projekt.
  Stegen strömmas till sidan med Server-Sent Events. Claudes markdown renderas utan rå HTML
  och utan `javascript:`-länkar.

## Paketbyggaren (`paketbyggare/`)

Ett paket beskrivs deklarativt (`paket/*.json`) med källor: canonical URL:er på en
FHIR-server, eller en katalog med FHIR-paket. Bygget:

1. **hämtar** hela resurser. tx-nordics svarar annars i sammanfattat läge
   (`SUBSETTED`), och en sådan resurs avvisas så att ett paket aldrig töms tyst
2. **följer beroenden**: med `withCodeSystems` hämtas de kodsystem ett ValueSet bygger
   på. Saknade kodsystem (SNOMED CT, HSA-kodverk) och för stora kodsystem rapporteras
3. **normaliserar**: serverns lagringsmetadata tas bort, id görs giltiga och unika, och
   informationsklassen läggs i `meta.security`
4. **jämför** med föregående version per canonical URL och räknar fram semver.
   Oförändrat ger inget nytt paket
5. **skriver** paketet, ändringslogg och `<namn>.feed.json`

**Informationsklass:** paketets klass är ett tak. En resurs med striktare märkning,
till exempel ett internt kodverk från inläsningstjänsten i det öppna startpaketet,
stoppar bygget. Därför finns två paket med olika klass: `nordiskt-startpaket` (U) och
`egna-kodverk` (N). Det upptäcktes när inläsningstjänstens release först lades i
startpaketet.

**Schemalagt jobb:** `.github/workflows/terminologipaket.yml` (eller kopian i
`paketbyggare/mall-repo/` för ett eget distributionsrepo) bygger varje vecka.
Jobbet testladdar paketet i en HAPI-tjänstecontainer och publicerar en GitHub Release
bara när innehållet ändrats. Releasernas URL:er är det som mock-tjänster och
testmiljöer grundladdas från.

## Mockad terminologitjänst (`mock-tx/`)

HAPI FHIR 8.12 (R4) i Docker räcker för utveckling och demo. Två inställningar
behövs: fulltextindex (Lucene i minnet), annars fungerar inte `$expand` och
`$validate-code`, och klienttilldelade id. `term-paket ladda` skickar resurserna
med `PUT` på sina id, så att en ny paketversion ersätter den gamla.

Begränsningar: HAPI använder inte supplement (de svenska språkpaketen) för
displaytexter, och förstår bara hierarkier som nästlade begrepp eller `parent`, inte
v3-kodsystemens `subsumedBy`. Det räcker för att utveckla och demonstrera mot, men
inte för att utvärdera svenska displaytexter.

## Inläsningstjänsten: från kalkylark till mall

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

En lista med t.ex. NPU-koder är inget nytt kodverk. SWE-koderna räknas till
samma kodsystem som NPU (`http://npu-terminology.org`), så som de ligger i
Ineras NPU-lista. Den blir ett ValueSet med
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
| **Fröregister** med FHIR:s `concept-properties` (comment, notSelectable, itemWeight …) och SNOMED-designation-typer (synonym, FSN) | `registry._SEED` |
| **Livscykelregler**: förvaltningskolumner (status, tillagd, borttagen, ersätts av, senast ändrad …) är ingen egenskap utan en egen roll med fasta mål, se nedan | `lifecycle.py` |
| **Skörd** av `CodeSystem.property` från kodverk som redan finns på tjänsten. Det som används där återanvänds. | `registry.harvest`, `term-inlasning harvest` |
| **Alias-inlärning**: varje bekräftad mappning gör rubriken till ett alias, så nästa ark mappas automatiskt med hög säkerhet | `registry.learn` |
| **Användningsvikt**: bland likvärdiga kandidater vinner den som redan används mest | `registry.match` |
| **Spärr mot dubbletter**: en ny egenskap med samma kod eller URI som en befintlig avvisas | `registry.add_property` |
| **Synonymdetektering**: poster som liknar varandra (och inte redan har olika URI:er) föreslås för sammanslagning, och `merge` gör den ena till alias för den andra | `near_duplicates`, `merge` |
| **AI-instruktion**: modellen ska välja en befintlig post när betydelsen är densamma och får bara föreslå ny egenskap med motivering | `ai.SYSTEM_PROMPT` |
| **Negationsspärr**: fuzzy-matchning får aldrig para ihop antonymer ("Aktiv" ≠ `inactive`, "Valbar" ≠ "Ej valbar") | `registry.is_negation` |
| **Lågt förtroende → fråga**: nya egenskaper och osäkra matchningar blir konkreta frågor till terminologen i stället för tysta beslut | `suggest.heuristic_choices` |

Synonyma *begrepp* hamnar alltså på samma `CodeSystem.property.code`, eller som
`designation` med gemensam `use`. Synonyma *rubriker* blir alias.

## Eget kodverk eller urval

En inläst fil är nästan alltid det ena eller det andra:

* **Urval ur ett befintligt kliniskt kodsystem** (NPU, SNOMED CT, ICD-10-SE …)
  blir ett ValueSet. Koderna och deras betydelse förvaltas av kodsystemets
  ägare; här förvaltas *urvalet*: vilka koder som ingår, i vilka grupper, med
  vilka regler.
* **Ett litet eget kodverk** blir ett CodeSystem. Här förvaltas både koderna
  och kodverket som helhet, och då blir metadata om kodverket viktig.

Kvalitetsgrinden avgör vilket (`SheetKind.SUBSET` eller `CODESYSTEM`), och
förslaget visar det överst ("Urval ur … → ValueSet" / "Eget kodverk →
CodeSystem"). Samma regler för livscykel och metadata gäller båda spåren, men de
får olika uttryck i FHIR.

## Livscykel och förvaltning (`lifecycle.py`)

Kolumner som "Tillagd", "Borttagetdatum", "Status", "Inaktiv", "Ersätts av"
eller "Senast ändrad" beskriver inte begreppet utan hur det förvaltas. Får
varje kodverk göra egna egenskaper av dem blir det `tillagdDatum` i ett kodverk
och `inford` i ett annat, och "Status" kan betyda olika saker i olika filer:
homonyma och synonyma egenskaper som ingen mottagare kan tolka.

Därför har de en egen roll, `lifecycle`, med en sluten uppsättning mål. Varje
mål har en regel för eget kodverk och en för urval:

| Mål | Typiska rubriker | Eget kodverk (CodeSystem) | Urval (ValueSet) |
|---|---|---|---|
| `status` | Status, Begreppsstatus | egenskapen `status` (concept-properties) | styr om raden ingår |
| `inactive` / `active` | Inaktiv, Avregistrerad / Aktiv, Gällande | egenskapen `inactive` (Aktiv inverteras) | inaktiva rader ingår inte |
| `effectiveDate` | Tillagd, Införd, Giltig fr.o.m. | egenskapen `effectiveDate` | raden ingår först från datumet |
| `deprecationDate` | Utfasad, Avråds från | `deprecated` (R4) / `deprecationDate` (R5) | ingår, men flaggas |
| `retirementDate` | Borttagen, Borttagetdatum, Giltig t.o.m. | egenskapen `retirementDate` | ingår inte efter datumet |
| `replacedBy` | Ersätts av, Ny kod | tillägget `codesystem-replacedby` + ConceptMap | flaggas om ersättaren saknas i urvalet |
| `changed`, `changeNote` | Senast ändrad, Ändringsorsak | förvaltningsuppgift, exporteras inte | samma |
| `versionAdded`, `versionRemoved` | Tillagd i version, Borttagen i version | förvaltningsuppgift (borttagen ger `retired`) | borttagen ingår inte |

Reglerna:

* **Igenkänning på rubrik och värden.** Rubriken matchas mot registrets
  livscykelposter (fasta alias plus inlärda). En ungefärlig träff godtas bara
  om värdena passar (datum för datummål, ja/nej för markeringar, kända
  statusord). Värdena kan också byta mål: "Avregistrerad" med datum blir
  `retirementDate`, "Borttagen" med bara kryss blir `inactive`.
* **Konsekvent härledning.** Ett passerat borttagsdatum ger `status = retired`
  och `inactive = true`. "Aktiv = Nej" ger `retired`. Ett passerat
  utfasningsdatum ger `deprecated`. Svenska statusord ("Gällande",
  "Avregistrerad", "Utfasad", "Preliminär") översätts till `active`, `retired`,
  `deprecated` och `experimental`.
* **Motsägelser blir varningar.** Exempel: status "Aktiv" med passerat
  borttagsdatum, borttagen före tillagd, eller ersättare på ett aktivt begrepp.
* **Inga egna egenskaper.** Att skapa en egenskap för en förvaltningskolumn
  spärras, både i vallistan och i kolumnredigeringen, med en hänvisning till rätt
  mål. Registret avvisar egenskapskoder som `status`, `inactive` och
  `retirementDate`. Äldre register och mallar migreras automatiskt. Claudes
  förslag rättas efter samma regel.
* **Ett alias, ett mål.** När terminologen väljer ett annat mål för en rubrik
  flyttas aliaset dit, så att rubriken inte pekar på två mål.
* **Administrationsvyn sköter stämplarna.** Nya begrepp får "Tillagd" = i dag
  och ändringar uppdaterar "Senast ändrad", om kolumnerna finns. Avveckling
  använder samma mål, med status, borttagsdatum och ersättare.
* **Urval behåller historiken.** Borttagna rader och rader som ännu inte gäller
  ligger kvar i arbetskopian men hålls utanför `compose`. Releasen anger hur
  många och vilka. Skillnaden mellan versioner syns i release-diffen.

Varför egenskaper och inte fler tillägg för eget kodverk? FHIR:s
`concept-properties` (status, inactive, effectiveDate, deprecationDate,
retirementDate) *är* standardformen. Tillägg behövs bara för ersättning
(`codesystem-replacedby`). För urval saknas en standard för datum per begrepp i
ett ValueSet (se "Status för ett begrepp i ett ValueSet"). Därför uttrycks
livscykeln där genom vad som ingår i varje version, inte genom ett egenhändigt
tillägg.

## Metadata om kodverket (`metadata.py`)

Ett eget kodverk behöver mer än koderna. Inläsningen letar efter metadata på
flera ställen, i prioritetsordning:

1. nyckel–värde-rader ovanför tabellen ("Kodverk: KV_X", "Version: 2")
2. dokumentationsflikar med etikett och värde ("Om kodverket": Utgivare,
   Kontakt, Beskrivning, Syfte, Licens, OID …), på samma rad eller etikett över
   värde
3. katalog och revisionshistorik i dokumentationen (OID, ägare, senaste
   version)
4. filens dokumentegenskaper (titel, beskrivning; inte författaren, som är en
   personuppgift)
5. fliknamnet

Varje värde sparas med sin källa och visas i inläsningsförslaget, där det kan
rättas. Fälten har tre nivåer:

| Nivå | Fält | Kontroll |
|---|---|---|
| krävs vid inläsning | namn, titel, canonical URL, version (X.Y.Z) | projektet skapas inte utan dem |
| krävs före release | utgivare, beskrivning (Shareable-profilerna kräver dem) | releasekandidaten stoppas; listan ställs in med `release_required_metadata` i `settings.json` |
| rekommenderas | kontakt, OID, upphovsrätt, syfte | varning i releasekandidaten |

Kontakt tolkas till `ContactDetail` ("Namn <e-post>"), OID till
`identifier` med `urn:oid:`. Det gäller både CodeSystem och ValueSet.

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
publicering som paket till en inläsningskatalog eller med `PUT` till en
terminologitjänst (det senare kräver `TX_PUBLISH=1`). Allt loggas med
den inloggade användarens namn.

### Inloggning och behörighet (`admin/auth.py`)

Verktyget har tre lägen: `demo` (användarval, ingen inloggning), `oidc` (OpenID Connect
direkt mot organisationens IdP) och `proxy` (en inloggningsproxy skickar användare och
grupper i huvuden). Besluten:

* **Roller i stället för användarlistor.** Fem additiva roller: läsare, redaktör, granskare,
  publicerare, administratör. Varje rutt kräver en roll och kontrolleras på servern. Fyra
  ögon-regeln i den redaktionella kedjan ligger kvar ovanpå rollerna.
* **IdP:n äger vem som är vem, verktyget äger vad rollerna betyder.** Grupper eller app roles
  från IdP:n översätts till roller med `auth.role_mapping`. Behörighet ges alltså i
  organisationens katalog och försvinner när någon slutar. Lokal tilldelning (med logg) finns
  för undantag och för organisationer som inte kan skapa grupper snabbt.
* **Inget litar på klienten.** Användarhuvuden godtas bara i proxyläget och bara från betrodda
  adresser (helst med en delad hemlighet). Tidigare godtogs `X-Remote-User` från vem som helst.
  ID-token valideras fullt ut (signatur via JWKS, `iss`, `aud`, `exp`, `nonce`), sessionen är en
  signerad cookie, formulär från andra webbplatser avvisas.
* **Fel konfiguration stoppar starten.** Med `TERM_PRODUCTION=1` vägras demoläget, http-issuer,
  kort sessionsnyckel och proxy som litar på alla adresser.

MCP-servern och `term-mcp-webb` har ingen egen inloggning och behöver ingen för
terminologitjänsten om den är öppen, eftersom de bara läser. Webbgränssnittet använder
däremot organisationens Claude-nyckel. Det ska därför vara privat eller ligga bakom samma
inloggningsproxy.

## Värdemängdsdefinitioner

Många kalkylark beskriver inte ett kodverk utan *hur en värdemängd byggs*.
Kolumnerna är då "Kodsystem", "Version", "Inkludera", "Regel"/"Matchning" och
"Kommentar"/"Källa". Om en kolumn innehåller kodsystem per rad ("SNOMED CT",
"ICD-10-SE", "LOINC", "NPU" eller en URL) klassas bladet som
värdemängdsdefinition, och kolumnerna får egna roller i stället för att bli
egenskaper:

| Roll | FHIR |
|---|---|
| `system` | `compose.include.system`; namn översätts till canonical URL, och okända (t.ex. ICD-10-SE) blir en fråga |
| `system_version` | `compose.include.version`; SNOMED CT-datum görs om till utgåve-URI (`http://snomed.info/sct/<modul>/version/<datum>`, förval svensk utgåva 45991000052106) |
| `include` | Ja/Nej, Inkludera/Exkludera → `compose.include` eller `compose.exclude` |
| `rule` | exakt kod → `concept`; "inklusive underordnade"/`<<` → filter `is-a`; "endast underordnade"/`<` → `descendent-of`; ECL-uttryck → filter `constraint` (SNOMED CT) |
| `comment` | tillägget `valueset-concept-comments` på koden |

Exakta koder samlas per kodsystem och version. Varje regel blir ett eget
`include`, eftersom FHIR inte tillåter `concept` och `filter` i samma element,
och flera filter i ett element betyder OCH.

### Status för ett begrepp i ett ValueSet

Det finns ingen aktiv internationell standard för en kods status *i sin
koppling till en värdemängd*:

* `valueset-deprecated` (kontext `ValueSet.compose.include.concept`) är
  **avvecklat** (FMM 1) och bör inte användas i nytt innehåll.
* `structuredefinition-standards-status` gäller kodverk och begrepp i
  CodeSystem, inte koder i ValueSet.
* Det som finns aktivt i ValueSet-kontext är beskrivande:
  `valueset-concept-comments`, `valueset-concept-definition`,
  `valueset-label` och `valueset-conceptOrder`.
* R5 tillåter egenskaper (t.ex. status) i `expansion.contains.property`. Det
  rör serverns expansion, inte definitionen.

Det beprövade mönstret är att (1) sätta status på begreppet i kodverket,
`inactive`/`status`, så att `$expand` med `activeOnly` filtrerar bort det, och
(2) exkludera koden ur värdemängden, eventuellt flyttad till en separat
"historisk" värdemängd, när den bara ska utgå ur just det urvalet. En
förklaring kan läggas i `valueset-concept-comments`.

Relevanta internationella IG:er:

* **CRMI** (`hl7.fhir.uv.crmi`) täcker livscykeln för värdemängder och
  andra artefakter: versioner, release, manifest med låsta
  kodsystemversioner och licens.
* **IHE SVCM** (`ihe.iti.svcm`) profilerar gränssnittet för att hämta
  värdemängder, kodsystem och mappningar från en terminologitjänst.
* **FHIR Terminology Ecosystem IG** (`hl7.fhir.uv.tx-ecosystem`) är
  testsviten och verktygstilläggen för terminologiservrar.

## Identitet, klassning och paket

* **Namnrymd** väljs vid inläsning (`settings.json`). Den ger bas-URL,
  URL-mönster, OID-rot och förvald utgivare. En releasekandidat spärras om
  någon resurs inte följer mönstret, eller om OID krävs men saknas eller
  ligger utanför roten. "Tilldela OID" ger nästa lediga båge under roten och
  räknar även med bågar som används av underliggande OID:er.
* **Informationsklass** (t.ex. öppen/intern/begränsad) följer projektet. Den
  märks på varje resurs i `meta.security` (v3-Confidentiality, med
  kodsystemets egna displaytexter) och i paketets `package.json`. Licensvillkor
  (`license_note`) följer med som `other/license.md`. Varje publiceringsmål tar
  emot högst en viss klass.
* **FHIR-paket** (`.tgz`, NPM-format) är releasens leverans för inläsning:
  `package.json` (namn från namnrymdens prefix, `fhirVersions` och beroende på
  core-paketet enligt `FHIR_VERSION`), `.index.json`, en fil per resurs,
  `other/release-notes.md` och `other/provenance.json`.

## Validering

Varje releasekandidat profilvalideras inbyggt mot Shareable-profilernas krav
och resursernas invarianter (`importer/validate.py`). Fel spärrar kandidaten,
varningar visas. På releasesidan, eller med `term-inlasning validate-package`,
kan dessutom HL7:s officiella validator köras på paketet
(`FHIR_VALIDATOR_JAR`), utan eller mot terminologitjänsten.

Exempelpaketen ger 0 fel i HL7-validatorn. De varningar som återstår är
väntade:

* `designation.use` med egna koder (kontext, textroll) finns inte i FHIR:s
  `designation-use`-värdemängd. Bindningen är bara rekommenderad, och de egna
  koderna är ett medvetet val.
* Språkkoder kan inte kontrolleras utan terminologiserver.
* `replacedBy` saknar URI, eftersom det inte finns någon standardegenskap för
  ersättning (se nedan). Den interoperabla uttrycksformen är ConceptMap.

## Ersatta koder och urval som behöver hanteras

**Hur uttrycks "ersätts av"?** Kontrollerat mot de officiella paketen:

* R4 (`hl7.fhir.r4.core` 4.0.1) definierar egenskaperna `inactive`,
  `deprecated`, `notSelectable`, `parent` och `child`.
* R5 (`hl7.fhir.r5.core` 5.0.0) lägger till bland annat `status`,
  `effectiveDate`, `deprecationDate`, `retirementDate`, `partOf`, `synonym`,
  `comment` och `itemWeight`. Någon *egenskap* för ersättning finns inte.
* HL7:s extensions-paket (`hl7.fhir.uv.extensions.r4` 5.3.0) har däremot
  tillägget **`codesystem-replacedby`** (aktivt, FMM 3, värde Coding,
  kontext `CodeSystem.concept`). Enligt definitionen ersätts det i R6 av
  egenskaperna `replacedByCode`/`replacedByCoding`.

Vid avveckling sätter verktyget därför `inactive = true`, `status = retired`
och `retirementDate` (R5:s URI:er, som också används i R4-kodverk i THO). Det
lägger också tillägget `codesystem-replacedby` på begreppet, samt en ConceptMap
från avvecklad till ersättande kod. ConceptMap är den form som mottagande
system kan använda maskinellt via `$translate`. Så hanteras ersättningar även
i SNOMED CT (historiska associationer).

Ett avvecklat begrepp kan peka på en ersättare i samma kodverk (kolumnen
"Ersätts av", relation och kommentar). Releasen får då en
ConceptMap från avvecklad till ersättande kod (R4: `equivalence`, R5:
`relationship`), som mottagare kan använda med `$translate`.

"Kontrollera urvalet" flaggar koder i ett ValueSet-projekt. Det finns två
källor:

1. **Lokalt förvaltade kodverk** (senaste release i arbetsytan): avvecklade
   koder, med ersättaren som förslag, och koder som inte finns.
2. **Terminologitjänsten**: `$validate-code` (okänd/inaktiv). För flaggade
   koder görs också `$lookup` (egenskaper som `replacedBy`) och `$translate`
   för att hitta förslag.

Flaggorna visas i tabellen och på begreppet, där en ersättare byts in med ett
klick. Bytet loggas med källa.

## Redaktionell kedja

```
förslag (manuellt eller AI) → granskning (annan person) → införande → releasekandidat → godkännande (fyra ögon) → paket/publicering
```

* AI-förslag går alltid via granskning. De sparas med modell, tidpunkt,
  kontrollsumma för underlaget, förbrukning och motivering.
* Manuella ändringar går via granskning om `review_manual_edits` är på.
  Annars införs de direkt och loggas.
* Ett förslag förs bara in om raden fortfarande har värdena förslaget utgick
  från. Annars blir det *inaktuellt*.
* Öppna förslag spärrar releasekandidat (inställbart). Godkännaren av en
  release får inte vara den som skapade kandidaten.
* Den redaktionella kedjan blir FHIR Provenance i paketet (`other/provenance.json`), en
  Bundle som validerar utan fel mot FHIR R4:

  | Provenance | Aktivitet | Agenter | Underlag (`entity`) |
  |---|---|---|---|
  | `intag-…`, bara i första releasen för ett projekt som tagits in från ett utkast | `v3-DataOperation#CREATE` | författare: utkastverktyget (Device, om utkastet är AI-stött) eller utgivaren; registrerare: den som tog in det | utkastets canonical URL och version, SHA-256 av utkastet, varifrån det kom (fil eller terminologitjänst) |
  | `forslag-…`, ett per godkänt förslag sedan förra releasen | `v3-DataOperation#CREATE`/`UPDATE` | författare: personen, eller modellen som Device med registreraren (den som startade) som egen agent; granskare | AI-assistenten: instruktionen (med SHA-256), terminologitjänsten, SNOMED CT-edition när SNOMED användes, kodsystemversionen varje målkod kontrollerades mot och verktygsanropen. AI-granskningen: underlagets SHA-256 och API-anropets id |
  | `release-…` | `v3-DocumentCompletion#LA` | författare: den som skapade kandidaten; attesterare: den som godkände | – |

  Personer anges med identifierare (`urn:term-assistant:anvandare`), modellen som en
  inbäddad Device. Varje resurs har genererad narrativ text. Motiveringen ligger i `reason`
  som text, eftersom värdemängden där (PurposeOfUse) gäller ändamål och inte redaktionella
  motiveringar.
* Ett utkasts märkning ("Förslag: … Motivering: …") följer inte med in i den släppta
  beskrivningen. Den finns kvar i projektets ursprung (`project.origin`) och i Provenance.

## Nästa steg

* **Hämta befintligt kodverk** från terminologitjänsten som nytt projekt
  (mall genererad från `CodeSystem.property`), för kodverk som aldrig legat i Excel.
* **Export till Excel** av arbetskopian för den som behöver granska i kalkylark.
* **Samtidig redigering**: arbetsytan är gjord för en eller ett fåtal
  redaktörer; vid fler behövs låsning eller en databas.
* **SITHS och behörighet per projekt**: inloggning med SITHS-kort via Ineras IdP fungerar
  redan som OIDC; nästa steg är roller per projekt eller förvaltningsgrupp i stället för
  globalt.
* **ConceptMap-stöd vid inläsning**: kolumner som innehåller koder från andra
  kodsystem kan valideras mot tjänsten (`validate_code`) och föreslås bli
  ConceptMap i stället för egenskaper.
* **Validering av befintliga koder** mot tjänsten när ett ark refererar till
  t.ex. SNOMED CT eller ICD-10-SE.
