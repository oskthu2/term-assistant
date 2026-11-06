# Paketbyggaren (`term-paket`)

En av tre fristående komponenter i [term-assistant](../README.md). Den bygger
**versionerade terminologipaket** (FHIR NPM-paket, `.tgz`) för distribution och
grundladdar terminologitjänster med dem. Den är gjord för att köras som ett
**schemalagt jobb**, till exempel varje vecka i GitHub Actions, och bygger bara en ny
version när innehållet faktiskt ändrats.

```bash
cd paketbyggare
pip install -e '.[dev]'
pytest
```

## Vad ett paket innehåller

En paketbeskrivning (`paket/*.json`) anger källorna:

- **`fhir`**: resurser via canonical URL (och ev. version) från en FHIR-server, till exempel
  tx-nordics. Med `withCodeSystems` på ett ValueSet följer de kodsystem det bygger på med,
  om de finns på servern och har högst `maxConcepts` begrepp (`allowLarge` för undantag).
  Kodsystem som inte finns där (SNOMED CT, HSA-kodverk) rapporteras som externa beroenden.
- **`packages`**: resurserna i FHIR-paket i en katalog, till exempel de releaser
  inläsningstjänsten publicerar till `arbetsyta/paket`. Senaste versionen per paket.

| Paket | Innehåll | Informationsklass |
|---|---|---|
| `paket/nordiskt-startpaket.json` | HL7 Sverige: civilstånd, säkerhetsmärkning, namndelar, svenska språkpaket. HL7 Norge: civilstånd, familjerelation, anslutningstyp. NPU:s egenskapskodsystem. Exempelmappningar SNOMED CT → ICD-10 (diabetes, hypertoni) och en norsk adressmappning. 21 resurser | U (öppen) |
| `paket/npu.json` | Hela NPU (27 777 begrepp) med värdemängd. Behövs för att kontrollera labb-urval mot mock-tjänsten | U |
| `paket/egna-kodverk.json` | Inläsningstjänstens releaser (`../arbetsyta/paket`) | N (intern) |

## Versioner

Jämförelsen sker mot föregående paket (senaste i `--ut`, eller `--tidigare`) per
resurstyp och canonical URL. Serverns lagringsmetadata (`meta.lastUpdated` m.m.)
räknas inte som en ändring.

| Ändring | Version |
|---|---|
| en resurs har tagits bort | major |
| en resurs har tillkommit eller fått ny version | minor |
| en resurs har ändrat innehåll utan ny version | patch |
| inget | inget nytt paket (`--tvinga` ger ändå en patch) |

Varje paket får `other/CHANGES.md` och `other/changes.json` med vad som ändrats och
från vilka källor. Bredvid paketet skrivs `<namn>-<version>.md` och
`<namn>.feed.json` med alla versioner, kontrollsummor och ändringsnivåer. Arkivet
skrivs deterministiskt (sorterade filer, tidsstämpel från byggdatum), så kontrollsumman
beror bara på innehållet och datumet.

**Informationsklass:** resurserna märks med paketets `classification` i `meta.security`.
En resurs med *striktare* märkning än paketet, till exempel ett internt kodverk från
inläsningstjänsten i ett öppet paket, stoppar bygget. Märkningen sänks aldrig tyst.

## Kommandon

```bash
term-paket katalog --server https://tx-nordics.fhir.org/fhir --filter hl7.se   # vad finns att välja?
term-paket bygg paket/nordiskt-startpaket.json --ut dist                         # nytt paket om ändrat
term-paket visa dist/se.exempel.terminologi.nordisk-1.0.0.tgz                    # innehåll och ändringar
term-paket ladda dist/ --server http://localhost:8080/fhir --vanta 300           # grundladda
term-paket ladda https://…/se.exempel.terminologi.nordisk-1.0.0.tgz              # direkt från en release
```

`ladda` skickar resurserna med `PUT` på sina id i transaktionsbundlar, så att en ny
version ersätter den gamla i stället för att skapa dubbletter. Stora kodsystem skickas
ett och ett. Om en transaktion avvisas laddas resurserna en och en, så att en trasig
resurs inte stoppar resten. `--vanta` väntar tills `$lookup` svarar för varje
kodsystem, eftersom HAPI indexerar i bakgrunden.

## Schemalagt jobb

[`../.github/workflows/terminologipaket.yml`](../.github/workflows/terminologipaket.yml)
körs måndagar 04:17 UTC och vid manuell start. Det:

1. hämtar senast publicerade version (GitHub Release `<namn>-v<version>`)
2. bygger, men bara om innehållet ändrats
3. testladdar paketet i en HAPI-tjänstecontainer
4. skapar en ny release med paketet, `<namn>.feed.json` och ändringsloggen som text

Det kan också ligga i ett **eget distributionsrepo**: kopiera [`mall-repo/`](mall-repo)
(arbetsflöde och paketbeskrivning) dit. Paketbyggaren installeras från det här repot
via repo-variabeln `TERM_PAKET`, och från ett privat repo med hemligheten
`TERM_PAKET_TOKEN`. Paketen hämtas sedan från releaserna, till exempel för
grundladdning:

```bash
term-paket ladda https://github.com/<org>/<repo>/releases/download/<namn>-v1.0.0/<namn>-1.0.0.tgz
```

`egna-kodverk.json` körs inte i det schemalagda jobbet, eftersom inläsningstjänstens
paketkatalog inte finns där. Kör det där katalogen finns, eller låt inläsningstjänsten
publicera till en katalog som jobbet når.
