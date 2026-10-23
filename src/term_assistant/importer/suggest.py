"""Förslag på hur kolumner ska mappas.

Förslagen tas fram i två steg:

1. **Heuristik** (alltid): rubrikalias, kolumnprofiler, bladets struktur
   (grupprubriker, härledda kolumner, externa koder) och egenskapsregistret.
2. **AI** (valfritt, se ``ai.py``): en språkmodell granskar heuristikens
   förslag med blad och register som underlag. Modellen får bara välja bland
   registrets poster eller uttryckligen föreslå en ny egenskap – det är det
   som håller antalet egenskaper nere över tid.

Båda stegen uttrycker sitt resultat som ``ColumnChoice``-objekt som sedan
översätts till en ``ImportTemplate`` av samma funktion.
"""

from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import BaseModel, Field

from .external import ExternalDetection, detect
from .mapping import (
    CodeSystemMeta,
    ColumnMapping,
    ColumnRole,
    ContextTarget,
    DesignationTarget,
    GeneratedCodeSystem,
    GeneratedConcept,
    ImportTemplate,
    PropertyDefinition,
    SheetSource,
    SourceInfo,
    UiHints,
)
from .profiling import ColumnProfile, profile_sheet
from .registry import PropertyRegistry, normalize, split_language
from .workbook import Sheet

ROLE_ALIASES: dict[ColumnRole, list[str]] = {
    ColumnRole.CODE: ["kod", "code", "id", "begreppskod", "kodvarde", "identifierare", "begrepps id", "concept id"],
    ColumnRole.DISPLAY: ["benamning", "term", "namn", "display", "klartext", "text", "preferred term",
                         "rekommenderad term", "begrepp", "kortnamn", "trivialnamn", "analys", "analysnamn",
                         "visningsnamn"],
    ColumnRole.DEFINITION: ["definition", "beskrivning", "forklaring", "description", "innebord"],
    ColumnRole.PARENT: ["overordnad", "overordnad kod", "foralder", "foraldrakod", "parent", "parent code",
                        "overordnat begrepp"],
    ColumnRole.GROUP: ["grupp", "kategori", "gruppering", "avsnitt", "sektion", "kapitel", "omrade", "category",
                       "group"],
}


class ColumnChoice(BaseModel):
    """Ett beslut om en kolumn – gemensamt format för heuristik och AI."""

    source_column: str
    role: ColumnRole
    registry_key: str | None = Field(None, description="Nyckel i egenskapsregistret (property eller designation)")
    new_property_code: str | None = Field(None, description="Endast om ingen registerpost passar")
    new_property_type: Literal["code", "Coding", "string", "integer", "boolean", "dateTime", "decimal"] | None = None
    new_property_description: str | None = None
    language: str | None = Field(None, description="Språk för designation, BCP-47")
    context_group: str | None = Field(None, description="Grupprubrik för role=context, t.ex. 'Används av'")
    pattern: str | None = Field(None, description="Härledningsmönster för role=derived")
    confidence: float = Field(ge=0, le=1)
    rationale: str


class MappingProposal(BaseModel):
    template: ImportTemplate
    choices: list[ColumnChoice]
    profiles: list[ColumnProfile]
    questions: list[str] = []
    new_properties: list[PropertyDefinition] = []
    merge_suggestions: list[tuple[str, str, float]] = []
    external: ExternalDetection | None = None


def _role_alias_score(header: str, role: ColumnRole) -> float:
    full = normalize(header)
    base, _ = split_language(header)
    names = ROLE_ALIASES[role]
    if full in names or base in names:
        return 1.0
    if any(re.search(rf"\b{re.escape(n)}\b", full) for n in names if len(n) >= 3):
        return 0.8
    return 0.0


def derive_pattern(sheet: Sheet, code_col: str, column: str) -> str | None:
    """Om kolumnens värden byggs av koden (t.ex. 'KV_X.KOD.RBK'), returnera mönstret."""
    names = [v for v in sheet.metadata.values() if v] + [sheet.name]
    patterns = set()
    n = 0
    for row in sheet.rows:
        code, value = row.get(code_col), row.get(column)
        if value is None or code is None:
            continue
        code, value = str(code), str(value)
        if code not in value or code == value:
            return None
        pattern = value
        for name in names:
            if name and name in pattern and name != code:
                pattern = pattern.replace(name, "{codesystem}")
        pattern = pattern.replace(code, "{code}")
        patterns.add(pattern)
        n += 1
    return patterns.pop() if n >= 2 and len(patterns) == 1 else None


