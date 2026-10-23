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
from ..importer.registry import PropertyRegistry
from ..importer.release import bump, check_policy, diff, release as to_release
from ..importer.suggest import MappingProposal, to_constant, to_property_code
from ..importer.transform import _TRUE, coerce
from ..importer.workbook import Sheet
from .store import Project, Question, Release, SheetData, Workspace

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
    ColumnRole.IGNORE: "Ignoreras",
}
CS_ROLES = [ColumnRole.CODE, ColumnRole.DISPLAY, ColumnRole.DEFINITION, ColumnRole.DESIGNATION, ColumnRole.PROPERTY,
            ColumnRole.PARENT, ColumnRole.CONTEXT, ColumnRole.DERIVED, ColumnRole.IGNORE]
VS_ROLES = [ColumnRole.CODE, ColumnRole.DISPLAY, ColumnRole.GROUP, ColumnRole.IGNORE]
PROPERTY_TYPES = ["string", "code", "boolean", "integer", "decimal", "dateTime", "Coding"]
_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")


class OpError(ValueError):
    """Fel som visas för användaren."""

    def __init__(self, messages: list[str] | str):
        self.messages = [messages] if isinstance(messages, str) else messages
        super().__init__("; ".join(self.messages))


# -- skapa projekt ------------------------------------------------------------------


def create_project(ws: Workspace, proposal: MappingProposal, sheets: list[Sheet], title: str,
                   author: str = "okänd") -> Project:
    tpl = proposal.template.model_copy(deep=True)
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
        new_values[col.source_column] = value

    code = new_values.get(code_col, row.get(code_col))
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
        if (before or None) != (v or None) or (isinstance(v, bool) and is_true(before) != v):
            changes[k] = (before, v)
        row[k] = v
    return changes


# -- kolumner -------------------------------------------------------------------------


def roles_for(tpl: ImportTemplate) -> list[ColumnRole]:
    return VS_ROLES if tpl.kind == "valueset" else CS_ROLES


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


def _default_widget(role: ColumnRole, definition: PropertyDefinition | None) -> str:
    if role in (ColumnRole.CONTEXT,):
        return "checkbox"
    if role == ColumnRole.PARENT:
        return "concept-picker"
    if role in (ColumnRole.DEFINITION,):
        return "textarea"
    if definition:
        return {"boolean": "checkbox", "dateTime": "date", "integer": "number", "decimal": "number"}.get(
            definition.type, "text")
    return "text"


def update_column(tpl: ImportTemplate, index: int, form: dict, registry: PropertyRegistry) -> ImportTemplate:
    """Returnera en ny mall där kolumn ``index`` har fått roll/mål/presentation enligt formuläret."""
    new_tpl = tpl.model_copy(deep=True)
    col = new_tpl.columns[index]
    role = ColumnRole(form.get("role", col.role.value))
    if role not in roles_for(new_tpl):
        raise OpError(f"Rollen '{role.value}' finns inte för den här typen av projekt.")
    col.role = role
    col.property_code = col.designation = col.context = col.pattern = None
    definition = None

    if role == ColumnRole.PROPERTY:
        choice = form.get("property") or ""
        if choice == "__new__":
            code = (form.get("new_property_code") or "").strip() or to_property_code(col.source_column)
            ptype = form.get("new_property_type") or "string"
            if not _CODE_RE.match(code):
                raise OpError("Egenskapskoden får bara innehålla bokstäver, siffror, _ och - och måste börja med "
                              "en bokstav.")
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
    ui.widget = requested if requested and requested != "auto" else _default_widget(role, definition)
    if role == ColumnRole.CONTEXT:
        ui.widget, ui.group = "checkbox", new_tpl.generated(col.context.codesystem).title.split(" (")[0]
    else:
        ui.group = None
    options = [o.strip() for o in (form.get("options") or "").split(",") if o.strip()]
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


