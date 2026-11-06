"""Mappningar mellan kodsystem -> ConceptMap.

Ett mappningsprojekt har en rad per mappning: källkod, målkod, relation och
kommentar. En källkod kan ha flera rader (flera mål). Raderna identifieras med
ett löpnummer (kodkolumnen), eftersom källkoden inte är unik.

Relationerna anges på svenska i vyn och översätts till FHIR:

=====================  ==================  ==================================
i vyn                  R4 equivalence      R5 relationship
=====================  ==================  ==================================
likvärdig              equivalent          equivalent
målet är bredare       wider               source-is-narrower-than-target
målet är smalare       narrower            source-is-broader-than-target
närliggande            relatedto           related-to
ingen motsvarighet     unmatched           (element.noMap = true)
=====================  ==================  ==================================

Rader utan målkod och utan "ingen motsvarighet" är ännu inte mappade och tas
inte med. "Målet är smalare" och "närliggande" ska motiveras med en kommentar
(i R4 kräver ConceptMap kommentar för smalare och inexakta mappningar).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .mapping import (
    CodeSystemMeta,
    ColumnMapping,
    ColumnRole,
    ImportTemplate,
    MappingSpec,
    SourceInfo,
    UiHints,
)
from .transform import _fhir_name, _resource_id, contact, identifier

RELATIONS = {  # nyckel: (etikett i vyn, R4 equivalence, R5 relationship)
    "equivalent": ("likvärdig", "equivalent", "equivalent"),
    "wider": ("målet är bredare", "wider", "source-is-narrower-than-target"),
    "narrower": ("målet är smalare", "narrower", "source-is-broader-than-target"),
    "related": ("närliggande", "relatedto", "related-to"),
    "unmatched": ("ingen motsvarighet", "unmatched", None),
}
LABELS = [label for label, _, _ in RELATIONS.values()]
NEEDS_COMMENT = ("narrower", "related")
_ALIASES = {"equal": "equivalent", "likvardig": "equivalent", "bredare": "wider", "smalare": "narrower",
            "relatedto": "related", "related-to": "related", "inexact": "related", "narliggande": "related",
            "source-is-narrower-than-target": "wider", "source-is-broader-than-target": "narrower",
            "nomap": "unmatched", "ingen": "unmatched", "saknas": "unmatched"}

SHEET = "Mappningar"
COLUMNS = [  # (rubrik, roll, widget)
    ("Nr", ColumnRole.CODE, "text"),
    ("Källkod", ColumnRole.MAP_SOURCE, "code-search"),
    ("Källtext", ColumnRole.MAP_SOURCE_DISPLAY, "text"),
    ("Relation", ColumnRole.MAP_RELATION, "select"),
    ("Målkod", ColumnRole.MAP_TARGET, "code-search"),
    ("Måltext", ColumnRole.MAP_TARGET_DISPLAY, "text"),
    ("Kommentar", ColumnRole.COMMENT, "textarea"),
]


def relation_key(value: Any) -> str | None:
    """'likvärdig', 'equivalent', 'source-is-narrower-than-target' … -> nyckel. None om tomt."""
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip().lower()
    for key, (label, r4, r5) in RELATIONS.items():
        if v in (key, label, r4, r5):
            return key
    import unicodedata
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", v) if not unicodedata.combining(ch))
    key = _ALIASES.get(v) or _ALIASES.get(plain)
    if key is None:
        raise ValueError(f"'{value}' är ingen känd relation ({', '.join(LABELS)})")
    return key


def label(key: str) -> str:
    return RELATIONS[key][0]


def template(meta: CodeSystemMeta, spec: MappingSpec) -> ImportTemplate:
    columns = [ColumnMapping(source_column=h, role=role, rationale="Mappningsprojekt",
                             ui=UiHints(label=h, order=i, widget=widget, editable=role != ColumnRole.CODE,
                                        options=LABELS if role == ColumnRole.MAP_RELATION else None))
               for i, (h, role, widget) in enumerate(COLUMNS)]
    return ImportTemplate(kind="conceptmap", codesystem=meta, source=SourceInfo(sheet=SHEET), columns=columns,
                          mapping=spec)


def _col(tpl: ImportTemplate, role: ColumnRole) -> str | None:
    c = tpl.column(role)
    return c.source_column if c else None


def next_row_id(rows: list[dict], tpl: ImportTemplate) -> str:
    code_col = _col(tpl, ColumnRole.CODE)
    used = {str(r.get(code_col)) for r in rows}
    n = len(rows) + 1
    while f"M{n}" in used:
        n += 1
    return f"M{n}"


def row_problems(tpl: ImportTemplate, row: dict) -> list[str]:
    """Det som måste rättas innan raden kan släppas (används vid redigering och releasekandidat)."""
    problems = []
    try:
        rel = relation_key(row.get(_col(tpl, ColumnRole.MAP_RELATION)))
    except ValueError as exc:
        return [str(exc)]
    target = row.get(_col(tpl, ColumnRole.MAP_TARGET))
    if rel == "unmatched" and target:
        problems.append("Relationen 'ingen motsvarighet' ska inte ha någon målkod.")
    if target and rel is None:
        problems.append("Ange relationen till målkoden.")
    if rel in NEEDS_COMMENT and not str(row.get(_col(tpl, ColumnRole.COMMENT)) or "").strip():
        problems.append(f"Relationen '{label(rel)}' ska motiveras med en kommentar.")
    return problems


def build_conceptmap(tpl: ImportTemplate, rows: list[dict], fhir_version: str = "R4"
                     ) -> tuple[dict[str, Any], list[str]]:
    spec, meta = tpl.mapping, tpl.codesystem
    src, src_d = _col(tpl, ColumnRole.MAP_SOURCE), _col(tpl, ColumnRole.MAP_SOURCE_DISPLAY)
    tgt, tgt_d = _col(tpl, ColumnRole.MAP_TARGET), _col(tpl, ColumnRole.MAP_TARGET_DISPLAY)
    rel_col, comment_col = _col(tpl, ColumnRole.MAP_RELATION), _col(tpl, ColumnRole.COMMENT)
    code_col = _col(tpl, ColumnRole.CODE)
    warnings, unmapped = [], []
    elements: dict[str, dict] = {}
    for row in rows:
        source = str(row.get(src) or "").strip()
        if not source:
            continue
        for p in row_problems(tpl, row):
            warnings.append(f"{row.get(code_col)} ({source}): {p}")
        try:
            rel = relation_key(row.get(rel_col))
        except ValueError:
            continue
        target = str(row.get(tgt) or "").strip()
        if not target and rel != "unmatched":
            unmapped.append(source)
            continue
        el = elements.setdefault(source, {"code": source, "display": row.get(src_d) or None, "target": []})
        _, r4, r5 = RELATIONS[rel]
        if rel == "unmatched":
            if fhir_version == "R5":
                el["noMap"] = True
                continue
            entry = {"equivalence": r4}
        else:
            entry = {"code": target, "display": row.get(tgt_d) or None}
            entry |= {"relationship": r5} if fhir_version == "R5" else {"equivalence": r4}
        if row.get(comment_col):
            entry["comment"] = str(row[comment_col]).strip()
        el["target"].append({k: v for k, v in entry.items() if v is not None})
    mapped_sources = set(elements)
    unmapped = sorted(set(unmapped) - mapped_sources)
    if unmapped:
        warnings.append(f"{len(unmapped)} källkoder är ännu inte mappade och tas inte med: "
                        + ", ".join(unmapped[:10]) + (" …" if len(unmapped) > 10 else ""))
    for el in elements.values():
        if not el["target"]:
            el.pop("target")
        if el.get("display") is None:
            el.pop("display")
    scope = ({"sourceScopeUri": spec.source_valueset, "targetScopeUri": spec.target_valueset}
             if fhir_version == "R5" else {"sourceUri": spec.source_valueset, "targetUri": spec.target_valueset})
    cm = {
        "resourceType": "ConceptMap",
        "id": _resource_id(meta.name),
        "url": meta.url,
        "identifier": identifier(meta.identifier)[0] if meta.identifier and fhir_version == "R4" else None,
        "version": meta.version,
        "name": _fhir_name(meta.name),
        "title": meta.title or meta.name,
        "status": "draft",
        "experimental": False,
        "date": date.today().isoformat(),
        "publisher": meta.publisher,
        "contact": contact(meta.contact),
        "description": meta.description or f"Mappning: {meta.title or meta.name}.",
        "purpose": meta.purpose,
        "copyright": meta.copyright,
        **{k: v for k, v in scope.items() if v},
        "group": [{"source": spec.source_system, **({"sourceVersion": spec.source_version} if spec.source_version
                                                    else {}),
                   "target": spec.target_system, **({"targetVersion": spec.target_version} if spec.target_version
                                                    else {}),
                   "element": list(elements.values())}],
    }
    if fhir_version == "R5" and meta.identifier:
        cm["identifier"] = identifier(meta.identifier)
    return {k: v for k, v in cm.items() if v not in (None, [])}, warnings


def rows_from_conceptmap(cm: dict[str, Any], tpl: ImportTemplate) -> tuple[list[dict], list[str]]:
    """Rader ur en befintlig ConceptMap (t.ex. ett förslag från MCP-servern). Bara första gruppen
    som matchar mallens käll- och målsystem; övriga grupper rapporteras."""
    names = {role: _col(tpl, role) for role in (ColumnRole.CODE, ColumnRole.MAP_SOURCE, ColumnRole.MAP_SOURCE_DISPLAY,
                                                ColumnRole.MAP_TARGET, ColumnRole.MAP_TARGET_DISPLAY,
                                                ColumnRole.MAP_RELATION, ColumnRole.COMMENT)}
    rows, notes = [], []
    groups = cm.get("group", [])
    for g in groups[1:]:
        notes.append(f"Gruppen {g.get('source')} → {g.get('target')} togs inte med (ett projekt = ett källsystem "
                     "och ett målsystem).")
    for el in (groups[0].get("element", []) if groups else []):
        targets = el.get("target") or ([{"equivalence": "unmatched"}] if el.get("noMap") else [])
        for t in targets:
            rel = relation_key(t.get("equivalence") or t.get("relationship") or "equivalent")
            rows.append({names[ColumnRole.CODE]: f"M{len(rows) + 1}",
                         names[ColumnRole.MAP_SOURCE]: el["code"], names[ColumnRole.MAP_SOURCE_DISPLAY]: el.get("display"),
                         names[ColumnRole.MAP_RELATION]: label(rel),
                         names[ColumnRole.MAP_TARGET]: t.get("code"), names[ColumnRole.MAP_TARGET_DISPLAY]: t.get("display"),
                         names[ColumnRole.COMMENT]: t.get("comment")})
    return rows, notes


def spec_from_conceptmap(cm: dict[str, Any]) -> MappingSpec:
    g = (cm.get("group") or [{}])[0]
    return MappingSpec(source_system=g.get("source", ""), target_system=g.get("target", ""),
                       source_version=g.get("sourceVersion"), target_version=g.get("targetVersion"),
                       source_valueset=cm.get("sourceUri") or cm.get("sourceCanonical") or cm.get("sourceScopeUri"),
                       target_valueset=cm.get("targetUri") or cm.get("targetCanonical") or cm.get("targetScopeUri"))