def derive_pattern_all(sheets: list[Sheet], code_col: str, column: str) -> str | None:
    """Som ``derive_pattern`` men mönstret måste vara detsamma i alla blad i familjen."""
    patterns = {derive_pattern(s, code_col, column) for s in sheets if any(r.get(column) for r in s.rows)}
    return patterns.pop() if len(patterns) == 1 and None not in patterns else None


def heuristic_choices(
    profiles: list[ColumnProfile],
    registry: PropertyRegistry,
    sheet: Sheet | None = None,
    family: list[Sheet] | None = None,
) -> tuple[list[ColumnChoice], list[str], ExternalDetection | None]:
    choices: dict[str, ColumnChoice] = {}
    questions: list[str] = []
    by_header = {p.header: p for p in profiles}

    # 1. Kodkolumn: rubrikalias, externa koder och unikhet.
    externals = {p.header: detect(sheet.column(p.header)) if sheet else None for p in profiles}
    candidates = [p for p in profiles if p.unique_ratio > 0.85 and p.fill_ratio > 0.9]
    code = max(candidates, key=lambda p: (_role_alias_score(p.header, ColumnRole.CODE) > 0,
                                          externals.get(p.header) is not None,
                                          p.unique_ratio > 0.98,
                                          p.inferred_type in ("code", "integer"), -p.avg_length), default=None)
    if code is None:
        questions.append("Ingen kolumn är (nästan) unik och fullständigt ifylld – vilken kolumn innehåller koden?")
        code = profiles[0]
    external = externals.get(code.header)
    rationale = f"Unik till {code.unique_ratio:.0%} och {code.inferred_type}-lik"
    if external:
        rationale += "; koder ur " + ", ".join(s.name for s in external.systems)
    choices[code.header] = ColumnChoice(
        source_column=code.header, role=ColumnRole.CODE,
        confidence=0.95 if _role_alias_score(code.header, ColumnRole.CODE) or external else 0.7,
        rationale=rationale,
    )
    if code.unique_ratio < 0.98:
        questions.append(f"Kodkolumnen '{code.header}' innehåller dubbletter – ska samma kod finnas på flera rader?")

    # 2. Display: alias, annars första textkolumn med hög ifyllnad.
    rest = [p for p in profiles if p.header not in choices and p.inferred_type != "marker"]
    display = max(rest, key=lambda p: (_role_alias_score(p.header, ColumnRole.DISPLAY)
                                       * (split_language(p.header)[1] in (None, "sv")),
                                       p.inferred_type == "text", p.fill_ratio), default=None)
    if display is not None and display.fill_ratio > 0.5:
        alias = _role_alias_score(display.header, ColumnRole.DISPLAY)
        choices[display.header] = ColumnChoice(
            source_column=display.header, role=ColumnRole.DISPLAY,
            confidence=0.9 if alias else 0.6,
            rationale="Rubriken liknar en benämning" if alias else "Första fritextkolumnen med hög ifyllnadsgrad",
        )

    if sheet is not None:
        # 3. Kryssmarkeringar under en grupprubrik ("Används av") -> användningskontext.
        for group, members in sheet.column_groups.items():
            for h in members:
                p = by_header.get(h)
                if p is None or h in choices or p.inferred_type not in ("marker", "boolean") and p.filled:
                    continue
                choices[h] = ColumnChoice(
                    source_column=h, role=ColumnRole.CONTEXT, context_group=group,
                    confidence=0.85 if p.inferred_type == "marker" else 0.7,
                    rationale=f"Kryssmarkering under grupprubriken '{group}'",
                )
        # Kryssmarkeringar utan grupprubrik blir också kontexter, om de är flera.
        loose = [p for p in profiles if p.header not in choices and p.inferred_type == "marker"]
        if len(loose) >= 2:
            for p in loose:
                choices[p.header] = ColumnChoice(source_column=p.header, role=ColumnRole.CONTEXT,
                                                 context_group="Användning", confidence=0.6,
                                                 rationale="Kryssmarkering; trolig användningskontext")

        # 4. Kolumner som kan härledas ur koden.
        for p in profiles:
            if p.header in choices:
                continue
            pattern = derive_pattern_all(family or [sheet], code.header, p.header)
            if pattern:
                choices[p.header] = ColumnChoice(source_column=p.header, role=ColumnRole.DERIVED, pattern=pattern,
                                                 confidence=0.95, rationale=f"Härleds ur koden: {pattern}")

    # 5. Urval ur externt kodsystem: en grupperingskolumn ger ett ValueSet per grupp.
    if external:
        groups = [p for p in profiles if p.header not in choices and p.inferred_type in ("text", "code")
                  and (_role_alias_score(p.header, ColumnRole.GROUP) or p.distinct <= 40 and p.filled >= p.distinct * 3)]
        group = max(groups, key=lambda p: (_role_alias_score(p.header, ColumnRole.GROUP), -p.distinct), default=None)
        if group:
            choices[group.header] = ColumnChoice(
                source_column=group.header, role=ColumnRole.GROUP, confidence=0.85,
                rationale=f"{group.distinct} grupper; ett ValueSet per grupp",
            )

    # 6. Övriga kolumner.
    for p in profiles:
        if p.header in choices:
            continue
        choices[p.header] = _choose_extra(p, code.header, registry, choices)

    # Följdfrågor där förtroendet är lågt.
    for c in choices.values():
        if c.confidence < 0.6 and c.role != ColumnRole.IGNORE:
            questions.append(f"Kolumnen '{c.source_column}': {c.rationale}. Stämmer tolkningen som {c.role.value}"
                             + (f" ({c.registry_key or c.new_property_code})" if c.registry_key or c.new_property_code else "")
                             + "?")
    return [choices[p.header] for p in profiles if p.header in by_header], questions, external


