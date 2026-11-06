"""Operationer för administrationsvyn – utan webbramverk, så att de går att testa.

* projekt skapas från ett mallförslag (en gång: "läs in och migrera")
* celler läses och skrivs via mallen (kontextkryss, härledda värden, typer)
* kolumner byter roll eller läggs till; mallen valideras före sparande
* begrepp avvecklas i stället för att tas bort när policyn kräver det
* releasekandidater byggs från raderna, jämförs med senaste release och
  kontrolleras mot releasepolicyn; godkända kandidater blir releaser
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from ..importer.build import _split_key, build_all
from ..importer.mapping import (
    ColumnMapping,
    ColumnRole,
    ContextTarget,
    DesignationTarget,
    GeneratedCodeSystem,
    GeneratedConcept,
    ImportTemplate,
    PropertyDefinition,
    UiHints,
)
from ..importer import conceptmap, lifecycle, metadata, supplement
from ..importer.registry import PropertyRegistry
from ..importer.package import build_package, label_resources, package_manifest
from ..importer.release import bump, check_policy, diff, release as to_release
from ..importer.replacements import RELATIONS, REPLACED_BY, REPLACED_BY_EXT, replacement_conceptmap
from ..importer.suggest import MappingProposal, lifecycle_widget, to_constant, to_property_code
from ..importer.transform import _TRUE, coerce
from ..importer.validate import validate_resources
from ..importer.workbook import Sheet
from .settings import Namespace, Settings, Target
from .store import Flag, Project, Question, Release, SheetData, Workspace

ROLE_LABELS = {
    ColumnRole.CODE: "Kod",
    ColumnRole.DISPLAY: "Benämning (display)",
    ColumnRole.DEFINITION: "Definition",
    ColumnRole.DESIGNATION: "Designation (synonym, översättning, textroll)",
    ColumnRole.PROPERTY: "Egenskap",
    ColumnRole.PARENT: "Överordnad kod",
    ColumnRole.CONTEXT: "Användningskontext (kryss)",
    ColumnRole.DERIVED: "Härledd ur koden",
    ColumnRole.GROUP: "Grupp (ett ValueSet per grupp)",
    ColumnRole.SYSTEM: "Kodsystem (per rad)",
    ColumnRole.SYSTEM_VERSION: "Kodsystemets version",
    ColumnRole.INCLUDE: "Inkludera/exkludera",
    ColumnRole.RULE: "Urvalsregel (exakt, underordnade, ECL)",
    ColumnRole.COMMENT: "Kommentar/källa",
    ColumnRole.LIFECYCLE: "Livscykel och förvaltning",
    ColumnRole.MAP_SOURCE: "Källkod",
    ColumnRole.MAP_SOURCE_DISPLAY: "Källkodens text",
    ColumnRole.MAP_TARGET: "Målkod",
    ColumnRole.MAP_TARGET_DISPLAY: "Målkodens text",
    ColumnRole.MAP_RELATION: "Relation (ConceptMap)",
    ColumnRole.ORIGINAL_DISPLAY: "Kodsystemets text",
    ColumnRole.TRANSLATION: "Översättning",
    ColumnRole.IGNORE: "Ignoreras",
}
CS_ROLES = [ColumnRole.CODE, ColumnRole.DISPLAY, ColumnRole.DEFINITION, ColumnRole.DESIGNATION, ColumnRole.PROPERTY,
            ColumnRole.PARENT, ColumnRole.CONTEXT, ColumnRole.DERIVED, ColumnRole.LIFECYCLE, ColumnRole.IGNORE]
VS_ROLES = [ColumnRole.CODE, ColumnRole.DISPLAY, ColumnRole.SYSTEM, ColumnRole.SYSTEM_VERSION, ColumnRole.INCLUDE,
            ColumnRole.RULE, ColumnRole.COMMENT, ColumnRole.GROUP, ColumnRole.LIFECYCLE, ColumnRole.IGNORE]
MAP_ROLES = [ColumnRole.CODE, ColumnRole.MAP_SOURCE, ColumnRole.MAP_SOURCE_DISPLAY, ColumnRole.MAP_RELATION,
             ColumnRole.MAP_TARGET, ColumnRole.MAP_TARGET_DISPLAY, ColumnRole.COMMENT, ColumnRole.IGNORE]
SUPPLEMENT_ROLES = [ColumnRole.CODE, ColumnRole.ORIGINAL_DISPLAY, ColumnRole.TRANSLATION, ColumnRole.COMMENT,
                    ColumnRole.IGNORE]
PROPERTY_TYPES = ["string", "code", "boolean", "integer", "decimal", "dateTime", "Coding"]
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")


class OpError(ValueError):
    """Fel som visas för användaren."""

    def __init__(self, messages: list[str] | str):
        self.messages = [messages] if isinstance(messages, str) else messages
        super().__init__("; ".join(self.messages))


# -- skapa projekt ------------------------------------------------------------------


def create_project(ws: Workspace, proposal: MappingProposal, sheets: list[Sheet], title: str,
                   author: str = "okänd", namespace: Namespace | None = None,
                   classification: str | None = None) -> Project:
    tpl = proposal.template.model_copy(deep=True)
    if namespace:
        tpl.namespace = namespace.key
        tpl.codesystem.publisher = tpl.codesystem.publisher or namespace.publisher
    tpl.classification = classification
    by_name = {s.name: s for s in sheets}
    names = [s.sheet for s in tpl.source.sheets] if tpl.kind == "codesystem" and tpl.source.sheets \
        else [tpl.source.sheet]
    derived = {c.source_column for c in tpl.columns if c.role == ColumnRole.DERIVED}
    data: dict[str, SheetData] = {}
    for name in names:
        sheet = by_name[name]
        data[name] = SheetData(headers=[h for h in sheet.headers if h not in derived],
                               rows=[{h: v for h, v in r.items() if h not in derived} for r in sheet.rows])
    warnings = fold_text_catalogue(tpl, by_name, data)
    if tpl.source.file:
        tpl.source.file = tpl.source.file.rsplit("/", 1)[-1]
    project = Project(id=ws.new_id(title), title=title, source_file=tpl.source.file, template=tpl,
                      questions=[Question(text=q) for q in proposal.questions + warnings])
    ws.save(project, data, author=author, action="Projekt skapat från inläsning",
            detail={"fil": tpl.source.file, "blad": names, "rader": sum(len(d.rows) for d in data.values())})
    return project


def fold_text_catalogue(tpl: ImportTemplate, sheets: dict[str, Sheet], data: dict[str, SheetData]) -> list[str]:
    """Flytta in textkatalogens texter som vanliga designation-kolumner.

    I kalkylarket låg texterna i ett eget blad, nycklade på "KV_X.KOD". Efter
    migreringen är de kolumner på begreppen, med textrollen som designation.use.
    """
    cat = tpl.text_catalogue
    if not cat or cat.sheet not in sheets:
        return []
    gen = tpl.generated(cat.use_codesystem)
    code_col = tpl.column(ColumnRole.CODE).source_column
    sheet_of = {s.name: s.sheet for s in tpl.source.sheets}
    for text in cat.texts:
        label = text.column
        if any(c.source_column == label for c in tpl.columns):
            continue
        tpl.columns.append(ColumnMapping(
            source_column=label, role=ColumnRole.DESIGNATION, rationale=f"Från textkatalogen '{cat.sheet}'",
            designation=DesignationTarget(language=tpl.codesystem.language, use_system=gen.url,
                                          use_code=text.use_code, use_display=label),
            ui=UiHints(label=label, order=len(tpl.columns), widget="textarea")))
        for d in data.values():
            d.headers.append(label)
    index = {(name, str(r.get(code_col))): r for name, d in data.items() for r in d.rows}
    warnings = []
    for row in sheets[cat.sheet].rows:
        key = row.get(cat.key_column)
        if not key:
            continue
        hit = _split_key(str(key), sheet_of.keys())
        target = index.get((sheet_of[hit[0]], hit[1])) if hit else None
        if target is None:
            warnings.append(f"Textkatalogen: '{key}' pekar inte på något begrepp och lästes inte in.")
            continue
        for text in cat.texts:
            if row.get(text.column):
                target[text.column] = row[text.column]
    tpl.text_catalogue = None
    return warnings


# -- celler --------------------------------------------------------------------------


def is_true(value: Any) -> bool:
    return value is True or (value is not None and str(value).strip().lower() in _TRUE)


def sheet_name_for_codes(tpl: ImportTemplate, sheet: str) -> str:
    """Kodverkets namn för ett blad (för härledda värden som '{codesystem}.{code}.RBK')."""
    return next((s.name for s in tpl.source.sheets if s.sheet == sheet), tpl.codesystem.name)


def code_column(tpl: ImportTemplate) -> str:
    return tpl.column(ColumnRole.CODE).source_column


def visible_columns(tpl: ImportTemplate) -> list[tuple[int, ColumnMapping]]:
    cols = [(i, c) for i, c in enumerate(tpl.columns) if c.ui is None or c.ui.visible]
    return sorted(cols, key=lambda ic: (ic[1].ui.order if ic[1].ui else ic[0], ic[0]))


def widget(tpl: ImportTemplate, col: ColumnMapping) -> str:
    if col.role == ColumnRole.CONTEXT:
        return "checkbox"
    if col.ui and col.ui.widget:
        return col.ui.widget
    return "text"


def cell(tpl: ImportTemplate, col: ColumnMapping, row: dict, sheet: str) -> Any:
    if col.role == ColumnRole.DERIVED and col.pattern:
        code = row.get(code_column(tpl))
        if code is None:
            return None
        return col.pattern.replace("{codesystem}", sheet_name_for_codes(tpl, sheet)).replace("{code}", str(code))
    value = row.get(col.source_column)
    if widget(tpl, col) == "checkbox":
        return is_true(value)
    return value


def find_row(data: SheetData, tpl: ImportTemplate, code: str) -> dict | None:
    col = code_column(tpl)
    return next((r for r in data.rows if str(r.get(col)) == code), None)


def update_row(tpl: ImportTemplate, data: SheetData, row: dict, form: dict, is_new: bool) -> dict:
    """Skriv formulärvärden till en rad. Returnerar ändrade fält {kolumn: (före, efter)}."""
    errors, changes = [], {}
    props = {p.code: p for p in tpl.properties}
    code_col = code_column(tpl)
    new_values = {}
    for i, col in enumerate(tpl.columns):
        if col.role in (ColumnRole.DERIVED, ColumnRole.IGNORE) or (col.ui and not col.ui.visible):
            continue
        if col.ui and not col.ui.editable and not (is_new and col.role == ColumnRole.CODE):
            continue
        key = f"c{i}"
        if widget(tpl, col) == "checkbox":
            value = True if form.get(key) else (None if col.role == ColumnRole.CONTEXT else False)
        else:
            value = (form.get(key) or "").strip() or None
        if value is not None and col.role == ColumnRole.PROPERTY and col.property_code in props:
            try:
                coerce(value, props[col.property_code])
            except ValueError as exc:
                errors.append(f"{col.ui.label if col.ui else col.source_column}: {exc}")
        if value is not None and col.role == ColumnRole.LIFECYCLE and col.lifecycle in lifecycle.FIELDS \
                and not isinstance(value, bool):
            problem = lifecycle.check_value(col.lifecycle, value)
            if problem:
                errors.append(f"{col.ui.label if col.ui else col.source_column}: {problem}")
        new_values[col.source_column] = value

    code = new_values.get(code_col, row.get(code_col))
    if tpl.kind == "conceptmap":
        if not code:  # mappningsrader har löpnummer, inte egna koder
            code = new_values[code_col] = conceptmap.next_row_id(data.rows, tpl)
        errors += conceptmap.row_problems(tpl, {**row, **new_values})
        source_col = tpl.column(ColumnRole.MAP_SOURCE).source_column
        if not (new_values.get(source_col) or row.get(source_col)):
            errors.append("Källkoden måste anges.")
    if not code:
        errors.append("Koden måste anges.")
    elif tpl.kind == "codesystem" and not re.fullmatch(r"\S+", str(code)):
        errors.append("Koden får inte innehålla mellanslag.")
    elif any(str(r.get(code_col)) == str(code) and r is not row for r in data.rows):
        if tpl.kind == "codesystem" or not tpl.column(ColumnRole.GROUP):
            errors.append(f"Koden '{code}' finns redan.")
    if errors:
        raise OpError(errors)
    for k, v in new_values.items():
        before = row.get(k)
        if isinstance(v, bool) or isinstance(before, bool):
            changed = is_true(before) != is_true(v)   # kryss: "x" och True betyder samma sak
        else:
            changed = (before or None) != (v or None)
        if changed:
            changes[k] = (before, v)
            row[k] = v
    _stamp(tpl, row, changes, is_new)
    return changes


def _stamp(tpl: ImportTemplate, row: dict, changes: dict, is_new: bool) -> None:
    """Förvaltningsuppgifter som sköts automatiskt: 'Tillagd' för nya rader, 'Senast ändrad' vid ändring."""
    from datetime import date
    today = date.today().isoformat()
    cols = lifecycle.lifecycle_columns(tpl.columns)
    stamps = []
    if is_new and cols.get("effectiveDate"):
        stamps.append(cols["effectiveDate"])
    if (changes or is_new) and cols.get("changed"):
        stamps.append(cols["changed"])
    for col in stamps:
        if col in changes or (col == cols.get("effectiveDate") and row.get(col)):
            continue
        changes[col] = (row.get(col), today)
        row[col] = today


# -- kolumner -------------------------------------------------------------------------


def roles_for(tpl: ImportTemplate) -> list[ColumnRole]:
    return {"valueset": VS_ROLES, "conceptmap": MAP_ROLES, "supplement": SUPPLEMENT_ROLES}.get(tpl.kind, CS_ROLES)


def property_choices(tpl: ImportTemplate, registry: PropertyRegistry) -> list[PropertyDefinition]:
    """Egenskaper att välja bland: mallens egna först, sedan registret (konvergens)."""
    seen, out = set(), []
    for p in tpl.properties + [e.property for e in registry.properties()]:
        if p and p.code not in seen and p.code != "parent":
            seen.add(p.code)
            out.append(p)
    return out


def designation_uses(tpl: ImportTemplate, registry: PropertyRegistry) -> list[tuple[str, str]]:
    """(värde, etikett) för designation.use: registrets typer och genererade textroller."""
    out = [("", "– ingen (t.ex. översättning) –")]
    for e in registry.entries:
        if e.kind == "designation" and e.designation:
            out.append((f"reg:{e.key}", f"{e.designation.use_display} ({e.designation.use_code})"))
    for g in tpl.generated_codesystems:
        if g.purpose == "designation-use":
            out += [(f"gen:{g.key}:{c.code}", f"{g.title}: {c.display}") for c in g.concepts]
    return out


def _default_widget(role: ColumnRole, definition: PropertyDefinition | None, lifecycle_key: str | None = None) -> str:
    if role in (ColumnRole.CONTEXT,):
        return "checkbox"
    if role == ColumnRole.PARENT:
        return "concept-picker"
    if role in (ColumnRole.DEFINITION, ColumnRole.COMMENT):
        return "textarea"
    if role == ColumnRole.LIFECYCLE and lifecycle_key:
        return lifecycle_widget(lifecycle_key)[0]
    if definition:
        return {"boolean": "checkbox", "dateTime": "date", "integer": "number", "decimal": "number"}.get(
            definition.type, "text")
    return "text"


# -- vallista för kolumnens mål (vid inläsning) ---------------------------------------------------


def target_options(tpl: ImportTemplate, registry: PropertyRegistry) -> list[tuple[str, list[tuple[str, str]]]]:
    """Grupperad vallista över vad en kolumn kan stå för: [(grupp, [(värde, etikett)])]."""
    life = ("Livscykel och förvaltning (fasta mål)", lifecycle_options(tpl.kind))
    if tpl.kind == "valueset":
        return [
            ("Begrepp", [("role:code", "Kod"), ("role:display", "Benämning (display)")]),
            ("Värdemängd", [(f"role:{r.value}", ROLE_LABELS[r]) for r in
                            (ColumnRole.SYSTEM, ColumnRole.SYSTEM_VERSION, ColumnRole.INCLUDE, ColumnRole.RULE,
                             ColumnRole.COMMENT, ColumnRole.GROUP)]),
            life,
            ("Övrigt", [("role:ignore", "Ignoreras")]),
        ]
    props = [(f"prop:{p.code}", f"{p.code} ({p.type}){' – ' + p.description if p.description and p.description != p.code else ''}")
             for p in property_choices(tpl, registry)]
    desig = [(f"desig:use:{e.designation.use_code}", f"{e.designation.use_display}") for e in registry.entries
             if e.kind == "designation" and e.designation]
    desig += [("desig:lang:en", "Översättning (engelska)"), ("desig:lang:sv", "Synonym/variant (svenska, utan typ)")]
    return [
        ("Begrepp", [("role:code", "Kod"), ("role:display", "Benämning (display)"),
                     ("role:definition", "Definition"), ("role:parent", "Överordnad kod")]),
        ("Egenskap", props + [("prop:__new__", "+ Ny egenskap (kod från rubriken)")]),
        ("Designation", desig),
        life,
        ("Övrigt", [("role:ignore", "Ignoreras")]),
    ]


def lifecycle_options(kind: str) -> list[tuple[str, str]]:
    """(värde, etikett) för livscykelmålen, med vad de blir i ett kodverk respektive urval."""
    return [(f"life:{f.key}", f"{f.label} → {f.valueset if kind == 'valueset' else f.codesystem}")
            for f in lifecycle.FIELDS.values()]


def learn_from_template(tpl: ImportTemplate, registry: PropertyRegistry) -> list[str]:
    """Registret lär sig de bekräftade mappningarna (rubrik -> egenskap/designation-typ) – konvergens."""
    learned = []
    for col in tpl.columns:
        if col.role == ColumnRole.PROPERTY and col.property_code and col.property_code != "parent":
            if registry.get(col.property_code) is None:
                definition = next((p for p in tpl.properties if p.code == col.property_code), None)
                if definition:
                    registry.add_property(definition, aliases=[col.source_column])
            registry.learn(col.source_column, col.property_code)
            learned.append(f"{col.source_column} -> {col.property_code}")
        elif col.role == ColumnRole.LIFECYCLE and col.lifecycle and registry.get(f"lifecycle:{col.lifecycle}"):
            registry.learn(col.source_column, f"lifecycle:{col.lifecycle}")
            learned.append(f"{col.source_column} -> livscykel {col.lifecycle}")
        elif col.role == ColumnRole.DESIGNATION and col.designation and col.designation.use_code:
            entry = next((e for e in registry.entries if e.designation
                          and e.designation.use_code == col.designation.use_code), None)
            if entry:
                registry.learn(col.source_column, entry.key)
                learned.append(f"{col.source_column} -> {entry.key}")
    return learned


def current_target(col: ColumnMapping) -> str:
    if col.role == ColumnRole.PROPERTY:
        return f"prop:{col.property_code}"
    if col.role == ColumnRole.LIFECYCLE:
        return f"life:{col.lifecycle}"
    if col.role == ColumnRole.DESIGNATION and col.designation:
        if col.designation.use_code:
            return f"desig:use:{col.designation.use_code}"
        return f"desig:lang:{col.designation.language or 'sv'}"
    return f"role:{col.role.value}"


def apply_target(tpl: ImportTemplate, index: int, value: str, registry: PropertyRegistry) -> ImportTemplate:
    """Sätt kolumnens mål från vallistan (``target_options``)."""
    col = tpl.columns[index]
    form: dict[str, str] = {"label": col.ui.label if col.ui else col.source_column, "visible": "on", "editable": "on"}
    kind, _, rest = value.partition(":")
    if kind == "role":
        form["role"] = rest
        if rest == ColumnRole.IGNORE.value:
            form.pop("visible")
    elif kind == "life":
        form.update(role="lifecycle", lifecycle=rest)
    elif kind == "prop":
        form.update(role="property", property=rest)
        if rest == "__new__":
            form["new_property_code"] = to_property_code(col.source_column)
    elif kind == "desig":
        sub, _, arg = rest.partition(":")
        use = ""
        if sub == "use":
            entry = next((e for e in registry.entries if e.designation and e.designation.use_code == arg), None)
            gen = next((f"gen:{g.key}:{arg}" for g in tpl.generated_codesystems
                        if g.purpose == "designation-use" and any(c.code == arg for c in g.concepts)), None)
            use = f"reg:{entry.key}" if entry else (gen or "")
        form.update(role="designation", use=use, language=arg if sub == "lang" else tpl.codesystem.language)
    else:
        raise OpError(f"Okänt val '{value}'.")
    return update_column(tpl, index, form, registry)


def update_column(tpl: ImportTemplate, index: int, form: dict, registry: PropertyRegistry) -> ImportTemplate:
    """Returnera en ny mall där kolumn ``index`` har fått roll/mål/presentation enligt formuläret."""
    new_tpl = tpl.model_copy(deep=True)
    col = new_tpl.columns[index]
    role = ColumnRole(form.get("role", col.role.value))
    if role not in roles_for(new_tpl):
        raise OpError(f"Rollen '{role.value}' finns inte för den här typen av projekt.")
    col.role = role
    col.property_code = col.designation = col.context = col.pattern = col.lifecycle = None
    definition = None

    if role == ColumnRole.PROPERTY:
        choice = form.get("property") or ""
        if choice == "__new__":
            code = (form.get("new_property_code") or "").strip() or to_property_code(col.source_column)
            ptype = form.get("new_property_type") or "string"
            if not _CODE_RE.match(code):
                raise OpError("Egenskapskoden får bara innehålla bokstäver, siffror, _ och - och måste börja med "
                              "en bokstav.")
            # Livscykel blir aldrig en egen egenskap – annars uppstår homonyma egenskaper i olika kodverk.
            homonym = lifecycle.lifecycle_target_for(code) or lifecycle.lifecycle_target_for(
                col.source_column, registry)
            if homonym:
                raise OpError(lifecycle.homonym_message(col.ui.label if col.ui else col.source_column, homonym))
            existing = registry.get(code) or next((p for p in new_tpl.properties if p.code == code), None)
            if existing:
                raise OpError(f"Egenskapen '{code}' finns redan – välj den i listan i stället för att skapa en ny.")
            if ptype not in PROPERTY_TYPES:
                raise OpError(f"Okänd typ '{ptype}'.")
            definition = PropertyDefinition(code=code, type=ptype, description=form.get("new_property_description")
                                            or col.source_column)
        else:
            definition = next((p for p in property_choices(new_tpl, registry) if p.code == choice), None)
            if definition is None:
                raise OpError("Välj en egenskap.")
        if all(p.code != definition.code for p in new_tpl.properties):
            new_tpl.properties.append(definition)
        col.property_code = definition.code

    elif role == ColumnRole.DESIGNATION:
        use = form.get("use") or ""
        target = DesignationTarget(language=(form.get("language") or "").strip() or new_tpl.codesystem.language)
        if use.startswith("reg:"):
            entry = registry.get(use[4:])
            if entry and entry.designation:
                target.use_system, target.use_code = entry.designation.use_system, entry.designation.use_code
                target.use_display = entry.designation.use_display
        elif use.startswith("gen:"):
            _, key, code = use.split(":", 2)
            gen = new_tpl.generated(key)
            concept = next((c for c in gen.concepts if c.code == code), None) if gen else None
            if concept is None:
                raise OpError("Okänd textroll.")
            target.use_system, target.use_code, target.use_display = gen.url, concept.code, concept.display
        col.designation = target

    elif role == ColumnRole.CONTEXT:
        key = form.get("context_group") or ""
        if key == "__new__" or not new_tpl.generated(key):
            label = (form.get("new_group") or "").strip()
            if not label:
                raise OpError("Ange namnet på kontextgruppen (t.ex. 'Används av').")
            key = to_property_code(label)
            if not new_tpl.generated(key):
                prefix = new_tpl.codesystem.name
                base = new_tpl.codesystem.url.rstrip("/") if len(new_tpl.source.sheets) > 1 \
                    else new_tpl.codesystem.url.rsplit("/", 1)[0]
                name = f"{prefix}_{to_constant(label)}"
                new_tpl.generated_codesystems.append(GeneratedCodeSystem(
                    key=key, url=f"{base}/{name}", name=name, title=f"{label} ({prefix})",
                    purpose="usage-context", property_code=key, concepts=[]))
        gen = new_tpl.generated(key)
        concept = next((c for c in gen.concepts if c.source_column == col.source_column), None)
        if concept is None:
            code, n = to_constant(col.source_column), 2
            while code in {c.code for c in gen.concepts}:
                code, n = f"{to_constant(col.source_column, 44)}_{n}", n + 1
            concept = GeneratedConcept(code=code, display=(form.get("label") or col.source_column).strip(),
                                       source_column=col.source_column)
            gen.concepts.append(concept)
        col.context = ContextTarget(codesystem=key, code=concept.code,
                                    mode="property" if form.get("context_mode") == "property" else "designation")

    elif role == ColumnRole.LIFECYCLE:
        key = form.get("lifecycle") or ""
        if key not in lifecycle.FIELDS:
            raise OpError("Välj vilken förvaltningsuppgift kolumnen står för.")
        col.lifecycle = key

    elif role == ColumnRole.DERIVED:
        pattern = (form.get("pattern") or "").strip()
        if "{code}" not in pattern:
            raise OpError("Härledningsmönstret måste innehålla {code}, t.ex. {codesystem}.{code}.RBK")
        col.pattern = pattern

    elif role == ColumnRole.PARENT and all(p.code != "parent" for p in new_tpl.properties):
        new_tpl.properties.append(PropertyDefinition(code="parent", type="code", description="Parent",
                                                     uri="http://hl7.org/fhir/concept-properties#parent"))
        new_tpl.codesystem.hierarchy_meaning = new_tpl.codesystem.hierarchy_meaning or "is-a"

    ui = col.ui or UiHints(label=col.source_column, order=index)
    ui.label = (form.get("label") or ui.label).strip()
    if form.get("order") not in (None, ""):
        try:
            ui.order = int(form["order"])
        except ValueError:
            raise OpError("Ordningen måste vara ett heltal.") from None
    ui.visible = bool(form.get("visible")) and role != ColumnRole.IGNORE
    ui.editable = role not in (ColumnRole.DERIVED,) and (bool(form.get("editable")) or role == ColumnRole.CODE)
    requested = form.get("widget") or ""
    ui.widget = requested if requested and requested != "auto" else _default_widget(role, definition, col.lifecycle)
    if role == ColumnRole.CONTEXT:
        ui.widget, ui.group = "checkbox", new_tpl.generated(col.context.codesystem).title.split(" (")[0]
    else:
        ui.group = None
    options = [o.strip() for o in (form.get("options") or "").split(",") if o.strip()]
    if not options and role == ColumnRole.LIFECYCLE and ui.widget == "select":
        options = lifecycle_widget(col.lifecycle)[1] or []
    ui.options = options or None
    if ui.widget == "select" and not ui.options:
        ui.widget = "text"
    col.ui = ui
    col.rationale = "Ändrad i administrationsvyn"
    col.confidence = 1.0

    _prune(new_tpl)
    problems = new_tpl.validate_template()
    if problems:
        raise OpError(problems)
    return new_tpl


def add_column(tpl: ImportTemplate, data: dict[str, SheetData], form: dict,
               registry: PropertyRegistry) -> ImportTemplate:
    label = (form.get("label") or "").strip()
    if not label:
        raise OpError("Ange kolumnens namn.")
    if any(c.source_column == label for c in tpl.columns):
        raise OpError(f"Kolumnen '{label}' finns redan.")
    staged = tpl.model_copy(deep=True)
    staged.columns.append(ColumnMapping(source_column=label, role=ColumnRole.IGNORE,
                                        ui=UiHints(label=label, order=len(staged.columns))))
    form = {"visible": "on", "editable": "on", **form}
    new_tpl = update_column(staged, len(staged.columns) - 1, form, registry)
    for d in data.values():
        if label not in d.headers:
            d.headers.append(label)
    return new_tpl


def _prune(tpl: ImportTemplate) -> None:
    """Ta bort egenskaper och genererade kodverk som ingen kolumn längre använder."""
    used = {c.property_code for c in tpl.columns if c.role == ColumnRole.PROPERTY}
    if any(c.role == ColumnRole.PARENT for c in tpl.columns):
        used.add("parent")
    tpl.properties = [p for p in tpl.properties if p.code in used]
    keep = []
    for g in tpl.generated_codesystems:
        if g.purpose == "usage-context":
            refs = {c.context.code for c in tpl.columns if c.role == ColumnRole.CONTEXT and c.context
                    and c.context.codesystem == g.key}
            g.concepts = [c for c in g.concepts if c.code in refs]
            if g.concepts:
                keep.append(g)
        elif any(c.designation and c.designation.use_system == g.url for c in tpl.columns):
            keep.append(g)
    tpl.generated_codesystems = keep


# -- begrepp: avveckling och borttag ----------------------------------------------------


def released_codes(ws: Workspace, pid: str, sheet: str) -> set[str]:
    rel = ws.latest_release(pid)
    return set(rel.codes.get(sheet, [])) if rel else set()


# -- livscykel: avveckling och ersättning ------------------------------------------------

REPLACED_LABEL, STATUS_LABEL, RETIRED_LABEL = "Ersätts av", "Status", "Avvecklad"
STATUS_VALUES = lifecycle.STATUS_VALUES
RELATION_KEY, COMMENT_KEY = "_ersattningsrelation", "_ersattningskommentar"


def lifecycle_columns(tpl: ImportTemplate, data: dict[str, SheetData]
                      ) -> tuple[ImportTemplate, ColumnMapping, ColumnMapping]:
    """Säkerställ livscykelkolumnerna för avveckling: status, borttagsdatum och ersättare.
    Befintliga kolumner med samma mål återanvänds (t.ex. 'Giltig t.o.m.' från källan).
    ``inactive`` behövs inte som egen kolumn – den härleds ur status (se lifecycle.py)."""
    new_tpl = tpl.model_copy(deep=True)

    def ensure(key: str, label: str) -> ColumnMapping:
        col = next((c for c in new_tpl.columns if c.role == ColumnRole.LIFECYCLE and c.lifecycle == key), None)
        if col is None:
            widget, options = lifecycle_widget(key)
            col = ColumnMapping(source_column=label, role=ColumnRole.LIFECYCLE, lifecycle=key,
                                rationale="Tillagd för avveckling av begrepp",
                                ui=UiHints(label=label, order=len(new_tpl.columns), widget=widget, options=options))
            new_tpl.columns.append(col)
            for d in data.values():
                if label not in d.headers:
                    d.headers.append(label)
        return col

    cols = lifecycle.lifecycle_columns(new_tpl.columns)
    status = ensure("status", STATUS_LABEL) if "status" in cols or not ({"inactive", "active"} & set(cols)) \
        else next(c for c in new_tpl.columns if c.role == ColumnRole.LIFECYCLE and c.lifecycle in ("inactive", "active"))
    ensure("retirementDate", RETIRED_LABEL)
    replaced = ensure("replacedBy", REPLACED_LABEL)
    return new_tpl, status, replaced


def is_retired(tpl: ImportTemplate, row: dict) -> bool:
    cols = lifecycle.lifecycle_columns(tpl.columns)
    return bool(cols) and lifecycle.row_state(cols, row).retired


def retire(tpl: ImportTemplate, data: dict[str, SheetData], sheet: str, row: dict, replaced_by: str | None = None,
           relation: str = "equivalent", comment: str = "") -> ImportTemplate:
    """Markera begreppet som avvecklat, ev. med en ersättande kod i samma kodverk."""
    code = str(row.get(code_column(tpl)))
    replaced_by = (replaced_by or "").strip() or None
    if replaced_by:
        if replaced_by == code:
            raise OpError("Ett begrepp kan inte ersätta sig självt.")
        target = find_row(data[sheet], tpl, replaced_by)
        if target is None:
            raise OpError(f"Ersättaren '{replaced_by}' finns inte i {sheet}.")
        if relation not in RELATIONS:
            raise OpError(f"Okänd relation '{relation}'.")
    new_tpl, status, replaced = lifecycle_columns(tpl, data)
    if replaced_by and is_retired(new_tpl, target):
        raise OpError(f"Ersättaren '{replaced_by}' är själv avvecklad – välj ett aktivt begrepp.")
    from datetime import date
    if status.lifecycle == "status":
        row[status.source_column] = "retired"
    elif status.lifecycle == "inactive":
        row[status.source_column] = True
    else:
        row[status.source_column] = False
    for col in new_tpl.columns:
        if col.role == ColumnRole.LIFECYCLE and col.lifecycle == "retirementDate":
            row[col.source_column] = row.get(col.source_column) or date.today().isoformat()
    row[replaced.source_column] = replaced_by
    row[RELATION_KEY] = relation if replaced_by else None
    row[COMMENT_KEY] = comment.strip() or None
    return new_tpl


def can_delete(tpl: ImportTemplate, released: set[str], code: str) -> tuple[bool, str]:
    if code in released and not tpl.policy.allow_code_removal:
        return False, ("Koden finns i en släppt release och kodverkets regler tillåter inte att koder tas "
                       f"bort ({tpl.policy.rationale or 'policy'}). Avveckla den i stället.")
    return True, ""


def replace_code(tpl: ImportTemplate, data: SheetData, row: dict, new_code: str, display: str | None = None) -> None:
    """Byt kod i ett urval (t.ex. inaktiv NPU-kod mot dess ersättare)."""
    code_col = code_column(tpl)
    new_code = new_code.strip()
    if not new_code:
        raise OpError("Ange den nya koden.")
    group = tpl.column(ColumnRole.GROUP)
    for r in data.rows:
        if r is not row and str(r.get(code_col)) == new_code and (
                not group or r.get(group.source_column) == row.get(group.source_column)):
            raise OpError(f"'{new_code}' finns redan i urvalet" + (" i samma grupp." if group else "."))
    row[code_col] = new_code
    display_col = tpl.column(ColumnRole.DISPLAY)
    if display and display_col and not row.get(display_col.source_column):
        row[display_col.source_column] = display


# -- bygg -------------------------------------------------------------------------------


def build(project: Project, data: dict[str, SheetData], version: str | None = None,
          fhir_version: str | None = None):
    """Bygg FHIR-resurser från arbetskopian, inklusive ConceptMap för ersatta koder."""
    from ..config import fhir_version as env_fhir_version
    from ..importer.transform import _fhir_name
    fv = fhir_version or env_fhir_version()
    tpl = project.template.model_copy(deep=True)
    if version:
        tpl.codesystem.version = version
    sheets = [Sheet(name=n, header_row=1, headers=d.headers, rows=copy.deepcopy(d.rows)) for n, d in data.items()]
    resources, warnings = build_all(tpl, sheets)
    if tpl.kind != "codesystem":
        return resources, warnings
    replaced = next((c for c in tpl.columns if c.role == ColumnRole.LIFECYCLE and c.lifecycle == "replacedBy"),
                    None)
    if replaced is None:
        return resources, warnings
    code_col = code_column(tpl)
    by_name = {r.get("name"): r for r in resources if r["resourceType"] == "CodeSystem"}
    items: dict[str, list[dict]] = {}
    for src in tpl.source.sheets or []:
        cs = by_name.get(_fhir_name(src.name))
        if cs is None or src.sheet not in data:
            continue
        for row in data[src.sheet].rows:
            if row.get(replaced.source_column):
                items.setdefault(cs["url"], []).append({
                    "code": str(row.get(code_col)), "target": str(row[replaced.source_column]),
                    "relation": row.get(RELATION_KEY) or "equivalent", "comment": row.get(COMMENT_KEY)})
    if items:
        name = f"{tpl.codesystem.name}_ERSATTNINGAR"
        cm = replacement_conceptmap([r for r in resources if r["resourceType"] == "CodeSystem"], items,
                                    url=f"{fhir_base(tpl.codesystem.url)}/ConceptMap/{name}", name=name,
                                    version=tpl.codesystem.version, publisher=tpl.codesystem.publisher,
                                    fhir_version=fv)
        if cm:
            resources.append(cm)
    return resources, warnings


def fhir_base(url: str) -> str:
    """'http://x/fhir/CodeSystem/KV' eller 'http://x/fhir/CodeSystem' -> 'http://x/fhir'."""
    for segment in ("/CodeSystem", "/ValueSet", "/ConceptMap"):
        if segment in url:
            return url.split(segment)[0]
    return url.rstrip("/").rsplit("/", 1)[0]


def resource_codes(resource: dict) -> list[str]:
    if resource.get("resourceType") == "ValueSet":
        return [f"{inc['system']}|{c['code']}" for inc in resource.get("compose", {}).get("include", [])
                for c in inc.get("concept", [])]
    if resource.get("resourceType") == "ConceptMap":
        return [f"{e['code']}→{t.get('code', '∅')} ({t.get('equivalence') or t.get('relationship')})"
                for g in resource.get("group", []) for e in g.get("element", []) for t in e.get("target", [])] + \
               [f"{e['code']}→∅ (noMap)" for g in resource.get("group", []) for e in g.get("element", [])
                if e.get("noMap")]
    out = []

    def walk(concepts):
        for c in concepts:
            out.append(c["code"])
            walk(c.get("concept", []))
    walk(resource.get("concept", []))
    return out


def resource_diff(old: dict, new: dict) -> dict:
    if new.get("resourceType") in ("ValueSet", "ConceptMap"):
        def as_cs(r):
            return {"version": r.get("version"), "concept": [{"code": c} for c in resource_codes(r)]}
        d = diff(as_cs(old), as_cs(new))
    else:
        d = diff(old, new)
    d.update(url=new["url"], name=new.get("name"), resourceType=new["resourceType"])
    return d


def suggest_version(ws: Workspace, project: Project) -> str:
    latest = ws.latest_release(project.id)
    if latest:
        return bump(latest.version, "minor")
    v = project.template.codesystem.version
    return v if re.fullmatch(r"\d+\.\d+\.\d+", v) else "1.0.0"


# -- namnrymd och identifierare -------------------------------------------------------------


def namespace_errors(project: Project, resources: list[dict], settings: Settings) -> list[str]:
    ns = settings.namespace(project.template.namespace)
    if ns is None:
        return [] if settings.allow_custom_namespace else ["Projektet saknar namnrymd."]
    errors = []
    pattern = ns.pattern()
    for r in resources:
        if not pattern.match(r.get("url", "")):
            errors.append(f"{r['resourceType']} {r.get('name')}: URL:en {r.get('url')} följer inte namnrymden "
                          f"'{ns.label}' ({pattern.pattern}).")
    if ns.require_oid or ns.oid_root:
        for src in project.template.source.sheets:
            oid = src.identifier or ""
            if ns.require_oid and not oid:
                errors.append(f"{src.name}: OID saknas (krävs i namnrymden '{ns.label}').")
            elif oid and ns.oid_root and not oid.removeprefix("urn:oid:").startswith(ns.oid_root + "."):
                errors.append(f"{src.name}: OID {oid} ligger inte under roten {ns.oid_root}.")
    return errors


def assign_oids(ws: Workspace, project: Project, settings: Settings) -> list[str]:
    """Föreslå nästa lediga OID under namnrymdens rot för kodverk som saknar OID."""
    ns = settings.namespace(project.template.namespace)
    if ns is None or not ns.oid_root:
        raise OpError("Namnrymden har ingen OID-rot.")
    used = set()
    for p in ws.projects():
        for identifier in [src.identifier for src in p.template.source.sheets] + [p.template.codesystem.identifier]:
            oid = (identifier or "").removeprefix("urn:oid:")
            if oid.startswith(ns.oid_root + "."):
                arc = oid[len(ns.oid_root) + 1:].split(".")[0]  # även underliggande led spärrar ledet
                if arc.isdigit():
                    used.add(int(arc))
    assigned, n = [], 1
    single = len(project.template.source.sheets) <= 1
    for src in project.template.source.sheets:
        if src.identifier or (single and project.template.codesystem.identifier):
            continue
        while n in used:
            n += 1
        src.identifier = f"urn:oid:{ns.oid_root}.{n}"
        used.add(n)
        assigned.append(f"{src.name} = {src.identifier}")
    return assigned


# -- releaser -------------------------------------------------------------------------------


def create_release_candidate(ws: Workspace, project: Project, data: dict[str, SheetData], base_version: str,
                             author: str, notes: str = "", settings: Settings | None = None) -> Release:
    from ..config import fhir_version
    settings = settings or Settings()
    if not re.fullmatch(r"\d+\.\d+\.\d+", base_version):
        raise OpError("Versionen ska ha formen X.Y.Z, t.ex. 1.2.0.")
    existing = ws.releases(project.id)
    if any(r.status == "release" and r.version == base_version for r in existing):
        raise OpError(f"Version {base_version} är redan släppt – välj en högre version.")
    open_props = [p for p in ws.proposals(project.id) if p.status == "öppen"]
    if open_props and settings.editorial.block_rc_with_open_proposals:
        raise OpError(f"Det finns {len(open_props)} öppna förslag i den redaktionella kedjan. "
                      "Granska dem innan en releasekandidat skapas.")
    n = 1 + max((int(r.version.rsplit(".", 1)[1]) for r in existing
                 if r.status == "rc" and r.version.startswith(f"{base_version}-rc.")), default=0)
    version = f"{base_version}-rc.{n}"
    resources, warnings = build(project, data, version)
    resources = label_resources(resources, settings.security_label(project.template.classification))
    previous = ws.latest_release(project.id)
    prev_by_url = {r["url"]: r for r in previous.resources} if previous else {}
    diffs, errors = [], []
    errors += namespace_errors(project, resources, settings)
    if project.template.kind == "conceptmap":  # i R4 krävs kommentar för smalare/inexakta mappningar
        code_col = code_column(project.template)
        for rows in data.values():
            for row in rows.rows:
                errors += [f"Mappning {row.get(code_col)}: {p}" for p in conceptmap.row_problems(project.template, row)]
    meta_errors, meta_warnings = metadata.release_errors(project.template, settings.release_required_metadata)
    errors += meta_errors
    warnings += meta_warnings
    issues = validate_resources(resources, fhir_version())
    errors += [f"Profilvalidering: {i}" for i in issues if i.severity == "error"]
    warnings += [f"Profilvalidering: {i}" for i in issues if i.severity == "warning"]
    for r in resources:
        old = prev_by_url.get(r["url"])
        if old:
            d = resource_diff(old, r)
            if r["resourceType"] == "CodeSystem":
                errors += [f"{r['name']}: {e}" for e in check_policy(d, project.template.policy)]
        else:
            d = {"url": r["url"], "name": r.get("name"), "resourceType": r["resourceType"], "new": True,
                 "added": resource_codes(r), "removed": [], "changed": []}
        diffs.append(d)
    gone = set(prev_by_url) - {r["url"] for r in resources}
    if gone and not project.template.policy.allow_code_removal:
        errors.append(f"Resurser saknas jämfört med senaste release: {', '.join(sorted(gone))}.")
    if errors:
        raise OpError(errors)
    code_col = code_column(project.template)
    codes = {name: [str(r.get(code_col)) for r in d.rows if r.get(code_col) is not None] for name, d in data.items()}
    rel = Release(version=version, status="rc", author=author, notes=notes, resources=resources, diffs=diffs,
                  warnings=warnings, based_on=previous.version if previous else None, codes=codes)
    ws.save_release(project.id, rel)
    ws.log(project.id, author, f"Releasekandidat {version} skapad",
           {"tillagda": sum(len(d.get("added", [])) for d in diffs),
            "borttagna": sum(len(d.get("removed", [])) for d in diffs),
            "ändrade": sum(len(d.get("changed", [])) for d in diffs)})
    return rel


def approve_release(ws: Workspace, project: Project, rc_version: str, author: str,
                    settings: Settings | None = None) -> Release:
    settings = settings or Settings()
    rc = ws.release(project.id, rc_version)
    if rc.status != "rc":
        raise OpError("Bara releasekandidater kan godkännas.")
    if settings.editorial.four_eyes_release:
        if author in ("", "okänd"):
            raise OpError("Fyra ögon kräver inloggning: godkännarens namn saknas (X-Remote-User).")
        if author == rc.author:
            raise OpError(f"Fyra ögon: kandidaten skapades av {rc.author} och måste godkännas av någon annan.")
    base = rc.version.split("-rc.")[0]
    if any(r.status == "release" and r.version == base for r in ws.releases(project.id)):
        raise OpError(f"Version {base} är redan släppt.")
    latest = ws.latest_release(project.id)
    if latest and rc.based_on != latest.version:
        raise OpError(f"Kandidaten bygger på {rc.based_on or 'ingen release'}, men senaste release är "
                      f"{latest.version}. Skapa en ny kandidat.")
    rel = Release(version=base, status="release", author=author, notes=rc.notes,
                  resources=[to_release(r) for r in rc.resources], diffs=rc.diffs, warnings=rc.warnings,
                  based_on=rc.version, codes=rc.codes)
    ws.save_release(project.id, rel)
    project.template.codesystem.version = base
    ws.save(project, author=author, action=f"Release {base} godkänd (från {rc.version}, skapad av {rc.author})")
    return rel


def provenance_bundle(ws: Workspace, project: Project, rel: Release) -> dict:
    """Den redaktionella kedjan som FHIR Provenance: förslag (manuella och AI), granskning, release."""
    previous = next((r for r in ws.releases(project.id) if r.status == "release" and r.created < rel.created
                     and r.version != rel.version), None)
    since = previous.created if previous else ""
    targets = [{"reference": f"{r['resourceType']}/{r.get('id')}", "display": f"{r['url']}|{r['version']}"}
               for r in rel.resources]
    entries = []
    for p in ws.proposals(project.id):
        if p.status != "godkänd" or (p.reviewed or "") <= since or (p.reviewed or "") > rel.created:
            continue
        agents = [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                        "code": "author"}]},
                   "who": {"display": (f"AI: {p.provenance.get('model')}" if p.origin == "ai" and p.provenance
                                       else p.author)}},
                  {"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                        "code": "verifier"}]}, "who": {"display": p.reviewer}}]
        if p.origin == "ai":
            agents.append({"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/"
                                                          "provenance-participant-type", "code": "enterer"}]},
                           "who": {"display": p.author}})
        entries.append({"resource": {
            "resourceType": "Provenance", "id": f"forslag-{p.id}", "target": targets[:1] or [{"display": "-"}],
            "recorded": p.reviewed, "agent": agents,
            "activity": {"text": f"{p.action} {p.sheet}/{p.code or 'nytt'}: {', '.join(p.changes)}"},
            "reason": [{"text": p.rationale}] if p.rationale else None,
            "entity": [{"role": "source", "what": {"display": json.dumps(p.provenance, ensure_ascii=False)}}]
            if p.provenance else None,
        }})
    entries.append({"resource": {
        "resourceType": "Provenance", "id": f"release-{rel.version}", "target": targets, "recorded": rel.created,
        "agent": [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                        "code": "author"}]}, "who": {"display": ws_release_author(ws, project, rel)}},
                  {"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/provenance-participant-type",
                                        "code": "attester"}]}, "who": {"display": rel.author}}],
        "activity": {"text": f"Release {rel.version} (kandidat {rel.based_on})"},
    }})
    for e in entries:
        e["resource"] = {k: v for k, v in e["resource"].items() if v is not None}
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def ws_release_author(ws: Workspace, project: Project, rel: Release) -> str:
    """Den som skapade kandidaten som en release bygger på."""
    if rel.status == "rc" or not rel.based_on:
        return rel.author
    try:
        return ws.release(project.id, rel.based_on).author
    except KeyError:
        return rel.author


def release_package(ws: Workspace, project: Project, version: str, settings: Settings) -> tuple[str, bytes]:
    """Bygg FHIR-paket (.tgz) för en kandidat eller release. Returnerar (filnamn, data)."""
    from ..config import fhir_version
    rel = ws.release(project.id, version)
    tpl = project.template
    ns = settings.namespace(tpl.namespace) or Namespace(key="egen", label="Egen", base_url=tpl.codesystem.url)
    cls = settings.classification(tpl.classification)
    name = ns.package_name(project.id)
    notes = f"# {project.title} {rel.version}\n\n{rel.notes or ''}\n\n## Ändringar\n\n" + "\n".join(
        f"- {d['resourceType']} {d.get('name')}: +{len(d.get('added', []))} "
        f"−{len(d.get('removed', []))} ~{len(d.get('changed', []))}" for d in rel.diffs) + "\n"
    manifest = package_manifest(
        name, rel.version, fhir_version(), title=project.title, description=tpl.codesystem.description or "",
        author=tpl.codesystem.publisher or ns.publisher,
        classification={"key": cls.key, "label": cls.label, "confidentiality": cls.confidentiality},
        license_note=tpl.license_note, canonical=ns.base_url)
    resources = label_resources(rel.resources, settings.security_label(tpl.classification))
    data = build_package(resources, manifest, release_notes=notes,
                         provenance=provenance_bundle(ws, project, rel), license_note=tpl.license_note)
    return f"{name}-{rel.version}.tgz", data


def check_target(project: Project, target: Target, settings: Settings) -> None:
    if not settings.allowed(project.template.classification, target):
        cls = settings.classification(project.template.classification)
        raise OpError(f"Projektet är klassat '{cls.label}', men målet '{target.label}' tar högst emot "
                      f"'{settings.classification(target.max_classification).label}'.")


async def publish_release(ws: Workspace, project: Project, version: str, target_key: str, author: str,
                          settings: Settings, client=None) -> Release:
    from pathlib import Path

    from .store import now
    target = settings.target(target_key)
    if target is None:
        raise OpError(f"Okänt mål '{target_key}'.")
    rel = ws.release(project.id, version)
    if rel.status != "release":
        raise OpError("Bara godkända releaser kan publiceras.")
    check_target(project, target, settings)
    log = []
    if target.kind == "package-dir":
        filename, data = release_package(ws, project, version, settings)
        folder = Path(target.directory or "")
        if not target.directory or not folder.is_dir():
            raise OpError(f"Katalogen för '{target.label}' finns inte: {target.directory}")
        (folder / filename).write_bytes(data)
        log.append(f"OK paket {filename} -> {folder}")
    else:
        resources = label_resources(rel.resources, settings.security_label(project.template.classification))
        for r in resources:
            try:
                stored = await client.put_resource(r)
                log.append(f"OK {r['resourceType']}/{r.get('id')} {r['url']}|{r['version']}")
                # Ersätter releasen ett förslag (samma id) behåller vissa servrar (t.ex. HAPI) förslagets tagg.
                if any(t.get("system") == PROPOSAL_TAG["system"] for t in (stored or {}).get("meta", {}).get("tag", [])):
                    await client.delete_tag(r["resourceType"], r["id"], PROPOSAL_TAG)
                    log.append(f"OK förslagstaggen borttagen från {r['resourceType']}/{r.get('id')}")
            except Exception as exc:  # visas för användaren
                log.append(f"FEL {r['resourceType']}/{r.get('id')}: {exc}")
    rel.publish_log = rel.publish_log + [f"{now()} {target.label}:"] + log
    if all(line.startswith("OK") for line in log):
        rel.published = now()
    ws.save_release(project.id, rel)
    ws.log(project.id, author, f"Release {version} publicerad till {target.label}", {"resultat": log})
    return rel


# -- urval: koder som behöver hanteras --------------------------------------------------------


def _local_codesystems(ws: Workspace) -> dict[str, dict[str, dict]]:
    """Kodverk som förvaltas i arbetsytan: url -> kod -> {inactive, replacedBy, display}."""
    out: dict[str, dict[str, dict]] = {}
    for p in ws.projects():
        if p.template.kind != "codesystem":
            continue
        rel = ws.latest_release(p.id)
        for r in (rel.resources if rel else []):
            if r["resourceType"] != "CodeSystem":
                continue
            concepts = {}
            for c in r.get("concept", []):
                props = {x["code"]: next((v for k, v in x.items() if k.startswith("value")), None)
                         for x in c.get("property", [])}
                replaced = next((e.get("valueCoding", {}).get("code") for e in c.get("extension", [])
                                 if e.get("url") == REPLACED_BY_EXT), None) or props.get(REPLACED_BY)
                concepts[c["code"]] = {"display": c.get("display"), "inactive": props.get("inactive") is True,
                                       "replacedBy": replaced}
            out[r["url"]] = concepts
    return out


# -- mappningar (ConceptMap-projekt) ----------------------------------------------------------------

# Taggen som MCP-serverns förslag bär (en konvention mellan komponenterna, ingen delad kod).
PROPOSAL_TAG = {"system": "urn:term-assistant:arbetsflode", "code": "forslag"}


def create_mapping_project(ws: Workspace, title: str, meta, spec, rows: list[dict], author: str,
                           namespace: Namespace | None = None, classification: str | None = None,
                           questions: list[str] | None = None, source: str | None = None) -> Project:
    tpl = conceptmap.template(meta, spec)
    if namespace:
        tpl.namespace = namespace.key
        tpl.codesystem.publisher = tpl.codesystem.publisher or namespace.publisher
    tpl.classification = classification
    data = {conceptmap.SHEET: SheetData(headers=[c.source_column for c in tpl.columns], rows=rows)}
    tpl.source.file = source
    project = Project(id=ws.new_id(title), title=title, template=tpl, source_file=source,
                      questions=[Question(text=q) for q in questions or []])
    ws.save(project, data, author=author, action="Mappningsprojekt skapat",
            detail={"källa": spec.source_system, "mål": spec.target_system, "rader": len(rows),
                    "ursprung": spec.origin})
    return project


def source_rows(tpl: ImportTemplate, codes: list[dict]) -> list[dict]:
    """En tom mappningsrad per källkod ({code, display}), att fylla i med målkod och relation."""
    cols = {r: tpl.column(r).source_column for r in (ColumnRole.CODE, ColumnRole.MAP_SOURCE,
                                                       ColumnRole.MAP_SOURCE_DISPLAY)}
    return [{cols[ColumnRole.CODE]: f"M{i}", cols[ColumnRole.MAP_SOURCE]: c["code"],
             cols[ColumnRole.MAP_SOURCE_DISPLAY]: c.get("display")} for i, c in enumerate(codes, start=1)]


async def check_mapping(project: Project, data: dict[str, SheetData], client) -> dict:
    """Kontrollera käll- och målkoder mot terminologitjänsten; flagga okända och inaktiva koder och
    rapportera källkoder i källvärdemängden som saknar mappning."""
    import asyncio
    tpl, spec = project.template, project.template.mapping
    if tpl.kind != "conceptmap":
        raise OpError("Kontrollen gäller mappningsprojekt.")
    sheet = next(iter(data))
    col = {r: tpl.column(r).source_column for r in (ColumnRole.CODE, ColumnRole.MAP_SOURCE, ColumnRole.MAP_TARGET,
                                                      ColumnRole.MAP_SOURCE_DISPLAY, ColumnRole.MAP_TARGET_DISPLAY)}
    sem = asyncio.Semaphore(8)

    async def check(system: str, code: str):
        async with sem:
            try:
                return await client.validate_code(code, system=system)
            except Exception as exc:  # noqa: BLE001 – rapporteras som fel på raden
                return {"result": False, "message": str(exc)}

    pairs = {(spec.source_system, str(r[col[ColumnRole.MAP_SOURCE]])) for r in data[sheet].rows
             if r.get(col[ColumnRole.MAP_SOURCE])}
    pairs |= {(spec.target_system, str(r[col[ColumnRole.MAP_TARGET]])) for r in data[sheet].rows
              if r.get(col[ColumnRole.MAP_TARGET])}
    results = dict(zip(pairs, await asyncio.gather(*(check(s, c) for s, c in pairs))))
    flags: dict[str, Flag] = {}
    for r in data[sheet].rows:
        notes, display = [], None
        for side, system, code_col, display_col in (("källkoden", spec.source_system, col[ColumnRole.MAP_SOURCE],
                                                     col[ColumnRole.MAP_SOURCE_DISPLAY]),
                                                    ("målkoden", spec.target_system, col[ColumnRole.MAP_TARGET],
                                                     col[ColumnRole.MAP_TARGET_DISPLAY])):
            code = r.get(code_col)
            if not code:
                continue
            res = results[(system, str(code))]
            if not res.get("result"):
                notes.append(f"{side} {code} finns inte i {system}")
            elif res.get("inactive"):
                notes.append(f"{side} {code} är inaktiv")
            elif res.get("display") and r.get(display_col) and res["display"] != r.get(display_col):
                display = res["display"]
                notes.append(f"{side}s text skiljer sig från tjänstens ('{res['display']}')")
        if notes:
            status = "okänd" if any("finns inte" in n for n in notes) else (
                "inaktiv" if any("inaktiv" in n for n in notes) else "text")
            flags[str(r.get(col[ColumnRole.CODE]))] = Flag(status=status, message="; ".join(notes),
                                                           server_display=display)
    unmapped = []
    if spec.source_valueset:
        try:
            expansion = await client.expand(url=spec.source_valueset, count=1000)
            present = {str(r.get(col[ColumnRole.MAP_SOURCE])) for r in data[sheet].rows}
            unmapped = [c["code"] for c in expansion["contains"] if c["code"] not in present]
        except Exception:  # noqa: BLE001 – täckningen är en bonus
            pass
    project.flags = {sheet: flags} if flags else {}
    return {"flags": len(flags), "unmapped": unmapped, "checked": len(pairs)}


# -- översättningar (CodeSystem-supplement) --------------------------------------------------------


async def codesystem_codes(client, url: str, limit: int = 5000) -> list[dict]:
    """Alla koder ({code, display}) i ett kodsystem på terminologitjänsten, via implicit ValueSet."""
    implicit = {"resourceType": "ValueSet", "compose": {"include": [{"system": url}]}}
    result = await client.expand(valueset=implicit, count=limit)
    return [{"code": c["code"], "display": c.get("display")} for c in result["contains"]]


def create_translation_project(ws: Workspace, title: str, meta, spec, rows: list[dict], author: str,
                               namespace: Namespace | None = None, classification: str | None = None,
                               questions: list[str] | None = None, source: str | None = None) -> Project:
    tpl = supplement.template(meta, spec)
    if namespace:
        tpl.namespace = namespace.key
        tpl.codesystem.publisher = tpl.codesystem.publisher or namespace.publisher
    tpl.classification = classification
    tpl.source.file = source
    data = {supplement.SHEET: SheetData(headers=[c.source_column for c in tpl.columns], rows=rows)}
    project = Project(id=ws.new_id(title), title=title, template=tpl, source_file=source,
                      questions=[Question(text=q) for q in questions or []])
    ws.save(project, data, author=author, action="Översättningsprojekt skapat",
            detail={"kodsystem": spec.codesystem, "språk": meta.language, "rader": len(rows), "ursprung": source})
    return project


async def check_translation(project: Project, data: dict[str, SheetData], client) -> dict:
    """Jämför raderna med kodsystemet: koder som inte (längre) finns där, ändrad originaltext och nya koder."""
    tpl = project.template
    sheet = next(iter(data))
    code_col = tpl.column(ColumnRole.CODE).source_column
    orig_col = tpl.column(ColumnRole.ORIGINAL_DISPLAY).source_column
    text_col = tpl.column(ColumnRole.TRANSLATION).source_column
    base = {c["code"]: c.get("display") for c in await codesystem_codes(client, tpl.translation.codesystem)}
    flags: dict[str, Flag] = {}
    for row in data[sheet].rows:
        code = str(row.get(code_col))
        if code not in base:
            flags[code] = Flag(status="okänd", message=f"Koden finns inte i {tpl.translation.codesystem}")
        elif base[code] and row.get(orig_col) and base[code] != row.get(orig_col):
            flags[code] = Flag(status="text", server_display=base[code],
                               message="Kodsystemets text har ändrats sedan översättningen – kontrollera den")
    present = {str(r.get(code_col)) for r in data[sheet].rows}
    new = [c for c in base if c not in present]
    untranslated = sum(1 for r in data[sheet].rows if not str(r.get(text_col) or "").strip())
    project.flags = {sheet: flags} if flags else {}
    return {"flags": len(flags), "new_codes": new, "untranslated": untranslated, "codes": len(base)}


async def add_missing_codes(project: Project, data: dict[str, SheetData], client) -> list[str]:
    """Lägg till rader för koder som finns i kodsystemet men inte i projektet (t.ex. nya koder)."""
    tpl = project.template
    sheet = next(iter(data))
    code_col = tpl.column(ColumnRole.CODE).source_column
    present = {str(r.get(code_col)) for r in data[sheet].rows}
    new = [c for c in await codesystem_codes(client, tpl.translation.codesystem) if c["code"] not in present]
    data[sheet].rows.extend(supplement.rows(tpl, new))
    return [c["code"] for c in new]


FHIR_FILE_LIMIT = 20 * 1024 * 1024        # största fil (och största resurs i ett paket)
FHIR_PACKAGE_TOTAL = 100 * 1024 * 1024    # största uppackade innehåll i ett paket
FHIR_PACKAGE_FILES = 2000
TAKE_IN_TYPES = ("ValueSet", "ConceptMap", "CodeSystem")


def resources_from_fhir_file(content: bytes) -> list[dict]:
    """Resurserna i en FHIR-Bundle, en enskild resurs (JSON) eller ett FHIR-paket (.tgz, NPM-format).

    Paketet läses i minnet – inget packas upp på disk – med gränser för storlek och antal filer."""
    import io
    import tarfile
    if len(content) > FHIR_FILE_LIMIT:
        raise OpError(f"Filen är större än {FHIR_FILE_LIMIT // (1024 * 1024)} MB.")
    if content[:2] == b"\x1f\x8b":
        resources, total = [], 0
        try:
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
                for n, member in enumerate(tar):
                    if n > FHIR_PACKAGE_FILES:
                        raise OpError(f"Paketet har fler än {FHIR_PACKAGE_FILES} filer.")
                    name = member.name.rsplit("/", 1)[-1]
                    if not member.isfile() or not name.endswith(".json") or name in ("package.json", ".index.json") \
                            or name.startswith("."):
                        continue
                    total += member.size
                    if member.size > FHIR_FILE_LIMIT or total > FHIR_PACKAGE_TOTAL:
                        raise OpError("Paketet är för stort.")
                    try:
                        data = json.loads(tar.extractfile(member).read())
                    except ValueError:
                        continue
                    if isinstance(data, dict) and data.get("resourceType"):
                        resources.append(data)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise OpError(f"Filen är inget läsbart FHIR-paket (.tgz): {exc}") from exc
    else:
        try:
            data = json.loads(content.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OpError("Filen är varken JSON (FHIR-Bundle eller resurs) eller ett FHIR-paket (.tgz).") from exc
        if not isinstance(data, dict) or not data.get("resourceType"):
            raise OpError("Filen innehåller ingen FHIR-resurs.")
        resources = [e.get("resource") for e in data.get("entry", []) if isinstance(e, dict)] \
            if data["resourceType"] == "Bundle" else [data]
        resources = [r for r in resources if isinstance(r, dict) and r.get("resourceType")]
    if not resources:
        raise OpError("Filen innehåller inga FHIR-resurser.")
    return resources


def can_take_in(resource: dict) -> str | None:
    """None om resursen kan tas in som projekt, annars varför inte."""
    if resource.get("resourceType") not in TAKE_IN_TYPES:
        return f"{resource.get('resourceType')} kan inte tas in som projekt."
    if not resource.get("url") or not (resource.get("id") or resource.get("name")):
        return "Resursen saknar canonical URL eller id."
    if resource["resourceType"] == "CodeSystem" and resource.get("content") != "supplement":
        return "Kodsystem tas in via Ny inläsning (Excel/CSV); här tas bara översättningar (supplement) in."
    return None


def project_from_resource(ws: Workspace, resource: dict, title: str, author: str,
                          namespace: Namespace | None = None, classification: str | None = None,
                          base_codes: list[dict] | None = None) -> Project:
    """Ta in ett förslag (eller annan resurs) från terminologitjänsten som projekt att granska och förvalta."""
    from ..importer.external import resolve_system_name
    from ..importer.mapping import CodeSystemMeta, SourceInfo
    meta = CodeSystemMeta(url=resource["url"], name=resource.get("name") or resource["id"],
                          title=resource.get("title"), version=re.sub(r"[^0-9.]", "", resource.get("version") or "")
                          or "0.1.0", description=resource.get("description"), purpose=resource.get("purpose"),
                          sources={"url": "förslag på terminologitjänsten"})
    if not re.fullmatch(r"\d+\.\d+\.\d+", meta.version):
        meta.version = "0.1.0"
    origin = f"{resource['resourceType']}/{resource.get('id')} ({resource['url']}|{resource.get('version', '-')})"
    if resource["resourceType"] == "ConceptMap":
        spec = conceptmap.spec_from_conceptmap(resource)
        spec.origin = origin
        tpl = conceptmap.template(meta, spec)
        rows, notes = conceptmap.rows_from_conceptmap(resource, tpl)
        project = create_mapping_project(ws, title, meta, spec, rows, author, namespace, classification,
                                         questions=notes + ["Taget in från ett förslag (AI-stött) – granska varje "
                                                            "mappning innan release."], source=origin)
        return project
    if resource["resourceType"] == "CodeSystem":
        if resource.get("content") != "supplement" or not resource.get("supplements"):
            raise OpError("Bara översättningar (CodeSystem-supplement) kan tas in härifrån.")
        from ..importer.mapping import TranslationSpec
        url, _, version = resource["supplements"].partition("|")
        meta.language = resource.get("language") or meta.language
        spec = TranslationSpec(codesystem=url, codesystem_version=version or None, origin=origin)
        tpl = supplement.template(meta, spec)
        translations = supplement.translations_from_supplement(resource)
        codes = base_codes if base_codes else [{"code": c} for c in translations]
        unknown = sorted(set(translations) - {c["code"] for c in codes})
        notes = ["Taget in från ett förslag (AI-stött) – granska varje översättning innan release."]
        if unknown:
            notes.append(f"{len(unknown)} översatta koder finns inte i kodsystemet: {', '.join(unknown[:10])}")
            codes = codes + [{"code": c} for c in unknown]
        return create_translation_project(ws, title, meta, spec, supplement.rows(tpl, codes, translations), author,
                                          namespace, classification, questions=notes, source=origin)
    if resource["resourceType"] != "ValueSet":
        raise OpError(f"{resource['resourceType']} kan inte tas in som projekt ännu.")
    includes = [i for i in resource.get("compose", {}).get("include", []) if i.get("system")]
    notes = [f"Taget in från förslaget {origin} (AI-stött) – granska urvalet innan release."]
    filtered = [i for i in includes if i.get("filter") or not i.get("concept")]
    if filtered:
        # Urval definierade med t.ex. ECL tas in som kodlista ur förslagets expansion (ögonblicksbild).
        expansion = resource.get("expansion") or {}
        contains = expansion.get("contains") or []
        if not contains or (expansion.get("total") is not None and expansion["total"] > len(contains)):
            raise OpError("Urvalet definieras med ett filter (t.ex. ECL) och förslaget saknar en fullständig "
                          "expansion. Bara urval med listade koder, eller med expansionen sparad, kan tas in.")
        listed = {(i["system"], c["code"]) for i in includes for c in i.get("concept", [])}
        by_system: dict[str, list[dict]] = {}
        for c in contains:
            if c.get("system") and (c["system"], c["code"]) not in listed:
                by_system.setdefault(c["system"], []).append({"code": c["code"], "display": c.get("display")})
        includes = [i for i in includes if i.get("concept") and not i.get("filter")] + \
            [{"system": system, "concept": concepts} for system, concepts in by_system.items()]
        rules = "; ".join(f"{f.get('property')} {f.get('op')} {f.get('value')}" for i in filtered
                          for f in i.get("filter", [])) or "hela kodsystemet"
        used = ", ".join(p.get("valueUri", "") for p in expansion.get("parameter", [])
                         if p.get("name") == "used-codesystem")
        notes.append(f"Förslaget definierar urvalet med {rules}. Det är taget in som kodlista ur expansionen "
                     f"({len(contains)} koder{', ' + used if used else ''}, {expansion.get('timestamp', 'okänd tid')}). "
                     "Kontrollera urvalet igen när en ny version av kodsystemet släpps.")
    systems = {i["system"]: resolve_system_name(i["system"])[0] for i in includes}
    columns = [ColumnMapping(source_column=h, role=role, rationale="Förslag från terminologitjänsten",
                             ui=UiHints(label=h, order=n))
               for n, (h, role) in enumerate((("Kod", ColumnRole.CODE), ("Benämning", ColumnRole.DISPLAY),
                                              ("Kodsystem", ColumnRole.SYSTEM)))]
    tpl = ImportTemplate(kind="valueset", codesystem=meta, source=SourceInfo(sheet="Urval"), columns=columns,
                         external_systems={key: system for system, key in systems.items()})
    rows = [{"Kod": c["code"], "Benämning": c.get("display"), "Kodsystem": i["system"]}
            for i in includes for c in i["concept"]]
    if namespace:
        tpl.namespace = namespace.key
        tpl.codesystem.publisher = tpl.codesystem.publisher or namespace.publisher
    tpl.classification = classification
    tpl.source.file = origin
    project = Project(id=ws.new_id(title), title=title, template=tpl, source_file=origin,
                      questions=[Question(text=n) for n in notes])
    ws.save(project, {"Urval": SheetData(headers=["Kod", "Benämning", "Kodsystem"], rows=rows)}, author=author,
            action="Urval taget in från förslag", detail={"ursprung": origin, "rader": len(rows)})
    return project


async def check_subset(ws: Workspace, project: Project, data: dict[str, SheetData], client=None) -> dict:
    """Flagga koder i ett urval som är inaktiva, okända eller ersatta – med förslag på ersättare.

    Källor: kodverk som förvaltas i arbetsytan (deras senaste release) och, om en
    klient anges, terminologitjänsten ($validate-code, $lookup och $translate).
    """
    from ..importer.subset import validate_valueset_codes
    if project.template.kind != "valueset":
        raise OpError("Kontrollen gäller urval (ValueSet-projekt).")
    tpl = project.template
    resources, _ = build(project, data)
    umbrella = next((r for r in resources if r["resourceType"] == "ValueSet"), None)
    local = _local_codesystems(ws)
    flags: dict[str, dict[str, Flag]] = {}
    sheet = next(iter(data))
    summary = {"lokala": 0, "server": None}
    server_error = None

    for inc in (umbrella or {}).get("compose", {}).get("include", []):
        known = local.get(inc["system"])
        if not known:
            continue
        for c in inc.get("concept", []):
            info = known.get(c["code"])
            if info is None:
                flags.setdefault(sheet, {})[c["code"]] = Flag(status="okänd", message=f"Finns inte i {inc['system']}")
            elif info["inactive"]:
                sug = [{"code": info["replacedBy"], "display": (known.get(info["replacedBy"]) or {}).get("display"),
                        "source": "ersätts av (lokalt kodverk)"}] if info["replacedBy"] else []
                flags.setdefault(sheet, {})[c["code"]] = Flag(status="inaktiv", message="Avvecklad i kodverket",
                                                             server_display=info["display"], suggestions=sug)
            summary["lokala"] += 1

    if client is not None and umbrella:
        remote = {**umbrella, "compose": {"include": [i for i in umbrella["compose"]["include"]
                                                      if i["system"] not in local]}}
        try:
            report = await validate_valueset_codes(client, remote)
            summary["server"] = report["summary"]
            for prob in report["problems"]:
                flag = Flag(status={"invalid": "okänd", "inactive": "inaktiv"}.get(prob["status"], prob["status"]),
                            message=prob.get("message") or "", server_display=prob.get("display"))
                flag.suggestions = await _server_suggestions(client, prob["system"], prob["code"])
                flags.setdefault(sheet, {})[prob["code"]] = flag
        except Exception as exc:  # nätverk m.m.: lokala resultat gäller ändå
            server_error = str(exc)
    project.flags = flags
    return {"flags": sum(len(v) for v in flags.values()), "summary": summary, "server_error": server_error}


async def _server_suggestions(client, system: str, code: str) -> list[dict]:
    out = []
    try:
        info = await client.lookup(system, code)
        for p in info.get("property", []):
            if isinstance(p, dict) and str(p.get("code", "")).lower() in (
                    "replacedby", "replaced_by", "moved_to", "same_as", "possibly_equivalent_to", "alternative"):
                value = p.get("value")
                value = value.get("code") if isinstance(value, dict) else value
                if value:
                    out.append({"code": str(value), "display": None, "source": f"$lookup {p.get('code')}"})
    except Exception:  # noqa: BLE001 – förslag är frivilliga
        pass
    try:
        result = await client.translate(code, system, target_system=system)
        matches = result.get("match", []) if result.get("result") else []
        for m in matches:
            concept = m.get("concept") if isinstance(m, dict) else None
            if isinstance(concept, dict) and concept.get("code") and concept.get("code") != code:
                out.append({"code": concept["code"], "display": concept.get("display"), "source": "$translate"})
    except Exception:  # noqa: BLE001
        pass
    seen, unique = set(), []
    for s in out:
        if s["code"] not in seen:
            seen.add(s["code"])
            unique.append(s)
    return unique
