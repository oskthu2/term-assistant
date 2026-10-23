"""Kommandoradsgränssnitt.

    term-assistant assess fil.xlsx                         # kvalitetsgrind: duger filen?
    term-assistant analyze fil.xlsx --base-url URL [--ai]  # mallar för alla kodverk/urval i filen
    term-assistant confirm mallar.json                     # registret lär sig mappningarna
    term-assistant build fil.xlsx --template mallar.json   # FHIR-resurser (Bundle)
    term-assistant diff gammal.json ny.json [--template]   # diff + kontroll mot releasepolicy
    term-assistant admin [--workspace DIR]                 # administrationsvyn (webbapp)
    term-assistant mcp [--http]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .importer.assess import Verdict, WorkbookAssessment
from .importer.build import build_all
from .importer.mapping import ImportTemplate
from .importer.plan import plan_import
from .importer.registry import PropertyRegistry
from .importer.release import check_policy, diff
from .importer.workbook import read_workbook


def _write(data: object, out: str | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"Skrev {out}", file=sys.stderr)
    else:
        print(text)


def _err(text: str = "") -> None:
    print(text, file=sys.stderr)


def print_assessment(wa: WorkbookAssessment) -> None:
    _err(f"{wa.file}: {wa.verdict.value.upper()}")
    for a in wa.sheets:
        family = f" [familj {a.family}]" if a.family else ""
        _err(f"  {a.sheet}: {a.kind.value} – {a.verdict.value}{family}")
        for r in a.reasons:
            _err(f"      {r}")
        for w in a.warnings:
            _err(f"    ! {w}")
    for r in wa.relations:
        _err(f"  ⊂ '{r.subset}' är till {r.overlap:.0%} ett urval av '{r.superset}'"
             + (f"; saknas där: {', '.join(r.missing[:8])}" if r.missing else ""))
        for h in r.hints[:10]:
            _err(f"      → {h}")
    if wa.catalogue:
        _err(f"  Katalog: {len(wa.catalogue)} kodverk beskrivs i dokumentationen")
    if wa.history:
        _err(f"  Historik: {len(wa.history)} versioner, senast {wa.history[-1]}")
    if not wa.policy.allow_code_removal:
        _err(f"  Regel: koder får inte tas bort – \"{wa.policy.rationale}\"")
    for t in wa.tips:
        _err(f"  * {t}")


def _load_templates(path: str) -> list[ImportTemplate]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [ImportTemplate.model_validate(t) for t in (data if isinstance(data, list) else [data])]


def cmd_assess(args: argparse.Namespace) -> None:
    from .importer.assess import assess_workbook
    wa, _ = assess_workbook(args.file)
    print_assessment(wa)
    if args.output:
        _write(wa.model_dump(mode="json", exclude_none=True), args.output)
    sys.exit(1 if wa.verdict == Verdict.REJECT else 0)


def cmd_analyze(args: argparse.Namespace) -> None:
    registry = PropertyRegistry.load(args.registry)
    plan, sheets = plan_import(args.file, args.base_url, registry, sheet=args.sheet, language=args.language)
    print_assessment(plan.assessment)
    if plan.assessment.verdict == Verdict.REJECT:
        sys.exit(1)
    by_name = {s.name: s for s in sheets}
    proposals = plan.proposals
    if args.ai:
        from .importer.ai import refine_with_claude
        proposals = [refine_with_claude(p, by_name[p.template.source.sheet], registry, p.template.codesystem)
                     for p in proposals]
    for p in proposals:
        t = p.template
        n = len(t.source.sheets) or 1
        _err(f"\nMall: {t.codesystem.name} ({t.kind}{f', {n} blad' if n > 1 else ''})")
        for c in p.choices:
            target = c.registry_key or c.new_property_code or c.context_group or c.pattern or c.language or ""
            _err(f"  {c.source_column[:40]:<40} -> {c.role.value:<11} {target[:28]:<28} {c.confidence:.2f}")
        for g in t.generated_codesystems:
            _err(f"  + genererat kodverk {g.name}: {', '.join(c.code for c in g.concepts)}")
        for q in p.questions:
            _err(f"  ? {q}")
    templates = [p.template.model_dump(mode="json", exclude_none=True) for p in proposals]
    _write(templates if len(templates) != 1 else templates[0], args.output)


def cmd_build(args: argparse.Namespace) -> None:
    sheets = read_workbook(args.file)
    resources, warnings = [], []
    for template in _load_templates(args.template):
        r, w = build_all(template, sheets)
        resources += r
        warnings += w
    for w in warnings:
        _err(f"  ! {w}")
    if args.validate:
        import asyncio

        from .config import ServerConfig
        from .fhir.client import TerminologyClient
        from .importer.subset import validate_valueset_codes

        async def run():
            async with TerminologyClient(ServerConfig.from_env()) as client:
                for vs in (r for r in resources if r["resourceType"] == "ValueSet"):
                    report = await validate_valueset_codes(client, vs)
                    _err(f"  {vs['name']}: {report['summary']}")
                    for p in report["problems"][:20]:
                        _err(f"    ! {p['code']}: {p['status']} {p.get('message') or ''}")
        asyncio.run(run())
    if len(resources) == 1:
        _write(resources[0], args.output)
    else:
        _write({"resourceType": "Bundle", "type": "collection",
                "entry": [{"fullUrl": r["url"], "resource": r} for r in resources]}, args.output)


def cmd_confirm(args: argparse.Namespace) -> None:
    from .mcp_server import confirm_mapping
    os.environ["TERM_REGISTRY"] = args.registry
    for template in _load_templates(args.template):
        _write(confirm_mapping(template.model_dump(mode="json")), None)


def cmd_diff(args: argparse.Namespace) -> None:
    old = json.loads(Path(args.old).read_text(encoding="utf-8"))
    new = json.loads(Path(args.new).read_text(encoding="utf-8"))
    change = diff(old, new)
    if args.template:
        errors = check_policy(change, _load_templates(args.template)[0].policy)
        change["policy_errors"] = errors
        for e in errors:
            _err(f"  ✗ {e}")
    _write(change, None)
    sys.exit(1 if change.get("policy_errors") else 0)


def cmd_admin(args: argparse.Namespace) -> None:
    from .admin.app import run
    run(args.workspace, host=args.host, port=args.port, base_url=args.base_url, registry_path=args.registry)


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import run
    run("streamable-http" if args.http else "stdio")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="term-assistant")
    parser.add_argument("--registry", default="registry.json", help="Egenskapsregister (JSON)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("assess", help="Bedöm om en fil duger som kodverk (Excel, CSV eller PDF)")
    p.add_argument("file")
    p.add_argument("-o", "--output", help="Skriv bedömningen som JSON")
    p.set_defaults(func=cmd_assess)

    p = sub.add_parser("analyze", help="Föreslå mallar för filens kodverk och urval")
    p.add_argument("file")
    p.add_argument("--base-url", required=True, help="Bas-URL, t.ex. http://example.org/fhir")
    p.add_argument("--sheet", help="Bara detta blad (eller dess familj)")
    p.add_argument("--language", default="sv")
    p.add_argument("--ai", action="store_true", help="Låt Claude granska förslagen")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("build", help="Bygg FHIR-resurser från fil och mall(ar)")
    p.add_argument("file")
    p.add_argument("--template", required=True)
    p.add_argument("--validate", action="store_true", help="Validera urvalens koder mot TX_BASE_URL")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("confirm", help="Bekräfta mall(ar): lär registret alias och nya egenskaper")
    p.add_argument("template")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("diff", help="Diff mellan två versioner av ett CodeSystem")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--template", help="Kontrollera mot mallens releasepolicy")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("admin", help="Starta administrationsvyn (webbapp)")
    p.add_argument("--workspace", default="arbetsyta", help="Katalog för projekt och releaser")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--base-url", default="http://example.org/fhir", help="Förvald bas-URL vid inläsning")
    p.set_defaults(func=cmd_admin)

    p = sub.add_parser("mcp", help="Starta MCP-servern")
    p.add_argument("--http", action="store_true")
    p.set_defaults(func=cmd_mcp)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
