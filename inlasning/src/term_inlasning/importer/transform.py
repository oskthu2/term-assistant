"""Tillämpa en mall på rader och bygg FHIR-resurser (CodeSystem, ValueSet)."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from . import lifecycle
from .mapping import ColumnRole, GeneratedCodeSystem, ImportTemplate, PropertyDefinition, SheetSource
from .profiling import MARKERS

_TRUE = {"ja", "j", "yes", "y", "true", "sant", "1"} | MARKERS
_FALSE = {"nej", "n", "no", "false", "falskt", "0", ""}

_VALUE_KEY = {
    "code": "valueCode", "Coding": "valueCoding", "string": "valueString", "integer": "valueInteger",
    "boolean": "valueBoolean", "dateTime": "valueDateTime", "decimal": "valueDecimal",
}


class TransformError(ValueError):
    pass


def build_codesystem(template: ImportTemplate, rows: list[dict[str, Any]],
                     sheet: SheetSource | None = None, fhir_version: str | None = None,
                     on: date | None = None) -> tuple[dict[str, Any], list[str]]:
    """Returnera (CodeSystem, varningar). ``sheet`` ger namn/URL/OID för ett blad i en familj.

    Livscykelkolumner översätts enligt ``lifecycle.py`` per ``fhir_version`` (standard: FHIR_VERSION)
    och datumet ``on`` (standard: i dag)."""
    if fhir_version is None:
        from ..config import fhir_version as env_fhir_version
        fhir_version = env_fhir_version()
    problems = template.validate_template()
    if problems:
        raise TransformError("; ".join(problems))
    meta = template.codesystem
    props = {p.code: p for p in template.properties}
    code_col = template.column(ColumnRole.CODE).source_column
    display_col = template.column(ColumnRole.DISPLAY)
    warnings: list[str] = []
    concepts: list[dict[str, Any]] = []
    seen: set[str] = set()
    cs_url = sheet.url if sheet and sheet.url else meta.url
    life_cols = lifecycle.lifecycle_columns(template.columns)
    life_used: set[str] = set()

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
            if value is None or str(value).strip() == "" or col.role in (ColumnRole.CODE, ColumnRole.IGNORE,
                                                                         ColumnRole.LIFECYCLE):
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
            elif col.role == ColumnRole.CONTEXT:
                if str(value).strip().lower() not in _TRUE:
                    continue
                gen = template.generated(col.context.codesystem)
                target = next(c for c in gen.concepts if c.code == col.context.code)
                coding = {"system": gen.url, "code": target.code, "display": target.display}
                if col.context.mode == "property":
                    concept.setdefault("property", []).append({"code": gen.property_code, "valueCoding": coding})
                else:
                    text_col = col.context.text_column or (display_col.source_column if display_col else None)
                    text = row.get(text_col) if text_col else None
                    if text:
                        concept.setdefault("designation", []).append(
                            {"language": meta.language, "use": coding, "value": str(text).strip()})
            elif col.role == ColumnRole.PROPERTY:
                definition = props[col.property_code]
                try:
                    typed = coerce(value, definition)
                except ValueError as exc:
                    warnings.append(f"Rad {line}, kolumn '{col.source_column}': {exc}")
                    continue
                concept.setdefault("property", []).append({"code": definition.code, _VALUE_KEY[definition.type]: typed})
        if life_cols:
            life_props, exts, problems = lifecycle.concept_elements(life_cols, row, cs_url, fhir_version, on)
            if life_props:
                concept.setdefault("property", []).extend(life_props)
                life_used.update(p["code"] for p in life_props)
            if exts:
                concept.setdefault("extension", []).extend(exts)
            warnings += [f"Rad {line}, '{code}': {msg}" for msg in problems]
        concepts.append(concept)

    for concept in concepts:
        for prop in concept.get("property", []):
            if prop["code"] == "parent" and prop["valueCode"] not in seen:
                warnings.append(f"Kod '{concept['code']}': förälder '{prop['valueCode']}' finns inte i kodverket")

    properties = [_property_resource(p) for p in template.properties] + lifecycle.declarations(life_used)
    for gen in template.generated_codesystems:
        if any(c.role == ColumnRole.CONTEXT and c.context.mode == "property" and c.context.codesystem == gen.key
               for c in template.columns):
            properties.append({"code": gen.property_code, "uri": gen.url, "description": gen.title, "type": "Coding"})
    name = sheet.name if sheet else meta.name
    codesystem = {
        "resourceType": "CodeSystem",
        "id": _resource_id(name),
        "url": (sheet.url if sheet and sheet.url else meta.url),
        "identifier": identifier(sheet.identifier if sheet and sheet.identifier else meta.identifier),
        "version": meta.version,
        "name": _fhir_name(name),
        "title": (sheet.title if sheet and sheet.title else None) or meta.title or name,
        "status": "draft",
        "experimental": False,
        "date": date.today().isoformat(),
        "publisher": (sheet.publisher if sheet and sheet.publisher else meta.publisher),
        "contact": contact(meta.contact),
        "description": meta.description or f"{(sheet.title if sheet and sheet.title else None) or meta.title or name}.",
        "purpose": meta.purpose,
        "copyright": meta.copyright,
        "language": meta.language,
        "caseSensitive": meta.case_sensitive,
        "hierarchyMeaning": meta.hierarchy_meaning,
        "content": "complete",
        "count": len(concepts),
        "property": properties,
        "concept": concepts,
    }
    return {k: v for k, v in codesystem.items() if v not in (None, [])}, warnings


def generated_codesystem(gen: GeneratedCodeSystem, template: ImportTemplate) -> dict[str, Any]:
    """FHIR CodeSystem för ett kodverk som skapats av kolumnetiketter (kontexter, textroller)."""
    return {
        "resourceType": "CodeSystem",
        "id": _resource_id(gen.name),
        "url": gen.url,
        "version": template.codesystem.version,
        "name": _fhir_name(gen.name),
        "title": gen.title,
        "status": "draft",
        "experimental": False,
        "date": date.today().isoformat(),
        "publisher": template.codesystem.publisher,
        "description": ("Användningskontexter som genererats från kolumnrubriker i källan."
                        if gen.purpose == "usage-context" else
                        "Designation-typer (textroller) som genererats från kolumnrubriker i källan."),
        "language": template.codesystem.language,
        "caseSensitive": True,
        "content": "complete",
        "count": len(gen.concepts),
        "concept": [{"code": c.code, "display": c.display} for c in gen.concepts],
    }


def identifier(value: str | None) -> list[dict] | None:
    """OID/UUID som Identifier ('1.2.752…' får prefixet urn:oid:)."""
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)+", value):
        value = f"urn:oid:{value}"
    return [{"system": "urn:ietf:rfc:3986", "value": value}]


def contact(value: str | None) -> list[dict] | None:
    """'namn <a@b.se>', 'a@b.se', 'https://…' eller ett namn -> ContactDetail."""
    if not value or not value.strip():
        return None
    value = value.strip()
    email = re.search(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", value)
    url = re.search(r"https?://\S+", value)
    name = re.sub(r"[<>()]", "", value.replace(email.group(0), "") if email else
                  value.replace(url.group(0), "") if url else value).strip(" ,;:-")
    detail: dict = {}
    if name:
        detail["name"] = name
    if email:
        detail["telecom"] = [{"system": "email", "value": email.group(0)}]
    elif url:
        detail["telecom"] = [{"system": "url", "value": url.group(0)}]
    return [detail]


def _ascii(name: str) -> str:
    """'Hjärtfrekvens' -> 'Hjartfrekvens' (å, ä, ö och andra diakritiska tecken translittereras)."""
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch))


def _resource_id(name: str) -> str:
    """FHIR-id: [A-Za-z0-9-.]{1,64}."""
    return "".join(ch if ch.isalnum() and ch.isascii() or ch in "-." else "-" for ch in _ascii(name))[:64].strip("-")


def _fhir_name(name: str) -> str:
    """CodeSystem.name ska vara maskinläsbart: inled med versal, bara [A-Za-z0-9_]."""
    cleaned = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in _ascii(name))
    return cleaned[:1].upper() + cleaned[1:] if cleaned else "CodeSystem"


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
