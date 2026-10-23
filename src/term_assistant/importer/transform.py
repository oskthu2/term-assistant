"""Tillämpa en mall på rader och bygg ett FHIR CodeSystem."""

from __future__ import annotations

from datetime import date
from typing import Any

from .mapping import ColumnRole, ImportTemplate, PropertyDefinition

_TRUE = {"ja", "j", "yes", "y", "true", "sant", "x", "1"}
_FALSE = {"nej", "n", "no", "false", "falskt", "0", ""}

_VALUE_KEY = {
    "code": "valueCode", "Coding": "valueCoding", "string": "valueString", "integer": "valueInteger",
    "boolean": "valueBoolean", "dateTime": "valueDateTime", "decimal": "valueDecimal",
}


class TransformError(ValueError):
    pass


def build_codesystem(template: ImportTemplate, rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """Returnera (CodeSystem, varningar)."""
    problems = template.validate_template()
    if problems:
        raise TransformError("; ".join(problems))
    meta = template.codesystem
    props = {p.code: p for p in template.properties}
    code_col = template.column(ColumnRole.CODE).source_column
    warnings: list[str] = []
    concepts: list[dict[str, Any]] = []
    seen: set[str] = set()

    for line, row in enumerate(rows, start=template.source.header_row + 1):
        raw_code = row.get(code_col)
        if raw_code is None or str(raw_code).strip() == "":
            warnings.append(f"Rad {line}: saknar kod, hoppas över")
            continue
        code = str(raw_code).strip()
        if code in seen:
            warnings.append(f"Rad {line}: dubblettkod '{code}', hoppas över")
            continue
        seen.add(code)
        concept: dict[str, Any] = {"code": code}
        for col in template.columns:
            value = row.get(col.source_column)
            if value is None or str(value).strip() == "" or col.role in (ColumnRole.CODE, ColumnRole.IGNORE):
                continue
            if col.role == ColumnRole.DISPLAY:
                concept["display"] = str(value).strip()
            elif col.role == ColumnRole.DEFINITION:
                concept["definition"] = str(value).strip()
            elif col.role == ColumnRole.DESIGNATION:
                d = col.designation
                # Flera synonymer i samma cell separeras med semikolon eller radbrytning.
                for term in str(value).replace("\n", ";").split(";"):
                    if not term.strip():
                        continue
                    designation: dict[str, Any] = {"value": term.strip()}
                    if d.language:
                        designation["language"] = d.language
                    if d.use_code:
                        designation["use"] = {k: v for k, v in
                                              {"system": d.use_system, "code": d.use_code,
                                               "display": d.use_display}.items() if v}
                    concept.setdefault("designation", []).append(designation)
            elif col.role == ColumnRole.PARENT:
                for parent in str(value).replace(";", ",").split(","):
                    if parent.strip():
                        concept.setdefault("property", []).append({"code": "parent", "valueCode": parent.strip()})
            elif col.role == ColumnRole.PROPERTY:
                definition = props[col.property_code]
                try:
                    typed = coerce(value, definition)
                except ValueError as exc:
                    warnings.append(f"Rad {line}, kolumn '{col.source_column}': {exc}")
                    continue
                concept.setdefault("property", []).append({"code": definition.code, _VALUE_KEY[definition.type]: typed})
        concepts.append(concept)

    for concept in concepts:
        for prop in concept.get("property", []):
            if prop["code"] == "parent" and prop["valueCode"] not in seen:
                warnings.append(f"Kod '{concept['code']}': förälder '{prop['valueCode']}' finns inte i kodverket")

    codesystem = {
        "resourceType": "CodeSystem",
        "url": meta.url,
        "version": meta.version,
        "name": meta.name,
        "title": meta.title,
        "status": "draft",
        "date": date.today().isoformat(),
        "publisher": meta.publisher,
        "language": meta.language,
        "caseSensitive": meta.case_sensitive,
        "hierarchyMeaning": meta.hierarchy_meaning,
        "content": "complete",
        "count": len(concepts),
        "property": [_property_resource(p) for p in template.properties],
        "concept": concepts,
    }
    return {k: v for k, v in codesystem.items() if v not in (None, [])}, warnings


def _property_resource(p: PropertyDefinition) -> dict[str, Any]:
    return {k: v for k, v in {"code": p.code, "uri": p.uri, "description": p.description, "type": p.type}.items() if v}


def coerce(value: Any, definition: PropertyDefinition) -> Any:
    t = definition.type
    if t == "boolean":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError(f"'{value}' är inte ett booleskt värde")
    if t == "integer":
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"'{value}' är inte ett heltal")
        return int(str(value).strip()) if not isinstance(value, (int, float)) else int(value)
    if t == "decimal":
        return float(str(value).replace(",", ".")) if not isinstance(value, (int, float)) else value
    if t == "dateTime":
        s = str(value).strip()
        if len(s) < 4 or not s[:4].isdigit():
            raise ValueError(f"'{value}' är inte ett datum (ÅÅÅÅ-MM-DD)")
        return s
    if t == "Coding":
        system, _, code = str(value).rpartition("|")
        return {"system": system, "code": code} if system else {"code": code}
    return str(value).strip()
