"""Kommandoradsgränssnitt.

    term-assistant analyze kodverk.xlsx --url http://example.org/cs/x --name X [--ai]
    term-assistant build kodverk.xlsx --template x.template.json -o x.codesystem.json
    term-assistant confirm x.template.json
    term-assistant diff gammal.json ny.json
    term-assistant mcp [--http]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .importer.mapping import CodeSystemMeta, ImportTemplate
from .importer.registry import PropertyRegistry
from .importer.release import diff
from .importer.suggest import propose_mapping
from .importer.transform import build_codesystem
from .importer.workbook import read_workbook


def _write(data: object, out: str | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"Skrev {out}", file=sys.stderr)
    else:
        print(text)


def cmd_analyze(args: argparse.Namespace) -> None:
    registry = PropertyRegistry.load(args.registry)
    sheets = read_workbook(args.file, sheet=args.sheet)
    if not sheets:
        sys.exit("Hittade inga blad med data")
    sheet = sheets[0]
    meta = CodeSystemMeta(url=args.url, name=args.name, title=args.title or sheet.name, language=args.language)
    proposal = propose_mapping(sheet, registry, meta, source_file=args.file)
    if args.ai:
        from .importer.ai import refine_with_claude
        proposal = refine_with_claude(proposal, sheet, registry, meta, source_file=args.file)

    print(f"Blad '{sheet.name}': rubrikrad {sheet.header_row}, {sheet.total_rows} rader", file=sys.stderr)
    for c in proposal.choices:
        target = c.registry_key or c.new_property_code or c.language or ""
        print(f"  {c.source_column:<30} -> {c.role.value:<12} {target:<20} {c.confidence:.2f}  {c.rationale}",
              file=sys.stderr)
    for q in proposal.questions:
        print(f"  ? {q}", file=sys.stderr)
    for a, b, s in proposal.merge_suggestions:
        print(f"  ~ Registret: '{a}' och '{b}' liknar varandra ({s}) – slå ihop?", file=sys.stderr)
    _write(proposal.template.model_dump(mode="json", exclude_none=True), args.output)


def cmd_build(args: argparse.Namespace) -> None:
    template = ImportTemplate.model_validate_json(Path(args.template).read_text(encoding="utf-8"))
    sheets = read_workbook(args.file, sheet=template.source.sheet)
    codesystem, warnings = build_codesystem(template, sheets[0].rows)
    for w in warnings:
        print(f"  ! {w}", file=sys.stderr)
    _write(codesystem, args.output)


def cmd_confirm(args: argparse.Namespace) -> None:
    from .mcp_server import confirm_mapping
    os.environ["TERM_REGISTRY"] = args.registry
    template = json.loads(Path(args.template).read_text(encoding="utf-8"))
    _write(confirm_mapping(template), None)


def cmd_diff(args: argparse.Namespace) -> None:
    old = json.loads(Path(args.old).read_text(encoding="utf-8"))
    new = json.loads(Path(args.new).read_text(encoding="utf-8"))
    _write(diff(old, new), None)


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import run
    run("streamable-http" if args.http else "stdio")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="term-assistant")
    parser.add_argument("--registry", default="registry.json", help="Egenskapsregister (JSON)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="Föreslå mall för ett Excel-/CSV-blad")
    p.add_argument("file")
    p.add_argument("--sheet")
    p.add_argument("--url", required=True, help="Canonical URL för kodverket")
    p.add_argument("--name", required=True, help="CodeSystem.name")
    p.add_argument("--title")
    p.add_argument("--language", default="sv")
    p.add_argument("--ai", action="store_true", help="Låt Claude granska förslaget")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("build", help="Bygg CodeSystem från fil och mall")
    p.add_argument("file")
    p.add_argument("--template", required=True)
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("confirm", help="Bekräfta mall: lär registret alias och nya egenskaper")
    p.add_argument("template")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("diff", help="Diff mellan två versioner av ett CodeSystem")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("mcp", help="Starta MCP-servern")
    p.add_argument("--http", action="store_true")
    p.set_defaults(func=cmd_mcp)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