def retire(tpl: ImportTemplate, data: SheetData, row: dict) -> ImportTemplate:
    """Markera begreppet som inaktivt (lägger till kolumnen 'Inaktiv' vid behov)."""
    new_tpl = tpl
    col = next((c for c in tpl.columns if c.role == ColumnRole.PROPERTY and c.property_code == "inactive"), None)
    if col is None:
        new_tpl = tpl.model_copy(deep=True)
        definition = PropertyRegistry.default().get("inactive").property
        if all(p.code != "inactive" for p in new_tpl.properties):
            new_tpl.properties.append(definition)
        col = ColumnMapping(source_column="Inaktiv", role=ColumnRole.PROPERTY, property_code="inactive",
                            rationale="Tillagd vid avveckling av begrepp",
                            ui=UiHints(label="Inaktiv", order=len(new_tpl.columns), widget="checkbox"))
        new_tpl.columns.append(col)
        if "Inaktiv" not in data.headers:
            data.headers.append("Inaktiv")
    row[col.source_column] = True
    return new_tpl


def can_delete(tpl: ImportTemplate, released: set[str], code: str) -> tuple[bool, str]:
    if code in released and not tpl.policy.allow_code_removal:
        return False, ("Koden finns i en släppt release och kodverkets regler tillåter inte att koder tas "
                       f"bort ({tpl.policy.rationale or 'policy'}). Avveckla den i stället.")
    return True, ""


# -- bygg och releaser --------------------------------------------------------------------


def build(project: Project, data: dict[str, SheetData], version: str | None = None):
    tpl = project.template.model_copy(deep=True)
    if version:
        tpl.codesystem.version = version
    sheets = [Sheet(name=n, header_row=1, headers=d.headers, rows=copy.deepcopy(d.rows)) for n, d in data.items()]
    return build_all(tpl, sheets)


def resource_codes(resource: dict) -> list[str]:
    if resource.get("resourceType") == "ValueSet":
        return [f"{inc['system']}|{c['code']}" for inc in resource.get("compose", {}).get("include", [])
                for c in inc.get("concept", [])]
    out = []

    def walk(concepts):
        for c in concepts:
            out.append(c["code"])
            walk(c.get("concept", []))
    walk(resource.get("concept", []))
    return out


def resource_diff(old: dict, new: dict) -> dict:
    if new.get("resourceType") == "ValueSet":
        def as_cs(vs):
            return {"version": vs.get("version"), "concept": [
                {"code": f"{inc['system']}|{c['code']}", "display": c.get("display")}
                for inc in vs.get("compose", {}).get("include", []) for c in inc.get("concept", [])]}
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


def create_release_candidate(ws: Workspace, project: Project, data: dict[str, SheetData], base_version: str,
                             author: str, notes: str = "") -> Release:
    if not re.fullmatch(r"\d+\.\d+\.\d+", base_version):
        raise OpError("Versionen ska ha formen X.Y.Z, t.ex. 1.2.0.")
    existing = ws.releases(project.id)
    if any(r.status == "release" and r.version == base_version for r in existing):
        raise OpError(f"Version {base_version} är redan släppt – välj en högre version.")
    n = 1 + max((int(r.version.rsplit(".", 1)[1]) for r in existing
                 if r.status == "rc" and r.version.startswith(f"{base_version}-rc.")), default=0)
    version = f"{base_version}-rc.{n}"
    resources, warnings = build(project, data, version)
    previous = ws.latest_release(project.id)
    prev_by_url = {r["url"]: r for r in previous.resources} if previous else {}
    diffs, errors = [], []
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


def approve_release(ws: Workspace, project: Project, rc_version: str, author: str) -> Release:
    rc = ws.release(project.id, rc_version)
    if rc.status != "rc":
        raise OpError("Bara releasekandidater kan godkännas.")
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
    ws.save(project, author=author, action=f"Release {base} godkänd (från {rc.version})")
    return rel


async def publish_release(ws: Workspace, project: Project, version: str, client, author: str) -> Release:
    from .store import now
    rel = ws.release(project.id, version)
    if rel.status != "release":
        raise OpError("Bara godkända releaser kan publiceras.")
    log = []
    for r in rel.resources:
        try:
            await client.put_resource(r)
            log.append(f"OK {r['resourceType']}/{r.get('id')} {r['url']}|{r['version']}")
        except Exception as exc:  # visas för användaren
            log.append(f"FEL {r['resourceType']}/{r.get('id')}: {exc}")
    rel.publish_log = log
    if all(line.startswith("OK") for line in log):
        rel.published = now()
    ws.save_release(project.id, rel)
    ws.log(project.id, author, f"Release {version} publicerad till {client.config.base_url}", {"resultat": log})
    return rel
