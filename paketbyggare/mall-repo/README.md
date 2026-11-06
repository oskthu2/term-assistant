# Terminologipaket – distributionsrepo

Det här repot bygger versionerade FHIR-terminologipaket med
[paketbyggaren](https://github.com/oskthu2/term-assistant/tree/main/paketbyggare) och
publicerar dem som GitHub Releases. Arbetsflödet körs varje måndag och vid manuell start,
och skapar en ny release bara när innehållet ändrats.

1. Lägg paketbeskrivningar i `paket/` och lista dem i `matrix.paket` i
   `.github/workflows/terminologipaket.yml`.
2. Ange varifrån paketbyggaren installeras med repo-variabeln `TERM_PAKET`
   (standard: `git+https://github.com/oskthu2/term-assistant@main#subdirectory=paketbyggare`).
   Är källrepot privat: lägg en läsbehörig token som hemligheten `TERM_PAKET_TOKEN`.
3. Kör arbetsflödet manuellt första gången (**Actions → Terminologipaket → Run workflow**).

Grundladda en terminologitjänst från en release:

```bash
term-paket ladda https://github.com/<org>/<repo>/releases/download/<namn>-v<version>/<namn>-<version>.tgz \
  --server http://localhost:8080/fhir --vanta 300
```
