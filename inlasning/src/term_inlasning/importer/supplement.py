"""Översättningar av kodsystem -> CodeSystem-supplement.

Ett översättningsprojekt har en rad per kod i kodsystemet som översätts:
koden, kodsystemets egen text (som referens) och översättningen på
projektets språk. Exporten följer samma form som HL7 Sveriges språkpaket:

* ``content = supplement`` och ``supplements = <kodsystemets URL>``
* ``language`` = översättningens språk
* per kod: översättningen som ``display`` och som ``designation`` med språket

Rader utan översättning tas inte med. Koder som inte finns i kodsystemet, eller
som tillkommit där sedan projektet skapades, hittas med "Kontrollera
översättningen" respektive "Hämta nya koder".
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .mapping import CodeSystemMeta, ColumnMapping, ColumnRole, ImportTemplate, SourceInfo, TranslationSpec, UiHints
from .transform import _fhir_name, _resource_id, contact, identifier

SHEET = "Översättningar"
COLUMNS = [  # (rubrik, roll, widget, redigerbar)
    ("Kod", ColumnRole.CODE, "text", False),
    ("Originaltext", ColumnRole.ORIGINAL_DISPLAY, "text", False),
    ("Översättning", ColumnRole.TRANSLATION, "text", True),
    ("Kommentar", ColumnRole.COMMENT, "textarea", True),
]
LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def template(meta: CodeSystemMeta, spec: TranslationSpec) -> ImportTemplate:
    columns = [ColumnMapping(source_column=h, role=role, rationale="Översättningsprojekt",
                             ui=UiHints(label=h, order=i, widget=widget, editable=editable))
               for i, (h, role, widget, editable) in enumerate(COLUMNS)]
    return ImportTemplate(kind="supplement", codesystem=meta, source=SourceInfo(sheet=SHEET), columns=columns,
                          translation=spec)


def _col(tpl: ImportTemplate, role: ColumnRole) -> str:
    return tpl.column(role).source_column


def rows(tpl: ImportTemplate, codes: list[dict], translations: dict[str, str] | None = None) -> list[dict]:
    """En rad per kod ({code, display}) med ev. befintliga översättningar ({kod: text})."""
    translations = translations or {}
    return [{_col(tpl, ColumnRole.CODE): c["code"], _col(tpl, ColumnRole.ORIGINAL_DISPLAY): c.get("display"),
             _col(tpl, ColumnRole.TRANSLATION): translations.get(c["code"])} for c in codes]


def build_supplement(tpl: ImportTemplate, data_rows: list[dict]) -> tuple[dict[str, Any], list[str]]:
    meta, spec = tpl.codesystem, tpl.translation
    code_col, text_col = _col(tpl, ColumnRole.CODE), _col(tpl, ColumnRole.TRANSLATION)
    concepts, missing, seen = [], [], set()
    for row in data_rows:
        code = str(row.get(code_col) or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        text = str(row.get(text_col) or "").strip()
        if not text:
            missing.append(code)
            continue
        concepts.append({"code": code, "display": text,
                         "designation": [{"language": meta.language, "value": text}]})
    warnings = []
    if missing:
        warnings.append(f"{len(missing)} av {len(seen)} koder saknar översättning och tas inte med: "
                        + ", ".join(missing[:10]) + (" …" if len(missing) > 10 else ""))
    supplements = spec.codesystem + (f"|{spec.codesystem_version}" if spec.codesystem_version else "")
    cs = {
        "resourceType": "CodeSystem",
        "id": _resource_id(meta.name),
        "url": meta.url,
        "identifier": identifier(meta.identifier),
        "version": meta.version,
        "name": _fhir_name(meta.name),
        "title": meta.title or meta.name,
        "status": "draft",
        "experimental": False,
        "date": date.today().isoformat(),
        "publisher": meta.publisher,
        "contact": contact(meta.contact),
        "description": meta.description or f"Översättning ({meta.language}) av {spec.codesystem_title or spec.codesystem}.",
        "purpose": meta.purpose,
        "copyright": meta.copyright,
        "language": meta.language,
        "caseSensitive": meta.case_sensitive,
        "content": "supplement",
        "supplements": supplements,
        "count": len(concepts),
        "concept": concepts,
    }
    return {k: v for k, v in cs.items() if v not in (None, [])}, warnings


def translations_from_supplement(resource: dict[str, Any]) -> dict[str, str]:
    """{kod: översättning} ur ett supplement: designation på supplementets språk, annars display."""
    language = resource.get("language")
    out = {}

    def walk(concepts):
        for c in concepts:
            text = next((d.get("value") for d in c.get("designation", [])
                         if d.get("value") and (not language or d.get("language") in (None, language))),
                        None) or c.get("display")
            if text:
                out[c["code"]] = text
            walk(c.get("concept", []))
    walk(resource.get("concept", []))
    return out
