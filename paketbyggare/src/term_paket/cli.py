"""Kommandorad för paketbyggaren.

    term-paket bygg paket.json [--ut dist]            # nytt paket om innehållet ändrats
    term-paket visa dist/x-1.2.0.tgz                  # innehåll och ändringar
    term-paket ladda dist/ --server http://localhost:8080/fhir [--vanta 300]
    term-paket katalog --server https://tx-nordics.fhir.org/fhir --filter hl7.se

``bygg`` jämför med senaste paketet i ``--ut`` (eller ``--tidigare``) och
bygger bara om något ändrats. Med ``--github-output`` (eller när
``GITHUB_OUTPUT`` är satt) skrivs ``changed``, ``version``, ``file`` och
``notes`` för nästa steg i ett schemalagt GitHub Actions-jobb.

Exit-koder: 0 = klart (även när inget ändrats), 1 = fel, 2 = ogiltig paketbeskrivning.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .build import ClassificationError, build
from .config import ConfigError, load_config
from .package import concept_count, latest_packages, read_package_file


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def cmd_build(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        _err(f"Ogiltig paketbeskrivning: {exc}")
        return 2
    from .sources import collect
    previous_path = Path(args.tidigare) if args.tidigare else latest_packages(args.ut, config.name).get(config.name)
    previous = read_package_file(previous_path) if previous_path else None
    _err(f"Bygger {config.name}" + (f" (föregående: {previous.version})" if previous else " (första versionen)"))
    collected = collect(config)
    for note in collected.notes:
        _err(f"  {note}")
    if not collected.resources:
        _err("Inga resurser hittades – inget paket byggs.")
        return 1
    try:
        result = build(config, collected, args.ut, previous, force=args.tvinga)
    except ClassificationError as exc:
        _err(f"Stoppat: {exc}")
        return 1
    if result.changed:
        _err(f"Nytt paket: {result.file} ({len(result.package.resources)} resurser, "
             f"{'första versionen' if result.previous is None else result.changes.bump or 'patch'})")
    else:
        _err(f"Oförändrat sedan {result.previous} – inget nytt paket.")
    output = os.environ.get("GITHUB_OUTPUT") if args.github_output or os.environ.get("GITHUB_OUTPUT") else None
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if result.changed else 'false'}\n")
            fh.write(f"version={result.version}\n")
            fh.write(f"name={config.name}\n")
            if result.changed:
                fh.write(f"file={result.file}\nnotes={result.notes_file}\n")
    print(result.version if result.changed else "")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    pkg = read_package_file(args.file)
    print(f"{pkg.name}#{pkg.version} – {pkg.manifest.get('title')} (FHIR {', '.join(pkg.manifest['fhirVersions'])})")
    for r in sorted(pkg.resources, key=lambda r: (r["resourceType"], r.get("url", ""))):
        size = f"  {concept_count(r)} begrepp" if r["resourceType"] == "CodeSystem" else ""
        print(f"  {r['resourceType']:11} {r.get('url')}|{r.get('version', '-')}{size}")
    if "CHANGES.md" in pkg.other:
        print("\n" + pkg.other["CHANGES.md"])
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    from .load import load, open_packages
    failed = 0
    for source in args.kalla:
        for pkg in open_packages(source):
            _err(f"Laddar {pkg.name}#{pkg.version} ({len(pkg.resources)} resurser) till {args.server}")
            report = load(pkg, args.server, wait=args.vanta, log=_err)
            _err(f"  {len(report.loaded)} laddade, {len(report.failed)} misslyckades")
            failed += len(report.failed)
    return 1 if failed else 0


def cmd_catalog(args: argparse.Namespace) -> int:
    from .sources import FhirServer
    server = FhirServer(args.server)
    try:
        for rtype in args.typ:
            hits = server.search(rtype, _summary="true")
            hits = [r for r in hits if not args.filter or args.filter.lower() in
                    f"{r.get('url', '')} {r.get('title', '')} {r.get('name', '')}".lower()]
            print(f"{rtype} ({len(hits)})")
            for r in sorted(hits, key=lambda r: r.get("url", "")):
                extra = f"  {r.get('content')} {r.get('count', '')}".rstrip() if rtype == "CodeSystem" else ""
                print(f"  {r.get('url')}|{r.get('version', '-')}  {r.get('title') or r.get('name', '')}{extra}")
    finally:
        server.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="term-paket", description="Bygg och ladda versionerade terminologipaket")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bygg", help="Bygg ett nytt paket om innehållet ändrats")
    p.add_argument("config", help="Paketbeskrivning (paket.json)")
    p.add_argument("--ut", default="dist", help="Katalog för paket och <namn>.feed.json (standard dist)")
    p.add_argument("--tidigare", help="Föregående paket att jämföra med (standard: senaste i --ut)")
    p.add_argument("--tvinga", action="store_true", help="Bygg en patch-version även om inget ändrats")
    p.add_argument("--github-output", action="store_true", help="Skriv resultat till $GITHUB_OUTPUT")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("visa", help="Visa ett pakets innehåll och ändringar")
    p.add_argument("file")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("ladda", help="Grundladda en FHIR-server med paket (fil, katalog eller URL)")
    p.add_argument("kalla", nargs="+", metavar="KÄLLA")
    p.add_argument("--server", default=os.environ.get("TX_BASE_URL", "http://localhost:8080/fhir"))
    p.add_argument("--vanta", type=float, default=0, metavar="SEK",
                   help="Vänta högst så här länge på att kodsystemen indexeras")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("katalog", help="Lista kodsystem och värdemängder på en FHIR-server (för att välja innehåll)")
    p.add_argument("--server", required=True)
    p.add_argument("--filter", help="Text som ska finnas i URL, titel eller namn")
    p.add_argument("--typ", nargs="+", default=["CodeSystem", "ValueSet", "ConceptMap"])
    p.set_defaults(func=cmd_catalog)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
