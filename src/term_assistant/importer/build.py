"""Bygg alla FHIR-resurser som en mall beskriver.

* ``kind='codesystem'``: ett CodeSystem per blad i familjen, plus genererade
  kodverk (användningskontexter, textroller). Om mallen har en textkatalog
  läggs dess texter till som designations på de kodverk de pekar på.
* ``kind='valueset'``: ValueSet för urvalet och, om mallen grupperar,
  ett ValueSet per grupp.
"""

from __future__ import annotations

from typing import Any

from .mapping import ImportTemplate
from .transform import build_codesystem, generated_codesystem
from .workbook import Sheet


def build_all(template: ImportTemplate, sheets: list[Sheet]) -> tuple[list[dict[str, Any]], list[str]]:
    by_name = {s.name: s for s in sheets}
    warnings: list[str] = []
    resources: list[dict[str, Any]] = []

    if template.kind == "valueset":
        from .subset import build_valuesets
        sheet = by_name.get(template.source.sheet) or sheets[0]
        return build_valuesets(template, sheet.rows)

    sources = template.source.sheets or [None]
    names: dict[str, dict[str, Any]] = {}  # källnamn -> CodeSystem
    for src in sources:
        sheet = by_name.get(src.sheet if src else template.source.sheet) or (sheets[0] if src is None else None)
        if sheet is None:
            warnings.append(f"Bladet '{src.sheet}' saknas i filen")
            continue
        cs, w = build_codesystem(template, sheet.rows, src)
        warnings += [f"{sheet.name}: {x}" for x in w]
        resources.append(cs)
        names[src.name if src else template.codesystem.name] = cs

    if template.text_catalogue:
        warnings += _apply_text_catalogue(template, by_name, names)

    for gen in template.generated_codesystems:
        resources.append(generated_codesystem(gen, template))
    return resources, warnings


def _apply_text_catalogue(template: ImportTemplate, sheets: dict[str, Sheet],
                          codesystems: dict[str, dict[str, Any]]) -> list[str]:
    cat = template.text_catalogue
    sheet = sheets.get(cat.sheet)
    gen = template.generated(cat.use_codesystem)
    if sheet is None or gen is None:
        return [f"Textkatalogen '{cat.sheet}' kunde inte tillämpas"]
    displays = {c.code: c.display for c in gen.concepts}
    warnings = []
    for row in sheet.rows:
        key = row.get(cat.key_column)
        if not key:
            continue
        match = _split_key(str(key), codesystems.keys())
        if match is None:
            warnings.append(f"{cat.sheet}: nyckeln '{key}' pekar inte på något inläst kodverk")
            continue
        cs_name, code = match
        concept = next((c for c in codesystems[cs_name].get("concept", []) if c["code"] == code), None)
        if concept is None:
            warnings.append(f"{cat.sheet}: koden '{code}' finns inte i {cs_name}")
            continue
        for text in cat.texts:
            value = row.get(text.column)
            if value:
                concept.setdefault("designation", []).append({
                    "language": template.codesystem.language,
                    "use": {"system": gen.url, "code": text.use_code, "display": displays.get(text.use_code)},
                    "value": str(value).strip(),
                })
    return warnings


def _split_key(key: str, names) -> tuple[str, str] | None:
    """'KV_EX_0001.UNDERSOKNING' -> ('KV_EX_0001', 'UNDERSOKNING'); längsta namnet vinner."""
    for name in sorted(names, key=len, reverse=True):
        if key.startswith(name + "."):
            return name, key[len(name) + 1:]
    return None