def _choose_extra(p: ColumnProfile, code_col: str, registry: PropertyRegistry,
                  taken: dict[str, ColumnChoice]) -> ColumnChoice:
    if p.filled == 0:
        return ColumnChoice(source_column=p.header, role=ColumnRole.IGNORE, confidence=0.9, rationale="Kolumnen är tom")

    if p.subset_of == code_col or _role_alias_score(p.header, ColumnRole.PARENT):
        conf = 0.9 if p.subset_of == code_col and _role_alias_score(p.header, ColumnRole.PARENT) else 0.7
        return ColumnChoice(source_column=p.header, role=ColumnRole.PARENT, confidence=conf,
                            rationale="Värdena är koder i kodkolumnen" if p.subset_of == code_col
                            else "Rubriken anger överordnat begrepp")

    if _role_alias_score(p.header, ColumnRole.DEFINITION) and not any(
            c.role == ColumnRole.DEFINITION for c in taken.values()) and split_language(p.header)[1] in (None, "sv"):
        return ColumnChoice(source_column=p.header, role=ColumnRole.DEFINITION, confidence=0.85,
                            rationale="Rubriken anger definition/beskrivning")

    base, language = split_language(p.header)
    # Översättning av benämning, t.ex. "Benämning (en)".
    if language and language != "sv" and base in ROLE_ALIASES[ColumnRole.DISPLAY]:
        return ColumnChoice(source_column=p.header, role=ColumnRole.DESIGNATION, language=language,
                            confidence=0.85, rationale=f"Benämning på språket '{language}'")

    used_keys = {c.registry_key for c in taken.values() if c.role == ColumnRole.PROPERTY}
    for cand in registry.match(p.header):
        entry = cand.entry
        if entry.kind == "designation":
            return ColumnChoice(source_column=p.header, role=ColumnRole.DESIGNATION, registry_key=entry.key,
                                language=cand.language, confidence=round(cand.score * 0.95, 2),
                                rationale=f"Registret: {cand.reason}")
        if entry.key in used_keys:
            continue
        conf = cand.score * (1.0 if _type_compatible(entry.property.type, p) else 0.6)
        return ColumnChoice(source_column=p.header, role=ColumnRole.PROPERTY, registry_key=entry.key,
                            confidence=round(conf, 2),
                            rationale=f"Registret: {cand.reason}"
                            + ("" if _type_compatible(entry.property.type, p) else
                               f", men värdena ser ut som {p.inferred_type}"))

    # Inget passar – föreslå en ny egenskap, men flagga den för granskning.
    return ColumnChoice(
        source_column=p.header, role=ColumnRole.PROPERTY,
        new_property_code=to_property_code(p.header), new_property_type=_fhir_type(p),
        new_property_description=p.header, confidence=0.4,
        rationale="Ingen befintlig egenskap matchar – föreslår ny egenskap",
    )


def _fhir_type(p: ColumnProfile) -> str:
    if p.inferred_type in ("boolean", "marker"):
        return "boolean"
    if p.inferred_type == "integer":
        return "integer"
    if p.inferred_type == "decimal":
        return "decimal"
    if p.inferred_type == "date":
        return "dateTime"
    if p.inferred_type == "code" and _low_cardinality(p):
        return "code"
    return "string"


def _low_cardinality(p: ColumnProfile) -> bool:
    return bool(p.top_values) and p.distinct <= 12 and p.filled >= p.distinct * 2


