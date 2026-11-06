# Mockad terminologitjänst (HAPI FHIR)

En HAPI FHIR-server (R4, `hapiproject/hapi:v8.12.0-2`) med terminologistöd påslaget,
som grundladdas med paket från [paketbyggaren](../paketbyggare/README.md). Den räcker för
utveckling och demo av MCP-servern och inläsningstjänsten. `$lookup`, `$validate-code`,
`$expand`, `$subsumes` och `$translate` fungerar mot de uppladdade resurserna.

```bash
mock-tx/starta.sh                 # starta och ladda paketen i paketbyggare/dist (bygger startpaketet om det saknas)
mock-tx/starta.sh dist/x.tgz URL  # ladda egna paket: filer, kataloger eller URL:er till releaser
mock-tx/starta.sh --stoppa        # stoppa (data ligger i minnet och försvinner)
```

Servern svarar på `http://localhost:8080/fhir`. Från tom server till grundladdad med
startpaketet tar det ungefär 35 sekunder. NPU-paketet (27 777 begrepp) laddas på
ungefär 20 sekunder till.

## Konfiguration

`docker-compose.yml` sätter det som behövs för terminologi:

- **Fulltextindex (Lucene, i minnet).** Utan det svarar `$expand` och
  `$validate-code` med "Hibernate Search was not initialized".
- **Klienttilldelade id** (`ALPHANUMERIC`). Paketen bär sina egna id, och en ny
  version ersätter den gamla.

## Begränsningar i HAPI att känna till

- **Supplement** (de svenska språkpaketen `cs-sv-*`) lagras, men används inte för
  displaytexter i `$expand`. HL7-koder visas därför på engelska.
- **Hierarkier** förstås bara som nästlade begrepp eller egenskapen `parent`. v3-kodsystem
  som uttrycker hierarkin med `subsumedBy` (till exempel v3-ActCode) ger platta svar på
  `$subsumes` och `concept_hierarchy`.
- **`ValueSet/$validate-code` kräver `system`.** MCP-servern löser det genom att hämta
  systemet ur expansionen.
- **SNOMED CT och ICD-10** finns inte som resurser. ConceptMaps till och från dem
  fungerar ändå med `$translate`.
