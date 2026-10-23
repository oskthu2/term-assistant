"""Förslag på hur kolumner ska mappas.

Förslagen tas fram i två steg:

1. **Heuristik** (alltid): rubrikalias, kolumnprofiler och egenskapsregistret.
2. **AI** (valfritt, se ``ai.py``): en språkmodell granskar heuristikens
   förslag med blad och register som underlag. Modellen får bara välja bland
   registrets poster eller uttryckligen föreslå en ny egenskap – det är det
   som håller antalet egenskaper nere över tid.

Båda stegen uttrycker sitt resultat som ``ColumnChoice``-objekt som sedan
översätts till en ``ImportTemplate`` av samma funktion.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from .mapping import (
    CodeSystemMeta,
    ColumnMapping,
    ColumnRole,
    DesignationTarget,
    ImportTemplate,
    PropertyDefinition,
    SourceInfo,
    UiHints,
)
from .profiling import ColumnProfile, profile_sheet
from .registry import PropertyRegistry, normalize, split_language
from .workbook import Sheet

ROLE_ALIASES: dict[ColumnRole, list[str]] = {
    ColumnRole.CODE: ["kod", "code", "id", "begreppskod", "kodvarde", "identifierare", "begrepps id", "concept id"],
    ColumnRole.DISPLAY: ["benamning", "term", "namn", "display", "klartext", "text", "preferred term",
                         "rekommenderad term", "begrepp"],
    ColumnRole.DEFINITION: ["definition", "beskrivning", "forklaring", "description", "innebord"],
    ColumnRole.PARENT: ["overordnad", "overordnad kod", "foralder", "foraldrakod", "parent", "parent code",
                        "overordnat begrepp"],
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
    confidence: float = Field(ge=0, le=1)
    rationale: str


class MappingProposal(BaseModel):
    template: ImportTemplate
    choices: list[ColumnChoice]
    profiles: list[ColumnProfile]
    questions: list[str] = []
    new_properties: list[PropertyDefinition] = []
    merge_suggestions: list[tuple[str, str, float]] = []


def _role_alias_score(header: str, role: ColumnRole) -> float:
    full = normalize(header)
    base, _ = split_language(header)
    names = ROLE_ALIASES[role]
    if full in names or base in names:
        return 1.0
    if any(re.search(rf"\b{re.escape(n)}\b", full) for n in names if len(n) >= 4):
        return 0.8
    return 0.0


def heuristic_choices(profiles: list[ColumnProfile], registry: PropertyRegistry) -> tuple[list[ColumnChoice], list[str]]:
    choices: dict[str, ColumnChoice] = {}
    questions: list[str] = []
    by_header = {p.header: p for p in profiles}

    # 1. Kodkolumn: alias + unikhet, annars första unika kodlika kolumnen.
    unique = [p for p in profiles if p.unique_ratio > 0.98 and p.fill_ratio > 0.95]
    code = max(unique, key=lambda p: (_role_alias_score(p.header, ColumnRole.CODE),
                                      p.inferred_type in ("code", "integer"), -p.avg_length), default=None)
    if code is None:
        questions.append("Ingen kolumn är unik och fullständigt ifylld – vilken kolumn innehåller koden?")
        code = profiles[0]
    choices[code.header] = ColumnChoice(
        source_column=code.header, role=ColumnRole.CODE,
        confidence=0.95 if _role_alias_score(code.header, ColumnRole.CODE) else 0.7,
        rationale=f"Unik ({code.unique_ratio:.0%}) och {code.inferred_type}-lik",
    )

    # 2. Display: alias, annars första textkolumn med hög ifyllnad.
    rest = [p for p in profiles if p.header not in choices]
    display = max(rest, key=lambda p: (_role_alias_score(p.header, ColumnRole.DISPLAY) * (split_language(p.header)[1] in (None, "sv")),
                                       p.inferred_type == "text", p.fill_ratio), default=None)
    if display is not None and display.fill_ratio > 0.5:
        choices[display.header] = ColumnChoice(
            source_column=display.header, role=ColumnRole.DISPLAY,
            confidence=0.9 if _role_alias_score(display.header, ColumnRole.DISPLAY) else 0.6,
            rationale="Rubriken liknar en benämning" if _role_alias_score(display.header, ColumnRole.DISPLAY)
            else "Första fritextkolumnen med hög ifyllnadsgrad",
        )

    # 3. Övriga kolumner.
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
    return [choices[p.header] for p in profiles if p.header in by_header], questions


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
    if p.inferred_type == "boolean":
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
        "boolean": {"boolean"}, "integer": {"integer"}, "decimal": {"decimal", "integer"},
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


def build_template(
    choices: list[ColumnChoice],
    profiles: list[ColumnProfile],
    registry: PropertyRegistry,
    meta: CodeSystemMeta,
    source: SourceInfo,
) -> tuple[ImportTemplate, list[PropertyDefinition]]:
    """Översätt beslut till en mall. Returnerar (mall, nya egenskaper)."""
    by_header = {p.header: p for p in profiles}
    columns: list[ColumnMapping] = []
    properties: dict[str, PropertyDefinition] = {}
    new_properties: list[PropertyDefinition] = []
    for order, choice in enumerate(choices):
        profile = by_header.get(choice.source_column)
        mapping = ColumnMapping(source_column=choice.source_column, role=choice.role,
                                confidence=choice.confidence, rationale=choice.rationale)
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
        mapping.ui = _ui_hints(choice, profile, order, properties.get(mapping.property_code or ""))
        columns.append(mapping)
    template = ImportTemplate(codesystem=meta, source=source, columns=columns, properties=list(properties.values()))
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


def propose_mapping(
    sheet: Sheet,
    registry: PropertyRegistry,
    meta: CodeSystemMeta,
    source_file: str | None = None,
) -> MappingProposal:
    profiles = profile_sheet(sheet)
    choices, questions = heuristic_choices(profiles, registry)
    template, new_props = build_template(
        choices, profiles, registry, meta, SourceInfo(file=source_file, sheet=sheet.name, header_row=sheet.header_row)
    )
    return MappingProposal(
        template=template, choices=choices, profiles=profiles, questions=questions,
        new_properties=new_props, merge_suggestions=registry.near_duplicates(),
    )