def _type_compatible(prop_type: str, p: ColumnProfile) -> bool:
    expected = {
        "boolean": {"boolean", "marker"}, "integer": {"integer"}, "decimal": {"decimal", "integer"},
        "dateTime": {"date"}, "code": {"code", "text", "integer"}, "Coding": {"code", "text"},
        "string": {"text", "code", "integer", "decimal", "date", "boolean"},
    }
    return p.inferred_type in expected.get(prop_type, {p.inferred_type})


def to_property_code(header: str) -> str:
    words = normalize(header).split()
    if not words:
        return "property"
    code = words[0] + "".join(w.capitalize() for w in words[1:])
    return code if code[0].isalpha() else f"p{code}"


def to_constant(label: str, max_len: int = 48) -> str:
    """'Utlåtande för vård' -> 'UTLATANDE_FOR_VARD'."""
    code = "_".join(normalize(label).upper().split()) or "KOD"
    return code[:max_len].rstrip("_")


def family_prefix(names: list[str]) -> str:
    """Gemensamt prefix för bladnamn, t.ex. ['KV_EX_0001', 'KV_EX_7263_01'] -> 'KV_EX'."""
    if not names:
        return ""
    prefix = os.path.commonprefix(names)
    return re.sub(r"[_\-\s.]*\d*$", "", prefix) or names[0]


def build_template(
    choices: list[ColumnChoice],
    profiles: list[ColumnProfile],
    registry: PropertyRegistry,
    meta: CodeSystemMeta,
    source: SourceInfo,
    sheet: Sheet | None = None,
    external: ExternalDetection | None = None,
) -> tuple[ImportTemplate, list[PropertyDefinition]]:
    """Översätt beslut till en mall. Returnerar (mall, nya egenskaper)."""
    by_header = {p.header: p for p in profiles}
    columns: list[ColumnMapping] = []
    properties: dict[str, PropertyDefinition] = {}
    new_properties: list[PropertyDefinition] = []
    generated: dict[str, GeneratedCodeSystem] = {}
    family = len(source.sheets) > 1
    url_base = meta.url.rstrip("/") if family else meta.url.rsplit("/", 1)[0]
    prefix = family_prefix([s.name for s in source.sheets]) if family else meta.name

    for order, choice in enumerate(choices):
        profile = by_header.get(choice.source_column)
        mapping = ColumnMapping(source_column=choice.source_column, role=choice.role,
                                confidence=choice.confidence, rationale=choice.rationale,
                                note=sheet.header_notes.get(choice.source_column) if sheet else None)
        if choice.role == ColumnRole.PROPERTY:
            entry = registry.get(choice.registry_key) if choice.registry_key else None
            if entry and entry.property:
                definition = entry.property
            else:
                definition = PropertyDefinition(code=choice.new_property_code or to_property_code(choice.source_column),
                                                type=choice.new_property_type or "string",
                                                description=choice.new_property_description or choice.source_column)
                new_properties.append(definition)
            properties[definition.code] = definition
            mapping.property_code = definition.code
        elif choice.role == ColumnRole.PARENT:
            properties["parent"] = PropertyDefinition(code="parent", uri="http://hl7.org/fhir/concept-properties#parent",
                                                      type="code", description="Parent")
        elif choice.role == ColumnRole.DESIGNATION:
            entry = registry.get(choice.registry_key) if choice.registry_key else None
            base = entry.designation.model_copy() if entry and entry.designation else DesignationTarget()
            base.language = choice.language or base.language or meta.language
            mapping.designation = base
        elif choice.role == ColumnRole.CONTEXT:
            group = choice.context_group or "Användning"
            key = to_property_code(group)
            if key not in generated:
                name = f"{prefix}_{to_constant(group)}"
                generated[key] = GeneratedCodeSystem(
                    key=key, url=f"{url_base}/{name}", name=name, title=f"{group} ({prefix})",
                    purpose="usage-context", property_code=key, concepts=[])
            gen = generated[key]
            existing = next((c for c in gen.concepts if c.source_column == choice.source_column), None)
            if existing:
                code = existing.code
            else:
                code, n = to_constant(choice.source_column), 2
                while code in {c.code for c in gen.concepts}:  # avkortade etiketter kan krocka
                    code, n = f"{to_constant(choice.source_column, 44)}_{n}", n + 1
                gen.concepts.append(GeneratedConcept(code=code, display=choice.source_column,
                                                     source_column=choice.source_column))
            mapping.context = ContextTarget(codesystem=key, code=code)
        elif choice.role == ColumnRole.DERIVED:
            mapping.pattern = choice.pattern
        mapping.ui = _ui_hints(choice, profile, order, properties.get(mapping.property_code or ""))
        columns.append(mapping)

    template = ImportTemplate(
        kind="valueset" if external else "codesystem",
        codesystem=meta, source=source, columns=columns, properties=list(properties.values()),
        generated_codesystems=list(generated.values()),
        external_systems={s.key: s.uri or f"urn:term-assistant:unresolved:{s.key}"
                          for s in external.systems} if external else {},
    )
    if any(c.role == ColumnRole.PARENT for c in columns) and meta.hierarchy_meaning is None:
        template.codesystem.hierarchy_meaning = "is-a"
    return template, new_properties


def _ui_hints(choice: ColumnChoice, p: ColumnProfile | None, order: int,
              definition: PropertyDefinition | None) -> UiHints:
    hints = UiHints(label=choice.source_column, order=order, visible=choice.role != ColumnRole.IGNORE)
    if choice.role == ColumnRole.CODE:
        hints.editable = False  # koder ändras inte efter release
    if choice.role == ColumnRole.PARENT:
        hints.widget = "concept-picker"
    elif choice.role == ColumnRole.CONTEXT:
        hints.widget, hints.group = "checkbox", choice.context_group
    elif choice.role == ColumnRole.DERIVED:
        hints.editable = False  # beräknas ur koden
    elif choice.role == ColumnRole.GROUP and p and p.top_values:
        hints.widget, hints.options = "select", p.top_values
    elif definition and definition.type == "boolean":
        hints.widget = "checkbox"
    elif definition and definition.type == "dateTime":
        hints.widget = "date"
    elif definition and definition.type in ("integer", "decimal"):
        hints.widget = "number"
    elif definition and definition.type in ("code", "string") and p and _low_cardinality(p):
        hints.widget, hints.options = "select", p.top_values
    elif p and (p.avg_length > 80 or choice.role == ColumnRole.DEFINITION):
        hints.widget = "textarea"
    return hints


def sheet_source(sheet: Sheet) -> SheetSource:
    """Kodverkets namn och titel ur bladets nyckel–värde-huvud, annars bladnamnet."""
    meta = {normalize(k): v for k, v in sheet.metadata.items()}
    name = meta.get("kodverk") or meta.get("namn") or meta.get("name") or sheet.name
    title = meta.get("avser") or meta.get("titel") or meta.get("title") or meta.get("beskrivning")
    return SheetSource(sheet=sheet.name, name=str(name), title=title)


def propose_mapping(
    sheet: Sheet,
    registry: PropertyRegistry,
    meta: CodeSystemMeta,
    source_file: str | None = None,
    family: list[Sheet] | None = None,
) -> MappingProposal:
    """Föreslå mall för ett blad, eller för en familj av blad med samma struktur.

    För en familj är ``meta.url`` bas-URL:en; varje blad får ``<bas>/<namn>``.
    Profilering görs på familjens alla rader så att t.ex. en kolumn som är tom
    i ett blad men ifylld i ett annat bedöms rätt.
    """
    sheets = family or [sheet]
    combined = sheet
    if len(sheets) > 1:
        combined = Sheet(name=sheet.name, header_row=sheet.header_row, headers=sheet.headers,
                         rows=[r for s in sheets for r in s.rows], total_rows=sum(s.total_rows for s in sheets),
                         metadata=sheet.metadata, column_groups=sheet.column_groups, header_notes=sheet.header_notes)
    profiles = profile_sheet(combined)
    choices, questions, external = heuristic_choices(profiles, registry, combined, family=sheets)
    sources = [sheet_source(s) for s in sheets]
    if len(sheets) > 1:
        for s in sources:
            s.url = f"{meta.url.rstrip('/')}/{s.name}"
    source = SourceInfo(file=source_file, sheet=sheet.name, header_row=sheet.header_row, sheets=sources)
    template, new_props = build_template(choices, profiles, registry, meta, source, sheet, external)
    if external:
        for anomaly in external.anomalies:
            questions.append(f"Koden '{anomaly.code}' {anomaly.problem}.")
        for s in external.systems:
            if not s.uri or not s.verified:
                questions.append(f"Vilken canonical URL har {s.name} på er terminologitjänst? "
                                 f"(föreslagen: {s.uri or 'saknas'})")
    return MappingProposal(
        template=template, choices=choices, profiles=profiles, questions=questions,
        new_properties=new_props, merge_suggestions=registry.near_duplicates(), external=external,
    )
